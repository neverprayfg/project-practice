# Agent 只读文档浏览工具 实施计划

## 目标

给 LangGraph agent 增加三个**只读**工具，让它在 `CODE_DRAFT` 阶段可以按需查阅 testlib/jngen 头文件，不再每次都把全部文档塞进 prompt。

新增工具（白名单目录限定为 testlib/jngen 根）：

| 工具 | 作用 |
|------|------|
| `read_doc` | 读单个文件内容（≤16KB，超出截断并标记） |
| `list_dir` | 列目录，可选 glob 模式过滤 |
| `grep_doc` | 在目录下搜索字符串模式 |

**只读、不执行、不写文件、不联网**。所有路径在执行前必须校验在白名单根目录下。

## 改动清单

| 文件 | 改动 |
|------|------|
| `backend/app/config.py` | +2 个 settings：`testlib_root`、`jngen_root` |
| `backend/app/services/tool_gateway.py` | +3 个 ToolArguments schema、+3 个 PERMISSIONS 条目、+3 个 _dispatch 分支、+1 个路径校验方法 |
| `backend/app/services/langgraph_runner.py` | 接 `tools: AgentToolGateway` 依赖；移除 `_generate` 对 `tool_requests` 的硬拒绝；新增 `_execute_tools` 节点接入图 |
| `backend/app/main.py` | 把 `tools` gateway 注入到 `LangGraphAgentRunner` |
| `backend/app/models.py` | 不改（`WorkflowOutput.tool_requests` 字段已存在） |
| `backend/tests/test_security.py` | +3 个测试：路径遍历拒绝 / 符号链接拒绝 / 文件大小限制 |
| `backend/tests/test_langgraph_workflow.py` | +1 个集成测试：mock LLM 返回 `tool_requests`，验证 gateway 被调用、结果回写 state |
| `docker/backend.Dockerfile` 或 `compose.yaml` | 把 testlib/jngen 目录挂进 backend 容器（或用已存在的路径） |

## 详细改动

### 1. `backend/app/config.py`

在 `Settings` 里加两个路径字段：

```python
testlib_root: Path = Field(default=Path("/opt/testlib"))
jngen_root: Path = Field(default=Path("/opt/jngen"))
```

### 2. `backend/app/services/tool_gateway.py`

**a) 新增三个 schema**（放在 `ValidateArguments` 后面）：

```python
class ReadDocArguments(ToolArguments):
    root: Literal["testlib", "jngen"]
    path: str = Field(min_length=1, max_length=512)

class ListDirArguments(ToolArguments):
    root: Literal["testlib", "jngen"]
    path: str = Field(default=".")
    pattern: str | None = Field(default=None, max_length=128)

class GrepDocArguments(ToolArguments):
    root: Literal["testlib", "jngen"]
    pattern: str = Field(min_length=1, max_length=256)
    path: str = Field(default=".")
    max_results: int = Field(default=20, ge=1, le=100)
```

**b) 在 `PERMISSIONS[TaskType.CODE_DRAFT]` 字典里加三个条目**（`:46` 后）：

```python
"read_doc": ReadDocArguments,
"list_dir": ListDirArguments,
"grep_doc": GrepDocArguments,
```

**c) 新增路径校验方法**（放在 `_dispatch` 前面）：

```python
MAX_READ_BYTES = 16_000
MAX_DIR_ENTRIES = 200
GREP_TIMEOUT_SECONDS = 10

def _resolve_readonly_path(self, root_name: str, relative: str) -> Path:
    if root_name == "testlib":
        root = self.settings.testlib_root.resolve()
    elif root_name == "jngen":
        root = self.settings.jngen_root.resolve()
    else:
        raise AppError("TOOL_DENIED", "root must be testlib or jngen")
    if ".." in Path(relative).parts:
        raise AppError("TOOL_DENIED", "path traversal not allowed")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise AppError("TOOL_DENIED", "path escapes allowed root")
    if candidate.is_symlink():
        raise AppError("TOOL_DENIED", "symlinks not allowed")
    if not candidate.exists():
        raise AppError("TOOL_NOT_FOUND", f"{relative} does not exist")
    return candidate
```

**d) `_dispatch` 末尾（`:169` 前）加三个分支**：

