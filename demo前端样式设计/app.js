const STAGES = [
  "创建项目",
  "编译标程",
  "确认输入结构",
  "规划子任务",
  "编写代码草稿",
  "批量生成数据",
  "验证与输出",
  "导出数据包",
];

const COMPLEXITIES = ["O(1)", "O(log n)", "O(n)", "O(n log n)", "O(n²)", "O(n³)"];
const API_BASE = window.location.port === "8000" ? "" : "http://localhost:8000";

const state = {
  projectId: localStorage.getItem("testforge.projectId"),
  project: null,
  drafts: { "3": null, "4": null, "5": null },
  draftBaselines: { "3": null, "4": null, "5": null },
  solutionCode: "",
  solutionBaseline: "",
  dirtySolution: false,
  activeStage: 1,
  apiOnline: false,
  apiMode: "",
  modelConfig: null,
  tagCatalog: { version: null, tags: [] },
  modelSettingsBusy: false,
  busy: false,
  busyLabel: "",
  error: null,
  compileResult: null,
  preview: null,
  previewHistory: [],
  buildResult: null,
  expandedSubtasks: new Set([0]),
  dirtyStages: new Set(),
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function cloneJson(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function draftContent(stage, draft) {
  if (stage === 3) {
    return {
      template: String(draft?.template || "").trim(),
      structure_tags: Array.isArray(draft?.structure_tags) ? cloneJson(draft.structure_tags) : [],
    };
  }
  if (stage === 4) {
    return {
      subtasks: Array.isArray(draft?.subtasks) ? draft.subtasks.map((subtask) => ({
        id: Number(subtask.id),
        constraints: String(subtask.constraints || "").trim(),
        test_count: Number(subtask.test_count || 0),
        expected_complexity: String(subtask.expected_complexity || "").trim(),
        special_cases: Array.isArray(subtask.special_cases) ? subtask.special_cases.map((item) => ({
          count: Number(item.count || 0),
          description: String(item.description || "").trim(),
        })) : [],
        runtime_parameters: Array.isArray(subtask.runtime_parameters)
          ? cloneJson(subtask.runtime_parameters)
          : [],
        subtask_tags: Array.isArray(subtask.subtask_tags) ? [...subtask.subtask_tags] : [],
      })) : [],
    };
  }
  return {
    generator_code: String(draft?.generator_code || "").trim(),
    validator_code: String(draft?.validator_code || "").trim(),
    constraint_coverage: Array.isArray(draft?.constraint_coverage)
      ? cloneJson(draft.constraint_coverage)
      : [],
  };
}

function draftContentChanged(stage, draft) {
  return JSON.stringify(draftContent(stage, draft)) !== JSON.stringify(draftContent(stage, state.draftBaselines[String(stage)]));
}

function updateDraftDirtyState(stage, draft) {
  if (draftContentChanged(stage, draft)) state.dirtyStages.add(stage);
  else state.dirtyStages.delete(stage);
  return state.dirtyStages.has(stage);
}

function icon(name, className = "") {
  return `<i data-lucide="${name}"${className ? ` class="${className}"` : ""}></i>`;
}

function hydrateIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
}

function showToast(message, type = "success") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show${type === "error" ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.className = "toast";
  }, 3200);
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(body?.message || `请求失败（HTTP ${response.status}）`);
    error.payload = body;
    error.status = response.status;
    throw error;
  }
  return body;
}

function errorMessage(error) {
  const map = {
    PROJECT_BUSY: "项目正在执行另一项任务，请稍后重试。",
    COMPILE_FAILED: "代码编译失败，请查看日志并修改代码。",
    INVALID_DRAFT: "草稿格式校验未通过，请查看下方错误详情。",
    INCOMPLETE_AI_DRAFT: "AI 未生成输入结构模板，当前没有可供修改的完整草稿。",
    INVALID_SUBTASK_RANGES: "子任务的数据范围字段与已确认的输入结构不一致。",
    INVALID_SUBTASK: "所选子任务不存在，请刷新项目后重新选择。",
    CONFIRMATION_REQUIRED: "需要先通过 AI 检查，才能由用户确认。",
    CONFIRMATION_PENDING: "当前候选正在等待确认；请先确认或保存修改后重新运行。",
    PREREQUISITE_REQUIRED: "前置阶段尚未通过，暂时不能执行此操作。",
    WORKFLOW_FAILED: "AI 工作流调用失败或返回格式不符合约定。",
    GENERATION_FAILED: "生成器运行失败，请返回阶段 5 修改代码。",
    VALIDATION_FAILED: "校验器拒绝了生成数据，请返回阶段 5 核验代码。",
    SOLUTION_FAILED: "标程运行异常，请检查失败详情后重试。",
    EXPORT_NOT_READY: "数据尚未完成生成与校验，暂时不能导出。",
    MODEL_NOT_CONFIGURED: "请先填写 API Key，再测试或运行真实模型。",
    MODEL_CONNECTION_FAILED: "模型连接失败，请检查 API 地址、模型名称和密钥。",
    MODEL_CONFIG_BUSY: "当前有任务正在运行，请等待任务完成后再修改模型配置。",
    MODEL_CONFIG_INVALID: "已保存的模型配置无效，请检查后端配置文件。",
    STAGE_INTERRUPTED: "上一次 AI 检查未正常结束，已可重新运行。",
    JNGEN_DOCUMENT_SELECTION_INCOMPLETE: "文档检索未明确结束，请重新运行 Agent4。",
    JNGEN_DOCUMENT_SELECTION_INVALID: "文档选择包含无效或重复文件，请重新运行 Agent4。",
    JNGEN_DOCUMENT_CONTEXT_TOO_LARGE: "所选文档过多，请重新运行 Agent4 进行更精简的选择。",
    STRUCTURE_TAG_REVIEW_REQUIRED: "阶段 3 结构标签尚未确认或存在冲突，请返回复核。",
    STRUCTURE_TAG_DOCUMENT_BUDGET_EXCEEDED: "标签所需 jngen 文档超过上下文预算，请复核标签或调整预算。",
    INVALID_STRUCTURE_TAGS: "阶段 3 结构标签不在目录中、不受支持或存在冲突。",
    INVALID_SUBTASK_TAGS: "子任务结构细化标签无效或与已确认标签冲突。",
    STALE_STAGE: "该阶段已不是当前活动阶段，请刷新项目状态。",
    INVALID_REQUEST: "配置内容不符合要求，请检查必填项和字段格式。",
  };
  return map[error?.payload?.code] || error?.message || "操作失败，请重试。";
}

function displayedError() {
  if (state.error) return state.error;
  if (state.busy) return null;
  const lastError = state.project?.last_error;
  if (!lastError || Number(lastError.stage) !== state.activeStage) return null;
  return { message: lastError.message, payload: lastError };
}

function validationEntries(error = displayedError()) {
  const raw = error?.payload?.details;
  const details = Array.isArray(raw) ? raw : Array.isArray(raw?.validation_errors) ? raw.validation_errors : [];
  return details.map((detail) => {
    const loc = (detail.loc || []).filter((part) => !["body", "draft"].includes(String(part)));
    const label = loc[0] === "template"
      ? "输入结构模板"
      : loc.length ? loc.join(" → ") : "草稿";
    let message = detail.msg || "内容不符合要求";
    if (loc[0] === "template" && detail.type === "missing") message = "AI 返回结果中缺少输入结构模板。";
    else if (detail.type === "missing") message = "此项为必填项。";
    else if (["string_too_short", "too_short"].includes(detail.type)) message = "内容为空或数量不足。";
    return { loc, label, message };
  });
}

async function runMutation(label, operation, successMessage = "操作完成") {
  if (state.busy) return;
  state.busy = true;
  state.busyLabel = label;
  state.error = null;
  render();
  try {
    await operation();
    showToast(successMessage);
  } catch (error) {
    state.error = error;
    showToast(errorMessage(error), "error");
    if (state.projectId) await refreshProject({ silent: true, keepError: true });
    const recovery = Number(error?.payload?.details?.recovery_stage || error?.payload?.stage);
    if ([4, 5, 7].includes(recovery)) state.activeStage = recovery;
  } finally {
    state.busy = false;
    state.busyLabel = "";
    render();
  }
}

