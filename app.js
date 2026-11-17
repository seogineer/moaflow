const STORAGE_KEY = "moaflow.demo.tasks.v1";
const DEMO_TEXT = [
  "안녕하세요, 팀원 여러분.",
  "내일까지 고객사 견적서 보내줘.",
  "견적서 발송 건 확인 부탁해요.",
  "금요일까지 주간 회의록 작성해 주세요.",
  "주간 회의록 정리 부탁해요.",
  "모레까지 팀 채널에 릴리스 노트 게시해 주세요.",
  "다음 주 월요일까지 로그인 오류 재현 단계 정리가 필요해요.",
  "다음 주 일정은 아직 미정이에요.",
  "오늘 신규 가입자 안내 메일 발송 준비해 주세요.",
  "감사합니다.",
].join("\n");
const PLAN_PREVIEW_STEPS = 8;
const EMPTY_PLAN_HTML = '<div class="empty-state"><div class="empty-orbit">✦</div><strong>업무가 바뀌었어요</strong><span>에이전트 팀 실행을 눌러 계획을 만들어 보세요.</span></div>';

let tasks = loadTasks();
let lastAnalysis = null;
let resetArmed = false;

function isValidTask(item) {
  return item !== null && typeof item === "object"
    && ["id", "title", "source", "due"].every(key => typeof item[key] === "string");
}

function loadTasks() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (!Array.isArray(stored)) return [];
    const seen = new Set();
    return stored.filter(isValidTask).filter(item => {
      if (seen.has(item.id)) return false; // drop duplicate ids after the first
      seen.add(item.id);
      return true;
    }).map(({ id, title, source, due }) => ({ id, title, source, due }));
  } catch (_) {
    return [];
  }
}

function saveTasks() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
  } catch (_) { /* storage may be unavailable; keep working in memory */ }
}

