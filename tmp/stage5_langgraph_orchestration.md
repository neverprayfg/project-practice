# 阶段五 LangGraph 编排规范

## 状态机

```text
START
  -> contract_preflight
  -> prepare_documents
  -> generate_candidate 或 verify_candidate
  -> [存在阻断缺陷] select_defect
  -> [否则且尚未审查] semantic_audit
  -> [全部通过] approve -> wait_user/END

select_defect
  -> repair_defect
  -> recheck_history
  -> evaluate_progress
  -> select_defect / approve / END
```

`semantic_audit` 是唯一开放式语义审查。修复后只运行已知缺陷的 `targeted_recheck`，不得再次开放找问题。

## 上游契约预检

任何模型调用前必须验证：

- 阶段三已有确认标签；
- 标签存在、无冲突，且子任务标签所需参数齐全；
- 每个子任务有连续覆盖全部测试点的运行时参数；
- 参数名唯一且不使用保留名；
- 特殊测试点数量不超过总数；
- 子任务 ID、测试点 ID 和生成的约束 ID 唯一。

失败抛出 `UPSTREAM_CONTRACT_INVALID`，流水线把当前阶段设回 4，阶段五不进入生成或修复。

## Proof Obligation 与实现映射

预检把确认契约转换为通用 `ProofObligation`：

- 每个子任务的自由文本约束；
- 每项特殊测试点要求；
- 每个测试点的每个运行时参数。

候选必须原样携带这些义务，并为每个约束提交 `implementation_mapping`：源码文件、符号与行号、实际参数、文档文件/digest/API 符号、测试构造策略。后端逐约束产生标准错误码，检查缺失映射、伪造位置、未读取文档、digest 不一致、未记录 API、参数未映射和参数只读未使用。

核心状态机不包含图、树等领域硬编码。领域检查器若未来加入，只能作为验证器插件返回标准缺陷。

## 稳定缺陷身份

缺陷 ID 只由以下规范字段的哈希决定：

```text
category + target_file + constraint_id + subtask + test_point + error_code
```

日志、行号、样例正文和自然语言 message 只作为证据或展示，不参与路由。实现映射错误按真实约束 ID 与标准错误码分别建缺陷，不能聚合成模糊的全局错误。

## 反例账本

每个缺陷对应稳定反例记录，持久保存：

- `open / closed / regressed` 状态；
- 首次和最近出现的候选修订；
- 子任务、测试点、种子与实际运行参数；
- 接受、回滚、仍存在和回归历史；
- 最后完整通过的候选修订。

确定性复验重新执行所有静态/编译门和所有带种子的历史反例；历史语义缺陷逐个定向复验。被拒补丁产生的新反例保留为已关闭回归证据，账本当前状态恢复到接受候选。

## 有限修复与进展证明

阻断缺陷按验证等级和稳定 ID 排序。每次只选择一个缺陷；如果当前运行已尝试，或持久修复历史已存在，立即停止。

补丁被接受当且仅当：

```text
(open_blockers 减少 OR target_defect 关闭 OR validation_rank 前进)
AND 没有重新出现任何修复前已关闭缺陷
AND target_defect 已关闭
```

否则保存补丁范围与变更行区间，恢复上一接受候选，恢复账本当前状态并停止。代码内容不同本身永远不是进展。

## 文档上下文

首次生成读取预构建的完整参考文档。文档索引记录文件名、标题、符号与 SHA-256 digest。

repair 不再携带完整文档，只携带与目标缺陷、约束和候选实现证据匹配的最多 6 个片段。片段选择按 `candidate_revision + defect_id + document_digest_set` 持久缓存。语义审查和定向复验只接收文档元数据，不接收正文。

## 缓存

- 候选修订 + 历史反例集合：完整确定性验证结果、已通过门和回放清单；
- 源码角色 digest：Sandbox 跳过未变化 solution/generator/validator 的编译；
- 标签集合与文档 digest：初始文档选择；
- 候选修订 + 缺陷 ID：repair 文档片段。

缓存命中不能绕过候选 Schema、修订关联或历史反例集合校验。

## 决策链

`agent4-decisions.jsonl` 每个事件至少记录：

- run ID、candidate revision、目标缺陷 ID；
- 模型调用类型与模型修复理由；
- 修改文件、前后 digest、行数和变更行区间；
- 验证前后缺陷集合、阻断数量和验证等级；
- 是否取得进展；
- observed、accepted、rolled_back 或 stopped 及原因。

API `GET /api/projects/{project_id}/stage5/decisions` 返回事件与反例账本；耗时由独立 timings API 返回。

## 用户确认

只有确定性验证、一次只读语义审查和所有阻断历史反例均通过后，`approve` 才设置完成。需要用户确认时进入仅包含 `interrupt()` 的 `wait_user` 节点；恢复不会重复生成、验证或写日志副作用。
