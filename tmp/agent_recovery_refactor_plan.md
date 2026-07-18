# 智能体阶段统一生成—修复恢复机制改造计划

## 1. 目标与结论

将当前分散在模型客户端、LangGraph 和一键生成流程中的“失败后再次调用模型”收敛为一套**有状态、可审计、有限次数**的恢复机制。

适用对象是会产出或修订阶段工件的智能体流程：输入规范化（Agent1）、标程修复、阶段三 Agent2、阶段四 Agent3、阶段五 Agent4。它们共享恢复骨架，但不共享同一份验证器、修复提示词或重生策略。

阶段四的目标链路如下：

```text
外层：新候选轮次（最多 N 轮）
  └─ 生成一个新候选
       └─ 内层：同一候选的修复（最多 5 次）
            ├─ 校验通过：持久化为正式草稿，等待确认/进入下一阶段
            ├─ 校验失败：记录候选和精确错误，修复同一候选
            └─ 第 5 次仍失败：撤销当前可用草稿，保留证据，进入下一外层轮次

所有外层轮次耗尽：标记阶段失败，保留全部证据并停止一键生成。
```

建议初始固定策略：`max_repair_attempts = 5`、`max_generation_rounds = 3`。它们必须是命名常量，而不是隐藏在循环中的字面量；本次不要求在前端开放配置。

## 2. 现状与根因

### 2.1 当前阶段四的两条不连贯修复路径

1. `Agent3Graph` 的 `validate_contract -> revise_contract` 只允许一次候选级修订：`revision_attempted=True` 后下一次校验失败即结束。
2. `OpenAICompatibleModel._call()` 在 JSON/Pydantic 响应契约失败时只进行一次同请求内重试；第二次仍不合法即抛出 `AppError(MODEL_FAILED)`，此时 LangGraph 尚未得到 `SubtaskPlanDraft`，无法调用 Agent3 修订器。
3. `Pipeline.auto_run()` 将上述异常当作阶段失败，按 `MAX_AUTO_REPAIRS_PER_STAGE=4` 从外部再次运行整个阶段。由于阶段三、四“重新运行”会以空候选调用生成器，这些外部重试是重新抽样，不是对上一个失败结果持续修复。

因此，目前既不能对“JSON 尚未形成合法草稿”的输出进行有状态修复，也不能保证阶段语义校验的多次收敛。

### 2.2 不能简单地把所有失败都重生

恢复策略必须区分工件的所有权和失败类别：

| 流程 | 是否允许丢弃候选后重新生成 | 原因 |
|---|---:|---|
| Agent1 输入规范化 | 否 | 只允许规范化展示，不能改写用户题面和标程事实；失败应报告或重试同一规范化请求。 |
| 标程编译修复 | 否 | 标程是用户输入/权威输入，不能因修复失败而自动换一份“新标程”。 |
| 阶段三 Agent2 | 是 | 测试数据设计方案是 AI 草稿。 |
| 阶段四 Agent3 | 是 | 子任务计划是 AI 草稿。 |
| 阶段五 Agent4 | 是，但只清空未发布工作模板 | 生成器/校验器是 AI 工作草稿；已发布版本与用户源文件不得删除。 |
| 用户指令分类器 | 不适用 | 它不产出阶段工件；仅保留传输级重试，失败直接向用户报告。 |

环境失败（模型服务不可用、Docker 不可用、磁盘错误、依赖缺失）也不属于候选修复对象。此类失败必须直接停止，避免无意义地消耗模型调用。

## 3. 目标架构

### 3.1 通用恢复策略

新增一个内部恢复策略模型/协议（名称可为 `AgentRecoveryPolicy`），至少包含：

```python
stage: int
max_generation_rounds: int
max_repair_attempts: int  # 固定为 5
can_regenerate: bool
generate(context) -> RawAgentResult
parse_and_validate(raw_result, context) -> ValidCandidate | ValidationFailure
repair(context, previous_raw, previous_candidate, errors) -> RawAgentResult
clear_working_draft(project_id) -> None
```

关键原则：

- `generate` 与 `repair` 是两个不同的操作、不同提示词和不同可观测事件。
- `repair` 的输入必须包含上一候选（若可解析）、原始模型文本（若不可解析）和结构化错误列表。
- 验证器必须仍是最终权威；模型只提出候选，不能自行宣称修复成功。
- 确定性、无歧义的规范化（例如可安全排序的 ID）可以在验证前由程序完成；不能静默猜测语义内容。
- 任何阶段恢复成功后，只保存最终通过验证的候选为正式草稿。

