const projects = {
  tree: {
    title: "树上路径查询",
    constraints: `2 <= N <= 5 * 10^5
1 <= Q <= 5 * 10^5
1 <= w_i <= 10^9
N is an integer
Edges form a tree on vertices 1..N
Each query contains two integers u and v`,
    cases: 12,
    difficulty: "中等",
  },
  string: {
    title: "字符串模式计数",
    constraints: `1 <= N <= 2 * 10^5
S is a string of length N consisting of o and x
1 <= Q <= 2 * 10^5
Each query contains l and r`,
    cases: 10,
    difficulty: "简单",
  },
  graph: {
    title: "稀疏图最短路",
    constraints: `1 <= N <= 10^5
0 <= M <= 2 * 10^5
1 <= W <= 10^9
Graph is undirected
Source vertex is S`,
    cases: 16,
    difficulty: "较难",
  },
};

const agents = [
  "Solution Input Analysis Agent",
  "Data Structure Recognition Agent",
  "Case Planning Agent",
  "Generator Code Agent",
  "Validator Code Agent",
  "Report Agent",
];

const planSeeds = [104729, 104759, 104761, 104773, 104779, 104789, 104801, 104803, 104827, 104831, 104849, 104851, 104869, 104879, 104891, 104911];

const caseCategories = [
  ["normal", "小规模随机树", "基础格式和普通路径查询"],
  ["normal", "中等随机树", "常规结构和重复权值"],
  ["boundary", "N = 2", "最小树结构"],
  ["boundary", "Q = 1", "查询数量边界"],
  ["boundary", "权值全相同", "数值重复模式"],
  ["extreme", "N, Q 接近上限", "极限规模"],
  ["extreme", "链状树", "结构退化"],
  ["extreme", "星状树", "高度不均衡"],
  ["special", "端点集中查询", "反常访问模式"],
  ["special", "同点查询", "特殊路径"],
  ["special", "随机打散编号", "编号扰动"],
  ["normal", "混合分布", "综合覆盖"],
];

const generatorCode = `#include "jngen.h"
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char** argv) {
    parseArgs(argc, argv);
    int n = getOpt("n", 1000);
    int q = getOpt("q", 1000);
    int seed = getOpt("seed", 1);
    rnd.setSeed(seed);

    cout << n << ' ' << q << '\\n';
    auto weights = Array::random(n, 1, 1000000000);
    cout << weights << '\\n';

    Tree t = Tree::random(n);
    cout << t.add1() << '\\n';

    for (int i = 0; i < q; ++i) {
        cout << rnd.next(1, n) << ' ' << rnd.next(1, n) << '\\n';
    }
}`;

const validatorCode = `#include "testlib.h"
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char** argv) {
    registerValidation(argc, argv);
    int N = inf.readInt(2, 500000, "N");
    inf.readSpace();
    int Q = inf.readInt(1, 500000, "Q");
    inf.readEoln();

    for (int i = 1; i <= N; ++i) {
        inf.readLong(1, 1000000000, "w_i");
        if (i == N) inf.readEoln();
        else inf.readSpace();
    }

    for (int i = 0; i < N - 1; ++i) {
        int u = inf.readInt(1, N, "u");
        inf.readSpace();
        int v = inf.readInt(1, N, "v");
        inf.readEoln();
        ensuref(u != v, "self-loop is not allowed");
    }

    for (int i = 0; i < Q; ++i) {
        inf.readInt(1, N, "u");
        inf.readSpace();
        inf.readInt(1, N, "v");
        inf.readEoln();
    }
    inf.readEof();
}`;

const state = {
  project: "tree",
  generated: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2200);
}

function setActiveTab(tabId) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === tabId));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === tabId));
}

function loadProject(projectId) {
  const project = projects[projectId];
  state.project = projectId;
  $("#projectTitle").textContent = project.title;
  $("#constraintsInput").value = project.constraints;
  $("#caseCountInput").value = String(project.cases);
  $("#difficultySelect").value = project.difficulty;
  $$(".project-item").forEach((item) => item.classList.toggle("active", item.dataset.project === projectId));
  renderCasePlan();
  updateReport();
  showToast(`已切换到：${project.title}`);
}

function validateCaseCount() {
  const input = $("#caseCountInput");
  const hint = $("#caseCountHint");
  const value = Number(input.value.trim());
  const valid = Number.isInteger(value) && value > 0 && value <= 50;
  input.style.borderColor = valid ? "var(--line)" : "var(--red)";
  hint.textContent = valid ? "请输入正整数。" : "请输入 1 到 50 之间的正整数。";
  hint.style.color = valid ? "var(--muted)" : "var(--red)";
  return valid;
}