function projectName() {
  if (!state.project) return "尚未创建项目";
  const firstLine = state.project.problem_description
    .split("\n")
    .map((line) => line.replace(/^#+\s*/, "").trim())
    .find(Boolean);
  if (!firstLine) return `项目 ${state.project.project_id.slice(0, 8)}`;
  return firstLine.length > 28 ? `${firstLine.slice(0, 28)}…` : firstLine;
}

function stageState(stage) {
  if (!state.project) return stage === 1 ? "active" : "locked";
  if (stage === 1) return "complete";
  if (stage === 2) return state.project.solution_compiled ? "complete" : "active";
  if (stage >= 3 && stage <= 5) {
    const status = state.project.stages?.[String(stage)]?.status;
    if (status === "passed") return "complete";
    return Number(state.project.current_stage) === stage ? "active" : "locked";
  }
  if (stage === 6) return state.project.generation_complete ? "complete" : Number(state.project.current_stage) === 6 ? "active" : "locked";
  if (stage === 7) return state.project.build_complete ? "complete" : Number(state.project.current_stage) === 7 ? "active" : "locked";
  if (stage === 8) return state.project.export_ready ? "active" : "locked";
  return "locked";
}

function canOpenStage(stage) {
  if (stage === 1) return true;
  if (!state.project) return false;
  if (stage <= Number(state.project.current_stage)) return true;
  return state.project.build_complete && stage <= 8;
}

function renderNav() {
  $$(".stage-link").forEach((button) => {
    const stage = Number(button.dataset.stage);
    const progress = stageState(stage);
    button.classList.toggle("active", stage === state.activeStage);
    button.classList.toggle("complete", progress === "complete");
    button.classList.toggle("locked", !canOpenStage(stage));
    const stateIcon = $(".stage-state", button);
    stateIcon.setAttribute(
      "data-lucide",
      progress === "complete" ? "check-circle-2" : progress === "active" ? "circle-dot" : "lock-keyhole",
    );
  });

  const select = $("#stageSelect");
  select.innerHTML = STAGES.map((name, index) => {
    const stage = index + 1;
    return `<option value="${stage}" ${stage === state.activeStage ? "selected" : ""} ${canOpenStage(stage) ? "" : "disabled"}>阶段 ${stage} · ${name}</option>`;
  }).join("");
}

function renderConnection() {
  const sidebarDot = $("#sidebarConnectionDot");
  const sidebarText = $("#sidebarConnectionText");
  const apiStatus = $("#apiStatus");
  sidebarDot.className = `connection-dot ${state.apiOnline ? "online" : "offline"}`;
  const modelLabel = state.modelConfig?.model_name || state.apiMode;
  sidebarText.textContent = state.apiOnline ? `后端已连接 · ${modelLabel}` : "后端未连接";
  apiStatus.innerHTML = `<span class="connection-dot ${state.apiOnline ? "online" : "offline"}"></span><span>${state.apiOnline ? "API 已连接" : "API 未连接"}</span>`;
  $("#modelSettingsBtn").disabled = !state.apiOnline;
}

function errorNotice() {
  const error = displayedError();
  if (!error) return "";
  const detail = error?.payload?.details;
  const detailText = typeof detail === "string" ? detail : detail?.stderr || "";
  const entries = validationEntries(error);
  const missingTemplate = entries.some((entry) => entry.loc[0] === "template");
  return `<div class="notice error"><span>${icon("circle-alert")}<span><strong>${escapeHtml(errorMessage(error))}</strong>${detailText ? `<br><small>${escapeHtml(String(detailText).slice(0, 600))}</small>` : ""}${entries.length ? `<ul class="validation-error-list">${entries.map((entry) => `<li><strong>${escapeHtml(entry.label)}：</strong>${escapeHtml(entry.message)}</li>`).join("")}</ul>` : ""}${missingTemplate ? `<small class="error-recommendation">请重新运行 AI 分析；也可以先在输入结构文本框中补充内容，再交由 AI 检查。</small>` : ""}</span></span></div>`;
}

function applyValidationMarkers() {
  const entries = validationEntries();
  for (const entry of entries) {
    if (entry.loc[0] !== "template") continue;
    const control = $("#inputStructureTemplate");
    control?.classList.add("invalid");
    control?.setAttribute("aria-invalid", "true");
    control?.setAttribute("title", entry.message);
  }
}

function render() {
  renderNav();
  renderConnection();
  $("#projectLabel").textContent = projectName();
  $("#stageLabel").textContent = `阶段 ${state.activeStage} · ${STAGES[state.activeStage - 1]}`;
  $("#pageTitle").textContent = pageTitle(state.activeStage);
  const renderer = STAGE_RENDERERS[state.activeStage] || renderStage1;
  $("#stageContent").innerHTML = `${errorNotice()}${renderer()}`;
  bindStage(state.activeStage);
  applyValidationMarkers();
  hydrateIcons();
}

function pageTitle(stage) {
  return [
    "创建数据项目",
    "标程编译检查",
    "输入结构确认",
    "子任务与测试点规划",
    "生成器与校验器草稿",
    "批量生成输入数据",
    "数据校验与标准输出",
    "导出最终数据包",
  ][stage - 1];
}

function busyButton(label = "处理中") {
  return `${icon("loader-circle", "spinner")}<span>${escapeHtml(state.busyLabel || label)}</span>`;
}

function renderStage1() {
  if (state.project) {
    return `<div class="page-intro"><div><h2>项目输入</h2><p>项目已创建。题目描述与难度仅用于当前项目，其中难度不会发送给 AI。</p></div><span class="badge success">${icon("check")}已保存</span></div>
      <section class="work-surface"><div class="surface-body form-stack">
        <label class="field"><span>题目描述</span><textarea class="text-area problem" disabled>${escapeHtml(state.project.problem_description)}</textarea></label>
        <div class="form-grid"><label class="field"><span>难度等级</span><input class="text-input" value="${escapeHtml(state.project.difficulty)}" disabled></label><label class="field"><span>项目编号</span><input class="text-input" value="${escapeHtml(state.project.project_id)}" disabled></label></div>
      </div></section>
      <div class="action-bar"><span class="save-state">${icon("check-circle-2")}阶段 1 已完成</span><div class="form-actions"><button class="button button-primary" id="goCurrentBtn" type="button">前往当前阶段${icon("arrow-right")}</button></div></div>`;
  }

  return `<div class="page-intro"><div><h2>录入题目与标准程序</h2><p>三项均为必填。题目描述可粘贴或上传文本，C++ 标程可直接编辑或上传 .cpp 文件。</p></div></div>
    <section class="work-surface"><div class="surface-body form-stack">
      <label class="field"><span class="required">题目描述</span><textarea id="problemDescription" class="text-area problem" placeholder="填写题意、输入格式、输出格式与必要说明"></textarea><div class="upload-actions"><input class="sr-only" id="problemFile" type="file" accept=".md,.txt,text/plain,text/markdown"><button class="button button-secondary" data-upload="problemFile" type="button">${icon("upload")}上传文本文件</button><span class="file-name" id="problemFileName">支持 .md / .txt</span></div></label>
      <label class="field"><span class="required">C++ 标程代码</span><textarea id="solutionCode" class="code-editor" spellcheck="false" placeholder="#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n  return 0;\n}"></textarea><div class="upload-actions"><input class="sr-only" id="solutionFile" type="file" accept=".cpp,.cc,.cxx,text/x-c++src"><button class="button button-secondary" data-upload="solutionFile" type="button">${icon("upload")}上传 .cpp 文件</button><span class="file-name" id="solutionFileName">代码将在 Docker 中编译</span></div></label>
      <label class="field"><span class="required">难度等级</span><select id="difficulty" class="select-input"><option value="">请选择</option><option>入门</option><option>普及-</option><option>普及/提高-</option><option>提高+/省选-</option><option>省选/NOI-</option><option>NOI/NOI+/CTSC</option></select><small>用于 Agent1 规范化输入和后续子任务规划。</small></label>
    </div></section>
    <div class="action-bar"><span class="save-state">${icon("shield-check")}创建后将自动执行标程编译</span><div class="form-actions"><button class="button button-primary" id="createProjectBtn" type="button" ${state.busy ? "disabled" : ""}>${state.busy ? busyButton() : `${icon("play")}创建并编译`}</button></div></div>`;
}

function renderStage2() {
  if (!state.project) return lockedEmpty(2);
  const compiled = state.project.solution_compiled;
  const failure = state.project.last_error?.stage === 2 ? state.project.last_error : null;
  const result = state.compileResult || failure?.details || null;
  const log = result ? [result.stdout, result.stderr].filter(Boolean).join("\n") || `进程退出码：${result.exit_code ?? "-"}` : "尚无编译日志。";
  return `<div class="page-intro"><div><h2>Docker 编译</h2><p>标程代码在受限容器中编译。修改代码后重新编译会使后续确认状态失效。</p></div><span class="badge ${compiled ? "success" : failure ? "error" : "warning"}">${icon(compiled ? "check-circle-2" : failure ? "circle-x" : "clock-3")}${compiled ? "编译通过" : failure ? "编译失败" : "等待编译"}</span></div>
    ${compiled ? `<div class="notice"><span>${icon("check-circle-2")}标程已通过编译，可以继续分析输入结构。</span></div>` : ""}
    <section class="work-surface"><div class="section-heading"><div><h3>solution.cpp</h3><p>保存并重新编译后，阶段 3 至 5 需要重新确认</p></div></div><textarea id="solutionEditor" class="code-editor" spellcheck="false">${escapeHtml(state.solutionCode)}</textarea></section>
    <section class="work-surface" style="margin-top:16px"><div class="section-heading" style="padding:12px 16px"><div><h3>编译日志</h3></div><span class="badge ${compiled ? "success" : failure ? "error" : ""}">exit ${escapeHtml(result?.exit_code ?? "-")}</span></div><pre class="log-output">${escapeHtml(log)}</pre></section>
    <div class="action-bar"><span class="save-state" data-solution-save-state>${state.dirtySolution ? `${icon("save")}标程已修改，保存后将重置后续阶段` : compiled ? `${icon("check-circle-2")}编译检查已通过` : `${icon("info")}修正所有编译错误后继续`}</span><div class="form-actions"><button class="button button-secondary" id="compileBtn" type="button" ${state.busy ? "disabled" : ""}>${state.busy ? busyButton("编译中") : `${icon("hammer")}${state.dirtySolution ? "保存并编译" : "重新编译"}`}</button><button class="button button-primary" id="nextStageBtn" type="button" ${compiled ? "" : "disabled"}>进入输入结构${icon("arrow-right")}</button></div></div>`;
}

function confirmationBand(stage) {
  const info = state.project?.stages?.[String(stage)] || {};
  const dirty = state.dirtyStages.has(stage);
  const checking = info.status === "checking" && !dirty;
  const aiConfirmed = info.ai_confirmed && !dirty;
  const userConfirmed = info.user_confirmed && !dirty;
  const failed = info.status === "failed" && !dirty;
  return `<div class="confirmation-band" data-confirmation-band="${stage}"><span>${icon("info")}仅内容发生修改时需要重新进行 AI 检查；检查通过并经用户确认后进入下一阶段。</span><div class="confirmation-actions"><span class="status-chip ${aiConfirmed ? "success" : failed ? "error" : "pending"}">${icon(aiConfirmed ? "badge-check" : failed ? "circle-x" : checking ? "loader-circle" : "clock-3", checking ? "spinner" : "")}AI 检查：${aiConfirmed ? "已通过" : failed ? "未通过" : checking ? "检查中" : "待检查"}</span><span class="status-chip ${userConfirmed ? "success" : "pending"}">${icon(userConfirmed ? "badge-check" : "user-round-check")}用户确认：${userConfirmed ? "已确认" : "待确认"}</span></div></div>`;
}

function stageIssueValues(draft, stage) {
  const draftIssues = Array.isArray(draft?.issues) ? draft.issues : [];
  const stateIssues = state.project?.stages?.[String(stage)]?.issues || [];
  const lastError = state.project?.last_error;
  const errorIssues = Number(lastError?.stage) === stage && lastError?.message ? [lastError.message] : [];
  const issueTranslations = {
    "draft does not match the stage schema": "草稿字段不完整或格式不正确。",
    "AI did not return any input fields": "AI 未生成任何输入字段。",
    "AI did not return an input structure template": "AI 未生成输入结构模板。",
    "Candidate does not guarantee exactly one pair; corrected to use rejection sampling ensuring uniqueness.": "当前生成器不能保证答案对唯一，已改用拒绝采样确保唯一性。",
    "The validator uses readSpace() between array elements, which may reject valid inputs with multiple spaces or tabs. It should not assume exactly one space separator; readInt automatically skips whitespace. Consider removing readSpace calls or replacing with a single inf.readInts call.": "validator 在数组元素之间使用 readSpace()，会严格要求单个空格；若题目允许多个空格或制表符，应移除 readSpace() 或改用 inf.readInts 读取。",
  };
  return [...new Set([...draftIssues, ...stateIssues, ...errorIssues]
    .map((item) => String(item).trim())
    .filter(Boolean)
    .map((item) => issueTranslations[item] || (/[㐀-鿿]/u.test(item)
      ? item
      : "AI 返回的问题说明不是中文，请重新运行 AI 检查。")))];
}

function stageIssues(draft, stage) {
  const issues = stageIssueValues(draft, stage);
  return `<div class="issues-box"><div class="column-title"><span>问题清单</span><span class="badge ${issues.length ? "warning" : "success"}">${issues.length ? `${issues.length} 个待处理问题` : "无待处理问题"}</span></div><label class="field"><textarea id="issuesInput" class="text-area" rows="3" aria-label="问题清单" readonly>${escapeHtml(issues.length ? issues.join("\n") : "无")}</textarea><small>问题清单由 AI 和确定性检查生成，不作为用户实质修改内容。</small></label></div>`;
}

function renderStage3() {
  if (!state.project || !canOpenStage(3)) return lockedEmpty(3);
  const draft = state.drafts["3"];
  const template = draft?.template || "";
  const structureTags = draft?.structure_tags || [];
  const supportedTags = (state.tagCatalog.tags || []).filter((item) => item.status === "supported");
  const info = state.project.stages?.["3"] || {};
  return `${confirmationBand(3)}<div class="page-intro"><div><h2>输入结构模板</h2><p>AI 综合题目描述与标程读取逻辑生成非结构化文本，统一变量名称并说明读取顺序、类型和依赖关系；用户可直接审核和修改。</p></div><span class="badge info">确认后保存为 Template</span></div>
    <section class="work-surface"><div class="surface-body">
      <label class="field"><span class="required">输入结构描述</span><textarea id="inputStructureTemplate" class="text-area structure-template" placeholder="运行 AI 分析后将在此生成输入结构模板；也可以先手动填写。">${escapeHtml(template)}</textarea><small>该文本确认后将作为阶段 4 的只读模板；需要修改时必须返回本阶段重新确认。</small></label>
      <label class="field"><span class="required">已识别的结构标签</span><textarea id="structureTagsInput" class="text-area" rows="9" placeholder='[{"tag_id":"graph.directed.weighted","applies_to":"边表","evidence":"M 行起点、终点和边权"}]'>${escapeHtml(JSON.stringify(structureTags, null, 2))}</textarea><small>标签与模板一起由用户确认；混合结构应保留多个标签及各自适用部分。目录版本：${escapeHtml(state.tagCatalog.version ?? "-")}</small></label>
      <div class="tag-catalog"><span class="helper-text">当前可用标签：</span>${supportedTags.map((item) => `<span class="badge">${escapeHtml(item.id)}</span>`).join(" ")}</div>
      ${stageIssues(draft, 3)}
    </div></section>
    ${interactiveActionBar(3, Boolean(template.trim()), info)}`;
}

function defaultSubtaskPlan() {
  return {
    subtasks: [],
    issues: [],
  };
}

function emptySubtask(id) {
  return { id, constraints: "", test_count: 10, expected_complexity: "O(n)", special_cases: [], runtime_parameters: [], subtask_tags: [] };
}

function complexityControl(value, index) {
  const common = COMPLEXITIES.includes(value);
  const options = COMPLEXITIES.map((item) => `<option ${item === value ? "selected" : ""}>${item}</option>`).join("");
  return `<select class="select-input" data-plan-key="complexity" data-subtask="${index}">${options}<option value="other" ${common ? "" : "selected"}>其它</option></select>
    <input class="text-input custom-complexity" data-plan-key="custom-complexity" data-subtask="${index}" value="${common ? "" : escapeHtml(value)}" placeholder="填写其它复杂度" ${common ? "hidden" : ""}>`;
}

function renderSubtask(subtask, index) {
  const expanded = state.expandedSubtasks.has(index);
  const specialTotal = subtask.special_cases.reduce((sum, item) => sum + Number(item.count || 0), 0);
  const normalCount = Number(subtask.test_count || 0) - specialTotal;
  const specials = subtask.special_cases.map((item, specialIndex) => `<div class="special-row">
      <input class="number-input" type="number" min="1" step="1" data-special-count="${specialIndex}" data-subtask="${index}" value="${escapeHtml(item.count)}" aria-label="特殊测试点数量">
      <input class="text-input" data-special-description="${specialIndex}" data-subtask="${index}" value="${escapeHtml(item.description)}" placeholder="描述数值、关系、分布、结构或顺序特征" aria-label="特殊测试点描述">
      <button class="icon-button small" data-remove-special="${specialIndex}" data-subtask="${index}" type="button" title="删除特殊测试点" aria-label="删除特殊测试点">${icon("trash-2")}</button>
    </div>`).join("");

  return `<article class="subtask-editor" data-subtask-editor="${index}">
    <header class="subtask-summary"><div class="subtask-title"><button class="icon-button small" data-toggle-subtask="${index}" type="button" title="${expanded ? "收起" : "展开"}" aria-label="${expanded ? "收起" : "展开"}子任务 ${subtask.id}">${icon(expanded ? "chevron-down" : "chevron-right")}</button><h3>子任务 ${subtask.id}</h3><span class="badge info">${escapeHtml(subtask.expected_complexity || "未设置")}</span></div><div class="subtask-tools"><span class="badge">${subtask.test_count} 个测试点</span><button class="icon-button small" data-delete-subtask="${index}" type="button" title="删除子任务" aria-label="删除子任务 ${subtask.id}">${icon("trash-2")}</button></div></header>
    <div class="subtask-body" ${expanded ? "" : "hidden"}>
      <section class="subtask-column constraints-column"><h4 class="column-title"><span>数据限制描述</span><span class="badge">自由文本</span></h4><textarea class="text-area constraint-editor" data-plan-key="constraints" data-subtask="${index}" placeholder="请参照输入结构描述约束，例如：n: [1000, 5000]">${escapeHtml(subtask.constraints || "")}</textarea><p class="helper-text">支持自然语言、数学不等式、键值范围、区间和分行文本，无需改写为固定句式。轻微名称差别会由 AI 自动修正。</p></section>
      <section class="subtask-column form-stack"><h4 class="column-title">数量与复杂度</h4><label class="field"><span class="required">测试点总数</span><input class="number-input" type="number" min="1" step="1" data-plan-key="test-count" data-subtask="${index}" value="${escapeHtml(subtask.test_count)}"></label><label class="field"><span class="required">期望通过的算法复杂度</span>${complexityControl(subtask.expected_complexity, index)}</label></section>
      <section class="subtask-column"><h4 class="column-title"><span>非规模性特殊测试点</span><button class="link-button" data-add-special="${index}" type="button">${icon("plus")}添加一项</button></h4><div class="special-list">${specials || `<p class="helper-text">当前没有特殊测试点，全部测试点按普通规模生成。</p>`}</div></section>
      <section class="subtask-column constraints-column"><h4 class="column-title"><span>逐测试点运行时参数</span><span class="badge">JSON</span></h4><textarea class="text-area constraint-editor" data-plan-key="runtime-parameters" data-subtask="${index}" placeholder='[{"case_id":1,"parameters":[{"name":"n_max","value":10,"category":"size"}]}]'>${escapeHtml(JSON.stringify(subtask.runtime_parameters || [], null, 2))}</textarea><p class="helper-text">case_id 必须覆盖 1..测试点总数；参数会作为白名单命令行选项传给 generator。</p></section>
      <section class="subtask-column constraints-column"><h4 class="column-title"><span>子任务结构细化</span><span class="badge">JSON</span></h4><textarea class="text-area constraint-editor" data-plan-key="subtask-tags" data-subtask="${index}" placeholder='["graph.dag"]'>${escapeHtml(JSON.stringify(subtask.subtask_tags || [], null, 2))}</textarea><p class="helper-text">仅填写该子任务新增的结构属性，不得删除阶段 3 全局标签。</p></section>
    </div>
    <div class="count-summary" ${expanded ? "" : "hidden"}><div><span>特殊测试点</span><strong data-special-total>${specialTotal}</strong></div><div><span>测试点总数</span><strong data-test-total>${subtask.test_count}</strong></div><div class="${normalCount < 0 ? "invalid-count" : ""}" data-normal-box><span>普通规模测试点</span><strong data-normal-total>${normalCount}</strong></div></div>
  </article>`;
}

function renderStage4() {
  if (!state.project || !canOpenStage(4)) return lockedEmpty(4);
  if (!state.drafts["4"]) state.drafts["4"] = defaultSubtaskPlan();
  const draft = state.drafts["4"];
  const info = state.project.stages?.["4"] || {};
  const template = state.drafts["3"]?.template || "";
  const globalTags = state.drafts["3"]?.structure_tags || [];
  return `${confirmationBand(4)}<div class="page-intro"><div><h2>模板与子任务配置</h2><p>左侧模板来自阶段 3 且不可编辑；右侧以自由文本描述各子任务的数据限制。</p></div><button class="button button-secondary" id="addSubtaskBtn" type="button">${icon("plus")}添加子任务</button></div>
    <div class="stage4-layout">
      <aside class="template-reference"><div class="section-heading"><div><h3>输入结构 Template</h3><p>阶段 3 已确认版本</p></div><span class="badge success">只读</span></div><textarea class="template-view" readonly aria-label="已确认输入结构模板">${escapeHtml(template)}</textarea><p class="helper-text">全局标签：${escapeHtml(globalTags.map((item) => item.tag_id).join(", ") || "无")}</p><p class="helper-text">如需修改，请返回阶段 3，重新运行 AI 检查并确认。</p></aside>
      <section class="stage4-planner"><div class="section-heading"><div><h3>子任务配置</h3><p>特殊测试点总数不得超过所属子任务测试点总数</p></div><span class="badge">${draft.subtasks.length} 个子任务</span></div><div class="subtask-list" id="subtaskList">${draft.subtasks.length ? draft.subtasks.map(renderSubtask).join("") : `<div class="planner-empty">${icon("list-plus")}<strong>等待 Agent3 规划</strong><span>直接运行 AI 检查将根据题目自动生成 5 个初始子任务，也可以先手动添加。</span></div>`}</div><div class="planner-issues">${stageIssues(draft, 4)}</div></section>
    </div>
    ${interactiveActionBar(4, draft.subtasks.length > 0, info)}`;
}

function renderStage5() {
  if (!state.project || !canOpenStage(5)) return lockedEmpty(5);
  const draft = state.drafts["5"] || { generator_code: "", validator_code: "", issues: [] };
  const info = state.project.stages?.["5"] || {};
  const hasBoth = Boolean(draft.generator_code && draft.validator_code);
  const preview = state.preview;
  const subtasks = state.drafts["4"]?.subtasks || [];
  return `${confirmationBand(5)}<div class="page-intro"><div><h2>代码草稿</h2><p>Agent4 会按输入中的全部结构加载 jngen 专题文档，再生成 generator 与 validator；随后分级执行静态检查、冒烟测试和完整验证。</p></div><span class="badge ${hasBoth ? "info" : "warning"}">${hasBoth ? `revision ${escapeHtml(draft.revision_id || "draft")}` : "等待生成"}</span></div>
    <div class="code-grid">
      <section class="work-surface code-pane"><div class="section-heading"><div><h3>generator.cpp</h3><p>testlib / jngen</p></div></div><textarea id="generatorCode" class="code-editor" spellcheck="false" placeholder="运行 AI 生成 generator.cpp">${escapeHtml(draft.generator_code)}</textarea></section>
      <section class="work-surface code-pane"><div class="section-heading"><div><h3>validator.cpp</h3><p>testlib strict validation</p></div></div><textarea id="validatorCode" class="code-editor" spellcheck="false" placeholder="运行 AI 生成 validator.cpp">${escapeHtml(draft.validator_code)}</textarea></section>
    </div>
    <details class="work-surface" style="margin-top:16px"><summary class="section-heading" style="padding:12px 16px;cursor:pointer"><div><h3>约束覆盖表</h3><p>逐子任务、逐测试点关联运行时参数与生成/校验策略</p></div><span class="badge">${draft.constraint_coverage?.length || 0} 项</span></summary><div class="surface-body"><pre>${escapeHtml(JSON.stringify(draft.constraint_coverage || [], null, 2))}</pre></div></details>
    <div class="preview-layout">
      <section class="work-surface preview-controls form-stack"><div><h3 style="margin:0;font-size:15px">种子试运行</h3><p class="helper-text">选择子任务和测试点；对应的数据与规模限制会通过命令行传给 generator。</p></div><label class="field"><span>子任务</span><select id="previewSubtask" class="select-input">${subtasks.map((item) => `<option value="${item.id}">子任务 ${item.id}</option>`).join("")}</select></label><label class="field"><span>测试点</span><input id="previewCase" class="number-input" type="number" min="1" step="1" value="${preview?.case_id ?? 1}"></label><label class="field"><span>种子</span><input id="previewSeed" class="number-input" type="number" step="1" value="${preview?.seed ?? 42}"></label><button class="button button-secondary" id="previewBtn" type="button" ${hasBoth && !state.busy ? "" : "disabled"}>${icon("play")}编译并试运行</button><ul class="preview-history">${state.previewHistory.map((item) => `<li><span>子任务 ${item.subtaskId} · 测试点 ${item.caseId} · seed ${item.seed}</span><strong>${item.ok ? "通过" : "失败"}</strong></li>`).join("")}</ul></section>
      <section class="work-surface preview-output"><div class="section-heading" style="margin-bottom:12px"><div><h3>输入数据预览</h3><p>${preview ? `seed ${escapeHtml(preview.seed)} · validator ${preview.validator?.ok ? "通过" : "未通过"}` : "尚未运行"}</p></div>${preview ? `<span class="badge ${preview.validator?.ok ? "success" : "error"}">${preview.validator?.ok ? "合法输入" : "校验失败"}</span>` : ""}</div><pre>${escapeHtml(preview?.content || "运行生成器后在此查看输入数据。")}</pre></section>
    </div>
    <section class="work-surface" style="margin-top:16px"><div class="surface-body">${stageIssues(draft, 5)}</div></section>
    ${interactiveActionBar(5, hasBoth, info)}`;
}

function renderStage6() {
  if (!state.project || !canOpenStage(6)) return lockedEmpty(6);
  const total = totalTests();
  const complete = state.project.generation_complete;
  const generatedIds = new Set(state.project.generated_subtasks || []);
  const subtasks = state.drafts["4"]?.subtasks || [];
  const choices = subtasks.map((item) => {
    const checked = complete ? generatedIds.has(item.id) : true;
    return `<label class="subtask-choice"><input type="checkbox" data-generate-subtask value="${item.id}" ${checked ? "checked" : ""}><span><strong>子任务 ${item.id}</strong><small>${item.test_count} 个测试点 · ${escapeHtml(item.expected_complexity)}</small></span></label>`;
  }).join("");
  return `<div class="page-intro"><div><h2>批量生成</h2><p>选择全部或部分子任务，在 Docker 容器中编译 generator 并批量生成输入数据。</p></div><span class="badge ${complete ? "success" : "info"}">${complete ? `${icon("check")}已生成` : `${icon("container")}受限容器`}</span></div>
    <section class="work-surface"><div class="section-heading" style="padding:12px 16px"><div><h3>生成范围</h3><p>每次执行会建立一个新批次，并替换上一批数据</p></div><label class="select-all"><input id="selectAllSubtasks" type="checkbox" checked>全选</label></div><div class="subtask-selection">${choices}</div></section>
    <section class="work-surface" style="margin-top:16px"><div class="surface-body form-grid"><label class="field"><span>基础种子</span><input id="baseSeed" class="number-input" type="number" step="1" value="1"><small>各测试点会结合子任务号与内部编号派生独立种子。</small></label><div class="field"><span>计划规模</span><div class="notice" style="margin:0;min-height:74px"><span>${icon("file-stack")}<strong>全部子任务共 ${total} 个输入文件</strong></span></div></div></div></section>
    <div class="action-bar"><span class="save-state">${complete ? `${icon("check-circle-2")}已生成所选子任务，可进入独立验证` : `${icon("info")}本阶段只生成 .in，验证与 .out 在阶段 7 执行`}</span><div class="form-actions">${complete ? `<button class="button button-secondary" id="generateBtn" type="button" ${state.busy ? "disabled" : ""}>${icon("rotate-cw")}重新生成所选项</button><button class="button button-primary" id="nextStageBtn" type="button">进入验证${icon("arrow-right")}</button>` : `<button class="button button-primary" id="generateBtn" type="button" ${state.busy ? "disabled" : ""}>${state.busy ? busyButton("批量生成中") : `${icon("play")}生成所选子任务`}</button>`}</div></div>`;
}

function generatedFiles() {
  const files = [];
  const generated = new Set(state.project?.generated_subtasks || []);
  for (const subtask of state.drafts["4"]?.subtasks || []) {
    if (generated.size && !generated.has(subtask.id)) continue;
    for (let internal = 1; internal <= Number(subtask.test_count || 0); internal += 1) {
      files.push(`${subtask.id}_${internal}.in`, `${subtask.id}_${internal}.out`);
    }
  }
  return files;
}

function renderStage7() {
  if (!state.project || !canOpenStage(7)) return lockedEmpty(7);
  const complete = state.project.build_complete;
  const failure = !complete ? state.project.last_error : null;
  const files = generatedFiles();
  return `<div class="page-intro"><div><h2>合法性与标准输出</h2><p>validator 逐个检查阶段 6 生成的输入，随后运行标程生成同名 .out；失败信息会反馈给 Agent4 修复。</p></div><span class="badge ${complete ? "success" : "info"}">${icon(complete ? "badge-check" : "shield-check")}${complete ? "全部通过" : "等待验证"}</span></div>
    ${failure ? `<div class="notice error"><span>${icon("circle-alert")}<span><strong>${escapeHtml(failure.message || "验证或标程运行失败")}</strong><br><small>恢复阶段：${escapeHtml(failure.details?.recovery_stage || failure.stage || "-")}</small></span></span></div>` : `<div class="notice"><span>${icon(complete ? "check-circle-2" : "info")}${complete ? "全部输入均通过 validator，标程已生成配对输出。" : "阶段 6 已完成；开始验证后将逐个处理当前批次。"}</span></div>`}
    <section class="work-surface"><div class="section-heading" style="padding:12px 16px"><div><h3>生成结果</h3><p>命名规则：子任务号_内部编号.in/out，编号均无前置零</p></div><span class="badge ${complete ? "success" : "warning"}">${complete ? `${files.length / 2} 对文件` : "未完成"}</span></div><div class="surface-body"><ul class="file-list">${files.slice(0, 30).map((file) => `<li>${icon(file.endsWith(".in") ? "file-input" : "file-output")}<span>${escapeHtml(file)}</span></li>`).join("")}</ul>${files.length > 30 ? `<p class="helper-text">另有 ${files.length - 30} 个文件未在预览中展开。</p>` : ""}</div></section>
    <div class="action-bar"><span class="save-state">${complete ? `${icon("check-circle-2")}阶段 7 已完成` : `${icon("info")}本阶段不会重新生成输入数据`}</span><div class="form-actions">${complete ? `<button class="button button-primary" id="nextStageBtn" type="button">进入导出${icon("arrow-right")}</button>` : `<button class="button button-primary" id="validateBtn" type="button" ${state.busy ? "disabled" : ""}>${state.busy ? busyButton("验证中") : `${icon("shield-check")}验证并生成输出`}</button>`}</div></div>`;
}

function renderStage8() {
  if (!state.project || !canOpenStage(8)) return lockedEmpty(8);
  const files = ["generator.cpp", "validator.cpp", ...generatedFiles().map((name) => `data/${name}`)];
  return `<div class="page-intro"><div><h2>最终数据包</h2><p>zip 只包含生成器、校验器和配对输入输出数据，不包含标程、报告或项目元数据。</p></div><span class="badge success">${icon("package-check")}可导出</span></div>
    <div class="notice"><span>${icon("check-circle-2")}数据包已就绪，共 ${files.length} 个文件。</span></div>
    <section class="work-surface"><div class="section-heading" style="padding:12px 16px"><div><h3>dataset.zip</h3><p>固定导出内容</p></div><span class="badge">${files.length} 个文件</span></div><div class="surface-body"><ul class="file-list">${files.slice(0, 32).map((file) => `<li>${icon(file.endsWith(".cpp") ? "file-code-2" : file.endsWith(".in") ? "file-input" : "file-output")}<span>${escapeHtml(file)}</span></li>`).join("")}</ul>${files.length > 32 ? `<p class="helper-text">另有 ${files.length - 32} 个数据文件包含在 zip 中。</p>` : ""}</div></section>
    <div class="action-bar"><span class="save-state">${icon("shield-check")}导出内容已按固定规则检查</span><div class="form-actions"><button class="button button-primary" id="exportBtn" type="button" ${state.busy ? "disabled" : ""}>${state.busy ? busyButton("准备下载") : `${icon("download")}下载 dataset.zip`}</button></div></div>`;
}

function lockedEmpty(stage) {
  return `<div class="empty-state"><div>${icon("lock-keyhole")}<h2>阶段 ${stage} 尚未解锁</h2><p>完成并确认前置阶段后，此阶段会自动开放。</p><button class="button button-primary" id="goCurrentBtn" type="button">返回当前阶段</button></div></div>`;
}

function interactiveActionBar(stage, canSave, info) {
  const dirty = state.dirtyStages.has(stage);
  const checking = info.status === "checking" && !dirty;
  const blocked = state.busy || checking;
  const alreadyPassed = info.status === "passed" && !dirty;
  return `<div class="action-bar"><span class="save-state" data-draft-save-state>${alreadyPassed ? `${icon("check-circle-2")}AI 与用户已共同确认` : checking ? `${icon("loader-circle", "spinner")}AI 正在检查，完成后刷新状态` : dirty ? `${icon("save")}内容已修改，保存后将回退并锁定后续阶段` : `${icon("check-circle-2")}内容未修改，无需重新检查`}</span><div class="form-actions"><button class="button button-secondary" id="saveDraftBtn" type="button" ${canSave && dirty && !blocked ? "" : "disabled"}>${icon("save")}保存草稿</button><button class="button button-secondary" id="runAiBtn" type="button" ${blocked || alreadyPassed ? "disabled" : ""}>${blocked ? busyButton("AI 检查中") : `${icon("play")}运行 AI 检查`}</button><button class="button button-primary" id="confirmBtn" type="button" ${info.ai_confirmed && !info.user_confirmed && !dirty && !blocked ? "" : "disabled"}>确认并继续${icon("arrow-right")}</button></div></div>`;
}

const STAGE_RENDERERS = {
  1: renderStage1,
  2: renderStage2,
  3: renderStage3,
  4: renderStage4,
  5: renderStage5,
  6: renderStage6,
  7: renderStage7,
  8: renderStage8,
};

function readIssues() {
  const value = $("#issuesInput")?.value.trim() || "";
  if (!value || value === "无") return [];
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function readStructureDraft() {
  let structureTags;
  try {
    structureTags = JSON.parse($("#structureTagsInput")?.value.trim() || "[]");
  } catch {
    throw new Error("结构标签不是合法 JSON。");
  }
  return {
    template: $("#inputStructureTemplate")?.value.trim() || "",
    structure_tags: structureTags,
    issues: readIssues(),
  };
}

function validateStructure(draft) {
  if (!draft.template) throw new Error("输入结构模板不能为空，请先运行 AI 分析或手动填写。");
  if (!Array.isArray(draft.structure_tags)) throw new Error("结构标签必须是 JSON 数组。");
}

function readPlanDraft() {
  const current = state.drafts["4"] || defaultSubtaskPlan();
  const subtasks = current.subtasks.map((subtask, index) => {
    const constraints = $(`[data-plan-key="constraints"][data-subtask="${index}"]`)?.value.trim() || "";
    const testCount = Number($(`[data-plan-key="test-count"][data-subtask="${index}"]`)?.value || 0);
    const complexitySelect = $(`[data-plan-key="complexity"][data-subtask="${index}"]`);
    const complexity = complexitySelect?.value === "other"
      ? $(`[data-plan-key="custom-complexity"][data-subtask="${index}"]`)?.value.trim()
      : complexitySelect?.value;
    const specialCases = $$(`[data-special-count][data-subtask="${index}"]`).map((input) => {
      const specialIndex = input.dataset.specialCount;
      return {
        count: Number(input.value),
        description: $(`[data-special-description="${specialIndex}"][data-subtask="${index}"]`)?.value.trim() || "",
      };
    });
    const runtimeText = $(`[data-plan-key="runtime-parameters"][data-subtask="${index}"]`)?.value.trim() || "[]";
    let runtimeParameters;
    try {
      runtimeParameters = JSON.parse(runtimeText);
    } catch {
      throw new Error(`子任务 ${subtask.id} 的运行时参数不是合法 JSON。`);
    }
    const tagText = $(`[data-plan-key="subtask-tags"][data-subtask="${index}"]`)?.value.trim() || "[]";
    let subtaskTags;
    try {
      subtaskTags = JSON.parse(tagText);
    } catch {
      throw new Error(`子任务 ${subtask.id} 的结构细化标签不是合法 JSON。`);
    }
    return { id: subtask.id, constraints, test_count: testCount, expected_complexity: complexity || "", special_cases: specialCases, runtime_parameters: runtimeParameters, subtask_tags: subtaskTags };
  });
  return { subtasks, issues: readIssues() };
}

function validatePlan(draft) {
  if (!draft.subtasks.length) throw new Error("至少需要一个子任务。");
  for (const subtask of draft.subtasks) {
    if (!subtask.constraints) throw new Error(`子任务 ${subtask.id} 缺少数据限制描述。`);
    if (!Number.isInteger(subtask.test_count) || subtask.test_count <= 0) throw new Error(`子任务 ${subtask.id} 的测试点总数必须为正整数。`);
    if (!subtask.expected_complexity) throw new Error(`子任务 ${subtask.id} 缺少期望复杂度。`);
    if (!Array.isArray(subtask.runtime_parameters) || subtask.runtime_parameters.length !== subtask.test_count) throw new Error(`子任务 ${subtask.id} 必须为每个测试点提供一组运行时参数。`);
    if (!Array.isArray(subtask.subtask_tags) || subtask.subtask_tags.some((item) => typeof item !== "string")) throw new Error(`子任务 ${subtask.id} 的结构细化标签必须是字符串数组。`);
    let specialTotal = 0;
    for (const item of subtask.special_cases) {
      if (!Number.isInteger(item.count) || item.count <= 0 || !item.description) throw new Error(`子任务 ${subtask.id} 的特殊测试点需要填写正整数数量与完整描述。`);
      specialTotal += item.count;
    }
    if (specialTotal > subtask.test_count) throw new Error(`子任务 ${subtask.id} 的特殊测试点数量超过测试点总数。`);
  }
}

function readCodeDraft() {
  return {
    generator_code: $("#generatorCode")?.value.trim() || "",
    validator_code: $("#validatorCode")?.value.trim() || "",
    constraint_coverage: cloneJson(state.drafts["5"]?.constraint_coverage || []),
    revision_id: state.drafts["5"]?.revision_id || null,
    issues: readIssues(),
  };
}

function validateCodeDraft(draft) {
  if (!draft.generator_code || !draft.validator_code) throw new Error("generator.cpp 与 validator.cpp 均不能为空。");
}

async function saveDraft(stage, draft) {
  const response = await api(`/api/projects/${state.projectId}/stages/${stage}/draft`, {
    method: "PUT",
    body: JSON.stringify({ draft }),
  });
  state.drafts[String(stage)] = response.draft;
  state.dirtyStages.delete(stage);
  await refreshProject({ silent: true });
  state.activeStage = Number(state.project.current_stage);
}

async function runAiStage(stage, taskType = null) {
  const body = taskType ? { task_type: taskType } : {};
  const response = await api(`/api/projects/${state.projectId}/stages/${stage}/run`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  state.drafts[String(stage)] = response.draft;
  state.dirtyStages.delete(stage);
  await refreshProject({ silent: true });
}

async function confirmStage(stage) {
  const project = await api(`/api/projects/${state.projectId}/stages/${stage}/confirm`, {
    method: "POST",
    body: JSON.stringify({ confirmed: true }),
  });
  state.project = project;
  state.dirtyStages.delete(stage);
  state.activeStage = stage + 1;
  await refreshProject({ silent: true });
}

function syncInteractiveControls(stage, readDraft) {
  const draft = readDraft();
  const dirty = updateDraftDirtyState(stage, draft);
  const info = state.project?.stages?.[String(stage)] || {};
  const checking = info.status === "checking" && !dirty;
  const blocked = state.busy || checking;
  const saveButton = $("#saveDraftBtn");
  const runAiButton = $("#runAiBtn");
  const confirmButton = $("#confirmBtn");
  if (saveButton) saveButton.disabled = !dirty || blocked;
  if (runAiButton) runAiButton.disabled = blocked || (info.status === "passed" && !dirty);
  if (confirmButton) confirmButton.disabled = dirty || !info.ai_confirmed || info.user_confirmed || blocked;
  const saveState = $("[data-draft-save-state]");
  if (saveState) {
    saveState.innerHTML = dirty
      ? `${icon("save")}内容已修改，保存后将回退并锁定后续阶段`
      : info.status === "passed"
        ? `${icon("check-circle-2")}AI 与用户已共同确认`
        : `${icon("check-circle-2")}内容未修改，无需重新检查`;
  }
  const currentBand = $(`[data-confirmation-band="${stage}"]`);
  if (currentBand) {
    const replacement = document.createElement("div");
    replacement.innerHTML = confirmationBand(stage);
    currentBand.replaceWith(replacement.firstElementChild);
  }
  hydrateIcons();
  renderNav();
  return dirty;
}

function bindInteractiveStage(stage, readDraft, validateDraft, contentSelector) {
  $$(contentSelector, $("#stageContent")).forEach((control) => {
    control.addEventListener("input", () => {
      const draft = readDraft();
      state.drafts[String(stage)] = { ...(state.drafts[String(stage)] || {}), ...draft };
      syncInteractiveControls(stage, readDraft);
    });
  });
  syncInteractiveControls(stage, readDraft);
  $("#saveDraftBtn")?.addEventListener("click", () => {
    const draft = readDraft();
    try { validateDraft(draft); } catch (error) { showToast(error.message, "error"); return; }
    runMutation("保存中", () => saveDraft(stage, draft), "内容修改已保存，请重新进行 AI 检查");
  });

  $("#runAiBtn")?.addEventListener("click", () => {
    const draft = readDraft();
    const hasDraft = stage === 3
      ? Boolean(draft.template)
      : stage === 4
        ? Boolean(draft.subtasks.length)
        : Boolean(draft.generator_code && draft.validator_code);
    if (hasDraft) {
      try { validateDraft(draft); } catch (error) { showToast(error.message, "error"); return; }
    }
    runMutation("AI 检查中", async () => {
      if (hasDraft && draftContentChanged(stage, draft)) await saveDraft(stage, draft);
      await runAiStage(stage);
    }, "AI 检查已完成，请核对结果与确认状态");
  });

  $("#confirmBtn")?.addEventListener("click", () => {
    if (syncInteractiveControls(stage, readDraft)) {
      showToast("内容已修改，请先保存并重新进行 AI 检查。", "error");
      return;
    }
    runMutation("确认中", () => confirmStage(stage), `阶段 ${stage} 已由 AI 与用户共同确认`);
  });
}

function bindStage(stage) {
  $("#goCurrentBtn")?.addEventListener("click", () => activateStage(state.project ? Number(state.project.current_stage) : 1));
  $("#nextStageBtn")?.addEventListener("click", () => activateStage(Math.min(stage + 1, 8)));
  if (stage === 1) bindStage1();
  if (stage === 2) bindStage2();
  if (stage === 3) bindStage3();
  if (stage === 4) bindStage4();
  if (stage === 5) bindStage5();
  if (stage === 6) bindStage6();
  if (stage === 7) bindStage7();
  if (stage === 8) bindStage8();
}

function bindStage1() {
  $$('[data-upload]').forEach((button) => {
    button.addEventListener("click", () => $(`#${button.dataset.upload}`).click());
  });
  $("#problemFile")?.addEventListener("change", (event) => loadFileInto(event.target, "#problemDescription", "#problemFileName"));
  $("#solutionFile")?.addEventListener("change", (event) => loadFileInto(event.target, "#solutionCode", "#solutionFileName"));
  $("#createProjectBtn")?.addEventListener("click", createProject);
}

async function loadFileInto(input, targetSelector, nameSelector) {
  const file = input.files?.[0];
  if (!file) return;
  $(targetSelector).value = await file.text();
  $(nameSelector).textContent = file.name;
}

function createProject() {
  const problem = $("#problemDescription").value.trim();
  const solution = $("#solutionCode").value.trim();
  const difficulty = $("#difficulty").value;
  if (!problem || !solution || !difficulty) {
    showToast("题目描述、C++ 标程和难度等级均为必填项。", "error");
    return;
  }
  runMutation("创建并编译中", async () => {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ problem_description: problem, solution_code: solution, difficulty }),
    });
    state.projectId = project.project_id;
    state.project = project;
    state.solutionCode = solution;
    localStorage.setItem("testforge.projectId", state.projectId);
    const response = await api(`/api/projects/${state.projectId}/solution/compile`, { method: "POST" });
    state.project = response.project;
    state.compileResult = response.result;
    await refreshProject({ silent: true });
    state.activeStage = state.project.solution_compiled ? 3 : 2;
    if (!state.project.solution_compiled) throw Object.assign(new Error("solution compilation failed"), { payload: state.project.last_error });
  }, "项目已创建，标程编译通过");
}

