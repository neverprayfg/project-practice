from __future__ import annotations

# ruff: noqa: E501
import hashlib
import json
from collections.abc import Awaitable
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import AppError
from app.models import (
    GeneratorAnalysisDraft,
    GeneratorAuditDraft,
    InputNormalizationDraft,
    StageInstructionDecision,
    SubtaskPlanDraft,
    TestDataPlanDraft,
)
from app.services.agent_recovery import (
    AgentCandidateResult,
    candidate_result,
    candidate_result_from_error,
)

AGENT1_PROMPT = (
    "你是独立的 Agent1，只负责从原始题目中整理输入说明、输出说明和样例，不得推断或补造事实。"
    "同时生成 project_name：根据题目含义取一个简短、易识别的项目名，不超过 40 个字符，不带题号、解法或额外说明。"
    "只输出 InputNormalizationDraft；题目原文、难度、标程源码、编译状态和 revision "
    "均由后端持有，禁止在响应中返回。"
)
SOLUTION_REPAIR_PROMPT = (
    "你只负责修复 solution.cpp 的编译错误。依据 inputs.context 中的题面与输入输出说明、"
    "inputs.source 中的当前源码，以及 inputs.execution 中的真实编译诊断，做最小修改使其通过"
    "C++17 编译。不得改变题意对应的算法和输入输出协议。"
    "响应只能是完整 solution.cpp 的纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
)
AGENT2_PROMPT = """角色定位：你是一位算法竞赛（Competitive Programming）测试数据设计专家。

任务：阅读给出的题目信息，每次都重新生成一份完整的【测试数据设计方案】。不得输出客套话，不得省略或重复任何标签。

只能使用以下 Markdown 模板。三组 XML 风格标签是后端的解析边界，必须原样保留；每组标签必须恰好出现一次，且内容不得为空。

# [题目编号或标题] 测试数据设计方案

<constraints>
## 1. 变量与合规约束 (YAML)
```yaml
metadata:
  problem_id: "[题目编号或标题]"
variables:
  variable_name: { min: 1, max: 100, type: "int" }
```
必须根据题面列出真实输入变量、取值范围、变量间关系和合法性规则，不得沿用示例占位值。
</constraints>

<test-matrix>
## 2. 核心测试点矩阵 (Markdown Table)
| 测试点编号 | 测试目的 | 规模与参数 | 数据结构与分布 | 要过滤的错误或复杂度 |
| :--- | :--- | :--- | :--- | :--- |
| #1 | [边界、合规、算法过滤或鲁棒性目标] | [具体取值] | [具体构造] | [具体错误或复杂度] |
必须根据题目的数据范围和算法风险设计完整矩阵。矩阵中隐含的数据范围层级将用于阶段四推导子任务数量。
</test-matrix>

<blueprint-for-generator>
## 3. 生成器逻辑实现大纲 (Generator Blueprint)
按测试点或共享构造策略，说明随机生成、确定性构造、边界控制和非法情况规避的具体步骤。
1. **测试点 #1 的生成逻辑**：
   - 步骤一：...
   - 步骤二：...
</blueprint-for-generator>"""
RUNTIME_PARAMETER_SCALAR_RULES = (
    "runtime_parameters 中每个 parameter.value 必须是单个 JSON boolean、integer、finite number，"
    "或长度不超过 64 且完全匹配 ^[A-Za-z0-9_.:-]+$ 的字符串。"
    "禁止使用空格、分号、逗号、括号、数组、对象，禁止把边列表、完整测试输入或多个值编码进一个参数。"
    "图结构等复合数据必须改为 construction_mode、n、edge_count、weight_profile 等模式名和数值标量，"
    "由阶段五 generator 根据这些标量构造；不要把具体边集合直接传给 generator。"
    "每个测试点都必须包含名为 construction_mode、category=structure 的字符串参数；"
    "其值必须是能直接映射到一段生成逻辑的稳定 snake_case 模式名，例如 fixed、random_uniform、"
    "solvable_constructed、unsolvable_targeted 或 high_branching，禁止使用 generic、default 等空泛名称。"
    "每个测试点还必须包含名为 variation_budget、category=limit 的整数参数，并在所有 profile 的"
    "parameter_names 中声明：fixed 模式必须取 0，非 fixed 模式必须取正整数，用于明确允许的随机变化预算。"
    "同一 profile 下的多个 fixed 测试点不得具有完全相同的运行参数；必须增加统一 schema 中的"
    "case_variant、目标输入字段值或其他安全标量来区分，否则 generator 无法知道应输出哪个固定点。"
)
GENERATOR_COVERAGE_PRINCIPLES = (
    "测试数据覆盖原则：把每个 generation_profile 的 goal 视为必须落实的覆盖契约，不能只读取参数后原样输出。"
    "对于可由构造保证的语义类别，优先从一个可验证的见证或反向构造出发，并在输出前检查题目全部范围与合法性；"
    "生成器内部应建立轻量级的闭环验证逻辑（如对见证进行实际求值），确保构造产物与输出数据完全等价，避免依赖口头逻辑。"
    "对于相反的语义类别，使用能稳定触发该类别的最小反例或受控扰动，而非仅生成未分类的随机输入。"
    "每个 profile 都应覆盖其相关的最小/最大边界、类别切换点、退化结构与重复或对称等易错形态；"
    "anti_algorithm 还必须包含约束内能放大目标算法时间、空间、分支或候选数量的压力结构，不能只取数值最大。"
    "对于搜索与回溯类问题，压力结构应重点构造“延迟失效”（Late-Failing）形态，通过大量伪合法前缀诱导算法深入搜索树深处，测试剪枝的极限性能。"
    "当方案未指定固定具体值时，所有 runtime 参数和 generation_profile 都必须实际影响构造；"
    "同一 profile 在不同 seed 下应保持该 profile 的语义类别并产生多样实例，且构造过程必须实施严格的溢出保护，避免生成器自身产生数值截断。"
    "若题目存在可判定的成功/失败、存在/不存在等结果类别，应显式同时覆盖这些类别。"
    "测试数据方案和 runtime_parameters 中已明确指定的输入字段值优先级最高：当它们定义固定测试点时，"
    "必须原样构造该值，generation_profile 只能说明覆盖目的，不能将其替换为无关的随机样例。"
    "只有方案明确给出范围、随机化或构造控制时，才可在该控制范围内随机化，并且仍须保留指定的语义类别和压力结构。"
)
AGENT3_PROMPT = (
    "你是独立的 Agent3，只负责重新生成阶段四的完整子任务配置。"
    "必须以 inputs.context.test_data_plan.plan_markdown 中已确认的测试数据生成方案为设计依据，"
    "从该方案的数据范围层级和测试目标中推导子任务数量，可生成任意正整数个子任务；"
    "子任务 id 必须从 1 开始连续排列。每次调用都必须重新生成完整结果，不得直接返回旧草稿。"
    "用 generation_profiles 规划全部测试点，必须同时覆盖 rules_format、anti_algorithm、"
    "boundary_edge 三类目标，profile 的 count 之和必须等于 test_count。"
    "为每个测试点提供 generation_profile_id 和完整 runtime_parameters；同一子任务的所有测试点"
    "必须使用完全一致的参数名称、类别和类型，profile 引用的 parameter_names 必须存在于该 schema。"
    "construction_mode 必须出现在每个 generation_profile 的 parameter_names 中；其余参数应表达"
    "固定值、规模上限、因子预算、密度或分布等构造控制，而不是把完整测试数据编码进字符串。"
    "不得把全部测试点都设为 fixed；每个 rules_format 和 anti_algorithm profile 都必须至少安排"
    "一个非 fixed 模式。rules_format 应包含正向/反向构造或随机合法构造，anti_algorithm 应包含"
    "受控负例或能放大复杂度的压力构造；boundary_edge 可以使用 fixed 精确边界。"
    "不要创建额外约束清单。生成策略、profile 分配和参数必须互相一致，"
    "不能把矛盾留给 Agent4。"
) + RUNTIME_PARAMETER_SCALAR_RULES
AGENT3_REVISE_PROMPT = (
    "你是 Agent3 的契约修订器。inputs.raw_output 是上一响应原文，inputs.candidate 是可解析候选"
    "（可能为空），inputs.validation_errors 是后端给出的 JSON、字段或业务契约精确错误。"
    "inputs.context.recovery_plan 是本次修复的根因阶段、可写工件与保护字段授权；"
    "inputs.context.recovery_evidence 是外层定位器选择的精确证据。"
    "当 runtime_schema_diffs 非空时，必须按 expected_schema 一次性修正其列出的全部"
    "mismatched_cases，参数名、category 和 value_type 必须完全一致，只允许值不同。"
    "当 generation_profile_diffs 非空时，必须对其中每个子任务同步修正全部计数和映射："
    "runtime_parameters 的 case_id 必须恰为该子任务的 1..test_count，不能使用跨子任务的全局编号；"
    "runtime_parameters 条数、generation_profiles 的 count 之和都必须恰为 test_count；"
    "每个 generation_profile_id 的测试点数量必须恰等于对应 profile 的 count。"
    "新增或删除 profile、测试点时，必须在同一份结果中同步更新这些字段，不能只补 profile 或只补测试点。"
    "每个测试点必须保留合法的 construction_mode 构造控制；缺失时根据测试目标补充具体模式，"
    "并同步到所有测试点的统一参数 schema。"
    "若 rules_format 或 anti_algorithm profile 只有 fixed，必须把其中至少一个测试点改为与 goal "
    "一致的非 fixed 可执行模式，并保留足以驱动该模式的标量参数。"
    "不得修改 recovery_plan.protected_fields，不得写入 write_grants 之外的工件。"
    "只修复这些错误，"
    "必须保留候选的子任务数量、顺序和 id，以及已经合法的测试点与参数，"
    "不得新增、删除或重做无关内容。"
    "规模、分布和构造策略必须留在 generation_profiles 或 runtime_parameters，不要创建约束清单。"
    "即使上一响应无法解析，也必须依据原文和错误重建完整 SubtaskPlanDraft。"
    "返回完整 SubtaskPlanDraft，不得解释。"
) + RUNTIME_PARAMETER_SCALAR_RULES
STAGE_INSTRUCTION_PROMPT = (
    "你负责判断用户对当前阶段的一次性指令。若用户只是在提问、要求解释或询问原因，"
    "action=answer、target=none，并直接用中文回答；不得修改当前产物。"
    "若用户明确要求增加、删除、调整、纠正或重写当前阶段产物，action=revise。"
    "阶段三和阶段四的修改 target=current_artifact；阶段五根据要求选择 generator、validator 或 both。"
    "answer 必须简洁说明本次回答，或用完成时说明实施的修改；不要虚构上下文中没有的信息。"
)
AGENT2_INSTRUCTION_PROMPT = (
    "你是 Agent2 的测试数据方案修订器。根据 inputs.user_instruction 修改 "
    "inputs.candidate.plan_markdown；未被要求的内容应保持不变。候选为空时生成完整方案。"
    "输出仍必须是完整 Markdown，并且 <constraints>、<test-matrix>、"
    "<blueprint-for-generator> 三组标签各恰好出现一次且内容非空。"
    "只输出修订后的方案，不得输出解释、JSON 或额外标签。"
)
AGENT2_REVISE_PROMPT = (
    "你是 Agent2 的候选修复器。inputs.raw_output 是上一版原始 Markdown，"
    "inputs.candidate 是可解析候选（可能为空），inputs.validation_errors 是后端给出的精确错误。"
    "保持已经正确的标签段和内容，只修复缺失、重复、为空或验证器明确指出的问题。"
    "输出必须是完整 Markdown，且 <constraints>、<test-matrix>、"
    "<blueprint-for-generator> 三组标签各恰好出现一次并且内容非空。"
    "只输出修复后的 Markdown，不得解释或输出 JSON。"
)
AGENT3_INSTRUCTION_PROMPT = (
    "你是 Agent3 的子任务方案修订器。根据 inputs.user_instruction 修改当前 "
    "inputs.candidate，并返回完整 SubtaskPlanDraft；未被要求的合法内容应保持不变。"
    "修改后仍须满足子任务 ID 连续、三类 generation_profiles 完整覆盖、profile count 总和等于"
    " test_count，以及逐测试点 runtime_parameters 契约。候选为空时依据 context 中已确认的"
    " test_data_plan 生成完整方案。"
) + RUNTIME_PARAMETER_SCALAR_RULES
GENERATOR_ANALYSIS_PROMPT = (
    "你是阶段五之前的独立生成器分析智能体。你可以读取题面、阶段三测试数据方案、阶段四完整子任务"
    "配置与标程源码，但不得生成 C++ 代码。你的任务是把题意限制、标程真实分支和复杂度风险转换为"
    "可供 generator.cpp 实现的完整构造规格。"
    "必须为阶段四中每一个 subtask_id、generation_profile_id、construction_mode 组合恰好生成一条"
    " strategy，不得遗漏、合并或创造未授权模式。profile_category 和 goal 必须与阶段四一致。"
    "runtime_parameters 必须列出该 profile 声明的全部参数及 construction_mode，并解释它们如何参与构造。"
    "input_invariants 要覆盖题面范围、字段关系、合法格式与标程依赖的前置条件；construction_steps 必须是"
    "可直接翻译成生成器分支的有限步骤，优先使用可验证的反向构造、受控负例、边界或退化形态及复杂度压力。"
    "post_checks 必须给出生成器内部可执行的闭环检查，不能只写人工判断。"
    "fixed 模式的 seed_policy 必须是 fixed，其他模式必须是 diverse，并在保持同一语义类别的前提下随 seed 变化。"
    "seed_policy=fixed 时 variation_dimensions 必须为空；seed_policy=diverse 时 variation_dimensions 必须"
    "明确列出随 seed 变化且不破坏语义的见证、因子、结构、扰动或规模维度，不能把固定常量伪装成多样构造。"
    "solution_branch_risks 要从标程源码中提炼分支、剪枝、溢出和复杂度薄弱点，但禁止复制大段标程源码。"
    "overflow_and_resource_guards 要说明生成过程中需要实施的数值和资源保护。只返回完整 GeneratorAnalysisDraft。"
)
GENERATOR_ANALYSIS_REVISE_PROMPT = (
    "你是生成器分析规格修订器。依据 inputs.validation_errors 修复 inputs.candidate；"
    "仍须覆盖 inputs.context 中阶段四的每一个 subtask/profile/construction_mode 组合。"
    "不得删除未报错的合法策略，不得生成 C++，只返回完整 GeneratorAnalysisDraft。"
)
GENERATOR_AUDIT_PROMPT = (
    "你是生成器分析智能体的代码审计环节。inputs.context.generator_analysis 是已经通过后端完整性"
    "检查的构造规格，inputs.generator_code 是待审计的完整 generator.cpp。逐条检查每个 strategy 是否"
    "具有真实且可达的 profile/mode 分支，所有声明参数是否参与构造，input_invariants 与 post_checks 是否"
    "在代码中执行，seed_policy=diverse 是否真正产生多样实例。可验证反向构造必须在代码中保存或重算"
    "见证，不能把普通随机数、平方数或注释当作有解证明；受控负例必须由确定性质保证；复杂度压力必须"
    "构造能放大目标分支、候选或状态数量的数据，不能只随机若干小因子。拒绝随机结果随后被常量覆盖、"
    "代数抵消、空分支、仅注释说明、dummy 随机调用以及 no need to verify 一类跳过闭环验证的实现。"
    "完全落实时 passed=true 且 issues=[]；否则 passed=false，并在 issues 中逐项给出可直接指导修复"
    "generator.cpp 的具体缺陷。issues 最多 8 条，每条应尽量不超过 500 字符，只保留会导致构造语义、溢出、"
    "复杂度压力或模式覆盖失败的真实缺陷，禁止输出自我辩论、可接受事项、逐 profile 复述或长篇解释。"
    "只返回 GeneratorAuditDraft。"
)
AGENT4_GENERATOR_PROMPT = (
    "你是 Agent4 的独立 generator.cpp 生成器。本次响应绝对禁止返回 validator.cpp。"
    "若 inputs.context.user_instruction 非空，生成结果必须同时落实该用户修改意见。"
    "结合题意、inputs.context.test_data_plan.plan_markdown 中已确认的测试数据生成方案，严格参照 inputs.context.library_context JSON 中唯一提供的 "
    "jngen_context 文档与实例生成 generator.cpp。"
    "inputs.context.input_format_contract 是后端冻结的输入格式。"
    "根据题面、input_template、样例和完整文档自行判断数据结构，按所有 policy 向标准输出写出"
    "一个完整测试点。"
    "同一行相邻 token 必须恰好使用一个 ASCII 空格 U+0020；禁止行首空格、行尾空格、Tab、"
    "模板未要求的空行和 CRLF；必须使用 LF 换行且文件末尾必须有一个换行。"
    "标准输出只能包含测试数据，禁止日志或解释。"
    "必须先调用 registerGen(argc, argv) 和 parseArgs(argc, argv)，再通过 "
    'getOpt("参数名") 读取运行时参数；参数名必须与 runtime_parameter_schema 中的 name '
    "逐字一致，禁止 getOpt(0)、getOpt(1) 等位置参数，也禁止自行缩写或改名；读取值必须实际影响构造。"
    "inputs.context.library_context 中唯一提供的 jngen_context.reference 是预构建的完整参考文档，"
    "包含英文 API 文档和示例代码；inputs.context.library_document_manifest 只包含该参考文档；"
    '必须读取 getOpt("generation_profile")，并按 generation_profiles 中的 profile id 实现三类策略。'
    "inputs.context.runtime_parameter_schema 只描述参数名称、类别和值类型，不包含逐测试点实例或"
    "case 到 profile 的分配；"
    "inputs.context.construction_controls 按子任务和 generation_profile 列出允许的 structure 类"
    "构造控制值。必须读取 construction_mode，并为列出的每一个模式实现明确分支；fixed 模式原样落实"
    "固定参数，其他模式必须执行对应的正向构造、受控负例、边界或压力生成逻辑。"
    "禁止用 (void) 丢弃 generation_profile、construction_mode 或其他运行参数，禁止用不影响输出的"
    "随机调用冒充 jngen 使用。"
    "inputs.context.generator_analysis 是专门分析智能体根据题面、阶段四控制和标程提炼出的构造规格；"
    "必须逐条实现其中与当前 subtask、generation_profile 和 construction_mode 对应的 strategy，"
    "落实其 runtime_parameters、input_invariants、construction_steps、post_checks、seed_policy、"
    "variation_dimensions 和 complexity_target。不得用更弱的通用随机或原样输出参数替代该规格。"
    "参数必须实际影响数据构造；文档没有出现的 API 禁止使用。"
    "响应只能是完整 generator.cpp 的纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
) + GENERATOR_COVERAGE_PRINCIPLES
AGENT4_VALIDATOR_PROMPT = (
    "你是 Agent4 的独立 validator.cpp 生成器。本次响应绝对禁止返回 generator.cpp。"
    "若 inputs.context.user_instruction 非空，生成结果必须同时落实与 validator 有关的用户修改意见。"
    "参考 inputs.context.library_context JSON 中 testlib validator 的文档和实例生成校验器。"
    "inputs.context.input_format_contract 是与 generator 并行共享且由后端冻结的输入格式。"
    "根据题面、input_template、样例和完整文档自行判断字段，"
    "严格按 input_template 的顺序读取一个测试点，"
    "不得自行添加、删除或重排字段。必须用 readSpace、readEoln 和 readEof 等 testlib 接口严格"
    "约束格式：同一行相邻 token 恰好一个 ASCII 空格 U+0020，禁止行首/行尾空格、Tab、"
    "模板未要求的空行和 CRLF，要求 LF 换行及文件末尾换行；完成全部约束检查后必须 readEof。"
    "只生成 validator.cpp。"
    "inputs.context.library_context 只提供 validator 角色的 testlib_context，且递归包含 doc 和 "
    "example；validator 不会收到 generation_profiles、运行时参数 schema 或逐测试点实例，"
    "不得依赖这些内容；"
    "不要假设或引用未提供的 jngen 文档。"
    "响应只能是完整 validator.cpp 的纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
)
AGENT4_REPAIR_GENERATOR_PROMPT = (
    "你只负责根据 inputs.execution 中的确定性失败诊断修复当前 generator.cpp。"
    "不得返回或修改 validator.cpp，也不得处理诊断之外的问题。"
    "inputs.context.library_context 是完整 jngen 文档上下文。"
    "必须依据 inputs.context.construction_controls 保留并修复所有 construction_mode 分支；"
    "必须继续满足 inputs.context.generator_analysis 中对应 strategy 的构造步骤、闭环检查、种子策略和"
    "复杂度目标，不能为了通过当前失败而退化成更弱的数据。"
    "运行参数和随机结果必须实际影响输出，禁止 (void) 丢弃或 dummy 随机调用。"
    "只要存在非 fixed 模式，就不能在所有分支中原样输出某个运行参数；必须计算或构造新的输出实例。"
    "同一个非 fixed 测试点会用至少两个 seed 试运行，输出必须随 seed 产生不同但仍满足该模式语义的实例；"
    "禁止 N=N+r-r、相同 if/else 分支或其他代数抵消随机性的写法。"
    "响应只能是修复后的完整 generator.cpp 纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
)
AGENT4_REPAIR_VALIDATOR_PROMPT = (
    "你只负责根据 inputs.execution 中的确定性失败诊断修复当前 validator.cpp。"
    "不得返回或修改 generator.cpp，也不得处理诊断之外的问题。"
    "inputs.context.library_context 是完整 testlib 文档上下文。"
    "响应只能是修复后的完整 validator.cpp 纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
)
AGENT4_INSTRUCTION_GENERATOR_PROMPT = (
    "你只负责按照 inputs.user_instruction 修改 inputs.candidate 中的 generator.cpp。"
    "必须遵守 inputs.context 中冻结的输入格式、测试数据方案、generation profiles、运行参数 schema"
    " 和 jngen 文档；不得修改或返回 validator.cpp。"
    "响应只能是修改后的完整 generator.cpp 纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
)
AGENT4_INSTRUCTION_VALIDATOR_PROMPT = (
    "你只负责按照 inputs.user_instruction 修改 inputs.candidate 中的 validator.cpp。"
    "必须遵守 inputs.context 中冻结的输入格式与 testlib 文档；不得修改或返回 generator.cpp。"
    "响应只能是修改后的完整 validator.cpp 纯 C++ 源码，禁止 JSON、Markdown 代码块、解释和其他文本。"
)
JSON_OUTPUT_PROMPT = (
    "必须只返回一个 JSON（json）object，不得输出 Markdown、解释或 JSON 之外的文本。"
    "JSON 的字段、嵌套结构和类型必须严格符合用户消息中的 response_contract。"
)


