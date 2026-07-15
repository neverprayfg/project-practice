# 信息学竞赛数据生成 MVP

本项目使用 Python、FastAPI 与 LangGraph 实现八阶段数据生成流程。`INPUT` 与 `SUBTASKS` 是全局事实来源；Agent1--4 在 LangGraph 中执行生成、确定性检查、自检和修复循环，阶段 3--5 使用 checkpoint 与用户确认中断。C++ 编译运行仍由无网络 Docker runner 完成。

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

`restart-update.sh` 保留业务状态和 LangGraph SQLite checkpoint；`clear-history.sh` 删除项目与 checkpoint，但保留 `.env` 模型配置。

## 开发

```bash
cd backend
uv sync --dev
uv run uvicorn app.main:app --reload
uv run ruff check .
uv run pytest -q
```

后端仅支持 OpenAI 兼容模型服务，但启动不再强制要求 API Key。未配置模型时，应用、历史数据和确定性功能仍可访问；生成、AI 检查和审查操作会返回 `MODEL_NOT_CONFIGURED`，直到用户在前端或 `.env` 中完成配置。

阶段 6 可通过 `/api/projects/{id}/generate` 选择全部或部分子任务；阶段 7 使用 `/api/projects/{id}/validate` 校验并生成输出。兼容接口 `/build` 会顺序执行两阶段。阶段 6/7 使用受限并发和单容器批处理，失败会记录带修订号的反馈并把流程返回 Agent4。

每个数据批次会在项目状态目录保存 `batch.json`，记录所用 INPUT、SUBTASKS、代码修订、基础种子和逐文件执行摘要；执行期间按检查点批量落盘，导出前会再次核对该清单与当前修订一致。

Agent 没有 Shell、写文件、任意宿主机文件、网络、Docker 或工具调用权限。阶段 3 从版本化目录选择输入结构标签，由用户与输入模板一起确认；阶段 5 只根据已确认标签及依赖解析 jngen 文档并记录审计信息。旧关键词/模型选文档流程仅在迁移开关显式开启时使用；`testlib_doc_context/` 仍作为系统级上下文。

阶段 4 为每个测试点保存受校验的 `runtime_parameters`。阶段 5 生成器必须通过 jngen `getOpt` 读取 `-subtask`、`-case`、`-seed` 以及该测试点的数据范围和规模参数；runner 只允许白名单标量参数，并把实际参数写入批次清单。Agent4 同时生成逐测试点的约束覆盖表，后端依次执行静态库接入检查、冒烟验证和完整验证。编译、试运行、校验、标程执行和流程路由仍由后端固定节点决定。

阶段 5 会按运行 ID 和修复轮次记录 `retrieval`、`model_generation`、`compile`、`trial_generation`、`validation`、`review` 六段耗时，并单独记录 `workflow_total`。可通过 `GET /api/projects/{project_id}/stage5/timings` 查看原始事件、逐轮汇总和各段占比，也可用 `run_id` 查询参数筛选单次运行。

阶段 5 审查通过时模型只返回结论，不再回传完整代码、修订号和 `trial_results`；审查修复时只返回发生变化的代码或覆盖表字段，由后端与原候选合并。只有确定性验证明确标记 `retrieval_required` 时才执行返修 jngen 检索。编译、testlib、参数遗漏和试生成失败默认直接进入定向修复。
