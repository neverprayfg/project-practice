# Agent 编排框架决策

生产主链路只使用 LangGraph。FastAPI 维护业务阶段，LangGraph 维护每个 Agent 的运行状态和用户确认中断，Docker Sandbox 只执行后端发起的固定动作。Dify、Deep Agents、通用工具调用和模型可控 Shell 均不进入运行时。

Agent1--4 不是同一 runner 的参数化实例。它们分别编译为四张顶层 `StateGraph`，拥有独立状态 Schema、节点、条件边、提示词、验证器和失败策略；只共享模型客户端、项目存储、SQLite checkpointer 与 Sandbox。共享设施由 `AgentGraphCoordinator` 负责生命周期，不参与任务路由决策。

选择 LangGraph 的原因：

- 条件边只读取结构化状态，不读取模型自由文本；
- SQLite checkpointer 与稳定 `thread_id` 支持跨请求恢复用户确认；
- 每张图可以独立演化，不需要把所有 Agent 塞入通用循环；
- `interrupt()` 把确认边界放在无副作用节点，恢复时不会重复执行生成或验证；
- 阶段五可以显式表达有限缺陷修复，而不是依赖迭代次数。

Agent 不拥有工具调用权限。文档由后端确定性路由，编译、生成、校验和标程执行由后端直接调用受限 Sandbox；模型只能返回各 Agent 的严格结构化契约。
