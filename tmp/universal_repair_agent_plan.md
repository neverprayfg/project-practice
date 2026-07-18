# 通用修复智能体（恢复协调器）改造计划

## 1. 目标

在现有 LangGraph 工作流中引入一个可复用的“通用恢复协调器”（以下简称恢复协调器）。它负责诊断、循环控制、工具调用、审计和停止条件；阶段专属智能体继续负责生成或修改特定工件。

目标不是创建一个宣称“能解决任何错误”的万能模型，而是让所有**可观测、可验证、被授权修复**的 AI 工件失败进入一致且可收敛的恢复流程。

```text
阶段工件生成/修订
       ↓
确定性验证工具
       ├─ 通过：保存正式草稿或推进阶段
       └─ 失败：恢复协调器分类错误
                 ├─ 可机械修复：程序修复后再验证
                 ├─ 可由 AI 修复：调用阶段专属修复器
                 └─ 不可修复：记录证据并停止
```

## 2. 设计原则

1. **协调通用，修复专属。**
   协调器统一管理重试和证据；不能用一个泛化提示词代替阶段三 Markdown 规则、阶段四 JSON 契约、阶段五编译与运行验证。

2. **验证器是最终权威。**
   模型不能自行声明“已经修好”；JSON Schema、Pydantic、业务校验器、编译器、运行器仍决定是否通过。

3. **先确定性处理，后调用模型。**
   对可无歧义处理的问题（例如格式提取、受控 ID 排序、错误归类）优先用程序；模型仅处理需要语义判断或代码修改的部分。

4. **候选、错误与修复必须连续。**
   每次修复都必须取得上一候选（或不可解析的原始输出）、精确字段错误与必要上下文，不能以空候选重新抽样。

5. **权限边界优先于自动化。**
   用户题面、标程、已发布代码、外部环境配置不是任意可重生的 AI 草稿。

6. **有限循环且可审计。**
   每个 AI 候选最多修复 5 次；允许重生的阶段最多 3 轮。每次操作都落盘，外层耗尽后安全停止。

## 3. 为什么继续使用 LangGraph

项目已使用 LangGraph 管理 Agent1/2/3 的状态、确认中断和 Agent4 的运行流程。此次改造应在其上扩展，而不是以 LangChain AgentExecutor 替换既有编排。

LangGraph 适合本需求的原因：

- 其状态图可表达“生成 → 验证 → 修复 → 再验证 → 人工确认”；
- 可持久化每个恢复运行的状态；
- 可显式限制循环次数；
- 可在用户确认处中断和恢复；
- 可将 Agent4 的多角色代码修复保留为专属子流程。

可使用 LangChain 的消息、工具抽象和模型封装，但不应让一个开放式 ReAct 循环拥有无边界文件删除、环境修改或任意命令执行能力。

## 4. 目标架构

```text
RecoveryCoordinator（通用恢复协调器）
  ├─ RecoveryPolicy（按阶段的策略定义）
  ├─ RecoveryEvidenceStore（审计证据）
  ├─ DeterministicTools（解析、验证、编译、运行）
  └─ StageRepairAdapter（阶段专属适配器）
       ├─ Agent1Adapter
       ├─ SolutionRepairAdapter
       ├─ Agent2Adapter
       ├─ Agent3Adapter
       └─ Agent4Adapter
```

### 4.1 `RecoveryPolicy`

每个阶段定义明确策略，而不是在协调器中写阶段分支：

```python
class RecoveryPolicy(Protocol):
    stage: int
    max_generation_rounds: int
    max_repair_attempts: int  # 统一为 5
    can_regenerate: bool

    async def generate_fresh(self, context: dict) -> RawCandidate: ...
    async def validate(self, candidate: RawCandidate, context: dict) -> ValidationResult: ...
    async def deterministic_fix(self, result: ValidationResult) -> RawCandidate | None: ...
    async def repair(self, context: dict, result: ValidationResult) -> RawCandidate: ...
    async def persist_verified(self, project_id: str, candidate: dict) -> None: ...
    async def clear_working_candidate(self, project_id: str) -> None: ...
```

`RawCandidate` 必须同时可容纳原始模型文本、已解析 JSON（可选）、响应元数据与操作名称；不能只用已通过 Pydantic 的模型对象表示候选。

### 4.2 `ValidationResult`

