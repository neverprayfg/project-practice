# 信息学竞赛数据生成 MVP

本项目使用 Python、FastAPI 与 LangGraph 实现八阶段数据生成流程。`INPUT` 与 `SUBTASKS` 是全局事实来源；Agent1--3 使用独立 LangGraph，标程与 Agent1--4 共享同一个受限恢复协调器，各阶段通过专属 `RecoveryPolicy` 保留自己的生成、验证与修复边界。阶段 3--4 使用 checkpoint 与用户确认中断，C++ 编译运行仍由无网络 Docker runner 完成。

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

前端提供“一键生成全部”和已有项目的“一键生成”入口，对应 `POST /api/projects/{id}/auto-run`。项目创建完成阶段 1 后，后端在同一项目锁内持续执行阶段 2--8：阶段 2 根据真实编译诊断最多修复标程 5 次，阶段 3--5 自动检查并确认，阶段 6/7 失败时携带失败测试点参数回退 Agent4，全部通过后自动构建导出包。每次修复前先由外层问题定位器确定责任阶段、所需证据、保护字段与可写工件；阶段四会获得逐子任务、逐测试点的运行参数 schema 期望/实际差异。一键生成中，若阶段五的错误定位为阶段四生成计划，系统会按限定授权自动回退修复并重新验证；若根因为用户标程，则停止并要求授权。先执行不消耗 AI 次数的无歧义机械修复，再调用阶段修复器；模型响应、业务契约、确定性执行、环境与权限错误分类处理，环境和权限错误不会盲目重试。允许重生的阶段一次恢复运行最多包含 3 轮新生成，每轮对同一候选最多调用 AI 修复 5 次；耗尽后清空策略允许的未确认工作草稿、停止下游，并保留 `logs/agent-recovery/<run_id>` 审计记录。

“一键生成”以项目的 `current_stage` 为恢复点：同一个项目多次中断后可直接再次点击，已通过阶段会被保留，当前失败阶段会开始新的有界恢复运行。如果上次请求或进程中断留下 `checking` 状态，系统会回收最早中断阶段后再继续至导出。

每个数据批次会在项目状态目录保存 `batch.json`，记录所用 INPUT、SUBTASKS、代码修订、基础种子和逐文件执行摘要；执行期间按检查点批量落盘，导出前会再次核对该清单与当前修订一致。

Agent 没有 Shell、写文件、任意宿主机文件、网络、Docker 或工具调用权限。阶段 3 由 Agent2 按固定 Markdown 结构生成并由用户确认一份测试数据设计方案，变量约束、核心测试点矩阵和生成器大纲分别使用 `<constraints>`、`<test-matrix>` 和 `<blueprint-for-generator>` 包裹；前端分区只读展示并允许复制。阶段 4 以该已确认方案推导子任务数量，规划全部子任务与三类 generation profiles。阶段 3/4 每次运行 AI 检查都重新生成并覆盖旧草稿。阶段 5 先调用独立的生成器分析智能体；它读取题面、阶段三方案、阶段四控制和标程源码，将标程分支与复杂度风险转换为可机读构造规格。原始标程源码不会继续传给 generator 或 validator。generator 固定加载一份预构建的 `jngen_context/agent4_reference.md`，其中整合全部英文 jngen 文档与示例代码；运行时不再拼接 jngen 文档。validator 仍固定加载其独立的 `testlib_context`。generator 与 validator 的上下文不包含题目难度、标程源码或标程编译状态。后端根据第一阶段规范化的题目输入说明建立带 digest 的输入格式契约，再并行调用两个独立生成接口；两个接口及其修复调用都只返回各自负责的纯 C++ 源码，格式契约 ID 由后端绑定。

阶段 3、4、5 各提供一个不保存历史的一次性用户指令框。后端先判断本次输入是提问还是修改意见：提问只返回回答且不改变草稿与确认状态；修改意见会定向更新当前阶段产物并重新进入该阶段的校验与用户确认流程。阶段 5 可分别修改 generator、validator 或同时修改两者。

阶段 4 使用 `generation_profiles` 将全部测试点明确分为合规性构建（`rules_format`）、算法过滤与压测（`anti_algorithm`）、边界与鲁棒性（`boundary_edge`）三类目标；每个测试点通过 `generation_profile_id` 归属一个 profile，并必须提供字符串 `construction_mode` 描述可执行的构造策略以及整数 `variation_budget` 描述随机变化预算：fixed 必须为 0，非 fixed 必须为正数。profile 数量必须恰好覆盖全部测试点，同一子任务的逐测试点参数必须共享同一 schema。生成器分析契约要求阶段四的每一个“子任务 × profile × mode”恰好对应一条包含参数用途、不变量、构造步骤、闭环检查、种子策略、变化维度和复杂度目标的规格；缺失或越权策略会先自动修订，规格会保存在项目状态中供审计。每版 generator 在确定性验证前还会由同一分析角色逐条审查规格是否真实落到代码，失败只授权修复 generator。阶段四确认后，精确的逐测试点数值与分配仍由后端保管，但 generator 会收到分析规格、按 profile 汇总的 structure 类构造控制值以及参数 schema，从而实现全部模式分支；validator 不接收这些内容。runner 自动传入 `generation_profile`、`construction_mode` 和其他参数。静态校验会拒绝丢弃构造控制或使用不影响输出的 dummy 随机调用；非固定模式还会用两个种子执行，输出不发生变化时直接判定伪构造。若阶段四缺少这些控制，阶段五会在调用代码生成前将问题定位回阶段四自动修复；若某次试生成失败，修复上下文还会包含该失败测试点的实际运行参数。

阶段 5 只维护一份可覆盖的当前工作模板：`generator.cpp` 与 `validator.cpp`。静态检查、编译或联合运行失败时，后端把确定性诊断归属到一个文件；智能体只能返回该文件的新源码，后端原子替换工作模板后重新执行全部检查。无法唯一归属到 generator 或 validator 的失败不会触发代码修改。每轮最多修复 5 次，耗尽后只清空 `current-code` 并进入下一生成轮；`released-code` 和历史恢复证据始终保留，新发布会把上一发布快照归档到 `released-code-history`。

用户确认阶段 5、进入阶段 6 时，后端冻结一份只读发布快照，记录 generator、validator 各自及组合内容的 SHA-256。阶段 6/7 和导出都会校验该快照及哈希，保证数据对应准确的代码版本。活动状态只保留当前工作模板；恢复过程的候选、诊断和调用摘要仅写入私有审计目录，不参与发布，也不维护反例账本、候选缓存或决策链。