function bindStage2() {
  const editor = $("#solutionEditor");
  editor?.addEventListener("input", () => {
    state.dirtySolution = editor.value.trim() !== state.solutionBaseline.trim();
    const saveState = $("[data-solution-save-state]");
    const compileButton = $("#compileBtn");
    if (saveState) {
      saveState.innerHTML = state.dirtySolution
        ? `${icon("save")}标程已修改，保存后将重置后续阶段`
        : `${icon("check-circle-2")}内容未修改，重新编译不会回退流程`;
      hydrateIcons();
    }
    if (compileButton && !state.busy) {
      compileButton.innerHTML = `${icon("hammer")}${state.dirtySolution ? "保存并编译" : "重新编译"}`;
      hydrateIcons();
    }
  });
  $("#compileBtn")?.addEventListener("click", () => {
    const solution = $("#solutionEditor").value.trim();
    if (!solution) { showToast("C++ 标程不能为空。", "error"); return; }
    runMutation("编译中", async () => {
      await api(`/api/projects/${state.projectId}/solution`, { method: "PUT", body: JSON.stringify({ solution_code: solution }) });
      state.solutionCode = solution;
      const response = await api(`/api/projects/${state.projectId}/solution/compile`, { method: "POST" });
      state.project = response.project;
      state.compileResult = response.result;
      await refreshProject({ silent: true });
      if (!response.result.ok) throw Object.assign(new Error("solution compilation failed"), { payload: response.project.last_error });
    }, "标程编译通过");
  });
}