验证结果应采用稳定结构，而非仅将异常字符串拼接到 `issues`：

```json
{
  "passed": false,
  "failure_class": "response_contract | business_contract | deterministic_execution | environment | authorization",
  "repairable": true,
  "candidate": {"optional": "parsed candidate"},
  "raw_output": "optional raw model response",
  "errors": [
    {
      "source": "pydantic | validator | compiler | runner",
      "location": ["subtasks", 0, "runtime_parameters"],
      "message": "field required",
      "code": "missing"
    }
  ],
  "diagnostics": {},
  "response_metadata": {}
}
```

错误类别的行为：

| 类别 | 是否调用 AI 修复 | 是否允许重生 | 例子 |
|---|---:|---:|---|
| `response_contract` | 是 | 取决于阶段策略 | 非法 JSON、字段类型错误、响应截断之外的契约错误。 |
| `business_contract` | 是 | 取决于阶段策略 | 阶段三标签缺失、阶段四 profile 计数不一致。 |
| `deterministic_execution` | 是（仅代码候选） | 阶段五允许 | 编译失败、运行失败、格式/覆盖验证失败。 |
| `environment` | 否 | 否 | Docker、网络、磁盘、模型服务故障。 |
| `authorization` | 否 | 否 | 需要改用户标程、已发布代码或受保护配置。 |

### 4.3 协调器循环

```python
async def recover(policy, context, audit):
    for generation_round in range(1, policy.max_generation_rounds + 1):
        raw = await policy.generate_fresh(context)
        audit.record("generate", generation_round, 0, raw)

        for repair_attempt in range(0, policy.max_repair_attempts + 1):
            result = await policy.validate(raw, context)
            audit.record("validate", generation_round, repair_attempt, result)

            if result.passed:
                await policy.persist_verified(project_id, result.candidate)
                return RecoverySuccess(...)

            if not result.repairable:
                return RecoveryFailure(...)

            fixed = await policy.deterministic_fix(result)
            if fixed is not None:
                raw = fixed
                continue

            if repair_attempt == policy.max_repair_attempts:
                await policy.clear_working_candidate(project_id)
                break

            raw = await policy.repair(context, result)
            audit.record("repair", generation_round, repair_attempt + 1, raw)

        if not policy.can_regenerate:
            return RecoveryFailure(...)

    return RecoveryExhausted(...)
```

说明：首次生成后先验证；因此单个外层轮次最多为 `1 次生成 + 5 次 AI 修复`。`deterministic_fix` 不消耗模型修复次数，但必须有严格、可测试的输入输出规则，且应有单独的防循环标记。

## 5. 阶段专属适配器

### 5.1 Agent1：输入规范化

- 允许的工件：输入说明的规范化表示。
- 禁止：修改题目原文、难度、标程事实。
- `can_regenerate=False`；失败时只允许携带原候选与错误的修订，最多 5 次后停止。

### 5.2 标程编译修复

- 允许的工件：在用户已授权自动修复范围内的标程副本。
- 输入：当前源码、真实编译诊断、修复历史。
- 禁止：修复耗尽后自动生成并替换一份新标程。
- Docker、编译器不存在等环境错误直接停止。

### 5.3 阶段三 Agent2：测试数据设计方案

- 新生成器：输出带三段固定标签的 Markdown。
- 修复器：输入原 Markdown、标签/业务校验错误；只修订错误段落。
- `can_regenerate=True`；5 次修复耗尽后清空阶段三正式草稿，失效阶段四和阶段五草稿，再进入下一生成轮次。

### 5.4 阶段四 Agent3：子任务计划

- 新生成器：只依据已确认的 `test_data_plan.plan_markdown` 生成全新 `SubtaskPlanDraft`。
- 修复器：同时支持两类上次输出：
  - 可解析但不满足 `subtask_plan_issues()` 的 JSON 候选；
  - 无法通过 JSON/Pydantic 契约的原始文本加字段级错误。
- `can_regenerate=True`；5 次修复耗尽后清空 `subtask_plan.json`、阶段四待确认 thread 与下游工作草稿，再开始新的生成轮次。
- 修复器不得读取标程代码，也不得将旧草稿作为新生成器的候选。

### 5.5 阶段五 Agent4：代码模板