function getCaseCount() {
  if (!validateCaseCount()) return 0;
  return Number($("#caseCountInput").value.trim());
}

function renderAgents(activeIndex = -1, completed = false) {
  const list = $("#agentList");
  list.innerHTML = agents
    .map((name, index) => {
      const rowClass = completed || index < activeIndex ? "done" : index === activeIndex ? "running" : "";
      const stateText = completed || index < activeIndex ? "完成" : index === activeIndex ? "运行中" : "等待";
      return `<div class="agent-row ${rowClass}">
        <strong>${name}</strong>
        <span class="agent-state">${stateText}</span>
      </div>`;
    })
    .join("");
}

function runWorkflow() {
  setActiveTab("workflow");
  const badge = $("#workflowBadge");
  let index = 0;
  badge.textContent = "运行中";
  badge.className = "badge warn";
  renderAgents(index);
  const timer = window.setInterval(() => {
    index += 1;
    if (index >= agents.length) {
      window.clearInterval(timer);
      renderAgents(agents.length, true);
      badge.textContent = "分析完成";
      badge.className = "badge good";
      showToast("AI 分析已生成测试点计划草稿");
      setActiveTab("plan");
      return;
    }
    renderAgents(index);
  }, 420);
}

function getPlanRows() {
  const count = Math.max(getCaseCount(), 1);
  return Array.from({ length: count }, (_, index) => {
    const template = caseCategories[index % caseCategories.length];
    return {
      id: index + 1,
      category: template[0],
      scale: template[1],
      target: template[2],
      seed: planSeeds[index % planSeeds.length],
      status: state.generated ? "valid" : "planned",
    };
  });
}

function renderCasePlan() {
  const body = $("#casePlanBody");
  body.innerHTML = getPlanRows()
    .map(
      (row) => `<tr>
        <td>${row.id}</td>
        <td><span class="pill ${row.category}">${row.category}</span></td>
        <td>${row.scale}</td>
        <td>${row.target}</td>
        <td>${row.seed}</td>
        <td>${row.status === "valid" ? "已通过 validator" : "计划中"}</td>
      </tr>`,
    )
    .join("");
}

function updateReport() {
  $("#reportCases").textContent = String(getCaseCount() || projects[state.project].cases);
}

function exportMockPackage() {
  const project = projects[state.project];
  const payload = {
    project: project.title,
    constraints: $("#constraintsInput").value,
    caseCount: $("#caseCountInput").value,
    difficulty: $("#difficultySelect").value,
    files: ["1.in", "1.out", "generator.cpp", "validator.cpp", "solution.cpp", "metadata.json", "report.md"],
    note: "Frontend-only demo export. No backend, database, or Docker execution.",
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "metadata-demo.json";
  anchor.click();
  URL.revokeObjectURL(url);
  showToast("已导出 mock metadata 文件");
}

function wireEvents() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => setActiveTab(tab.dataset.tab)));
  $$(".project-item").forEach((item) => item.addEventListener("click", () => loadProject(item.dataset.project)));
  $("#caseCountInput").addEventListener("input", () => {
    validateCaseCount();
    renderCasePlan();
    updateReport();
  });
  $("#difficultySelect").addEventListener("change", () => {
    showToast(`难度等级已设为：${$("#difficultySelect").value}`);
    renderCasePlan();
  });
  $("#analyzeBtn").addEventListener("click", runWorkflow);
  $("#compileBtn").addEventListener("click", () => showToast("标程编译通过（前端模拟）"));
  $("#formatBtn").addEventListener("click", () => showToast("已格式化代码区域（前端模拟）"));
  $("#saveConfigBtn").addEventListener("click", () => {
    if (!validateCaseCount()) return;
    showToast("输入配置已保存到当前页面状态");
  });
  $("#generateBtn").addEventListener("click", () => {
    if (!validateCaseCount()) return;
    state.generated = true;
    renderCasePlan();
    updateReport();
    showToast("测试数据已生成并通过校验（前端模拟）");
  });
  $("#exportBtn").addEventListener("click", exportMockPackage);
  $("#refreshBtn").addEventListener("click", () => showToast("状态已刷新"));
  $("#newProjectBtn").addEventListener("click", () => showToast("Demo 暂使用左侧示例项目"));
}

function init() {
  $("#generatorPreview").textContent = generatorCode;
  $("#validatorPreview").textContent = validatorCode;
  renderAgents();
  renderCasePlan();
  updateReport();
  wireEvents();
}

init();