def structured_output_controls() -> dict[str, Any]:
    """Disable model reasoning and require JSON for every structured call."""
    return {
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }


T = TypeVar("T", bound=BaseModel)


class AgentModel(Protocol):
    async def classify_stage_instruction(
        self,
        stage: int,
        context: dict[str, Any],
        candidate: dict[str, Any],
        instruction: str,
    ) -> StageInstructionDecision: ...

    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputNormalizationDraft: ...

    async def agent1_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputNormalizationDraft: ...

    async def agent1_normalize_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult: ...

    async def agent1_revise_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult: ...

    async def repair_solution(
        self, context: dict[str, Any], source: str, execution: dict[str, Any]
    ) -> str: ...

    async def agent2_test_data_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> TestDataPlanDraft: ...

    async def agent2_apply_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> TestDataPlanDraft: ...

    async def agent2_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> TestDataPlanDraft: ...

    async def agent2_test_data_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult: ...

    async def agent2_revise_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult: ...

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft: ...

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft: ...

    async def agent3_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult: ...

    async def agent3_revise_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult: ...

    async def agent3_apply_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> SubtaskPlanDraft: ...

    async def agent4_analyze_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GeneratorAnalysisDraft: ...

    async def agent4_revise_generator_analysis(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        validation_errors: list[str],
    ) -> GeneratorAnalysisDraft: ...

    async def agent4_audit_generator(
        self,
        context: dict[str, Any],
        generator_code: str,
    ) -> GeneratorAuditDraft: ...

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str: ...

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str: ...

    async def agent4_repair_generator(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> str: ...

    async def agent4_repair_validator(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> str: ...

    async def agent4_apply_generator_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> str: ...

    async def agent4_apply_validator_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> str: ...


class OpenAICompatibleAgentModel:
    """One shared HTTP client with explicit, agent-specific operations."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def classify_stage_instruction(
        self,
        stage: int,
        context: dict[str, Any],
        candidate: dict[str, Any],
        instruction: str,
    ) -> StageInstructionDecision:
        return await self._call(
            "stage.instruction",
            STAGE_INSTRUCTION_PROMPT,
            StageInstructionDecision,
            {
                "stage": stage,
                "context": context,
                "candidate": _candidate_for_instruction(stage, candidate),
                "user_instruction": instruction,
            },
            temperature=0.5,
        )

    async def agent1_normalize_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult:
        return await self._recoverable_structured(
            self.agent1_normalize(context, candidate)
        )

    async def agent1_revise_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult:
        return await self._recoverable_structured(self.agent1_revise(context, candidate))

    async def agent1_normalize(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputNormalizationDraft:
        return await self._call(
            "agent1.normalize",
            AGENT1_PROMPT,
            InputNormalizationDraft,
            {"context": context, "candidate": candidate},
        )

    async def agent1_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> InputNormalizationDraft:
        return await self._call(
            "agent1.revise",
            AGENT1_PROMPT
            + " 仅修复 inputs.validation_errors 指出的规范化字段；不得改动权威题面、难度或标程。",
            InputNormalizationDraft,
            {
                "context": context,
                "candidate": candidate,
                "raw_output": context.get("raw_output", ""),
                "validation_errors": context.get("validation_errors", []),
            },
        )

    async def repair_solution(
        self, context: dict[str, Any], source: str, execution: dict[str, Any]
    ) -> str:
        return await self._call_text(
            "solution.repair_compile",
            SOLUTION_REPAIR_PROMPT,
            {
                "context": context,
                "source": source,
                "execution": execution,
            },
            output_instructions="只输出修复后的完整 solution.cpp 纯 C++ 源码。",
        )

    async def agent2_test_data_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> TestDataPlanDraft:
        markdown = await self._call_text(
            "agent2.test_data_plan",
            AGENT2_PROMPT,
            {"context": context, "candidate": candidate},
            output_instructions="只输出系统提示词规定的 Markdown 测试数据生成方案。",
            temperature=0.5,
        )
        return TestDataPlanDraft(plan_markdown=markdown)

    async def agent2_apply_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> TestDataPlanDraft:
        markdown = await self._call_text(
            "agent2.apply_instruction",
            AGENT2_INSTRUCTION_PROMPT,
            {
                "context": context,
                "candidate": candidate,
                "user_instruction": instruction,
            },
            output_instructions="只输出修订后的完整 Markdown 测试数据方案。",
            temperature=0.5,
        )
        return TestDataPlanDraft(plan_markdown=markdown)

    async def agent2_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> TestDataPlanDraft:
        markdown = await self._call_text(
            "agent2.revise",
            AGENT2_REVISE_PROMPT,
            {
                "context": context,
                "candidate": candidate,
                "raw_output": context.get("raw_output", ""),
                "validation_errors": context.get("validation_errors", []),
            },
            output_instructions="只输出修复后的完整 Markdown 测试数据方案。",
            temperature=0.5,
        )
        return TestDataPlanDraft(plan_markdown=markdown)

    async def agent2_test_data_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult:
        result = await self.agent2_test_data_plan(context, candidate)
        return AgentCandidateResult(
            candidate=result.model_dump(mode="json", exclude={"issues"}),
            raw_output=result.plan_markdown,
        )

    async def agent2_revise_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult:
        result = await self.agent2_revise(context, candidate)
        return AgentCandidateResult(
            candidate=result.model_dump(mode="json", exclude={"issues"}),
            raw_output=result.plan_markdown,
        )

    async def agent3_plan(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        return await self._call(
            "agent3.plan",
            AGENT3_PROMPT,
            SubtaskPlanDraft,
            {"context": context, "candidate": candidate},
            temperature=0.5,
        )

    async def agent3_revise(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> SubtaskPlanDraft:
        return await self._call(
            "agent3.revise",
            AGENT3_REVISE_PROMPT,
            SubtaskPlanDraft,
            {
                "context": context,
                "candidate": candidate,
                "raw_output": context.get("raw_output", ""),
                "validation_errors": context.get("validation_errors", []),
            },
            temperature=0.5,
        )

    async def agent3_plan_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult:
        return await self._recoverable_structured(self.agent3_plan(context, candidate))

    async def agent3_revise_result(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> AgentCandidateResult:
        return await self._recoverable_structured(self.agent3_revise(context, candidate))

    async def agent3_apply_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> SubtaskPlanDraft:
        return await self._call(
            "agent3.apply_instruction",
            AGENT3_INSTRUCTION_PROMPT,
            SubtaskPlanDraft,
            {
                "context": context,
                "candidate": candidate,
                "user_instruction": instruction,
            },
            temperature=0.5,
        )

    async def agent4_analyze_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> GeneratorAnalysisDraft:
        return await self._call(
            "agent4.analyze_generator",
            GENERATOR_ANALYSIS_PROMPT,
            GeneratorAnalysisDraft,
            {"context": context, "candidate": candidate},
            temperature=0.5,
        )

    async def agent4_revise_generator_analysis(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        validation_errors: list[str],
    ) -> GeneratorAnalysisDraft:
        return await self._call(
            "agent4.revise_generator_analysis",
            GENERATOR_ANALYSIS_REVISE_PROMPT,
            GeneratorAnalysisDraft,
            {
                "context": context,
                "candidate": candidate,
                "validation_errors": validation_errors,
            },
            temperature=0.5,
        )

    async def agent4_audit_generator(
        self,
        context: dict[str, Any],
        generator_code: str,
    ) -> GeneratorAuditDraft:
        inputs = {
            "context": context,
            "generator_code": generator_code,
        }
        try:
            return await self._call(
                "agent4.audit_generator",
                GENERATOR_AUDIT_PROMPT,
                GeneratorAuditDraft,
                inputs,
                temperature=0.5,
            )
        except AppError as exc:
            details = exc.details if isinstance(exc.details, dict) else {}
            if exc.code != "MODEL_FAILED" or details.get("failure_kind") not in {
                "json_syntax",
                "schema_validation",
            }:
                raise
            return await self._call(
                "agent4.audit_generator.revise_contract",
                GENERATOR_AUDIT_PROMPT
                + " 上一响应未通过 JSON 契约；本次必须压缩并重新输出合法 JSON。",
                GeneratorAuditDraft,
                {
                    **inputs,
                    "previous_validation_errors": details.get("validation_errors", []),
                },
                temperature=0.5,
            )

    async def agent4_generate_generator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        return await self._call_text(
            "agent4.generate_generator",
            AGENT4_GENERATOR_PROMPT,
            {"context": context, "candidate": _candidate_for_model(candidate)},
            output_instructions="只输出完整 generator.cpp 的纯 C++ 源码。",
        )

    async def agent4_generate_validator(
        self, context: dict[str, Any], candidate: dict[str, Any]
    ) -> str:
        return await self._call_text(
            "agent4.generate_validator",
            AGENT4_VALIDATOR_PROMPT,
            {"context": context, "candidate": _candidate_for_model(candidate)},
            output_instructions="只输出完整 validator.cpp 的纯 C++ 源码。",
        )

    async def agent4_repair_generator(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> str:
        return await self._call_text(
            "agent4.repair_generator",
            AGENT4_REPAIR_GENERATOR_PROMPT,
            {
                "context": context,
                "candidate": _candidate_for_model(candidate),
                "execution": _execution_for_model(execution),
            },
            output_instructions="只输出修复后的完整 generator.cpp 纯 C++ 源码。",
        )

    async def agent4_repair_validator(
        self,
        context: dict[str, Any],
        candidate: dict[str, Any],
        execution: dict[str, Any],
    ) -> str:
        return await self._call_text(
            "agent4.repair_validator",
            AGENT4_REPAIR_VALIDATOR_PROMPT,
            {
                "context": context,
                "candidate": _candidate_for_model(candidate),
                "execution": _execution_for_model(execution),
            },
            output_instructions="只输出修复后的完整 validator.cpp 纯 C++ 源码。",
        )

    async def agent4_apply_generator_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> str:
        return await self._call_text(
            "agent4.apply_generator_instruction",
            AGENT4_INSTRUCTION_GENERATOR_PROMPT,
            {
                "context": context,
                "candidate": _candidate_for_model(candidate),
                "user_instruction": instruction,
            },
            output_instructions="只输出修改后的完整 generator.cpp 纯 C++ 源码。",
        )

    async def agent4_apply_validator_instruction(
        self, context: dict[str, Any], candidate: dict[str, Any], instruction: str
    ) -> str:
        return await self._call_text(
            "agent4.apply_validator_instruction",
            AGENT4_INSTRUCTION_VALIDATOR_PROMPT,
            {
                "context": context,
                "candidate": _candidate_for_model(candidate),
                "user_instruction": instruction,
            },
            output_instructions="只输出修改后的完整 validator.cpp 纯 C++ 源码。",
        )

    @staticmethod
    async def _recoverable_structured(call: Awaitable[BaseModel]) -> AgentCandidateResult:
        try:
            value = await call
        except AppError as exc:
            recovered = candidate_result_from_error(exc)
            if recovered is None:
                raise
            return recovered
        return candidate_result(value.model_dump(mode="json", exclude={"issues"}))

    async def _call_text(
        self,
        operation: str,
        system_prompt: str,
        inputs: dict[str, Any],
        *,
        output_instructions: str,
        temperature: float = 0,
    ) -> str:
        """Call agents whose response is plain text rather than structured JSON."""
        if not self.settings.model_api_key:
            raise AppError(
                "MODEL_NOT_CONFIGURED",
                "MODEL_API_KEY is not configured",
                status_code=503,
            )
        payload = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "format_version": 1,
                            "operation": operation,
                            "inputs": _json_safe(inputs),
                            "output_instructions": output_instructions,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": temperature,
            "max_tokens": self.settings.model_max_output_tokens,
        }
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            response = await self._client.post(
                f"{self.settings.model_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except httpx.HTTPError as exc:
            raise AppError(
                "MODEL_FAILED",
                "模型服务请求失败，请检查服务地址、网络和模型状态。",
                status_code=502,
                details={"operation": operation, **_http_error_details(exc)},
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(
                "MODEL_FAILED",
                "模型没有返回可读取的文本结果。",
                status_code=502,
                details={"operation": operation, "error_type": type(exc).__name__},
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise AppError(
                "MODEL_FAILED",
                "模型没有返回文本结果。",
                status_code=502,
                details={"operation": operation},
            )
        return content.strip()

    async def _call(
        self,
        operation: str,
        system_prompt: str,
        response_model: type[T],
        inputs: dict[str, Any],
        *,
        temperature: float = 0,
    ) -> T:
        if not self.settings.model_api_key:
            raise AppError(
                "MODEL_NOT_CONFIGURED",
                "MODEL_API_KEY is not configured",
                status_code=503,
            )
        request = {
            "format_version": 2,
            "operation": operation,
            "inputs": _json_safe(inputs),
            "response_contract": response_model.model_json_schema(),
            "output_instructions": (
                "仅输出符合 response_contract 的 JSON object；"
                '例如对象必须使用 {"字段名": "符合契约的值"} 这种 JSON 形式。'
            ),
        }
        payload = {
            "model": self.settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n" + JSON_OUTPUT_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "temperature": temperature,
            "max_tokens": self.settings.model_max_output_tokens,
            **structured_output_controls(),
        }
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        attempt_failures: list[dict[str, Any]] = []
        for attempt in range(2):
            content: Any = None
            response_metadata: dict[str, Any] = {}
            try:
                response = await self._client.post(
                    f"{self.settings.model_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                usage = body.get("usage") if isinstance(body, dict) else None
                response_metadata = {
                    "finish_reason": choice.get("finish_reason")
                    if isinstance(choice, dict)
                    else None,
                    "usage": {
                        key: usage.get(key)
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if usage.get(key) is not None
                    }
                    if isinstance(usage, dict)
                    else {},
                    "reasoning_content_present": bool(message.get("reasoning_content"))
                    if isinstance(message, dict)
                    else False,
                }
                content = message["content"]
                return response_model.model_validate(_parse_json(content))
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                failure = {
                    "operation": operation,
                    "attempt": attempt + 1,
                    "kind": "transport",
                    **_http_error_details(exc),
                }
                attempt_failures.append(failure)
                if attempt == 0 and (
                    status in {408, 429, 500, 502, 503, 504} or isinstance(exc, httpx.RequestError)
                ):
                    continue
                raise AppError(
                    "MODEL_FAILED",
                    "模型服务请求失败，请检查服务地址、网络和模型状态。",
                    status_code=502,
                    details={
                        "operation": operation,
                        **_http_error_details(exc),
                        "attempts": attempt_failures,
                    },
                ) from exc
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                failure = _contract_failure_details(
                    operation,
                    attempt + 1,
                    exc,
                    content,
                    response_metadata,
                )
                attempt_failures.append(failure)
                truncated = response_metadata.get("finish_reason") == "length"
                runtime_scalar_failure = _is_agent3_runtime_scalar_failure(
                    operation,
                    failure["errors"],
                )
                raise AppError(
                    "MODEL_RESPONSE_TRUNCATED" if truncated else "MODEL_FAILED",
                    (
                        "模型输出达到 token 上限，未形成可验证的最终 JSON。"
                        if truncated
                        else (
                            "阶段四运行时参数必须是安全标量，模型返回了边列表或其他复合值。"
                            if runtime_scalar_failure
                            else "模型返回的 JSON 未通过响应契约校验。"
                        )
                    ),
                    status_code=502,
                    details={
                        "operation": operation,
                        "max_tokens": self.settings.model_max_output_tokens,
                        "failure_kind": failure["kind"],
                        "raw_output": content if isinstance(content, str) else "",
                        "candidate": _parse_partial_json(content),
                        "validation_errors": failure["errors"],
                        "response_metadata": response_metadata,
                    },
                ) from exc
        raise AppError(
            "MODEL_FAILED",
            "模型服务请求失败，请检查服务地址、网络和模型状态。",
            status_code=502,
            details={"operation": operation, "failure_kind": "transport", "attempts": attempt_failures},
        )


def _is_agent3_runtime_scalar_failure(
    operation: str, errors: list[dict[str, Any]]
) -> bool:
    return operation.startswith("agent3.") and any(
        "runtime_parameters" in error.get("location", [])
        and "value" in error.get("location", [])
        for error in errors
    )


def _http_error_details(exc: httpx.HTTPError) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    details: dict[str, Any] = {
        "http_status": getattr(response, "status_code", None),
        "error_type": type(exc).__name__,
    }
    if response is None:
        details["provider_message"] = str(exc)[:500]
        return details
    try:
        body = response.json()
    except (ValueError, TypeError):
        text = response.text.strip()
        if text:
            details["provider_message"] = text[:500]
        return details
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        for source, target in (
            ("code", "provider_code"),
            ("type", "provider_type"),
            ("message", "provider_message"),
            ("param", "provider_param"),
        ):
            value = error.get(source)
            if value is not None:
                details[target] = str(value)[:500]
    elif error is not None:
        details["provider_message"] = str(error)[:500]
    return details


def _candidate_for_model(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "generator_code",
            "validator_code",
            "format_contract_id",
        )
        if key in candidate
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _candidate_for_instruction(stage: int, candidate: dict[str, Any]) -> dict[str, Any]:
    if stage == 5:
        return _candidate_for_model(candidate)
    return {key: value for key, value in candidate.items() if key != "issues"}


def _execution_for_model(execution: dict[str, Any]) -> dict[str, Any]:
    gate_counts: dict[str, int] = {}
    failed_checks: list[dict[str, Any]] = []
    for check in execution.get("checks", []):
        if not isinstance(check, dict):
            continue
        operation = str(check.get("operation") or "unknown")
        gate_counts[operation] = gate_counts.get(operation, 0) + 1
        result = check.get("result")
        failed = check.get("ok") is False or (
            isinstance(result, dict) and result.get("ok") is False
        )
        if not failed:
            continue
        evidence = {
            key: value
            for key, value in check.items()
            if key
            in {
                "operation",
                "level",
                "role",
                "constraint_id",
                "target_file",
                "subtask_id",
                "case_id",
                "seed",
                "runtime_arguments",
                "error_code",
                "issues",
                "diagnostics",
            }
        }
        if isinstance(result, dict):
            evidence["result"] = {
                key: (
                    value[:2000]
                    if key in {"stdout", "stderr"} and isinstance(value, str)
                    else value
                )
                for key, value in result.items()
                if key in {"ok", "exit_code", "timed_out", "stdout", "stderr"}
            }
        failed_checks.append(evidence)
    return {
        "ok": execution.get("ok", False),
        "failure_category": execution.get("failure_category"),
        "validation_level": execution.get("validation_level"),
        "message": execution.get("message", ""),
        "gate_counts": gate_counts,
        "failed_checks": failed_checks,
    }


def _parse_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise TypeError("model output content must be a string")
    text = value.strip()
    if not text:
        raise ValueError("model output content is empty")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("model output must be a JSON object")
    return parsed


def _parse_partial_json(value: Any) -> dict[str, Any] | None:
    try:
        return _parse_json(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _contract_failure_details(
    operation: str,
    attempt: int,
    error: Exception,
    content: Any,
    response_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(error, ValidationError):
        kind = "schema_validation"
        errors = [
            {
                "type": item.get("type"),
                "location": [str(part) for part in item.get("loc", ())],
                "message": item.get("msg"),
                "input": _bounded_contract_value(item.get("input")),
            }
            for item in error.errors(include_url=False, include_context=False)[:20]
        ]
    elif isinstance(error, json.JSONDecodeError):
        kind = "json_syntax"
        errors = [
            {
                "type": "json_decode_error",
                "location": [str(error.lineno), str(error.colno)],
                "message": error.msg,
            }
        ]
    else:
        kind = "response_envelope" if isinstance(error, (KeyError, TypeError)) else "json_contract"
        errors = [{"type": type(error).__name__, "location": [], "message": str(error)[:500]}]
    raw = content if isinstance(content, str) else ""
    return {
        "operation": operation,
        "attempt": attempt,
        "kind": kind,
        "errors": errors,
        "response_chars": len(raw),
        "response_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None,
        "response": response_metadata or {},
    }


def _bounded_contract_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
        return value if len(text) <= 160 else text[:157] + "..."
    if isinstance(value, list):
        return [_bounded_contract_value(item) for item in value[:5]]
    if isinstance(value, dict):
        return {str(key): _bounded_contract_value(item) for key, item in list(value.items())[:8]}
    return str(value)[:160]
