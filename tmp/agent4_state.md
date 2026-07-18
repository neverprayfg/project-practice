# Agent4State 字段说明

> 源文件：`backend/app/services/agent_graphs.py:79-104`
> 关联图：`Agent4Graph`（同文件 `agent_graphs.py:304-376`）

`Agent4State` 是项目中**第五阶段（Stage 5 / Agent4）** 的 LangGraph 工作流所使用的状态 TypedDict。它服务于一个由 10 个节点组成的、面向「代码缺陷自动修复 + 多轮反例验证」的工作流。

```python
class Agent4State(TypedDict, total=False):
    run_id: str
    project_id: str
    context: dict[str, Any]
    candidate: dict[str, Any]
    accepted_candidate: dict[str, Any]
    candidate_revision: str
    accepted_revision: str
    execution: dict[str, Any]
    defects: list[dict[str, Any]]
    issues: list[str]
    ledger: dict[str, Any]
    target_defect: dict[str, Any]
    attempted_defect_ids: list[str]
    closed_defect_ids_before: list[str]
    baseline_summary: dict[str, Any]
    accepted_ledger: dict[str, Any]
    semantic_audit_done: bool
    validation_level: str
    patch_scope: list[str]
    patch_summary: dict[str, Any]
    complete: bool
    stopped: bool
    stop_reason: str
    requires_user: bool
    user_confirmed: bool
```

注意 `total=False` 表示**所有字段都是可选的**——LangGraph 节点每次只返回需要更新的部分，由 LangGraph 自动合并到全局状态中。下面对每个字段按用途分组说明。

---

## 1. 运行身份与上下文

### `run_id: str`
本次 Agent4 运行的唯一标识，等同于 LangGraph 的 `thread_id`（在 `Agent4Graph.run()` 中由 `thread_id` 直接赋值）。同时会写入 `_agent4_timing` 性能日志，用于按 run 维度聚合耗时。

### `project_id: str`
对应的项目 ID，用于从 `ProjectStorage` 读写项目数据、加载反例账本（`CounterexampleLedgerService.load(project_id)`），以及在 retry 时反推 `project_id`（`thread_id.split(":", 1)[0]`）。

### `context: dict[str, Any]`
上游（Agent1-3）产出的全局输入上下文，作为 Agent4 各节点的只读输入。

### `accepted_candidate: dict[str, Any]`
已经被认可的代码候选。在 `_output()` 中作为最终 `WorkflowOutput.result` 的来源（当它存在时优先于 `candidate`）。当用户确认（`user_confirmed=True`）或一轮修复整体通过验证后更新。

### `accepted_revision: str`
与 `accepted_candidate` 配对的修订号（`candidate_revision()` 的产物）。后续比较候选是否“变更”时会同时对比 `accepted_revision` 和 `candidate_revision`。

### `candidate: dict[str, Any]`
当前正在被验证/审计/修复的代码候选（通常为 `CodeDraft` 的 `model_dump(mode="json")`）。它由 `generate_candidate` 节点产出，被 `verify_candidate`、`semantic_audit`、`select_defect`、`repair_defect` 读写。

### `candidate_revision: str`
`candidate` 的当前修订号。每当候选内容发生变化（生成新候选、修复缺陷后重生成）都会更新。

### `execution: dict[str, Any]`
执行信息（运行输出、用时、返回值等），通常在调用 `verify_candidate` → `defects_from_execution()` 时被填充，最终用于推导缺陷列表。

---

## 2. 缺陷与问题清单

### `defects: list[dict[str, Any]]`
当前候选下发现的所有缺陷（每项为 `Defect.model_validate` 兼容的字典）。`verify_candidate` / `recheck_history` 节点都会向其中追加。`Agent4Graph._output` 会把 `severity == "blocker"` 的缺陷消息聚合进 `issues`。

### `issues: list[str]`
面向用户/上游的人类可读问题描述。`approve` 节点中如果存在 blocker，会作为 issues 返回（去重后），让上游阶段知道本轮为何未通过。

### `target_defect: dict[str, Any]`
当前正在尝试修复的缺陷（`select_defect` 节点的输出）。`repair_defect` 节点据此生成针对性 patch。

### `attempted_defect_ids: list[str]`
本 run 中**已经被尝试修复过**的缺陷 ID 列表。`select_defect` 据此避免重复修复同一缺陷；只有当 `attempted` 之外的缺陷仍有未解决项时才会继续走 `repair_defect` 分支。

### `closed_defect_ids_before: list[str]`
进入本次 run 之前就已被视为“已解决”的缺陷 ID 集合，用于在 `recheck_history` 节点里区分「历史已关闭」与「本轮新关闭」，从而判断是否真正取得新进展、是否需要触发语义审计等。

### `baseline_summary: dict[str, Any]`
`prepare_documents` 节点产出的基线快照：基线候选、基线缺陷数、基线账本摘要等。后续所有“进步评估”节点都会拿它和当前状态做差量对比，避免把历史遗留问题误算成本轮的功劳。

---

## 3. 反例账本

### `ledger: dict[str, Any]`
当前的 `CounterexampleLedger`（反例账本）。初值由 `self.ledger_service.load(project_id)` 加载并以 `model_dump(mode="json")` 写入。它记录了用于对抗性验证的反例样本，新发现的反例会追加进去。