- 保留 Generator / Validator 两个角色及其专属文档上下文，不合并为单模型随意修改两份代码。
- 通用协调器只管理轮次、审计和失败分类；角色适配器决定应修复 `generator.cpp`、`validator.cpp` 或整个工作模板。
- 5 次修复耗尽后仅清除未发布 `current-code` 候选；绝不删除 `released-code`、用户标程和恢复证据。
- 编译、运行、格式和覆盖检查由 `Agent4CandidateVerifier` 决定，不由模型自评。

## 6. 工具白名单与安全边界

恢复协调器可使用的工具必须是显式白名单：

- 读取当前阶段上下文、正式草稿和恢复证据；
- 解析 JSON / Pydantic 校验 / 阶段业务校验；
- 阶段五受控沙箱编译、运行和诊断收集；
- 写入恢复审计；
- 仅清理策略允许的 AI 工作草稿；
- 调用指定的生成器或修复器。

明确禁止：

- 任意 shell 命令、任意路径删除、修改全局配置；
- 修改用户题面或标程而没有单独授权；
- 修改已发布代码版本；
- 因模型服务、网络、磁盘等环境错误而盲目重复调用模型；
- 将完整内部提示词或敏感原始模型输出暴露到前端。

## 7. 持久化与可观测性

新增私有审计路径：

```text
<project>/logs/agent-recovery/<run_id>/manifest.json
<project>/logs/agent-recovery/<run_id>/round-01/generate.json
<project>/logs/agent-recovery/<run_id>/round-01/repair-01.json
...
```

`project.json` 仅保存最近一次恢复运行的轻量摘要：`run_id`、阶段、状态、生成轮次、修复次数、最后错误摘要与时间。原始候选和模型输出不写入 `project.json`，也不写入正式草稿路径。

API/前端仅显示安全摘要，例如“第 2 轮生成，第 4 次修复失败：`subtasks[1].test_count` 与 profile count 不一致”；管理员或本地调试可读取完整审计。

## 8. 实施步骤

1. 新增 `RecoveryPolicy`、`RawCandidate`、`ValidationResult`、`RecoverySummary` 等最小模型与单元测试。
2. 新增 `agent_recovery.py`，实现受限循环、错误分类、审计调用和停止条件。
3. 扩展 `ProjectStorage`：恢复 run 生命周期、事件追加、正式草稿与审计分离、受控清空 working draft。
4. 改造 `model_client.py`：将响应契约失败封装为 `RawCandidate + ValidationResult`，不在 `_call()` 中自行执行多轮修复。
5. 先接入 Agent3，验证“非法 JSON/字段错误/业务错误 → 5 次修复 → 重生”全链路。
6. 接入 Agent2，保持三段只读 Markdown 与下游失效关系。
7. 将 Agent4Runner 纳入协调器，保留 Generator/Validator 角色修复并统一限额为 5。
8. 接入 Agent1 和标程修复的审计与限额，但禁止其重生。
9. 改造 `Pipeline.auto_run()`：使用恢复协调器结果，而不是对失败阶段无状态重复调用。
10. 补充 API/UI 的恢复摘要展示、文档与端到端测试。

## 9. 测试与验收

必须覆盖：

- 无法解析 JSON 的 Agent3 输出能携带原文和字段错误进入修复器；
- 同一候选连续修复时，修复器确实接收到上一候选，而非 `{}`；
- 每轮严格最多 5 次 AI 修复，允许重生阶段严格最多 3 轮生成；
- 阶段四 3 轮耗尽后不进入阶段五；
- 阶段三耗尽时阶段四/五正式草稿失效但恢复证据保留；
- 阶段五耗尽时只清除 `current-code`，不影响 `released-code`；
- Docker/网络/模型服务错误不触发 AI 修复和重生；
- 用户手动重新运行创建新 `run_id`，旧审计仍可读取；
- 服务重启后可读取恢复摘要，遗留 checking 状态可安全收束；
- 现有一键生成、人工确认、下游回退和导出回归测试全部通过。

## 10. 完成定义

完成时，系统具备一个可复用的恢复协调器：它能够统一处理所有 AI 工件阶段的恢复生命周期，同时仍让阶段专属验证器、工具权限和工件所有权决定“能否修、如何修、能否重生”。系统不承诺自动解决任何错误，而是对可验证且被授权的错误提供有界、可追溯的自动恢复。