### 3.2 原始输出与正式草稿分离

`ProjectStorage.load_draft()` 当前会立即按 `TestDataPlanDraft`、`SubtaskPlanDraft` 或 `CodeDraft` 解析文件。因此不合法 JSON/候选不能写进正式 `state/*.json` 草稿路径。

新增独立的恢复审计目录，例如：

```text
<project>/logs/agent-recovery/<run_id>/
  manifest.json
  round-01/generate.json
  round-01/repair-01.json
  round-01/repair-02.json
  ...
```

每个事件最少保存：

- `stage`、`agent_role`、`operation`（generate / repair / validate）；
- 外层轮次、内层修复次数、时间戳；
- 原始模型文本或其安全截断版本；
- 可解析候选（如有）；
- JSON/Pydantic/业务验证的字段级错误；
- 模型响应元数据（finish reason、token 用量等）；
- 结果状态（passed / failed / exhausted / skipped）。

正式草稿被清空时，审计目录不得删除。可以在 `ProjectRecord` 中只记录最近恢复运行的 `run_id`、状态和摘要；详细内容留在项目私有日志目录，避免把大段模型输出塞入 `project.json`。

### 3.3 统一状态机

对允许重生的阶段（3、4、5）使用以下伪代码：

```python
for generation_round in range(1, MAX_GENERATION_ROUNDS + 1):
    raw = generate_fresh(context)

    for repair_attempt in range(0, MAX_REPAIR_ATTEMPTS + 1):
        outcome = parse_normalize_validate(raw, context)
        audit.record(outcome)

        if outcome.passed:
            save_verified_draft(outcome.candidate)
            return PASS

        if outcome.is_environment_failure:
            return FAIL

        if repair_attempt == MAX_REPAIR_ATTEMPTS:
            clear_working_draft_preserving_audit()
            break

        raw = repair_same_candidate(
            context=context,
            raw_output=outcome.raw_output,
            parsed_candidate=outcome.candidate_or_none,
            validation_errors=outcome.errors,
        )

return FAIL_EXHAUSTED
```

这里“修复最多 5 次”表示：初次生成后先校验；若失败，最多额外调用修复器 5 次。每轮最多为 `1 次生成 + 5 次修复`。

## 4. 分阶段落地设计

### 4.1 阶段三 Agent2：固定标签 Markdown

1. 保留现有 `<constraints>`、`<test-matrix>`、`<blueprint-for-generator>` 的确定性验证。
2. 新增 `agent2_revise`，输入原始 Markdown、固定标签错误和业务问题；要求保持已正确标签段，只修正缺失、重复、为空或不符合设计要求的部分。
3. 初次输出不是可用 Markdown、或标签验证失败时，进入同一候选的最多 5 次修复。
4. 五次修复后清空阶段三正式草稿、失效下游阶段四/五，并进入下一生成轮次。
5. 阶段三重新生成成功后维持现有“只读展示 + 用户确认”行为。

### 4.2 阶段四 Agent3：结构化子任务计划

1. 保留 `agent3_plan` 作为纯新生成器；其输入仍只包括已确认的 `test_data_plan.plan_markdown` 及必要上下文，不包括旧草稿或标程源码。
2. 将现有 `agent3_revise` 改为可处理两种输入：
   - 已解析但未通过业务验证的 `SubtaskPlanDraft` 候选；
   - 不能通过 JSON/Pydantic 契约的原始输出文本加字段错误。
3. 修复提示词必须要求输出完整 `SubtaskPlanDraft`，仅修改验证器列出的冲突，保留其他可解析的合法字段；当原始响应完全不可解析时，允许在同一轮中根据原始响应和错误重建完整对象。
4. 每次修复后执行两层验证：
   - JSON/Pydantic 响应契约；
   - `Agent3Validator` 与 `subtask_plan_issues()` 的业务契约。
5. 第五次修复失败后，清除 `state/subtask_plan.json`、阶段四 thread/确认状态和下游阶段五草稿；保留恢复日志；再开始全新的 `agent3_plan` 外层轮次。
6. 外层全部耗尽后，阶段四停留在 `FAILED`，`last_error` 提供恢复运行 ID、最后轮次和最后错误摘要；一键生成不得确认阶段四或推进到阶段五。