function newId(prefix) {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || Date.now() + Math.random().toString(16).slice(2)}`;
}

function escapeText(node, value) {
  node.textContent = value == null ? "" : String(value);
}

function tasksChanged() {
  resetArmed = false;
  lastAnalysis = null;
  saveTasks();
  document.getElementById("agent-trace").replaceChildren();
  document.getElementById("plan-output").innerHTML = EMPTY_PLAN_HTML;
  render();
}

function setStatus(status) {
  const text = document.getElementById("privacy-text");
  const chip = document.getElementById("engine-chip");
  if (status && status.mode === "configured-llm") {
    chip.textContent = "AI 연결 엔진";
    text.textContent = "AI 연결됨 · 입력 텍스트가 AI 제공자로 전송됨";
  } else if (status && status.mode === "local-demo") {
    chip.textContent = "로컬 데모 엔진";
    text.textContent = "로컬 데모 · 외부 전송 없음";
  } else {
    chip.textContent = "엔진 상태 알 수 없음";
    text.textContent = "서버 상태를 확인할 수 없음";
  }
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error("status failed");
    setStatus(await response.json());
  } catch (_) {
    setStatus(null);
  }
}

function render() {
  const analysisById = new Map(lastAnalysis ? (lastAnalysis.tasks || []).map(task => [task.id, task]) : []);
  const analyzed = lastAnalysis !== null;
  const list = document.getElementById("task-list");
  list.replaceChildren();
  document.getElementById("stat-total").textContent = String(tasks.length);
  document.getElementById("stat-approval").textContent = analyzed
    ? String([...analysisById.values()].filter(t => t.requires_approval).length) : "-";
  document.getElementById("stat-duplicate").textContent = analyzed
    ? String([...analysisById.values()].filter(t => t.duplicate_of).length) : "-";
  document.getElementById("list-count").textContent = String(tasks.length);

  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const title = document.createElement("strong");
    title.textContent = "아직 모은 일이 없어요";
    const hint = document.createElement("span");
    hint.textContent = "원문을 붙여 넣거나 위 입력창에서 첫 할 일을 추가해 주세요.";
    empty.append(title, hint);
    list.append(empty);
  }

  for (const item of tasks) {
    const result = analysisById.get(item.id);
    const row = document.createElement("article");
    row.className = "task-item";
    const main = document.createElement("div");
    main.className = "task-main";
    const title = document.createElement("div");
    title.className = "task-title";
    escapeText(title, item.title);
    const meta = document.createElement("div");
    meta.className = "task-meta";
    meta.append(makeTag(item.source, "source"));
    if (item.due) meta.append(makeTag(`기한 ${item.due}`, "due"));
    if (result && result.requires_approval) meta.append(makeTag("승인 필요", "approval"));
    if (result && result.duplicate_of) meta.append(makeTag("중복 후보", "duplicate"));
    main.append(title, meta);
    const remove = document.createElement("button");
    remove.className = "remove-button";
    remove.type = "button";
    remove.setAttribute("aria-label", `${item.title} 삭제`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      tasks = tasks.filter(task => task.id !== item.id);
      tasksChanged();
    });
    row.append(main, remove);
    list.append(row);
  }
}

function makeTag(text, kind) {
  const tag = document.createElement("span");
  tag.className = `tag ${kind}`;
  tag.textContent = text;
  return tag;
}

function makeStep(item, index, task) {
  const row = document.createElement("div");
  row.className = "plan-step";
  const number = document.createElement("span");
  number.className = "step-number";
  number.textContent = String(index + 1).padStart(2, "0");
  const copy = document.createElement("div");
  copy.className = "step-copy";
  const heading = document.createElement("strong");
  heading.textContent = task ? `${task.title} · ${item.action}` : String(item.action ?? "");
  const reason = document.createElement("p");
  reason.textContent = item.reason || "";
  copy.append(heading, reason);
  row.append(number, copy);
  return row;
}

function renderAnalysis(data) {
  const trace = document.getElementById("agent-trace");
  const output = document.getElementById("plan-output");
  trace.replaceChildren();
  output.replaceChildren();
  const agentTrace = Array.isArray(data.agent_trace) ? data.agent_trace : [];
  agentTrace.forEach((agent, index) => {
    const step = document.createElement("div");
    step.className = "trace-step done";
    const badge = document.createElement("b");
    badge.textContent = String(index + 1).padStart(2, "0");
    const label = document.createElement("span");
    label.textContent = `${agent.name}: ${agent.status}`;
    step.append(badge, label);
    trace.append(step);
  });
  const plan = data.plan || {};
  const summary = document.createElement("p");
  summary.className = "plan-summary";
  summary.textContent = plan.summary || "계획을 만들었습니다.";
  output.append(summary);
  const taskById = new Map((data.tasks || []).map(task => [task.id, task]));
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  steps.forEach((item, index) => {
    const row = makeStep(item, index, taskById.get(item.task_id));
    if (index >= PLAN_PREVIEW_STEPS) {
      row.hidden = true;
      row.dataset.extra = "true";
    }
    output.append(row);
  });
  if (steps.length > PLAN_PREVIEW_STEPS) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "text-button more-button";
    more.textContent = `더 보기 (${steps.length - PLAN_PREVIEW_STEPS}개)`;
    more.addEventListener("click", () => {
      output.querySelectorAll("[data-extra]").forEach(node => { node.hidden = false; });
      more.remove();
    });
    output.append(more);
  }
  if (data.engine_status) setStatus(data.engine_status);
  // The chip shows the engine that built this plan. It can differ from the configured mode after a fallback.
  document.getElementById("engine-chip").textContent = plan.engine === "configured-llm" ? "AI 연결 엔진" : "로컬 데모 엔진";
}

async function runPlan() {
  const button = document.getElementById("run-plan");
  button.disabled = true;
  button.textContent = "에이전트 협업 중…";
  try {
    const response = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tasks }),
    });
    if (!response.ok) throw new Error("분석 요청에 실패했습니다.");
    lastAnalysis = await response.json();
    render();
    renderAnalysis(lastAnalysis);
  } catch (error) {
    const output = document.getElementById("plan-output");
    output.replaceChildren();
    const message = document.createElement("p");
    message.className = "plan-summary";
    message.textContent = "계획을 불러오지 못했어요. 서버가 실행 중인지 확인한 뒤 다시 시도해 주세요.";
    output.append(message);
  } finally {
    button.disabled = false;
    button.innerHTML = '<span class="run-icon">✦</span> 에이전트 팀 실행';
  }
}

async function runExtract() {
  const button = document.getElementById("extract-button");
  const field = document.getElementById("paste-text");
  const note = document.getElementById("extract-note");
  const text = field.value.trim();
  if (!text) {
    note.textContent = "원문을 먼저 붙여 넣어 주세요.";
    return;
  }
  button.disabled = true;
  note.textContent = "추출 중…";
  try {
    const response = await fetch("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, source: document.getElementById("paste-source").value }),
    });
    if (!response.ok) throw new Error("추출 요청에 실패했습니다.");
    const data = await response.json();
    const found = (Array.isArray(data.tasks) ? data.tasks : []).filter(isValidTask);
    if (!found.length) {
      note.textContent = "추출된 할 일이 없어요. 원문에 요청·기한 표현이 있는지 확인해 주세요.";
      return;
    }
    tasks = [...found.map(({ title, source, due }) => ({ id: newId("ext"), title, source, due })), ...tasks];
    note.textContent = `${found.length}개 할 일을 작업함에 추가했어요.`;
    tasksChanged();
  } catch (error) {
    note.textContent = "추출하지 못했어요. 서버가 실행 중인지 확인한 뒤 다시 시도해 주세요.";
  } finally {
    button.disabled = false;
  }
}

document.getElementById("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const titleField = document.getElementById("task-title");
  const title = titleField.value.trim();
  if (!title) return;
  tasks.unshift({
    id: newId("task"),
    title,
    source: document.getElementById("task-source").value,
    due: document.getElementById("task-due").value,
  });
  titleField.value = "";
  document.getElementById("task-due").value = "";
  tasksChanged();
});

document.getElementById("extract-button").addEventListener("click", runExtract);
document.getElementById("run-plan").addEventListener("click", runPlan);
document.getElementById("reset-demo").addEventListener("click", () => {
  const note = document.getElementById("extract-note");
  if (tasks.length && !resetArmed) {
    // First click only warns (in-page text, no browser dialog). Second click clears.
    resetArmed = true;
    note.textContent = `작업함에 ${tasks.length}개 항목이 있어요. 데모 원문 채우기를 한 번 더 누르면 작업함을 비웁니다.`;
    return;
  }
  resetArmed = false;
  tasks = [];
  tasksChanged();
  document.getElementById("paste-text").value = DEMO_TEXT;
  note.textContent = "작업함을 비우고 데모 원문을 채웠어요. 할 일 추출을 눌러 보세요.";
});

loadStatus();
render();
