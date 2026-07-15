# 部署步骤

1. 安装并启动 Docker Desktop，确认 Docker Compose v2 可用。
2. macOS/Linux 运行 `./deploy.sh`；Windows 运行 `deploy.cmd`。
3. 按提示输入模型名称和 DeepSeek API Key。脚本会拉取固定依赖、构建 LangGraph 后端与受限 runner，并写入本机 `.env`。
4. 打开 `http://localhost:8000` 使用工作台；API 文档位于 `http://localhost:8000/docs`。

后续更新执行 `./restart-update.sh`，停止执行 `./stop.sh`。业务状态与 LangGraph checkpoint 保存在 Docker named volume 中。