```python
if name == "read_doc":
    path = self._resolve_readonly_path(arguments.root, arguments.path)
    if path.is_dir():
        raise AppError("TOOL_DENIED", "use list_dir for directories")
    data = path.read_bytes()[:MAX_READ_BYTES]
    return {
        "path": arguments.path,
        "content": data.decode("utf-8", errors="replace"),
        "truncated": len(data) == MAX_READ_BYTES,
        "size_bytes": path.stat().st_size,
    }

if name == "list_dir":
    path = self._resolve_readonly_path(arguments.root, arguments.path)
    if not path.is_dir():
        raise AppError("TOOL_DENIED", "not a directory")
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
    if arguments.pattern:
        import fnmatch
        entries = [e for e in entries if fnmatch.fnmatch(e, arguments.pattern)]
    return {"path": arguments.path, "entries": entries[:MAX_DIR_ENTRIES]}

if name == "grep_doc":
    path = self._resolve_readonly_path(arguments.root, arguments.path)
    result = subprocess.run(
        ["grep", "-rn", "--", arguments.pattern, str(path)],
        capture_output=True, text=True, timeout=GREP_TIMEOUT_SECONDS, check=False,
    )
    lines = result.stdout.splitlines()[: arguments.max_results]
    return {
        "pattern": arguments.pattern,
        "matches": lines,
        "total_matches": len(result.stdout.splitlines()),
        "truncated": len(result.stdout.splitlines()) > arguments.max_results,
    }
```

**e) 文件顶部加 import**：

```python
import subprocess
from pathlib import Path  # 已经有，新增
```

### 3. `backend/app/services/langgraph_runner.py`

**a) `__init__` 加 `tools` 参数**：

```python
def __init__(
    self, settings, storage, model, verifier, tools: AgentToolGateway
) -> None:
    ...
    self.tools = tools
```

**b) `_generate`（`:119`）移除硬拒绝，但保留记账**：

```python
# 旧：
if output.tool_requests:
    issues.append("智能体不得直接请求工具；执行检查由后端固定节点完成。")
# 新：保留 tool_requests 到 state，下游 _execute_tools 处理
return {
    "candidate": output.result or state.get("candidate", {}),
    "issues": issues,
    "tool_requests": [req.model_dump() for req in output.tool_requests],
    "execution": {},
    "complete": False,
}
```

**c) 新增 `_execute_tools` 节点**（放在 `_review` 前面）：

```python
async def _execute_tools(self, state: AgentLoopState) -> dict[str, Any]:
    requests = state.get("tool_requests") or []
    if not requests:
        return {"tool_results": []}
    results = []
    for raw in requests[: self.settings.max_tool_calls_per_run]:
        try:
            request = ToolRequest.model_validate(raw)
            result = await self.tools.execute(
                state["project_id"],
                Stage.CODE_DRAFT,
                TaskType(state["task_type"]),
                request,
                run_id=state["project_id"],
            )
        except (ValidationError, AppError) as exc:
            results.append({"tool": raw.get("name"), "ok": False, "error": str(exc)})
        else:
            results.append(result.model_dump(mode="json"))
    return {"tool_results": results, "tool_requests": []}
```

**d) 图构造（`:56-70`）插入新节点和边**：

```python
builder.add_node("execute_tools", self._execute_tools)
builder.add_edge("verify", "execute_tools")
builder.add_edge("execute_tools", "review")
# 删除：builder.add_edge("verify", "review")
```

**e) `_review`（`:147`）开始处注入工具结果到上下文**：

```python
async def _review(self, state):
    tool_results = state.get("tool_results") or []
    if tool_results:
        # 把工具结果附加到 execution，让 LLM 看到
        execution = {**state.get("execution", {}), "tool_results": tool_results}
    else:
        execution = state.get("execution", {})
    output = await self.model.run(
        TaskType(state["task_type"]),
        "review",
        state["context"],
        state.get("candidate", {}),
        execution,
        state.get("issues", []),
    )
    ...
```

**f) 删除 `_review` 里对 `tool_requests` 的拒绝**（`:159-161`），因为请求已经在 `_execute_tools` 消费了：

```python
# 旧：
if output.tool_requests:
    issues.append("智能体自检不得直接请求工具。")
# 新：删除这两行
```

**g) `_to_output` 里保留对 `tool_requests` 字段的处理**：目前 `WorkflowOutput.result` 是 dict，`tool_requests` 已在生成时消费，无需改。

### 4. `backend/app/main.py`

`main.py:49` 注入 tools：

```python
agent_runner = LangGraphAgentRunner(settings, storage, model, verifier, tools)
```

### 5. `backend/app/models.py`

**不改**。`WorkflowOutput.tool_requests: list[ToolRequest]`（`:273`）已存在，schema 已支持。

### 6. 容器挂载

在 `compose.yaml` 或 `docker/backend.Dockerfile` 里把 testlib/jngen 目录挂进 backend 容器。最简单的方式：