### 4.3 阶段五 Agent4：代码工件

1. 保留 Generator / Validator 分角色生成与 `Agent4CandidateVerifier` 的确定性验证，不将其合并为单一泛化修复 Agent。
2. 将 `MAX_REPAIRS_PER_RUN=4` 调整为统一的内层上限 5，且每次修复绑定前一个工作模板、编译诊断、运行日志和目标文件。
3. 若一轮的 5 次代码修复耗尽，清空**未发布的** `state/current-code` 工作候选，保留 `state/released-code`、用户标程和审计目录；进入下一轮重新生成 Generator/Validator 候选。
4. 只有由候选代码导致的编译/运行/格式/覆盖失败才进入代码修复。Docker、编译器、文件系统等基础设施错误直接结束本轮，并标记为不可修复环境错误。
5. 外层耗尽时禁止生成输入文件、验证、导出；现有下游失败回退至阶段五的逻辑应复用同一恢复运行，而不是重置修复证据。

### 4.4 Agent1 与标程修复

1. Agent1 的验证失败仅允许基于原候选与错误信息的修订；不能“新生成”题面或标程事实。
2. 标程编译失败可继续使用“源码 + 编译诊断 → 修复源码”的链路，内层上限统一为 5；耗尽后停止并让用户处理，不能自动清空或替换为一份全新标程。
3. 两者应接入统一审计模型，以获得相同的失败可见性，但配置 `can_regenerate=False`。

## 5. 代码改动清单

### 5.1 模型层与错误边界

- `backend/app/services/model_client.py`
  - 将当前 `_call()` 中“第二次契约重试后抛 `AppError`”的原始文本、字段错误和响应元数据以可消费的结果对象返回给恢复协调器，而不是仅存在异常详情中。
  - 维持短暂的传输重试；将 HTTP/网络错误与输出契约错误明确分类。
  - 为 Agent2/Agent3 增加或完善专用 `revise` 方法；修复调用接收 `raw_output`、`candidate`（可为空）和 `validation_errors`。
  - 不让 `_call()` 自行多轮“修复”；轮次由恢复协调器统一计数，避免双重计数。

- `backend/app/models.py`
  - 新增最小化的恢复摘要模型，例如 `StageRecoverySummary`；字段包括 run id、阶段、状态、生成轮次、修复次数、最后错误摘要、开始/结束时间。
  - 将摘要挂接到 `ProjectRecord`，但不将原始模型文本直接写入该文件。

### 5.2 协调与持久化

- 新增 `backend/app/services/agent_recovery.py`（建议），承载通用外层/内层循环、失败分类、审计写入和停止条件；避免把复杂循环复制到 `pipeline.py`、各 LangGraph 与 Agent4 runner。
- `backend/app/storage.py`
  - 新增 `start_recovery_run`、`append_recovery_event`、`finish_recovery_run`、`clear_working_draft` 等明确方法。
  - 对阶段 3/4 清空正式草稿时使用受控的单文件删除；对阶段 5 只清空当前工作候选，永不删除 released code。
- `backend/app/services/project_service.py`
  - 增加以阶段为粒度的“清空当前可用候选但保留审计”操作；统一更新 `StageStatus`、`stage_threads`、`failed_stage_threads`、下游失效状态与 `last_error`。

### 5.3 LangGraph 与流水线

- `backend/app/services/agent_graphs.py`
  - Agent2/3 图只表达“生成/修订候选—业务验证—用户确认”的阶段语义；多轮恢复由协调器调用，或将轮次明确扩展进图状态。二者择一，不能同时在图和 Pipeline 中分别计数。
  - Agent3 不再用 `revision_attempted: bool` 限制为一次；改为来自统一恢复状态的修复次数。
  - Agent4Runner 使用相同的恢复协议，但继续保留 Generator/Validator 的角色级修复路由。

- `backend/app/services/pipeline.py`
  - `auto_run()` 不再把一个阶段的 `AppError` 简单视为“再次从头运行该阶段”。
  - 对 Agent3 等恢复策略的结果进行处理：成功自动确认；不可修复环境错误立即中止；外层耗尽写入最终失败摘要并中止。
  - 用户手动“运行 AI 检查”必须显式创建新的恢复运行；不能复用旧候选，也不能删除旧审计记录。

### 5.4 API 与前端

