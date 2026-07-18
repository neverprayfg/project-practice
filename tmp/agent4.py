"""Agent4（第五阶段）缺陷自动修复工作流。

本模块定义了 ``Agent4Graph``，它是项目流水线第五阶段的核心组件。它以 LangGraph 状态机
的形式串联起一份 ``generator.cpp / validator.cpp`` 联合代码候选（Candidate）的：
    1. 文档检索与契约对齐（prepare_documents）
    2. 双模型并行生成（generate_candidate）
    3. 确定性验证 + 反例账本更新（verify_candidate）
    4. 只读语义审计（semantic_audit）
    5. 阻断缺陷选择（select_defect）
    6. 定向补丁生成（repair_defect）
    7. 历史反例回放复验（recheck_history）
    8. 进步评估与回滚（evaluate_progress）
    9. 通过确认（approve）
   10. 等待用户确认（wait_user）

状态结构由 ``Agent4State`` TypedDict 定义（total=False，所有字段可选），
LangGraph 在每次节点返回时按字段合并到全局状态中。

关键设计要点：
    - **确定性优先**：优先用编译/执行反例等"硬"验证手段，只有在硬验证无 blocker 时
      才追加一次"软"的只读语义审计。
    - **反例账本**（``CounterexampleLedger``）贯穿全流程：每次验证都会把可关闭的反例
      标记为 closed，新发现的反例登记为 open；定向复验与定向修复都依赖账本提供上下文。
    - **单向晋升**：``accepted_candidate / accepted_revision / accepted_ledger`` 只有
      在进步评估确认后才更新，一旦发现回归会回滚。
    - **预算控制**：每个缺陷在一个 run 内只能被修复一次（``attempted_defect_ids``），
      ``semantic_audit_done`` 用于防止重复审计。
    - **缓存加速**：``_cached_verify`` 基于候选修订号 + 反例指纹 + 文档指纹 + 环境指纹
      做键，命中缓存可跳过实际验证，大幅加速反复重跑。
    - **人类在环**：当 ``requires_user=True`` 时通过 LangGraph ``interrupt`` 暂停，让用户
      确认后才结束。

对外暴露三个入口方法：``run``（全新运行）、``retry``（从已有 checkpoint 恢复）、
``resume``（对 interrupt 中的工作流显式恢复）。
"""