**`compose.yaml`** 给 backend 服务加 volumes（路径与 `runner.Dockerfile:6-7` 保持一致）：

```yaml
services:
  backend:
    volumes:
      - ${TESTLIB_HOST_PATH:-./testlib}:/opt/testlib:ro
      - ${JNGEN_HOST_PATH:-./jngen}:/opt/jngen:ro
```

`:ro` 标记只读。

### 7. 系统提示词更新

`backend/app/services/model_client.py` 的 system prompt 在 `CODE_DRAFT` 阶段需要告诉 LLM 现在可以请求工具：

```python
# model_client.py:30-33 的 TASK_GUIDANCE[CODE_DRAFT]，末尾追加：
"如需查阅 testlib/jngen API，可以在 tool_requests 中请求 read_doc / list_dir / grep_doc。"
"参数 root 取 'testlib' 或 'jngen'，path 是相对路径。"
```

`_system_prompt`（`:164`）把 `tool_requests 固定为空数组` 改为：

```
"tool_requests 在 CODE_DRAFT 阶段可填写 read_doc / list_dir / grep_doc 请求；其他阶段必须为空数组。"
```

## 测试要点

### `backend/tests/test_security.py` 新增

```python
def test_read_doc_rejects_path_traversal(gateway, tmp_path):
    # testlib_root 指向 tmp_path/testlib，建一个 a.txt
    # 调 read_doc(root="testlib", path="../etc/passwd")
    # 期望：抛 TOOL_DENIED

def test_read_doc_rejects_symlink(gateway, tmp_path):
    # 在白名单内建符号链接指向白名单外
    # 期望：抛 TOOL_DENIED

def test_read_doc_truncates_large_file(gateway, tmp_path):
    # 写 20KB 文件
    # 期望：返回 truncated=True，content 长度 ≤ 16KB

def test_grep_doc_respects_max_results(gateway, tmp_path):
    # 写 50 行匹配的文件
    # max_results=10，期望 matches 长度=10，truncated=True
```

### `backend/tests/test_langgraph_workflow.py` 新增

```python
async def test_agent_can_request_read_doc(runner, mock_model, mock_gateway):
    # mock_model.run 返回 WorkflowOutput(tool_requests=[ToolRequest(name="read_doc", arguments={"root":"testlib","path":"testlib.h"})])
    # mock_gateway.execute 返回 ToolExecutionResult(ok=True, output={"content":"..."})
    # 跑 runner.run()
    # 期望：gateway.execute 被调用一次，state.tool_results 包含返回内容
```

### 手工验证清单

- [ ] 启动后端，`docker compose logs backend` 确认无报错
- [ ] `curl http://localhost:8000/docs` 看新工具 schema
- [ ] 创建一个项目到阶段 5，看 LLM 是否在 prompt 里看到 `read_doc` 提示
- [ ] 触发一次 read_doc 调用，看 `storage/<id>/logs/tool-audit.jsonl` 写入
- [ ] 尝试 path=`../etc/passwd`，期望拒绝
- [ ] 尝试 root=`/etc`，期望 schema 拒绝（Literal 校验）

## 不做的事（明确边界）

- **不加 interrupt 审核**：每次调用都问用户会很烦，先全自动
- **不开阶段 3/4 的工具**：subtask_plan 和 input_structure 不需要查文档
- **不引入 OpenAI function calling 格式**：继续用现有 JSON 契约 + `tool_requests` 数组
- **不改现有 `compile_*` / `preview_*` / `validate_*` 工具**
- **不改 sandbox / runner / Docker 配置**：testlib/jngen 已经在 runner 镜像里，只需要 backend 也挂上只读卷
- **不动 LangGraph 主图结构**：只在 verify → review 之间插一个 execute_tools 节点

## 风险与注意事项

1. **prompt 体积变化**：让 LLM 主动查文档可能让某些本来"看过就写"的场景变成"反复查文档-不写代码"。需要在 `max_tool_calls_per_run=4` 限制下保持收敛。
2. **路径解析的 host/容器不一致**：`config.py` 默认是容器内路径（`/opt/testlib`）。本地开发（不在 Docker 里跑 backend）需要改成宿主机路径，建议在 `.env` 里覆盖。
3. **grep 性能**：超大目录下 grep 可能 timeout，已用 `subprocess.run(timeout=10)` 保护，但慢调用会拖慢单次 agent 循环。
4. **审计日志**：现有 `tool-audit.jsonl` 自动覆盖，不需要新增格式，但要在文档里说明新工具会写入。
5. **库升级后路径变化**：testlib/jngen 仓库结构如果未来加新目录，白名单根不动即可，agent 通过 `list_dir` 自动发现。