- API 响应中增加可选恢复摘要：当前状态、生成轮次、修复次数、最终错误摘要、恢复运行 ID。
- 阶段 3/4/5 的失败 UI 显示“已进行第 X 轮生成、第 Y 次修复”及最后字段/诊断错误；不展示或编辑原始不合格草稿。
- 用户点击重新运行时提示“将开始新的恢复运行并覆盖当前可用草稿；历史失败记录仍会保留”。
- 不新增用户可编辑的阶段三三段 Markdown 区域，保持现有只读、可复制需求。

## 6. 实施顺序

1. **先建立恢复结果和审计存储。**
   - 验证：单元测试能写入并读取恢复 manifest/event，正式草稿路径不接受非法候选。
2. **改造模型客户端错误边界。**
   - 验证：模拟非法 JSON、Pydantic 错误、截断、HTTP 错误，得到可区分的恢复结果或不可修复错误。
3. **先接入阶段四 Agent3。**
   - 验证：覆盖“无效 JSON → 修复成功”“业务契约错误 → 多次修复成功”“修复 5 次耗尽 → 新生成”“3 轮全部耗尽”四条路径。
4. **接入阶段三 Agent2。**
   - 验证：缺标签、重复标签、空标签内容均经有状态修复；外层重生会失效阶段四/五。
5. **接入阶段五 Agent4。**
   - 验证：代码诊断绑定上一个模板；5 次修复后仅清空 current-code；released-code 保持不变。
6. **接入 Agent1 与标程修复的审计与限额。**
   - 验证：二者失败不触发新的权威输入生成。
7. **改造一键生成、API 和前端状态展示。**
   - 验证：一键生成在恢复期间不中途确认阶段；外层耗尽后不推进下游；手动重新运行创建新 run id。
8. **补齐回归、文档与端到端测试。**
   - 验证：完整 `pytest`、静态检查、模拟模型的 API 流程、阶段五沙箱测试均通过。

## 7. 必测场景与验收标准

### 通用

- 同一修复调用的输入含上一候选或原始输出以及精确验证错误。
- 任何阶段单轮修复调用数不超过 5。
- 允许重生阶段的单次恢复运行不超过 3 次新生成。
- 环境错误不调用修复智能体、不触发重生。
- 审计记录在手动重新运行、草稿清空和服务重启后仍可读取。

### 阶段四重点

- 首次输出不是 JSON：修复器收到原文和 JSON 解析错误，修复后通过。
- JSON 可解析但 `SubtaskPlanDraft` 字段缺失：修复器收到字段 location/type 错误。
- Pydantic 通过但 profile count、参数 schema 等业务契约失败：修复器收到 `subtask_plan_issues()` 的错误。
- 第 5 次修复仍失败：正式 `subtask_plan.json` 不存在/被清空，旧候选未被复用；下一外层轮次调用 `agent3_plan`。
- 外层第 3 轮仍失败：阶段四 `FAILED`，一键生成返回失败，阶段五不被运行。
- 外层后续新生成成功：只保存成功候选，用户确认和阶段五正常可用。

### 阶段五重点

- 修复器只拿到目标源码、上次诊断和必要上下文。
- 当前工作模板清空后，已发布代码仍可下载/读取。
- 下游生成或验证失败回退阶段五时，恢复日志不丢失，且不会越过总轮次上限。

## 8. 非目标与风险控制

- 不把模型输出视为可信代码或可信结构；所有通过条件仍由本地验证器、编译器和运行器决定。
- 不把“外层 3 轮、内层 5 次”做成无限后台任务；达到上限必须结束请求并给出审计 ID。
- 不让自动恢复改写用户题目、标程源码或已发布代码。
- 不将完整原始输出直接返回给前端；它可能很大、包含内部提示上下文，且不适合用户编辑。
- 不改变阶段三“重新运行必须覆盖旧草稿”的既有产品语义；只增加历史证据保留。

## 9. 完成定义

当以下条件同时满足时，本改造完成：

1. 阶段三、四、五均可经历“生成—最多 5 次有状态修复—清空—新一轮生成”的受限链路。
2. 阶段四 JSON 响应契约失败不再退化为无状态重复抽样。
3. 所有失败均有可追溯恢复运行记录，且失败证据不会被下一次手动运行清除。
4. 非 AI 工件和环境错误不会被错误地自动重生。
5. 一键生成只在每个阶段恢复成功后自动确认并推进；恢复耗尽后安全停止。
