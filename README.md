# 信息学竞赛数据生成 MVP

本项目使用 Python、FastAPI 与 LangGraph 实现八阶段数据生成流程。`INPUT` 与 `SUBTASKS` 是全局事实来源；Agent1--4 各自拥有独立状态 Schema、Graph、提示词、验证器和失败策略，只共享模型客户端、持久存储、SQLite checkpointer 与 Sandbox。阶段 3--5 使用 checkpoint 与用户确认中断，C++ 编译运行仍由无网络 Docker runner 完成。

## 主要目录

- `backend/app/`：API、LangGraph 状态机、模型客户端、项目状态与文档上下文。
- `demo前端样式设计/`：八阶段前端工作台。
- `docker/runner/`：只接受固定动作的容器入口；编译镜像包含 GCC/testlib/jngen，轻量执行镜像只运行已编译程序。
- `testlib/`、`jngen/`：固定版本的 C++ 数据生成与校验库。
- `langgraph/`：用于实现时参照的 LangGraph 项目仓库；后端安装锁定的 Python 包版本。

## 部署

机器需安装 Git、Docker Engine 与 Compose v2。macOS/Linux 执行：

```bash
./deploy.sh
```

Windows 可执行 `deploy.cmd` 或：

```powershell
.\deploy.ps1
```

部署时可以留空 API Key，脚本仍会拉取固定依赖、构建编译/执行双 runner 镜像与后端并启动服务。模型凭据可在前端模型设置中补充，或写入被 Git 忽略的 `.env`；凭据不进入源码或项目状态。

应用地址为 `http://localhost:8000`，API 文档为 `http://localhost:8000/docs`。

## 日常命令

```bash
./start.sh
./stop.sh
./restart-update.sh
./clear-history.sh
```

`restart-update.sh` 会同步固定版本的 testlib/jngen 后重建服务，并保留业务状态和 LangGraph SQLite checkpoint；`clear-history.sh` 删除项目与 checkpoint，但保留 `.env` 以及前端保存的模型配置。

## 开发

```bash
cd backend
uv sync --dev
uv run uvicorn app.main:app --reload
uv run ruff check .
uv run pytest -q
```

后端仅支持 OpenAI 兼容模型服务，但启动不再强制要求 API Key。未配置模型时，应用、历史数据和确定性功能仍可访问；生成、AI 检查和审查操作会返回 `MODEL_NOT_CONFIGURED`，直到用户在前端或 `.env` 中完成配置。

阶段 6 可通过 `/api/projects/{id}/generate` 选择全部或部分子任务；阶段 7 使用 `/api/projects/{id}/validate` 校验并生成输出。阶段 6/7 使用受限并发和单容器批处理，失败会记录带修订号的反馈并把流程返回 Agent4。

每个数据批次会在项目状态目录保存 `batch.json`，记录所用 INPUT、SUBTASKS、代码修订、基础种子和逐文件执行摘要；执行期间按检查点批量落盘，导出前会再次核对该清单与当前修订一致。

Agent 没有 Shell、写文件、任意宿主机文件、网络、Docker 或工具调用权限。阶段 3 从版本化目录选择全局输入结构标签，由用户与输入模板一起确认；阶段 4 可增加子任务结构标签；阶段 5 根据两者的并集一次性解析 jngen 文档并记录审计信息，不再调用模型选择文档。`testlib_doc_context/` 仍作为系统级上下文。

阶段 4 为每个测试点保存受校验的 `runtime_parameters`。阶段 5 启动前先执行上游契约预检；矛盾或缺失会明确返回阶段 4，不进入代码修复。有效契约会被转换为通用 `ProofObligation`。Agent4 必须随代码提交“约束 ID → 源码位置 → 实际参数 → 文档/API 证据 → 测试构造策略”的 `implementation_mapping`；后端检查缺失映射、只读未使用参数、伪造 API 证据和特殊测试点策略。生成器必须通过 jngen `getOpt` 读取并实际使用参数；runner 只允许白名单标量参数，并把实际参数写入批次清单。

阶段 5 会按运行 ID 记录 `retrieval`、`model_generation`、`compile`、`trial_generation`、`validation`、`semantic_audit`、`targeted_repair`、`targeted_recheck`、`verification_cache` 与 `workflow_total`。可通过 `GET /api/projects/{project_id}/stage5/timings` 查看原始事件和各段占比。完整决策链另存于 `logs/agent4-decisions.jsonl`，并可通过 `GET /api/projects/{project_id}/stage5/decisions` 连同反例账本和最后完整通过的候选快照读取；事件包含候选修订、目标缺陷 ID、模型调用类型、修改范围、验证前后摘要、进展判定以及接受、回滚或停止原因。

阶段 5 有匹配当前 Proof Obligation 的代码时先验证，否则重新生成。确定性失败与一次开放式只读语义审查只输出结构化缺陷，不得修改代码；修复节点一次只处理一个稳定缺陷 ID。每个失败会写入持久反例账本，后续补丁必须重跑全部历史反例。只有“阻断缺陷减少、目标缺陷关闭或验证等级前进”且没有重新引入已关闭缺陷时，补丁才会被接受；否则立即回滚。同一缺陷修复一次仍存在时直接停止，不再按代码变化或迭代次数消耗模型调用。初次生成读取完整相关文档，定向修复只读取由文件名、标题、符号和 digest 索引出的相关片段；验证门和文档选择按候选修订缓存。