function bindStage3() {
  bindInteractiveStage(3, readStructureDraft, validateStructure, "#inputStructureTemplate, #structureTagsInput");
}

function preservePlanAndRender(mutator) {
  state.drafts["4"] = readPlanDraft();
  mutator(state.drafts["4"]);
  updateDraftDirtyState(4, state.drafts["4"]);
  render();
}

function bindStage4() {
  bindInteractiveStage(4, readPlanDraft, validatePlan, "[data-plan-key], [data-special-count], [data-special-description]");
  $("#addSubtaskBtn")?.addEventListener("click", () => preservePlanAndRender((draft) => {
    const id = Math.max(0, ...draft.subtasks.map((item) => item.id)) + 1;
    draft.subtasks.push(emptySubtask(id));
    state.expandedSubtasks.add(draft.subtasks.length - 1);
  }));
  $$('[data-toggle-subtask]').forEach((button) => button.addEventListener("click", () => preservePlanAndRender(() => {
    const index = Number(button.dataset.toggleSubtask);
    if (state.expandedSubtasks.has(index)) state.expandedSubtasks.delete(index); else state.expandedSubtasks.add(index);
  })));
  $$('[data-delete-subtask]').forEach((button) => button.addEventListener("click", () => {
    if ((state.drafts["4"]?.subtasks.length || 0) <= 1) {
      showToast("至少保留一个子任务；如需重新规划，请直接运行 AI 检查。", "error");
      return;
    }
    preservePlanAndRender((draft) => draft.subtasks.splice(Number(button.dataset.deleteSubtask), 1));
  }));
  $$('[data-add-special]').forEach((button) => button.addEventListener("click", () => preservePlanAndRender((draft) => {
    draft.subtasks[Number(button.dataset.addSpecial)].special_cases.push({ count: 1, description: "" });
  })));
  $$('[data-remove-special]').forEach((button) => button.addEventListener("click", () => preservePlanAndRender((draft) => {
    draft.subtasks[Number(button.dataset.subtask)].special_cases.splice(Number(button.dataset.removeSpecial), 1);
  })));
  $$('[data-plan-key="complexity"]').forEach((select) => select.addEventListener("change", () => {
    const custom = $(`[data-plan-key="custom-complexity"][data-subtask="${select.dataset.subtask}"]`);
    custom.hidden = select.value !== "other";
    if (!custom.hidden) custom.focus();
  }));
  $("#subtaskList")?.addEventListener("input", updateCountSummaries);
}

