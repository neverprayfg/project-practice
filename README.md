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

脚本只要求模型名称、OpenAI 兼容 API Base URL 和 API Key，随后拉取固定依赖、构建编译/执行双 runner 镜像与后端并启动服务。模型凭据写入被 Git 忽略的 `.env`，不进入源码或项目状态。

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
MODEL_MODE=mock uv run uvicorn app.main:app --reload
uv run ruff check .
uv run pytest -q
```

阶段 6 可通过 `/api/projects/{id}/generate` 选择全部或部分子任务；阶段 7 使用 `/api/projects/{id}/validate` 校验并生成输出。兼容接口 `/build` 会顺序执行两阶段。阶段 6/7 使用受限并发和单容器批处理，失败会记录带修订号的反馈并把流程返回 Agent4。

每个数据批次会在项目状态目录保存 `batch.json`，记录所用 INPUT、SUBTASKS、代码修订、基础种子和逐文件执行摘要；执行期间按检查点批量落盘，导出前会再次核对该清单与当前修订一致。

Agent 没有 Shell、写文件、任意宿主机文件、网络、Docker 或工具调用权限。阶段 5 运行前，后端会进行有界的 jngen 文档检索：每轮都提供全部文件名，后续轮额外提供已读正文；模型只能从未读文件中选择少量文档。初始至少检索两轮、最多三轮，模型可提前结束，达到上限时由后端结束后再生成代码。每轮选择和失败原因都会写入项目审计日志；`testlib_doc_context/` 仍作为系统级上下文。编译、试运行、校验、标程执行和流程路由仍由后端固定节点决定。