### `accepted_ledger: dict[str, Any]`
被最终接受的账本快照。在用户确认或一轮修复整体通过验证后保存，作为后续 compare 的“上一次成功状态”。

---

## 4. 验证与审计

### `validation_level: str`
当前生效的验证深度，取自 `VALIDATION_LEVELS`（见 `app/services/defects.py`）。常见值：
- `"contract"`：只校验契约（默认初始值）；
- `"exec"`：执行反例；
- `"counterexample"`：使用完整反例账本进行对抗验证。

随着 run 推进，可逐步加深。

### `semantic_audit_done: bool`
是否已经完成过语义审计。`semantic_audit` 节点置为 `True`，并被 `_route_after_progress` 等条件路由器使用，避免重复审计或在审计未过时短路结束。

### `patch_scope: list[str]`
本轮修复涉及的文件 / 区域范围，用于在 `repair_defect` 中约束 patch 的粒度，也便于审计与日志记录。

### `patch_summary: dict[str, Any]`
本轮 patch 的概要（命中文件数、变更行数、是否触及 contract 等）。`recheck_history` 与 `evaluate_progress` 会基于它判断“进步幅度”。

---

## 5. 终止与用户交互

### `complete: bool`
**最终通过标志**。`true` 表示工作流已经收敛、最终候选可用，`_output()` 会据此返回 `Confirmation.PASS`。

### `stopped: bool`
**主动停止标志**。与 `complete` 不同，`stopped=True` 表示工作流主动放弃（无可修复缺陷、达到最大尝试次数等），但 `complete` 仍为 `False`，上游应当据此回退到上一阶段或要求用户介入。

### `stop_reason: str`
解释 `stopped=True` 的原因，会被 `_output()` 追加进 `issues` 列表。

### `requires_user: bool`
是否需要用户确认（来自 `Agent4Graph.run(..., requires_user=...)`），决定 `approve` 之后是否要进入 `wait_user` 节点。

### `user_confirmed: bool`
用户在 `wait_user` 中通过 LangGraph `interrupt` 给出的确认结果。`true` 后候选会被晋升为 `accepted_candidate`。

---

## 6. 状态生命周期

```
Agent4Graph.run(...)
  └─ 初始化（run_id=thread_id, ledger=load(), validation_level="contract", ...）
       ↓
prepare_documents        → context, baseline_summary, closed_defect_ids_before
       ↓
generate_candidate       → candidate, candidate_revision
       ↓
verify_candidate         → execution, defects, validation_level
       ↓
(semantic_audit)         → semantic_audit_done, defects (补充语义缺陷)
       ↓
select_defect            → target_defect
       ↓
repair_defect            → candidate, candidate_revision, attempted_defect_ids, patch_scope, patch_summary
       ↓
recheck_history          → defects, ledger (更新反例账本)
       ↓
evaluate_progress        → 可能再次 select_defect / semantic_audit / approve / END
       ↓
approve                  → accepted_candidate?, accepted_revision?, accepted_ledger?, complete, stopped
       ↓
(wait_user 若 requires_user) → user_confirmed
       ↓
END
```

---

## 7. 关键不变量

- **`candidate` 与 `candidate_revision` 始终同步**：修改 candidate 必须同时刷新 revision，否则下游无法检测到内容变化。
- **`accepted_*` 是单调晋升的**：只有经过验证 + 用户确认（或自动通过）才会写入；不允许倒退回更早的 accepted 版本。
- **`attempted_defect_ids` 单调递增**：保证每个缺陷在一次 run 内不会被无限循环地重试。
- **`complete` 与 `stopped` 互斥**：`complete=True` ⇒ `stopped=False`；`stopped=True` ⇒ `complete=False`。
- **`semantic_audit_done` 是一次 run 内审计预算**：`True` 后再回到 `semantic_audit` 节点会被路由到 `select_defect` 或 `approve`。
- **`ledger` 与 `accepted_ledger` 不必同步**：`accepted_ledger` 仅在通过验证的关键节点更新，`ledger` 则在每次 `recheck_history` 中不断追加新反例。

---

## 8. 与其他 Agent 的状态对比

| 字段 | Agent1 | Agent2 | Agent3 | **Agent4** |
|---|---|---|---|---|
| `project_id` | ✓ | ✓ | ✓ | ✓ |
| `context` | ✓ | ✓ | ✓ | ✓ |
| `candidate` | ✓ | ✓ | ✓ | ✓ |
| `accepted_candidate` | — | — | — | ✓ |
| `revision` | — | — | — | ✓（两个） |
| `execution` / `defects` | — | — | — | ✓ |
| `issues` | ✓ | ✓ | ✓ | ✓ |
| `ledger` / `target_defect` / `attempted_defect_ids` / `closed_defect_ids_before` | — | — | — | ✓ |
| `semantic_audit_done` / `validation_level` / `patch_scope` / `patch_summary` | — | — | — | ✓ |
| `complete` | ✓ | ✓ | ✓ | ✓ |
| `requires_user` / `user_confirmed` | — | ✓ | ✓ | ✓ |
| `stopped` / `stop_reason` | — | — | — | ✓ |

可以看到 Agent4 在 Agent1-3 的基础上大幅扩展，引入「缺陷修复 + 反例账本 + 多轮回环」等机制，是整条流水线中状态最复杂的一环。