function updateCountSummaries() {
  $$('[data-subtask-editor]').forEach((editor) => {
    const index = editor.dataset.subtaskEditor;
    const total = Number($(`[data-plan-key="test-count"][data-subtask="${index}"]`)?.value || 0);
    const special = $$(`[data-special-count][data-subtask="${index}"]`).reduce((sum, input) => sum + Number(input.value || 0), 0);
    $("[data-special-total]", editor).textContent = String(special);
    $("[data-test-total]", editor).textContent = String(total);
    $("[data-normal-total]", editor).textContent = String(total - special);
    $("[data-normal-box]", editor).classList.toggle("invalid-count", total - special < 0);
  });
}

function bindStage5() {
  bindInteractiveStage(5, readCodeDraft, validateCodeDraft, "#generatorCode, #validatorCode");
  $("#previewBtn")?.addEventListener("click", () => {
    const draft = readCodeDraft();
    const seed = Number($("#previewSeed").value);
    const subtaskId = Number($("#previewSubtask").value);
    const caseId = Number($("#previewCase").value);
    try { validateCodeDraft(draft); } catch (error) { showToast(error.message, "error"); return; }
    if (!Number.isInteger(seed)) { showToast("种子必须为整数。", "error"); return; }
    if (!Number.isInteger(caseId) || caseId <= 0) { showToast("测试点必须为正整数。", "error"); return; }
    runMutation("试运行中", async () => {
      if (draftContentChanged(5, draft)) await saveDraft(5, draft);
      const preview = await api(`/api/projects/${state.projectId}/preview`, { method: "POST", body: JSON.stringify({ subtask_id: subtaskId, case_id: caseId, seed }) });
      state.preview = { ...preview, subtaskId };
      state.previewHistory.unshift({ seed, subtaskId, caseId, ok: preview.validator?.ok });
      state.previewHistory = state.previewHistory.slice(0, 5);
    }, `种子 ${seed} 试运行完成`);
  });
}