class Agent4Graph:
    """Agent4 状态机构建与运行控制器。

    构造时把以下依赖注入并把 10 个节点 + 路由边连成一张 LangGraph，
    之后通过 ``run`` / ``retry`` / ``resume`` 触发执行。
    """

    def __init__(
        self,
        settings: Settings,                  # 全局配置（runner 镜像、试验种子数等）
        storage: ProjectStorage,             # 项目级持久化（候选、账本、timing、decision 日志）
        model: AgentModel,                   # LLM 调用适配器（generate / audit / repair / recheck）
        verifier: Agent4CandidateVerifier,   # 确定性验证器（编译、执行反例、检查门）
        documents: Agent4DocumentContext,    # 文档检索上下文（library docs / api reference 等）
        ledger_service: CounterexampleLedgerService,  # 反例账本的读写与状态机
        saver: AsyncSqliteSaver,             # LangGraph 的异步 SQLite checkpoint 存储器
    ) -> None:
        # 把依赖保存为实例属性，便于各节点方法访问
        self.settings = settings
        self.storage = storage
        self.model = model
        self.verifier = verifier
        self.documents = documents
        self.ledger_service = ledger_service

        # ---- 构建 LangGraph 状态机 ----
        builder = StateGraph(Agent4State)

        # 注册 10 个节点，每个节点都是 async / sync 方法，签名 (state) -> partial_state
        builder.add_node("prepare_documents", self._prepare_documents)   # 文档检索 + 输入契约
        builder.add_node("generate_candidate", self._generate_candidate) # 并行调用 generator / validator 模型
        builder.add_node("verify_candidate", self._verify_candidate)     # 确定性验证（编译/执行反例）
        builder.add_node("semantic_audit", self._semantic_audit)         # 只读语义审计（一次性）
        builder.add_node("select_defect", self._select_defect)           # 选择下一个待修复缺陷
        builder.add_node("repair_defect", self._repair_defect)           # 定向生成补丁
        builder.add_node("recheck_history", self._recheck_history)       # 历史反例回放
        builder.add_node("evaluate_progress", self._evaluate_progress)   # 进步评估 + 可能的回滚
        builder.add_node("approve", self._approve)                       # 终审通过
        builder.add_node("wait_user", self._wait_user)                   # interrupt 等待用户确认

        # ---- 主流程边 ----
        # 起点 → 准备文档
        builder.add_edge(START, "prepare_documents")

        # 准备文档之后：若候选为空或契约对不上 → 先生成；否则直接验证
        builder.add_conditional_edges(
            "prepare_documents",
            self._route_after_prepare,
            {"generate": "generate_candidate", "verify": "verify_candidate"},
        )

        # 生成 → 立刻验证（生成出的候选必须经过硬验证）
        builder.add_edge("generate_candidate", "verify_candidate")

        # 验证后路由：
        #   - 仍有 blocker → 选下一个缺陷去修
        #   - 无 blocker 但未做语义审计 → 先做一次软审计
        #   - 否则直接进入终审
        builder.add_conditional_edges(
            "verify_candidate",
            self._route_after_verify,
            {"select": "select_defect", "audit": "semantic_audit", "approve": "approve"},
        )

        # 审计后路由：发现新 blocker 继续修，否则终审
        builder.add_conditional_edges(
            "semantic_audit",
            self._route_after_audit,
            {"select": "select_defect", "approve": "approve"},
        )

        # 选缺陷后路由：未停 → 修复；已停 → 结束
        builder.add_conditional_edges(
            "select_defect",
            self._route_after_select,
            {"repair": "repair_defect", "end": END},
        )

        # 修复后路由：未停 → 复验；已停 → 结束
        builder.add_conditional_edges(
            "repair_defect",
            self._route_after_repair,
            {"verify": "recheck_history", "end": END},
        )

        # 复验 → 评估进步
        builder.add_edge("recheck_history", "evaluate_progress")

        # 评估后路由（最复杂的分支）：
        #   - 停了 → 结束
        #   - 还有 blocker → 再选一个修
        #   - 还没审计过 → 补一次语义审计
        #   - 都好了 → 进入终审
        builder.add_conditional_edges(
            "evaluate_progress",
            self._route_after_progress,
            {
                "select": "select_defect",
                "audit": "semantic_audit",
                "approve": "approve",
                "end": END,
            },
        )

        # 终审后路由：通过 + 需要用户 → interrupt；否则结束
        builder.add_conditional_edges(
            "approve",
            self._route_after_approve,
            {"wait_user": "wait_user", "end": END},
        )

        # 用户确认（或拒绝）后直接结束
        builder.add_edge("wait_user", END)

        # 编译时挂上 SQLite checkpoint，使 ``retry`` 可恢复任意时刻
        self.graph = builder.compile(checkpointer=saver)

    async def run(
        self,
        project_id: str,             # 项目 ID
        context: dict[str, Any],     # 上游（Agent1-3）准备好的全局输入上下文
        candidate: dict[str, Any],   # 已有的联合代码候选；为空表示首次进入第五阶段
        *,
        requires_user: bool,         # 是否在 approve 后强制走 wait_user 节点
        thread_id: str | None = None,  # 可选；为空则按规则派生
    ) -> tuple[str, WorkflowOutput, bool]:
        """启动一次全新的 Agent4 run。

        返回 ``(thread_id, WorkflowOutput, was_interrupted)``：
            - ``thread_id`` 可用于 ``retry`` / ``resume``；
            - ``WorkflowOutput`` 是面向调用方的统一输出；
            - ``was_interrupted`` 表示本次是否在 ``wait_user`` 中挂起。

        抛出 ``AppError`` 的两种典型场景：
            - ``AGENT4_STATE_INCOMPATIBLE``：上游传入了不符合当前 CodeDraft 契约的候选；
            - ``AGENT4_CHECKPOINT_INCOMPATIBLE``：thread_id 指向旧版本图，会被拒绝恢复。
        """
        # ---- 入参校验：候选必须能通过当前 CodeDraft 契约 ----
        if candidate:
            try:
                # 强制重新解析，保证候选 schema 与当前严格契约一致
                candidate = CodeDraft.model_validate(candidate).model_dump(mode="json")
            except ValidationError as exc:
                raise AppError(
                    "AGENT4_STATE_INCOMPATIBLE",
                    "已有阶段五候选不符合当前严格契约；旧候选不会被重新解释或兼容。",
                    stage=5,
                    status_code=409,
                ) from exc

        # ---- 计算 thread_id ----
        # 若调用方没有显式指定，则按 (project_id, AGENT4_GRAPH_ID, context) 派生；
        # 这使得同一 (project, context) 的重跑能复用同一 thread。
        thread_id = thread_id or _thread_id(project_id, AGENT4_GRAPH_ID, context)

        # ---- 拒绝旧 checkpoint：避免被旧图重新解释 ----
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )

        # 计算初始修订号；空候选用哨兵值 "uninitialized"
        initial_revision = candidate_revision(candidate) if candidate else "uninitialized"

        # ---- 主流程：构造初始状态并交给 LangGraph 执行 ----
        started = perf_counter()
        status = "ok"
        try:
            # 反例账本从存储里加载（项目级持久化）
            ledger = self.ledger_service.load(project_id)
            state = await self.graph.ainvoke(
                # 初始状态显式列出所有字段，方便阅读；实际 LangGraph 会与后续节点返回合并
                {
                    "run_id": thread_id,
                    "project_id": project_id,
                    "context": context,
                    "candidate": candidate,
                    # 初始 accepted_* 与 candidate 同步；只有经过 _evaluate_progress 才会被升级
                    "accepted_candidate": candidate,
                    "candidate_revision": initial_revision,
                    "accepted_revision": initial_revision,
                    "execution": {},          # 待 verify_candidate 填充
                    "defects": [],            # 空列表起步
                    "issues": [],
                    "ledger": ledger.model_dump(mode="json"),
                    "attempted_defect_ids": [],
                    "semantic_audit_done": False,  # 审计预算尚未消耗
                    "validation_level": "contract", # 最浅一档，按需加深
                    "patch_scope": [],
                    "patch_summary": {},
                    "complete": False,
                    "stopped": False,
                    "stop_reason": "",
                    "requires_user": requires_user,
                    "user_confirmed": False,
                },
                _config(thread_id),  # 携带 thread_id 给 SQLite saver 做 checkpoint
            )
        except AppError as exc:
            # 已知业务异常：把 thread_id 附到 details，便于上层定位
            status = "error"
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["thread_id"] = thread_id
            exc.details = details
            raise
        except Exception as exc:
            # 未预期异常：把 thread_id 挂到对象上，由中间件统一序列化
            status = "error"
            exc.agent_thread_id = thread_id
            raise
        finally:
            # 不论成败都记录一次工作流总耗时
            self.storage.append_agent4_timing(
                project_id,
                {
                    "run_id": thread_id,
                    "segment": "workflow_total",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "status": status,
                    "metadata": {},
                },
            )
        # __interrupt__ 是 LangGraph 在 wait_user 节点暂停时自动写入状态的标记
        return thread_id, self._output(state), bool(state.get("__interrupt__"))

    async def retry(self, thread_id: str) -> tuple[str, WorkflowOutput, bool]:
        """从已有的 SQLite checkpoint 恢复一次 run。

        与 ``run`` 的区别：
            - 输入 ``None`` 表示不重新提供初始状态，而是用 saver 中保存的当前状态继续；
            - thread_id 是必传的，``project_id`` 从 ``thread_id`` 的第一段反推。

        该方法不会重启任何已完成节点，只会从最近一个未完成的节点继续。
        """
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )
        started = perf_counter()
        status = "ok"
        try:
            # 传 None：LangGraph 会去 SQLite 里读最新的 checkpoint
            state = await self.graph.ainvoke(None, _config(thread_id))
        except AppError as exc:
            status = "error"
            details = dict(exc.details) if isinstance(exc.details, dict) else {}
            details["thread_id"] = thread_id
            exc.details = details
            raise
        except Exception as exc:
            status = "error"
            exc.agent_thread_id = thread_id
            raise
        finally:
            # 这里的 project_id 是从 thread_id 反解出来的（格式：project_id:graph_id:hash）
            self.storage.append_agent4_timing(
                thread_id.split(":", 1)[0],
                {
                    "run_id": thread_id,
                    "segment": "workflow_retry_total",
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "status": status,
                    # 标记这次属于"从 checkpoint 恢复"，方便后续统计
                    "metadata": {"resumed_checkpoint": True},
                },
            )
        return thread_id, self._output(state), bool(state.get("__interrupt__"))

    @staticmethod
    def _output(state: dict[str, Any]) -> WorkflowOutput:
        """把 LangGraph 内部状态转换为对外的 ``WorkflowOutput``。

        转换规则：
            - ``confirmation``：只有 ``complete=True`` 时才返回 PASS，否则 REVISE；
            - ``result``：优先用 ``accepted_candidate``（已被晋升过的最终候选），
              缺失时回退到 ``candidate``；
            - ``issues``：去重保留顺序；未完成时额外把 blocker 缺陷的 ``message``
              和 ``stop_reason`` 一并加入，让上游知道为何被打回。
        """
        defects = [Defect.model_validate(item) for item in state.get("defects", [])]
        issues = list(state.get("issues", []))
        if not state.get("complete"):
            # 收集所有 blocker 的可读消息
            issues.extend(item.message for item in defects if item.severity == "blocker")
            # 若是因为主动停止而未完成，把停止原因也算作 issue
            if state.get("stop_reason"):
                issues.append(state["stop_reason"])
        output = WorkflowOutput(
            # 只有 complete 时才算 PASS；其余一律 REVISE
            confirmation=Confirmation.PASS if state.get("complete") else Confirmation.REVISE,
            # 优先取已被晋升过的候选；尚未晋升时退回到当前候选
            result=state.get("accepted_candidate") or state.get("candidate"),
            # dict.fromkeys 既去重又保留首次出现顺序
            issues=list(dict.fromkeys(item for item in issues if item)),
        )
        return output

    async def resume(self, thread_id: str) -> None:
        """对已 ``interrupt`` 在 ``wait_user`` 的 run 显式发送恢复信号。

        与 LangGraph 的 ``Command(resume=True)`` 不同：这里
        - 通过 ``interrupt`` 的实际返回值（用户是否确认）来填充 ``user_confirmed``；
        - 若恢复后 state 中仍无 ``user_confirmed=True``，说明 interrupt 被
          取消或传入了空值，会抛 ``CONFIRMATION_FAILED``。
        """
        if not _is_current_agent4_thread(thread_id):
            raise AppError(
                "AGENT4_CHECKPOINT_INCOMPATIBLE",
                "旧阶段五 checkpoint 不会被当前 Agent4 图恢复或兼容。",
                stage=5,
                status_code=409,
                details={"thread_id": thread_id, "required_graph": AGENT4_GRAPH_ID},
            )
        # Command(resume=True) 会让 wait_user 节点把 interrupt 的返回值当作 approved
        state = await self.graph.ainvoke(Command(resume=True), _config(thread_id))
        if not state.get("user_confirmed"):
            raise AppError("CONFIRMATION_FAILED", "Agent4 未记录用户确认。", status_code=409)

    def _prepare_documents(self, state: Agent4State) -> dict[str, Any]:
        """节点：文档检索与输入契约对齐。

        步骤：
            1. 从 ``Agent4DocumentContext`` 拉取所有相关文档（jngen/testlib 等）；
            2. 根据 ``state["context"]`` 构建"输入格式契约"——一个唯一标识 generator/validator
               输出形态的冻结 ID；
            3. 把文档、契约、运行计时标识一并写回 ``context``，供后续模型调用读取。

        返回仅 ``context``，由 LangGraph 合并到全局状态。
        """
        started = perf_counter()
        # 一次拉取所有相关文档，避免后续每个模型调用都重复检索
        documentation = self.documents.load_all_documents()
        # 输入格式契约：固化输入/输出格式要求，模型必须遵循
        format_contract = build_input_format_contract(state["context"])
        # 复制原 context，避免直接修改 state 中的字段
        context = dict(state["context"])
        # 文档包放在 agent4_library_context_bundle，模型层通过 _context_for_code_role 取出
        context["agent4_library_context_bundle"] = documentation
        # 把契约序列化进 context，下游生成节点会比对返回的 format_contract_id
        context["input_format_contract"] = format_contract.model_dump(mode="json")
        # 计时上下文：后续每个节点的 timing 都会带上 run_id 与当前轮次
        context["_agent4_timing"] = {"run_id": state["run_id"], "round": 1}
        self._timing(
            state,
            "retrieval",   # 节点段名
            started,
            metadata={
                "purpose": "complete_document_load",
                "document_count": documentation["document_count"],
                "total_characters": documentation["total_characters"],
            },
        )
        return {"context": context}

    async def _generate_candidate(self, state: Agent4State) -> dict[str, Any]:
        """节点：并行生成 generator / validator 的源码，合并成联合候选。

        流程：
            1. ``asyncio.gather`` 并行调用两个模型（generator 写 generator.cpp，
               validator 写 validator.cpp），使用 ``return_exceptions=True`` 避免
               一次失败导致另一侧的响应也被丢弃；
            2. 任何一侧抛异常 → 记录 decision 并重抛（不会写入新 candidate）；
            3. 校验两侧返回的 ``format_contract_id`` 与后端冻结的契约一致；
               不一致视为违反协议，停止；
            4. 合并双方代码为联合 candidate，计算新 revision，写决策日志。
        """
        started = perf_counter()
        # 并行生成两份代码；return_exceptions=True 让两个调用互不干扰
        results = await asyncio.gather(
            self.model.agent4_generate_generator(
                _context_for_code_role(state["context"], "generator"),
                state.get("candidate", {}),
            ),
            self.model.agent4_generate_validator(
                _context_for_code_role(state["context"], "validator"),
                state.get("candidate", {}),
            ),
            return_exceptions=True,
        )
        # ---- 异常处理：任一角色失败都要记录并终止 ----
        roles = ("generator", "validator")
        failures = [
            (role, result)
            for role, result in zip(roles, results, strict=True)
            if isinstance(result, Exception)
        ]
        if failures:
            for role, error in failures:
                # 把异常转化为决策日志，便于审计
                self._decision(
                    state,
                    state["candidate_revision"],
                    model_call_type=f"{role}_generation",
                    decision="stopped",
                    reason=f"{role} 初始生成响应未通过协议校验，未形成联合候选。",
                    after={
                        "model_error": error.payload()
                        if isinstance(error, AppError)
                        else {"error_type": type(error).__name__}
                    },
                )
            self._timing(
                state,
                "model_generation",
                started,
                status="error",
                metadata={"parallel_calls": 2, "failed_roles": [item[0] for item in failures]},
            )
            # 把第一个失败原样抛出（一般是更具体的 AppError）
            raise failures[0][1]

        # ---- 契约一致性校验 ----
        generated_generator, generated_validator = results
        expected_contract_id = state["context"]["input_format_contract"]["format_contract_id"]
        returned_contract_ids = {
            "generator": generated_generator.format_contract_id,
            "validator": generated_validator.format_contract_id,
        }
        # 任一角色返回的契约 ID 与冻结的不一致 → 视为违反协议
        mismatched_roles = [
            role
            for role, contract_id in returned_contract_ids.items()
            if contract_id != expected_contract_id
        ]
        if mismatched_roles:
            for role in mismatched_roles:
                self._decision(
                    state,
                    state["candidate_revision"],
                    model_call_type=f"{role}_generation",
                    decision="stopped",
                    reason=f"{role} 未遵守后端冻结的输入格式契约。",
                    after={
                        "expected_format_contract_id": expected_contract_id,
                        "returned_format_contract_id": returned_contract_ids[role],
                    },
                )
            self._timing(
                state,
                "model_generation",
                started,
                status="error",
                metadata={"parallel_calls": 2, "format_mismatch_roles": mismatched_roles},
            )
            raise AppError(
                "FORMAT_CONTRACT_MISMATCH",
                "generator 或 validator 未遵守冻结的输入格式契约。",
                stage=5,
                details={
                    "expected_format_contract_id": expected_contract_id,
                    "returned_format_contract_ids": returned_contract_ids,
                },
            )

        # ---- 合并为联合候选 ----
        candidate = {
            "format_contract_id": expected_contract_id,
            "generator_code": generated_generator.generator_code,
            "validator_code": generated_validator.validator_code,
        }
        # 新候选的修订号，用于缓存键 & 增量判断
        revision = candidate_revision(candidate)
        # 为两份源码分别记一条 decision 日志（observed 而非 accepted：还要等验证）
        for role, modified_file in (
            ("generator", "generator.cpp"),
            ("validator", "validator.cpp"),
        ):
            self._decision(
                state,
                revision,
                model_call_type=f"{role}_generation",
                decision="observed",
                reason=f"并行生成 {modified_file} 并合入联合候选，尚待确定性验证。",
                modified_files=[modified_file],
                after={"format_contract_id": expected_contract_id},
            )
        self._timing(
            state,
            "model_generation",
            started,
            metadata={"parallel_calls": 2, "roles": list(roles)},
        )
        return {"candidate": candidate, "candidate_revision": revision}

    async def _verify_candidate(self, state: Agent4State) -> dict[str, Any]:
        """节点：确定性验证候选（编译 + 执行反例）。

        流程：
            1. 调用 ``_cached_verify``（带缓存的执行验证）：得到 ``candidate`` 与
               ``execution``（其中包含各类门：编译、运行时、契约等）；
            2. ``defects_from_execution`` 从执行结果里抽取本轮新缺陷；
            3. ``_deterministically_covered_defect_ids`` 计算"历史缺陷中，本轮实际
               真正覆盖到、可关闭"的子集（仅基于硬验证，不包含语义层）；
            4. 把可关闭的历史反例交给账本服务去更新 ledger；
            5. 计算 ``validation_level``：通过则 "complete"，否则取执行报告中的
               验证档位；
            6. 记录决策日志并返回增量更新。
        """
        # 从 state 反序列化账本
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        # _cached_verify：先查缓存，命中即直接返回上次结果；未命中才真正调用 verifier
        candidate, execution = await self._cached_verify(
            state["project_id"], state["candidate"], state["context"], ledger
        )
        # 从 execution 中抽取本轮新发现的所有缺陷（包含新增与回归）
        defects = defects_from_execution(execution)
        # 计算候选的修订号（candidate 可能因 verifier 的"清洗"略有变化）
        revision = candidate_revision(candidate)
        # 仅基于确定性验证找出"真的复验过、可关闭"的历史缺陷 ID
        covered_ids = self._deterministically_covered_defect_ids(ledger, execution)
        # 通知账本：新增 defects、关闭 covered_ids 中的项
        ledger = self.ledger_service.observe(
            state["project_id"],
            ledger,
            defects,
            revision,
            closable_defect_ids=covered_ids,
        )
        # validation_level：完全通过 → complete；否则取执行报告里的层级
        level = str(execution.get("validation_level") or "static")
        if execution.get("ok"):
            level = "complete"
        self._decision(
            state,
            revision,
            after={
                **verification_summary(defects, level),
                "covered_historical_defect_ids": sorted(covered_ids),
                "history_replay_complete": execution.get("history_replay_complete", False),
            },
            decision="observed",
            reason=(
                "候选确定性验证通过。" if execution.get("ok") else "候选确定性验证发现阻断缺陷。"
            ),
        )
        return {
            "candidate": candidate,
            "candidate_revision": revision,
            "execution": execution,
            "defects": [item.model_dump(mode="json") for item in defects],
            "ledger": ledger.model_dump(mode="json"),
            "validation_level": level,
        }

    async def _semantic_audit(self, state: Agent4State) -> dict[str, Any]:
        """节点：执行一次只读的语义审计。

        注意：此节点**只读**——它不会修改源码，只是让一个独立的审计模型对
        当前候选做一次"软"评判，识别语义层面的缺陷（例如边界条件错误、
        题意理解偏差等）。整个 run 中只允许调用一次（由 ``semantic_audit_done``
        标志位控制）。

        流程：
            1. 收集账本中已知的所有 semantic 缺陷（用于提示审计模型避免重复）；
            2. 调用 ``model.agent4_audit``，异常分类处理（AppError 记决策日志，
               其他异常只记 timing）；
            3. 把审计结果规范化成 Defect 列表（按 defect_id 去重）；
            4. 与确定性 defects 合并：保留全部非 semantic 缺陷 + 新发现的 semantic 缺陷；
            5. 把已知的 semantic 缺陷 ID 视为可关闭（账本 observe）；
            6. 若本次审计发现缺陷，将 ``validation_level`` 升到 "semantic"；
               否则保持不变（避免档位倒退）。
        """
        started = perf_counter()
        existing_ledger = CounterexampleLedger.model_validate(state["ledger"])
        # 把账本里已有的 semantic 缺陷交给模型，避免它重复报告
        known_semantic = [
            item.defect.model_dump(mode="json")
            for item in existing_ledger.counterexamples
            if item.defect.validation_level == "semantic"
        ]
        # 构造审计上下文（不暴露 generator.cpp / validator.cpp 的具体角色）
        audit_context = {
            **_context_for_review(state["context"], ("generator", "validator")),
            "known_semantic_defects": known_semantic,
        }
        try:
            audit = await self.model.agent4_audit(
                audit_context, state["candidate"], state["execution"]
            )
        except AppError as exc:
            # 协议错误：审计响应不符合契约，记 decision 并抛出
            self._decision(
                state,
                state["candidate_revision"],
                model_call_type="semantic_audit",
                decision="stopped",
                reason="只读语义审查的模型响应未通过协议校验，未产生缺陷。",
                after={"model_error": exc.payload()},
            )
            self._timing(state, "semantic_audit", started, status="error")
            raise
        except Exception:
            # 其他异常：只记 timing，不写 decision（日志由调用栈处理）
            self._timing(state, "semantic_audit", started, status="error")
            raise
        self._timing(state, "semantic_audit", started)
        # 把审计报告规范化为 Defect（按 defect_id 去重）
        semantic_defects = self._normalize_audit(audit)
        # 保留所有非 semantic 缺陷（确定性验证发现的）
        deterministic = [
            Defect.model_validate(item)
            for item in state.get("defects", [])
            if item.get("validation_level") != "semantic"
        ]
        defects = [*deterministic, *semantic_defects]
        # 已知 semantic 缺陷的 ID —— 本次审计已覆盖，可关闭
        semantic_ids = {
            item.defect.defect_id
            for item in existing_ledger.counterexamples
            if item.defect.validation_level == "semantic"
        }
        # 把已关闭的 semantic 反例通知账本
        ledger = self.ledger_service.observe(
            state["project_id"],
            existing_ledger,
            defects,
            state["candidate_revision"],
            closable_defect_ids=semantic_ids,
        )
        self._decision(
            state,
            state["candidate_revision"],
            model_call_type="semantic_audit",
            decision="observed",
            reason=f"一次开放只读审查发现 {len(semantic_defects)} 个缺陷。",
            after={
                "reported_defect_ids": [item.defect_id for item in semantic_defects],
                "rechecked_historical_defect_ids": sorted(semantic_ids),
            },
        )
        return {
            "defects": [item.model_dump(mode="json") for item in defects],
            "ledger": ledger.model_dump(mode="json"),
            # 关键标志：审计预算已耗尽，后续路由不会再回到这个节点
            "semantic_audit_done": True,
            # 仅当本次审计没发现新缺陷时升级到 semantic；否则保持原档位
            "validation_level": "semantic" if not semantic_defects else state["validation_level"],
        }

    def _select_defect(self, state: Agent4State) -> dict[str, Any]:
        """节点：挑选下一个待修复的阻断缺陷（blocker）。

        挑选策略：
            1. 在所有 severity == "blocker" 的缺陷中按 ``(validation_level, defect_id)``
               排序，优先处理档位更深（更严重）的缺陷；
            2. 检查目标文件是否在 Agent4 可修改范围内（generator.cpp / validator.cpp）；
               不在 → 立即停止（防止误改其他文件）；
            3. 检查本 run 中是否已尝试修复过该缺陷（``attempted_defect_ids``）；
               已尝试过 → 停止，避免无限循环；
            4. 满足条件则把目标缺陷写入 ``target_defect``、追加 ``attempted_defect_ids``、
               并把"修复前的快照"（accepted_candidate / accepted_revision / accepted_ledger /
               baseline_summary / closed_defect_ids_before）写入 state，便于
               ``_evaluate_progress`` 判断进步与回滚。
        """
        defects = [Defect.model_validate(item) for item in state.get("defects", [])]
        # 只考虑 blocker；按档位深 → 浅排序，档位相同按 defect_id 稳定排序
        blockers = sorted(
            (item for item in defects if item.severity == "blocker"),
            key=lambda item: (VALIDATION_LEVELS[item.validation_level], item.defect_id),
        )
        # 没有 blocker：没必要继续挑，本次 run 结束
        if not blockers:
            reason = "没有可修复的阻断缺陷。"
            self._decision(
                state,
                state["candidate_revision"],
                decision="stopped",
                reason=reason,
            )
            return {"stopped": True, "stop_reason": reason}
        target = blockers[0]
        # Agent4 只能修改 generator.cpp / validator.cpp；目标文件不在此范围 → 立即停止
        if target.identity.target_file not in {
            "generator.cpp",
            "validator.cpp",
        }:
            reason = (
                f"缺陷 {target.defect_id} 的目标 {target.identity.target_file} 不属于 Agent4 "
                "可修改范围，已停止且未调用修复模型。"
            )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                decision="stopped",
                reason=reason,
            )
            return {"stopped": True, "stop_reason": reason}
        # 单个 run 内每个缺陷只能修复一次，避免循环
        attempted = state.get("attempted_defect_ids", [])
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        if target.defect_id in attempted:
            reason = f"缺陷 {target.defect_id} 修复一次后仍存在，已停止。"
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                decision="stopped",
                reason=reason,
            )
            return {"stopped": True, "stop_reason": reason}
        # 记录"修复前"的状态快照：
        #   - closed_defect_ids_before：开始本轮修复时已关闭的缺陷，用于回归检测；
        #   - baseline_summary：当前验证摘要，用于与修复后做差量；
        #   - accepted_ledger / accepted_candidate / accepted_revision：用于回滚
        closed_before = [
            item.defect.defect_id for item in ledger.counterexamples if item.status == "closed"
        ]
        return {
            "target_defect": target.model_dump(mode="json"),
            # 单调追加：确保每个缺陷在本次 run 中只被选一次
            "attempted_defect_ids": [*attempted, target.defect_id],
            "closed_defect_ids_before": closed_before,
            "baseline_summary": verification_summary(defects, state["validation_level"]),
            "accepted_ledger": state["ledger"],
            "accepted_candidate": state["candidate"],
            "accepted_revision": state["candidate_revision"],
        }

    async def _repair_defect(self, state: Agent4State) -> dict[str, Any]:
        """节点：定向生成补丁修复 ``target_defect``。

        严格的四道闸：
            1. **协议闸**：模型响应必须符合 Agent4Repair 协议（AppError → 停止）；
            2. **目标闸**：返回的 ``target_defect_id`` 必须等于请求的（防错位修复）；
            3. **范围闸**：模型只能修改目标缺陷所属角色的源码
               （generator.cpp → generator_code，validator.cpp → validator_code），
               返回其他字段视为越权；
            4. **完整性闸**：源码类缺陷必须返回对应源码字段，缺失即停止。

        通过四道闸后，把补丁合并进 ``proposed``，计算新 revision 与 patch_summary。
        若 ``revision == candidate_revision`` 说明补丁是空的（与原候选一致），
        也视为失败并停止。
        """
        started = perf_counter()
        target = Defect.model_validate(state["target_defect"])
        # ---- 闸 1：调用修复模型 ----
        try:
            patch = await self.model.agent4_repair(
                _context_for_defect(state["context"], target),
                state["candidate"],
                target,
            )
        except AppError as exc:
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason="定向修复的模型响应未通过协议校验，未形成补丁。",
                after={"model_error": exc.payload()},
            )
            self._timing(state, "targeted_repair", started, status="error")
            raise
        except Exception:
            self._timing(state, "targeted_repair", started, status="error")
            raise
        self._timing(
            state,
            "targeted_repair",
            started,
            metadata={"target_defect_id": target.defect_id},
        )
        # ---- 闸 2：目标一致性 ----
        if patch.target_defect_id != target.defect_id:
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason="定向修复模型返回了不同的目标缺陷 ID。",
            )
            raise AppError(
                "REPAIR_TARGET_MISMATCH",
                "修复模型返回了不同的目标缺陷 ID。",
                stage=5,
            )
        # ---- 闸 3：角色范围一致性 ----
        # 缺陷所在的文件 → 应当修改的源码字段
        expected_code_field = {
            "generator.cpp": "generator_code",
            "validator.cpp": "validator_code",
        }.get(target.identity.target_file)
        returned_code_fields = {
            field
            for field in ("generator_code", "validator_code")
            if getattr(patch, field) is not None
        }
        # 若补丁修改了"非目标角色"的源码 → 越权
        if expected_code_field and returned_code_fields - {expected_code_field}:
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason="定向修复返回了目标角色之外的源码。",
                after={"returned_code_fields": sorted(returned_code_fields)},
            )
            raise AppError(
                "REPAIR_SCOPE_MISMATCH",
                "定向修复只能返回目标缺陷所属角色的源码。",
                stage=5,
            )
        # ---- 闸 4：源码完整性 ----
        # 对源码行为类缺陷，模型必须给出对应的源码字段
        required_code_field = _required_source_patch_field(target)
        if required_code_field and getattr(patch, required_code_field) is None:
            reason = (
                f"缺陷 {target.defect_id} 属于源码行为缺陷，但补丁没有返回 "
                f"{required_code_field}，已停止。"
            )
            ledger = self.ledger_service.record_repair(
                state["project_id"],
                CounterexampleLedger.model_validate(state["ledger"]),
                target.defect_id,
                state["candidate_revision"],
                [],                      # patch_scope 为空：未形成有效补丁
                "still_open",            # 状态：缺陷仍然 open
                reason,
            )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason=reason,
                after={
                    "required_patch_field": required_code_field,
                    "model_rationale": patch.rationale,
                },
            )
            return {
                "stopped": True,
                "stop_reason": reason,
                "patch_scope": [],
                "ledger": ledger.model_dump(mode="json"),
            }
        # ---- 把补丁合并进候选 ----
        proposed = dict(state["candidate"])
        patch_scope: list[str] = []
        for field, scope_name in (
            ("generator_code", "generator.cpp"),
            ("validator_code", "validator.cpp"),
        ):
            value = getattr(patch, field)
            if value is not None:
                proposed[field] = value
                patch_scope.append(scope_name)
        revision = candidate_revision(proposed)
        patch_summary = _patch_summary(state["candidate"], proposed, patch_scope)
        # ---- 空补丁兜底：模型说改但其实什么都没改 ----
        if revision == state["candidate_revision"]:
            reason = f"缺陷 {target.defect_id} 的补丁没有形成新候选，已停止。"
            ledger = self.ledger_service.record_repair(
                state["project_id"],
                CounterexampleLedger.model_validate(state["ledger"]),
                target.defect_id,
                revision,
                patch_scope,
                "still_open",
                reason,
            )
            self._decision(
                state,
                revision,
                target_defect_id=target.defect_id,
                model_call_type="repair",
                decision="stopped",
                reason=reason,
                modified_files=patch_scope,
                after={"patch": patch_summary, "model_rationale": patch.rationale},
            )
            return {
                "stopped": True,
                "stop_reason": reason,
                "patch_scope": patch_scope,
                "patch_summary": patch_summary,
                "ledger": ledger.model_dump(mode="json"),
            }
        # ---- 正常路径：候选已更新，待 recheck_history 复验 ----
        self._decision(
            state,
            revision,
            target_defect_id=target.defect_id,
            model_call_type="repair",
            modified_files=patch_scope,
            after={"patch": patch_summary, "model_rationale": patch.rationale},
            decision="observed",
            reason="定向修复模型已提交补丁，等待全部历史反例复验。",
        )
        return {
            "candidate": proposed,
            "candidate_revision": revision,
            "patch_scope": patch_scope,
            "patch_summary": patch_summary,
        }

    async def _recheck_history(self, state: Agent4State) -> dict[str, Any]:
        """节点：定向复验历史反例，重点回放语义缺陷。

        流程：
            1. 跑一次确定性验证（带缓存），得到新 execution；
            2. 取出所有已知 semantic 缺陷，**并行**让审计模型针对每条复验
               （``agent4_recheck``：判断在新候选下该缺陷是否仍存在）；
            3. 对每条复验：要求返回的 defect_id 与请求一致（防错位），
               并且声称"仍存在"时必须提供**基于当前候选源码**的证据；
               不 grounded 的证据不接受（防止幻觉缺陷阻断修复）；
            4. 把确定性 defects 与被接受的"仍存在"的 semantic defects 合并；
            5. 把所有历史 semantic defects 视为已覆盖、通知账本关闭。
        """
        started = perf_counter()
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        # 跑一次验证（带缓存）：得到最新 execution 与（可能修正过的）candidate
        candidate, execution = await self._cached_verify(
            state["project_id"], state["candidate"], state["context"], ledger
        )
        # 从执行报告抽取新发现的确定性缺陷
        deterministic = defects_from_execution(execution)
        # 历史语义反例：需要模型针对每条单独复验
        known_semantic = [
            item.defect
            for item in ledger.counterexamples
            if item.defect.validation_level == "semantic"
        ]
        # 并行调用复验模型（一条反例一次调用）
        try:
            checks = await asyncio.gather(
                *(
                    self.model.agent4_recheck(
                        {
                            **_context_for_defect(state["context"], defect),
                            "candidate_revision": state["candidate_revision"],
                        },
                        candidate,
                        defect,
                        execution,
                    )
                    for defect in known_semantic
                )
            )
        except AppError as exc:
            self._decision(
                state,
                state["candidate_revision"],
                model_call_type="targeted_recheck",
                decision="stopped",
                reason="历史反例定向复验的模型响应未通过协议校验，未改变缺陷状态。",
                after={"model_error": exc.payload()},
            )
            self._timing(state, "targeted_recheck", started, status="error")
            raise
        # 逐条评估复验结果
        semantic_open: list[Defect] = []
        for defect, check in zip(known_semantic, checks, strict=True):
            # 缺陷 ID 必须对齐（防错位复验）
            if check.defect_id != defect.defect_id:
                self._decision(
                    state,
                    state["candidate_revision"],
                    target_defect_id=defect.defect_id,
                    model_call_type="targeted_recheck",
                    decision="stopped",
                    reason="定向复验返回了不同的缺陷 ID。",
                )
                raise AppError(
                    "RECHECK_TARGET_MISMATCH",
                    "定向复验返回了不同的缺陷 ID。",
                    stage=5,
                )
            # evidence_grounded：声称"仍存在"的证据必须真的来自当前候选源码
            evidence_grounded = _semantic_recheck_evidence_is_grounded(check, defect, candidate)
            accepted_still_present = bool(check.still_present and evidence_grounded)
            if accepted_still_present:
                # 通过接地检查：把缺陷作为"仍 open"放入列表，并把证据标注来源
                assert check.evidence is not None
                semantic_open.append(
                    defect.model_copy(
                        update={
                            "message": check.message,
                            "evidence": {
                                **check.evidence.model_dump(mode="json"),
                                "origin": "candidate",
                                "grounded_in_candidate_revision": state["candidate_revision"],
                            },
                        }
                    )
                )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=defect.defect_id,
                model_call_type="targeted_recheck",
                after={
                    "reported_still_present": check.still_present,
                    "accepted_still_present": accepted_still_present,
                    "evidence_grounded": evidence_grounded,
                    "message": check.message,
                    "evidence": check.evidence.model_dump(mode="json")
                    if check.evidence is not None
                    else None,
                },
                decision="observed",
                reason=(
                    "历史语义反例定向复验完成。"
                    if not check.still_present or evidence_grounded
                    else "复验声称缺陷仍存在，但证据不属于当前候选源码，未作为阻断缺陷接受。"
                ),
            )
        self._timing(
            state,
            "targeted_recheck",
            started,
            metadata={"known_semantic_defects": len(known_semantic)},
        )
        # 合并缺陷列表：确定性 + 通过接地的语义缺陷
        defects = [*deterministic, *semantic_open]
        revision = candidate_revision(candidate)
        # 本轮复验视为覆盖了所有历史 semantic 缺陷
        covered_ids = self._deterministically_covered_defect_ids(ledger, execution)
        covered_ids.update(item.defect_id for item in known_semantic)
        ledger = self.ledger_service.observe(
            state["project_id"],
            ledger,
            defects,
            revision,
            closable_defect_ids=covered_ids,
        )
        level = (
            "complete"
            if execution.get("ok")
            else str(execution.get("validation_level") or "static")
        )
        return {
            "candidate": candidate,
            "candidate_revision": revision,
            "execution": execution,
            "defects": [item.model_dump(mode="json") for item in defects],
            "ledger": ledger.model_dump(mode="json"),
            "validation_level": level,
        }

    def _evaluate_progress(self, state: Agent4State) -> dict[str, Any]:
        """节点：评估本轮修复是否取得进步。

        进步判定规则（同时满足才视为进步）：
            - **目标关闭**：``target.defect_id`` 不在 after 缺陷集合里；
            - **整体好转**：(open_blockers 减少) 或 (验证档位提升)；
            - **无回归**：进入本轮前已关闭的缺陷集合与 after 没有交集。

        任意一条失败 → 回滚候选到 ``accepted_*``、记 ``rolled_back`` 决策、停止；
        全部通过 → 把当前候选晋升为 ``accepted_*``、记 ``accepted`` 决策。
        """
        target = Defect.model_validate(state["target_defect"])
        defects = [Defect.model_validate(item) for item in state.get("defects", [])]
        # 当前验证摘要（缺陷 ID 集合、blocker 列表、档位等）
        after = verification_summary(defects, state["validation_level"])
        # 修复前的快照（select_defect 写入）
        before = state["baseline_summary"]
        after_ids = set(after["defect_ids"])
        # 本轮相对基线新冒出的 blocker（用于决策日志中的"新增"统计）
        newly_observed_blockers = sorted(set(after["blocker_ids"]) - set(before["blocker_ids"]))
        # 回归：本轮已关闭的缺陷又出现了
        regression = bool(set(state.get("closed_defect_ids_before", [])) & after_ids)
        target_closed = target.defect_id not in after_ids
        # 综合进步指标：目标关闭 OR 验证档位提升 OR blocker 减少
        progress = (
            after["open_blockers"] < before["open_blockers"]
            or target_closed
            or after["validation_rank"] > before["validation_rank"]
        ) and not regression
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        # ---- 不进步情形：决定具体的回滚原因 ----
        if not target_closed:
            progress = False
            reason = f"缺陷 {target.defect_id} 修复一次后仍存在，候选已回滚并停止。"
        elif regression:
            progress = False
            reason = "补丁重新引入了已关闭缺陷，候选已回滚并停止。"
        elif not progress:
            reason = "阻断缺陷、目标缺陷和验证等级均未改善，候选已回滚并停止。"
        else:
            reason = "目标缺陷已关闭或验证等级前进，且未发生回归。"

        # ---- 回滚分支 ----
        if not progress:
            # rollback_repair：把账本从 accepted_ledger 还原，并标记本次修复 rolled_back
            ledger = self.ledger_service.rollback_repair(
                state["project_id"],
                CounterexampleLedger.model_validate(state["accepted_ledger"]),
                ledger,
                target.defect_id,
                state["candidate_revision"],
                state.get("patch_scope", []),
                "rolled_back",
                reason,
            )
            self._decision(
                state,
                state["candidate_revision"],
                target_defect_id=target.defect_id,
                model_call_type="repair",
                modified_files=state.get("patch_scope", []),
                before=before,
                after={
                    **after,
                    "newly_observed_blocker_ids": newly_observed_blockers,
                    "patch": state.get("patch_summary", {}),
                },
                progress=False,
                decision="rolled_back",
                reason=reason,
            )
            # 把 candidate 还原到 accepted 版本（防止后续 evaluate_progress 再次回退）
            return {
                "candidate": state["accepted_candidate"],
                "candidate_revision": state["accepted_revision"],
                "ledger": ledger.model_dump(mode="json"),
                "stopped": True,
                "stop_reason": reason,
            }

        # ---- 晋升分支 ----
        ledger = self.ledger_service.record_repair(
            state["project_id"],
            ledger,
            target.defect_id,
            state["candidate_revision"],
            state.get("patch_scope", []),
            "accepted",
            reason,
        )
        self._decision(
            state,
            state["candidate_revision"],
            target_defect_id=target.defect_id,
            model_call_type="repair",
            modified_files=state.get("patch_scope", []),
            before=before,
            after={
                **after,
                "newly_observed_blocker_ids": newly_observed_blockers,
                "patch": state.get("patch_summary", {}),
            },
            progress=True,
            decision="accepted",
            reason=reason,
        )
        # 把当前 candidate / revision 标记为新的"已接受基线"
        return {
            "accepted_candidate": state["candidate"],
            "accepted_revision": state["candidate_revision"],
            "ledger": ledger.model_dump(mode="json"),
        }

    def _approve(self, state: Agent4State) -> dict[str, Any]:
        """节点：终审通过。

        通过条件：账本中没有任何"未关闭且 severity == blocker"的反例。
        通过后：
            - 把 ``candidate`` 重新序列化为 CodeDraft（去掉 issues 字段）；
            - 同步 ``accepted_*``；
            - ``complete=True``（工作流收敛）；
            - 清空 ``issues``。
        """
        ledger = CounterexampleLedger.model_validate(state["ledger"])
        # 任意未关闭 blocker 都视为存在阻断 → 拒绝通过
        if any(
            item.status != "closed" and item.defect.severity == "blocker"
            for item in ledger.counterexamples
        ):
            reason = "仍有未关闭或回归的历史反例，不能确认通过。"
            self._decision(
                state,
                state["candidate_revision"],
                decision="stopped",
                reason=reason,
            )
            return {
                "stopped": True,
                "stop_reason": reason,
            }
        # 重新解析为 CodeDraft 并排除 issues（issues 是 Agent 层产物，不属于 CodeDraft 契约）
        accepted_candidate = CodeDraft.model_validate(state["candidate"]).model_dump(
            mode="json", exclude={"issues"}
        )
        self._decision(
            state,
            state["candidate_revision"],
            decision="accepted",
            progress=True,
            reason="确定性验证、全部历史反例和一次只读语义审查均通过。",
        )
        return {
            "candidate": accepted_candidate,
            "accepted_candidate": accepted_candidate,
            "accepted_revision": state["candidate_revision"],
            # complete=True 让 _output 返回 Confirmation.PASS
            "complete": True,
            "issues": [],
        }

    @staticmethod
    def _wait_user(state: Agent4State) -> dict[str, Any]:
        """节点：通过 LangGraph ``interrupt`` 暂停工作流，等待用户确认。

        暂停时会把 agent 名称、candidate 修订号和 candidate 一起交给上层 UI；
        用户确认后调用 ``Agent4Graph.resume(thread_id)`` 发送 ``Command(resume=True)``，
        该方法的返回值会成为 ``approved``，进而填入 ``user_confirmed``。
        """
        approved = interrupt(
            {
                "agent": "agent4",
                "candidate_revision": state["candidate_revision"],
                "candidate": state["candidate"],
            }
        )
        return {"user_confirmed": bool(approved)}

    async def _cached_verify(
        self,
        project_id: str,
        candidate: dict[str, Any],
        context: dict[str, Any],
        ledger: CounterexampleLedger,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """带缓存的验证。

        缓存键 = ``revision : environment_fingerprint : ledger_fingerprint``，
        其中：
            - ``environment_fingerprint`` 包含文档指纹、输入/子任务修订号、
              solution digest、运行镜像、试验种子数、verifier 修订号；
            - ``ledger_fingerprint`` 是所有可复现反例（带 seed 的）的指纹。

        三者任一变化都会让缓存失效。同样的输入第二次进入时直接读缓存，
        命中记 ``verification_cache`` 时长为 0 ms 的 hit 事件。
        """
        revision = candidate_revision(candidate)
        # 反例指纹：只考虑带 seed 的（可重放）；按 counterexample_id + reproduction 排序哈希
        replay_fingerprints = [
            json.dumps(
                {
                    "counterexample_id": item.counterexample_id,
                    "reproduction": item.reproduction,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in ledger.counterexamples
            if item.reproduction.get("seed") is not None
        ]
        documentation = context.get("agent4_library_context_bundle", {})
        document_fingerprints = [
            f"{item.get('filename')}:{item.get('digest')}"
            for item in documentation.get("documents", [])
            if isinstance(item, dict)
        ]
        role_digests = _role_digests(candidate, project_id, self.storage)
        # 环境指纹：决定"同样的输入在同样的环境下"是否复用结果
        environment_fingerprint = ledger_digest(
            [
                *document_fingerprints,
                f"input_revision:{context.get('input_revision')}",
                f"subtasks_revision:{context.get('subtasks_revision')}",
                f"solution:{role_digests['solution']}",
                f"compile_image:{self.settings.runner_compile_image}",
                f"execute_image:{self.settings.runner_execute_image}",
                f"trial_seeds:{self.settings.agent_trial_seeds_per_subtask}",
                AGENT4_VERIFIER_REVISION,
            ]
        )
        key = f"{revision}:{environment_fingerprint}:{ledger_digest(replay_fingerprints)}"
        cache = self.storage.load_agent4_cache(project_id)
        cached = cache.setdefault("candidates", {}).get(key)
        if isinstance(cached, dict):
            # 缓存命中：直接返回上次结果，并记一条 hit 计时
            timing = context.get("_agent4_timing", {})
            self.storage.append_agent4_timing(
                project_id,
                {
                    "run_id": timing.get("run_id", "cache"),
                    "segment": "verification_cache",
                    "duration_ms": 0.0,
                    "status": "hit",
                    "metadata": {"candidate_revision": revision},
                },
            )
            return cached["candidate"], cached["execution"]
        # 缓存未命中：实际调用 verifier 跑一次完整验证
        verified, execution = await self.verifier.verify(
            project_id,
            candidate,
            context,
            ledger.counterexamples,
        )
        # 把结果写回缓存（含 replays、gates、role_digests 等元信息，便于审计）
        cache["candidates"][key] = {
            "candidate": verified,
            "execution": execution,
            "replayed_counterexamples": execution.get("replayed_counterexample_ids", []),
            "gates": [
                check.get("operation")
                for check in execution.get("checks", [])
                if isinstance(check, dict) and check.get("ok") is not False
            ],
            "role_digests": role_digests,
            "environment_fingerprint": environment_fingerprint,
        }
        self.storage.save_agent4_cache(project_id, cache)
        return verified, execution

    @staticmethod
    def _deterministically_covered_defect_ids(
        ledger: CounterexampleLedger,
        execution: dict[str, Any],
    ) -> set[str]:
        """计算"历史缺陷中本轮真的覆盖过"的子集（仅基于硬验证）。

        覆盖规则（任一满足即视为覆盖）：
            1. 反例在 execution 中被显式 replay（``replayed_counterexample_ids``）；
            2. 整体执行 ``ok=True``（所有门都通过，等价覆盖所有更浅档位的缺陷）；
            3. 缺陷对应的检查 operation 出现在 ``fully_evaluated_operations`` 中；
            4. 缺陷的档位 < 当前执行档位（更浅档位的门在失败前都已通过）。

        语义缺陷 (``validation_level == "semantic"``) 一律不计入，因为它们
        需要单独的语义复验。
        """

        replayed_ids = set(execution.get("replayed_counterexample_ids", []))
        fully_evaluated_operations = set(execution.get("fully_evaluated_operations", []))
        current_level = (
            "complete"
            if execution.get("ok")
            else str(execution.get("validation_level") or "static")
        )
        current_rank = VALIDATION_LEVELS.get(current_level, 0)
        covered: set[str] = set()
        for item in ledger.counterexamples:
            # 语义层缺陷交给 _recheck_history 处理
            if item.defect.validation_level == "semantic":
                continue
            # 显式 replay 或全跑通 → 覆盖
            if item.counterexample_id in replayed_ids or execution.get("ok"):
                covered.add(item.defect.defect_id)
                continue
            # 缺陷对应的检查 operation 已完整评估 → 覆盖
            check = item.defect.evidence.get("check", {})
            if isinstance(check, dict) and check.get("operation") in fully_evaluated_operations:
                covered.add(item.defect.defect_id)
                continue
            # 缺陷档位比当前档位更浅 → 失败前的门都跑过了 → 覆盖
            if VALIDATION_LEVELS[item.defect.validation_level] < current_rank:
                covered.add(item.defect.defect_id)
        return covered

    def _timing(
        self,
        state: Agent4State,
        segment: str,        # 计时段名，如 "retrieval" / "model_generation"
        started: float,      # perf_counter() 时间戳
        *,
        status: str = "ok",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """记录节点级耗时，便于后续按 run/segment 维度聚合。"""
        self.storage.append_agent4_timing(
            state["project_id"],
            {
                "run_id": state["run_id"],
                "segment": segment,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "status": status,
                "metadata": metadata or {},
            },
        )

    @staticmethod
    def _normalize_audit(audit: SemanticAudit) -> list[Defect]:
        """把审计报告里的 defects 规范化为 ``Defect`` 列表。

        关键操作：
            - 用 ``stable_defect_id`` 把 identity 转成稳定 ID（同一缺陷多次审计应得到同一 ID）；
            - 强制 ``validation_level = "semantic"``；
            - 用 dict 按 defect_id 去重（保留最后出现的）。
        """
        defects: list[Defect] = []
        for report in audit.defects:
            defects.append(
                Defect(
                    defect_id=stable_defect_id(report.identity),
                    identity=report.identity,
                    severity=report.severity,
                    validation_level="semantic",
                    message=report.message,
                    evidence=report.evidence,
                )
            )
        # 用 dict 构造去重（同 defect_id 只保留最后一条）
        return list({item.defect_id: item for item in defects}.values())

    def _decision(
        self,
        state: Agent4State,
        revision: str,
        *,
        target_defect_id: str | None = None,
        model_call_type: str = "none",
        modified_files: list[str] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        progress: bool = False,
        decision: str,   # observed / accepted / rolled_back / stopped
        reason: str,
    ) -> None:
        """把每次关键决策写入 ``AgentDecisionEvent`` 审计日志。

        通常在每个节点的关键分支调用，用于事后追溯"为什么会出现/关闭某个缺陷"
        "为什么候选被接受或回滚"。
        """
        event = AgentDecisionEvent(
            run_id=state["run_id"],
            candidate_revision=revision,
            target_defect_id=target_defect_id,
            model_call_type=model_call_type,
            modified_files=modified_files or [],
            before=before or {},
            after=after or {},
            progress=progress,
            decision=decision,
            reason=reason,
        )
        self.storage.append_agent4_decision(state["project_id"], event.model_dump(mode="json"))

    @staticmethod
    def _route_after_prepare(state: Agent4State) -> str:
        """prepare_documents 之后：判断候选是否已就绪。

        若候选缺少必需字段、或 ``format_contract_id`` 与后端冻结契约不一致，
        就走 ``generate`` 重新生成；否则直接 ``verify``。
        """
        candidate = state.get("candidate", {})
        required = {
            "format_contract_id",
            "generator_code",
            "validator_code",
        }
        # 缺字段 → 必须先生成
        if not required.issubset(candidate):
            return "generate"
        contract_id = (
            state.get("context", {}).get("input_format_contract", {}).get("format_contract_id")
        )
        # 契约对齐 → 直接验证；否则视为旧候选，需要重生成
        return "verify" if candidate.get("format_contract_id") == contract_id else "generate"

    @staticmethod
    def _route_after_verify(state: Agent4State) -> str:
        """verify_candidate 之后：
            - 有 blocker → 选下一个去修（select）；
            - 没 blocker 且还没做语义审计 → 补一次（audit）；
            - 否则直接终审（approve）。
        """
        if any(item.get("severity") == "blocker" for item in state.get("defects", [])):
            return "select"
        if not state.get("semantic_audit_done"):
            return "audit"
        return "approve"

    @staticmethod
    def _route_after_audit(state: Agent4State) -> str:
        """semantic_audit 之后：审计后还有 blocker 就 select，否则 approve。

        注意：审计一旦完成，``semantic_audit_done=True``，本 run 不再回到 audit。
        """
        return (
            "select"
            if any(item.get("severity") == "blocker" for item in state.get("defects", []))
            else "approve"
        )

    @staticmethod
    def _route_after_select(state: Agent4State) -> str:
        """select_defect 之后：被 stop 就结束，否则去 repair。"""
        return "end" if state.get("stopped") else "repair"

    @staticmethod
    def _route_after_repair(state: Agent4State) -> str:
        """repair_defect 之后：被 stop 就结束，否则去 recheck_history 复验。"""
        return "end" if state.get("stopped") else "verify"

    @staticmethod
    def _route_after_progress(state: Agent4State) -> str:
        """evaluate_progress 之后：
            - 停了 → 结束；
            - 仍有 blocker → 再选一个修（select）；
            - 还没审计过 → 补一次语义审计（audit）；
            - 都通过 → 终审（approve）。
        """
        if state.get("stopped"):
            return "end"
        if any(item.get("severity") == "blocker" for item in state.get("defects", [])):
            return "select"
        if not state.get("semantic_audit_done"):
            return "audit"
        return "approve"

    @staticmethod
    def _route_after_approve(state: Agent4State) -> str:
        """approve 之后：通过且需要用户确认就 wait_user，否则直接结束。"""
        return "wait_user" if state.get("complete") and state.get("requires_user") else "end"