function totalTests() {
  return (state.drafts["4"]?.subtasks || []).reduce((sum, item) => sum + Number(item.test_count || 0), 0);
}

async function generateDataset(baseSeed, selectedSubtaskIds) {
  state.buildResult = await api(`/api/projects/${state.projectId}/generate`, { method: "POST", body: JSON.stringify({ base_seed: baseSeed, selected_subtask_ids: selectedSubtaskIds }) });
  await refreshProject({ silent: true });
  state.activeStage = 7;
}

function bindStage6() {
  const checkboxes = $$('[data-generate-subtask]');
  const selectAll = $("#selectAllSubtasks");
  const syncSelectAll = () => {
    const selected = checkboxes.filter((item) => item.checked).length;
    selectAll.checked = selected === checkboxes.length;
    selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
  };
  selectAll?.addEventListener("change", () => checkboxes.forEach((item) => { item.checked = selectAll.checked; }));
  checkboxes.forEach((item) => item.addEventListener("change", syncSelectAll));
  syncSelectAll();
  $("#generateBtn")?.addEventListener("click", () => {
    const seed = Number($("#baseSeed").value);
    if (!Number.isInteger(seed)) { showToast("基础种子必须为整数。", "error"); return; }
    const selected = checkboxes.filter((item) => item.checked).map((item) => Number(item.value));
    if (!selected.length) { showToast("请至少选择一个子任务。", "error"); return; }
    runMutation("批量生成中", () => generateDataset(seed, selected), "所选子任务的输入数据已生成");
  });
}

function bindStage7() {
  $("#validateBtn")?.addEventListener("click", () => runMutation("验证中", async () => {
    state.buildResult = await api(`/api/projects/${state.projectId}/validate`, {
      method: "POST",
      body: JSON.stringify({ selected_subtask_ids: state.project.generated_subtasks || null }),
    });
    await refreshProject({ silent: true });
  }, "输入校验通过，标准输出已生成"));
}

function bindStage8() {
  $("#exportBtn")?.addEventListener("click", () => runMutation("准备下载", async () => {
    const response = await fetch(`${API_BASE}/api/projects/${state.projectId}/export`);
    if (!response.ok) {
      const body = await response.json();
      throw Object.assign(new Error(body.message), { payload: body });
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${state.projectId}-dataset.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }, "数据包已导出"));
}

function discardUnsavedChangesBeforeNavigation(stage) {
  if (stage === state.activeStage) return true;
  const hasDirtyDraft = state.dirtyStages.has(state.activeStage);
  const hasDirtySolution = state.activeStage === 2 && state.dirtySolution;
  if (!hasDirtyDraft && !hasDirtySolution) return true;
  if (!window.confirm("当前修改尚未保存。离开将放弃这些修改，是否继续？")) {
    return false;
  }
  if (hasDirtyDraft) {
    state.drafts[String(state.activeStage)] = cloneJson(
      state.draftBaselines[String(state.activeStage)],
    );
    state.dirtyStages.delete(state.activeStage);
  }
  if (hasDirtySolution) {
    state.solutionCode = state.solutionBaseline;
    state.dirtySolution = false;
  }
  return true;
}

function activateStage(stage) {
  if (!canOpenStage(stage)) {
    showToast("请先完成并确认前置阶段。", "error");
    return;
  }
  if (!discardUnsavedChangesBeforeNavigation(stage)) return;
  state.activeStage = stage;
  state.error = null;
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function refreshProject({ silent = false, keepError = false } = {}) {
  if (!state.projectId) return;
  try {
    const [projectResponse, solutionResponse] = await Promise.all([
      api(`/api/projects/${state.projectId}`),
      api(`/api/projects/${state.projectId}/solution`),
    ]);
    state.project = projectResponse.project;
    state.tagCatalog = projectResponse.structure_tag_catalog || { version: null, tags: [] };
    state.drafts = projectResponse.drafts;
    state.draftBaselines = Object.fromEntries([3, 4, 5].map((stage) => {
      const baseline = cloneJson(projectResponse.drafts[String(stage)]);
      if (baseline) baseline.issues = stageIssueValues(baseline, stage);
      return [String(stage), baseline];
    }));
    state.dirtyStages.clear();
    state.solutionCode = solutionResponse.solution_code;
    state.solutionBaseline = solutionResponse.solution_code;
    state.dirtySolution = false;
    if (!canOpenStage(state.activeStage)) {
      state.activeStage = Number(state.project.current_stage);
    }
    if (!keepError) state.error = null;
    if (!silent) showToast("项目状态已刷新");
  } catch (error) {
    if (error.status === 404) {
      localStorage.removeItem("testforge.projectId");
      state.projectId = null;
      state.project = null;
      state.activeStage = 1;
    } else {
      state.error = error;
    }
  }
}

async function checkHealth() {
  try {
    const health = await api("/health");
    state.apiOnline = health.status === "ok";
    state.apiMode = "真实模型";
    if (!state.modelConfig) {
      state.modelConfig = {
        model_name: health.model_name || state.apiMode,
        api_key_configured: Boolean(health.model_api_configured),
      };
    }
  } catch {
    state.apiOnline = false;
    state.apiMode = "";
  }
}

async function loadModelConfiguration() {
  const config = await api("/api/settings/model");
  state.modelConfig = config;
  state.apiMode = "真实模型";
  return config;
}

function renderModelConfigStatus(config, result = null) {
  const target = $("#modelConfigStatus");
  if (!target) return;
  if (result) {
    target.innerHTML = `${icon("badge-check")}<span><strong>${escapeHtml(result.message)}</strong> 响应 ${escapeHtml(result.latency_ms)} ms</span>`;
    hydrateIcons();
    return;
  }
  const key = config.api_key_configured ? config.api_key_hint : "API Key 未配置";
  target.innerHTML = `${icon("cloud")}<span><strong>真实模型 · ${escapeHtml(config.model_name)}</strong><br>${escapeHtml(key)}</span>`;
  hydrateIcons();
}

function fillModelSettings(config) {
  $("#modelBaseUrl").value = config.base_url || "https://api.deepseek.com/v1";
  $("#modelName").value = config.model_name || "deepseek-chat";
  $("#modelApiKey").type = "password";
  $("#modelApiKey").value = "";
  $("#toggleApiKeyBtn").setAttribute("title", "显示 API Key");
  $("#toggleApiKeyBtn").setAttribute("aria-label", "显示 API Key");
  $("#toggleApiKeyBtn").innerHTML = icon("eye");
  $("#modelApiKey").placeholder = config.api_key_configured
    ? `已保存（${config.api_key_hint}），留空则保持不变`
    : "输入新的 API Key";
  $("#apiKeyHelp").textContent = config.api_key_configured
    ? `后端已保存密钥（${config.api_key_hint}），页面不会读取原值。`
    : "密钥只保存到后端，不会在页面中回显。";
  $("#modelTimeout").value = config.timeout_seconds ?? 120;
  $("#agentMaxIterations").value = config.max_iterations ?? 4;
  $("#trialSeedsPerSubtask").value = config.trial_seeds_per_subtask ?? 1;
  renderModelConfigStatus(config);
}

function readModelSettings() {
  const form = $("#modelSettingsForm");
  if (!form.reportValidity()) return null;
  return {
    base_url: $("#modelBaseUrl").value.trim(),
    model_name: $("#modelName").value.trim(),
    api_key: $("#modelApiKey").value.trim() || null,
    clear_api_key: false,
    timeout_seconds: Number($("#modelTimeout").value),
    max_iterations: Number($("#agentMaxIterations").value),
    trial_seeds_per_subtask: Number($("#trialSeedsPerSubtask").value),
  };
}

function setModelSettingsBusy(busy, label = "") {
  state.modelSettingsBusy = busy;
  ["#testModelConnectionBtn", "#saveModelSettingsBtn", "#cancelModelSettingsBtn"].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = busy;
  });
  if (busy && label) {
    const button = label === "测试连接中" ? $("#testModelConnectionBtn") : $("#saveModelSettingsBtn");
    if (button) button.innerHTML = `${icon("loader-circle", "spinner")}<span>${label}</span>`;
  } else {
    $("#testModelConnectionBtn").innerHTML = `${icon("plug-zap")}<span>测试连接</span>`;
    $("#saveModelSettingsBtn").innerHTML = `${icon("save")}<span>保存配置</span>`;
  }
  hydrateIcons();
}

async function openModelSettings() {
  if (!state.apiOnline) {
    showToast("后端未连接，暂时无法读取模型配置。", "error");
    return;
  }
  try {
    const config = await loadModelConfiguration();
    fillModelSettings(config);
    $("#modelSettingsDialog").showModal();
    hydrateIcons();
  } catch (error) {
    showToast(errorMessage(error), "error");
  }
}

async function testModelConnection() {
  const payload = readModelSettings();
  if (!payload || state.modelSettingsBusy) return;
  setModelSettingsBusy(true, "测试连接中");
  try {
    const result = await api("/api/settings/model/test", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderModelConfigStatus({ ...state.modelConfig, ...payload }, result);
    showToast("模型连接测试通过");
  } catch (error) {
    renderModelConfigStatus({ ...state.modelConfig, ...payload });
    showToast(errorMessage(error), "error");
  } finally {
    setModelSettingsBusy(false);
  }
}

async function saveModelSettings(event) {
  event.preventDefault();
  const payload = readModelSettings();
  if (!payload || state.modelSettingsBusy) return;
  setModelSettingsBusy(true, "保存中");
  try {
    const config = await api("/api/settings/model", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    state.modelConfig = config;
    state.apiMode = "真实模型";
    $("#modelApiKey").value = "";
    $("#modelSettingsDialog").close();
    renderConnection();
    showToast("模型配置已保存并立即生效");
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    setModelSettingsBusy(false);
  }
}

function resetForNewProject() {
  if (state.project && !window.confirm("创建新项目不会删除当前项目，但会从本机工作台移除其快捷入口。是否继续？")) return;
  localStorage.removeItem("testforge.projectId");
  Object.assign(state, {
    projectId: null,
    project: null,
    drafts: { "3": null, "4": null, "5": null },
    draftBaselines: { "3": null, "4": null, "5": null },
    solutionCode: "",
    solutionBaseline: "",
    dirtySolution: false,
    activeStage: 1,
    error: null,
    compileResult: null,
    preview: null,
    previewHistory: [],
    buildResult: null,
    expandedSubtasks: new Set([0]),
    dirtyStages: new Set(),
  });
  render();
}

async function init() {
  $$(".stage-link").forEach((button) => button.addEventListener("click", () => activateStage(Number(button.dataset.stage))));
  $("#stageSelect").addEventListener("change", (event) => activateStage(Number(event.target.value)));
  $("#newProjectBtn").addEventListener("click", resetForNewProject);
  $("#modelSettingsBtn").addEventListener("click", openModelSettings);
  $("#modelSettingsForm").addEventListener("submit", saveModelSettings);
  $("#testModelConnectionBtn").addEventListener("click", testModelConnection);
  $("#closeModelSettingsBtn").addEventListener("click", () => $("#modelSettingsDialog").close());
  $("#cancelModelSettingsBtn").addEventListener("click", () => $("#modelSettingsDialog").close());
  $("#toggleApiKeyBtn").addEventListener("click", () => {
    const input = $("#modelApiKey");
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    $("#toggleApiKeyBtn").setAttribute("title", reveal ? "隐藏 API Key" : "显示 API Key");
    $("#toggleApiKeyBtn").setAttribute("aria-label", reveal ? "隐藏 API Key" : "显示 API Key");
    $("#toggleApiKeyBtn").innerHTML = icon(reveal ? "eye-off" : "eye");
    hydrateIcons();
  });
  $("#refreshBtn").addEventListener("click", async () => {
    await checkHealth();
    if (state.apiOnline) {
      try { await loadModelConfiguration(); } catch { /* keep health state visible */ }
    }
    await refreshProject();
    render();
  });
  await checkHealth();
  if (state.apiOnline) {
    try { await loadModelConfiguration(); } catch { /* health remains authoritative for connectivity */ }
  }
  if (state.projectId) {
    await refreshProject({ silent: true });
    if (state.project) state.activeStage = Number(state.project.current_stage);
  }
  render();
}

window.addEventListener("beforeunload", (event) => {
  if (!state.dirtySolution && state.dirtyStages.size === 0) return;
  event.preventDefault();
  event.returnValue = "";
});

init();
