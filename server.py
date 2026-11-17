#!/usr/bin/env python3
"""MoaFlow: local task-triage PoC with an optional OpenAI-compatible planner."""
from __future__ import annotations

import json
import os
import re
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 64_000
MAX_TASKS = 80
MAX_EXTRACT_CHARS = 8_000
MAX_LLM_RESPONSE_BYTES = 256 * 1024
ALLOWED_STATIC_PATHS = frozenset({"/", "/index.html", "/app.js", "/styles.css", "/docs/plan.md"})
STATUS_PATH = "/api/status"

# Single source of truth for approval keywords. ASCII terms match on word
# boundaries (so "recall", "facebook", "postmortem" do not match). Korean terms
# match as substrings. "메일" alone is not a keyword: see MAIL_ACTION_PATTERN.
APPROVAL_TERMS = (
    "send", "email", "publish", "post", "submit", "pay", "payment", "purchase", "delete", "call", "book",
    "remove", "transfer", "refund", "share", "wire",
    "보내", "발송", "전달", "게시", "업로드", "공개", "제출", "결제", "구매", "삭제", "전화", "예약 확정",
    "지우", "제거", "송금", "이체", "환불", "공유", "초대",
)
MAIL_ACTION_PATTERN = re.compile(r"메일\s*(?:보내|발송|전송)|메일(?:로|을)")


def _build_ascii_pattern(term: str) -> re.Pattern[str]:
    forms = [f"{term}(?:s|es|d|ed|ing)?"]
    if term.endswith("e"):
        forms.append(f"{term[:-1]}ing")
    # Hyphen counts as part of a word, so "post-mortem" does not match "post".
    return re.compile(r"(?<![\w-])(?:" + "|".join(forms) + r")(?![\w-])", re.I)


_ASCII_PATTERNS = [(t, _build_ascii_pattern(t)) for t in APPROVAL_TERMS if t.isascii()]
_KOREAN_TERMS = [t for t in APPROVAL_TERMS if not t.isascii()]

SOURCE_VALUES = {"슬랙", "노션", "깃허브", "메신저", "기타"}
STOP_WORDS = {"하기", "진행", "확인", "관련", "자료", "업무", "작업", "해주세요", "부탁", "정리"}
# Words in one set are normalized to the first word of that set.
SYNONYM_SETS = (
    ("발송", "보내", "전달"),
    ("견적", "견적서"),
    ("초안", "정리", "작성"),
)
TASK_PHRASES = (
    "해주세요", "해 주세요", "해줘", "주세요", "부탁", "까지", "필요", "확인", "작성", "정리", "준비",
    "보내", "발송", "제출", "공유", "요청", "please", "need", "todo",
)
GREETING_PREFIX = re.compile(r"^(?:안녕하세요|안녕|감사합니다|수고하세요|고생하셨습니다)[\s,!.]*")
MIN_PIECE_CHARS = 6
WEEKDAYS = "월화수목금토일"
DUPLICATE_REASON = "비슷한 제목의 업무가 이미 있어 중복 진행을 막습니다."
APPROVAL_ACTION = "초안만 준비하고 승인 대기"


def _clean_text(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def normalize_tasks(raw_tasks: Any) -> list[dict[str, str]]:
    if not isinstance(raw_tasks, list):
        return []
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_tasks[:MAX_TASKS]):
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"), 240)
        if not title:
            continue
        source = _clean_text(item.get("source"), 24)
        if source not in SOURCE_VALUES:
            source = "기타"
        due = _clean_text(item.get("due"), 10)
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            due = ""
        base_id = _clean_text(item.get("id"), 80) or f"task-{index + 1}"
        task_id = base_id
        suffix = 2
        while task_id in seen_ids:
            task_id = f"{base_id}-{suffix}"
            suffix += 1
        seen_ids.add(task_id)
        result.append({"id": task_id, "title": title, "source": source, "due": due})
    return result


def _canonical(token: str) -> str:
    for words in SYNONYM_SETS:
        for word in words:
            if token == word or token.startswith(word):
                return words[0]
    return token


def _tokens(text: str) -> set[str]:
    pieces = re.findall(r"[a-zA-Z0-9가-힣]{2,}", text.lower())
    mapped = {_canonical(piece) for piece in pieces}
    return {piece for piece in mapped if piece not in STOP_WORDS}


def deduplicate_agent(tasks: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Flag likely duplicates without deleting or merging user-entered tasks."""
    result: list[dict[str, Any]] = []
    for task in tasks:
        tokens = _tokens(task["title"])
        duplicate_of = None
        for previous in result:
            previous_tokens = _tokens(previous["title"])
            if not tokens or not previous_tokens:
                continue
            overlap = len(tokens & previous_tokens)
            jaccard = overlap / len(tokens | previous_tokens)
            containment = overlap / min(len(tokens), len(previous_tokens))
            if jaccard >= 0.38 or (containment >= 0.75 and overlap >= 2):
                duplicate_of = previous["id"]
                break
        result.append({**task, "duplicate_of": duplicate_of})
    return result


def find_approval_term(title: str) -> str | None:
    lower = title.lower()
    for term, pattern in _ASCII_PATTERNS:
        if pattern.search(lower):
            return term
    for term in _KOREAN_TERMS:
        if term in lower:
            return term
    match = MAIL_ACTION_PATTERN.search(lower)
    return match.group(0) if match else None


def safety_agent(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify tasks that need human approval."""
    result = []
    for task in tasks:
        match = find_approval_term(task["title"])
        result.append({
            **task,
            "requires_approval": bool(match),
            "approval_reason": f"외부 실행 표현('{match}')이 있어 사람 확인이 필요합니다." if match else "조회·초안 등 되돌릴 수 있는 작업으로 분류했습니다.",
        })
    return result


def safety_enforce(tasks: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    """Final authority: runs after the Planner, whatever engine produced the plan."""
    by_id = {task["id"]: task for task in tasks}
    steps = []
    for step in plan["steps"]:
        task = by_id.get(step["task_id"])
        step = dict(step)
        if task and task.get("requires_approval"):
            step["action"] = APPROVAL_ACTION
            reason = step.get("reason") or ""
            if task.get("duplicate_of") and DUPLICATE_REASON not in reason:
                reason = f"{reason} {DUPLICATE_REASON}".strip()
            if task["approval_reason"] not in reason:
                reason = f"{reason} {task['approval_reason']}".strip()
            step["reason"] = reason
        steps.append(step)
    return {**plan, "steps": steps}


def _kst_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return date.today()


def _local_step(task: dict[str, Any]) -> tuple[str, str]:
    if task.get("duplicate_of"):
        action = "중복 확인 후 대표 항목에 합치기"
        reason = DUPLICATE_REASON
        if task.get("requires_approval"):
            reason = f"{reason} {task['approval_reason']}"
        return action, reason
    if task.get("requires_approval"):
        return APPROVAL_ACTION, task["approval_reason"]
    if task.get("due"):
        return "다음 실행 단계부터 처리", f"마감일 {task['due']} 기준으로 우선순위를 정했습니다."
    return "담당·기한을 확인하고 작은 단계로 나누기", "기한 정보가 없어 먼저 범위를 구체화합니다."


def _summary(tasks: list[dict[str, Any]]) -> str:
    return (f"{len(tasks)}개 항목을 정리했습니다. 중복 후보 {sum(bool(t.get('duplicate_of')) for t in tasks)}개, "
            f"승인 확인 필요 {sum(bool(t.get('requires_approval')) for t in tasks)}개입니다.")


def planner_agent(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    today = _kst_today()

    def key(task: dict[str, Any]) -> tuple[int, str, str]:
        due = task.get("due") or "9999-12-31"
        days = 99
        if task.get("due"):
            try:
                days = (date.fromisoformat(due) - today).days
            except ValueError:
                pass
        urgency = 0 if days <= 1 else 1 if days <= 3 else 2
        return (urgency, due, task["title"].lower())

    steps = []
    for index, task in enumerate(sorted(tasks, key=key)):
        action, reason = _local_step(task)
        steps.append({"task_id": task["id"], "order": index + 1, "action": action, "reason": reason})
    return {"summary": _summary(tasks), "steps": steps, "engine": "local-demo"}


# ---- optional OpenAI-compatible LLM ---------------------------------------

def _llm_config() -> tuple[str, str, str] | None:
    """Return (api_key, model, base_url) only if all three variables are set."""
    api_key = os.environ.get("AI_API_KEY", "").strip()
    model = os.environ.get("AI_MODEL", "").strip()
    base_url = os.environ.get("AI_BASE_URL", "").strip().rstrip("/")
    if api_key and model and base_url:
        return api_key, model, base_url
    return None


def engine_status() -> dict[str, Any]:
    configured = _llm_config() is not None
    return {"mode": "configured-llm" if configured else "local-demo", "sends_text_to_provider": configured}


def _llm_json(config: tuple[str, str, str], system: str, user: dict[str, Any]) -> Any:
    """Call the chat endpoint and return the parsed JSON content. Raises on any problem."""
    api_key, model, base_url = config
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(MAX_LLM_RESPONSE_BYTES + 1)
    if len(raw) > MAX_LLM_RESPONSE_BYTES:
        raise ValueError("LLM response too large")
    data = json.loads(raw.decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I)
    return json.loads(text)


def _try_llm_plan(tasks: list[dict[str, Any]], local_plan: dict[str, Any]) -> dict[str, Any]:
    """Use an explicitly configured endpoint; never persist its key. Any problem falls back."""
    config = _llm_config()
    if config is None:
        return local_plan
    system = (
        "당신은 MoaFlow의 Planner Agent입니다. 입력된 업무 목록만 사용해 우선순위와 다음 행동을 한국어로 제안하세요. "
        "외부 행동(메일/게시/제출/결제/전화/삭제)은 절대 수행하지 말고 승인 대기라고 표시하세요. "
        "반드시 JSON 객체로만 답하세요: {\"summary\": string, \"steps\": [{\"task_id\": string, \"action\": string, \"reason\": string}]}"
    )
    try:
        candidate = _llm_json(config, system, {"today_kst": _kst_today().isoformat(), "tasks": tasks})
        if not isinstance(candidate, dict) or not isinstance(candidate.get("steps"), list):
            return local_plan
        valid_ids = {task["id"] for task in tasks}
        local_by_id = {step["task_id"]: step for step in local_plan["steps"]}
        chosen: dict[str, dict[str, Any]] = {}
        for item in candidate["steps"]:
            if not isinstance(item, dict):
                continue
            task_id = item.get("task_id")
            if not isinstance(task_id, str) or task_id not in valid_ids or task_id in chosen:
                continue
            local_step = local_by_id[task_id]
            chosen[task_id] = {
                "task_id": task_id,
                "action": _clean_text(item.get("action"), 180) or local_step["action"],
                "reason": _clean_text(item.get("reason"), 240) or local_step["reason"],
            }
        if tasks and not chosen:
            return local_plan
        steps = list(chosen.values())
        for step in local_plan["steps"]:  # fill in anything the LLM skipped
            if step["task_id"] not in chosen:
                steps.append({"task_id": step["task_id"], "action": step["action"], "reason": step["reason"]})
        for index, step in enumerate(steps):
            step["order"] = index + 1
        summary = _clean_text(candidate.get("summary"), 300) if isinstance(candidate.get("summary"), str) else ""
        return {"summary": summary or local_plan["summary"], "steps": steps, "engine": "configured-llm"}
    except Exception:
        return local_plan


def build_plan(raw_tasks: Any) -> dict[str, Any]:
    tasks = normalize_tasks(raw_tasks)                  # Intake
    deduped = deduplicate_agent(tasks)                  # Deduplication
    guarded = safety_agent(deduped)                     # approval flags the Planner may read
    plan = safety_enforce(guarded, _try_llm_plan(guarded, planner_agent(guarded)))  # Planner, then Safety
    return {
        "tasks": guarded,
        "plan": plan,
        "engine_status": engine_status(),
        "agent_trace": [
            {"name": "Intake", "status": f"{len(tasks)}개 항목 정리"},
            {"name": "Deduplication", "status": f"{sum(bool(t['duplicate_of']) for t in guarded)}개 중복 후보 표시"},
            {"name": "Planner", "status": "LLM 설정" if plan["engine"] == "configured-llm" else "로컬 데모 규칙"},
            {"name": "Safety", "status": f"{sum(bool(t['requires_approval']) for t in guarded)}개 승인 필요로 확정, 계획 후처리 적용"},
        ],
    }


# ---- extraction -------------------------------------------------------------

def _next_weekday(today: date, weekday: int, this_week: bool) -> date:
    if this_week:
        return today - timedelta(days=today.weekday()) + timedelta(days=weekday)
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def parse_due(text: str, today: date | None = None) -> str:
    """Return an ISO date found in text, or an empty string."""
    today = today or _kst_today()
    try:
        match = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", text)
        if match:
            return date(*map(int, match.groups())).isoformat()
        match = re.search(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일", text) or re.search(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", text)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            found = date(today.year, month, day)
            if found < today:
                found = date(today.year + 1, month, day)
            return found.isoformat()
    except ValueError:
        return ""
    match = re.search(r"다음\s*주\s*([월화수목금토일])요일", text)
    if match:
        return (_next_weekday(today, WEEKDAYS.index(match.group(1)), True) + timedelta(days=7)).isoformat()
    match = re.search(r"이번\s*주\s*([월화수목금토일])요일", text)
    if match:
        found = _next_weekday(today, WEEKDAYS.index(match.group(1)), True)
        return found.isoformat() if found >= today else ""  # never return a past date
    match = re.search(r"([월화수목금토일])요일", text)
    if match:
        return _next_weekday(today, WEEKDAYS.index(match.group(1)), False).isoformat()
    for word, days in (("모레", 2), ("내일", 1), ("오늘", 0)):
        if word in text:
            return (today + timedelta(days=days)).isoformat()
    return ""


def extract_tasks_local(text: str, source: str = "기타", today: date | None = None) -> list[dict[str, str]]:
    today = today or _kst_today()
    source = source if source in SOURCE_VALUES else "기타"
    tasks: list[dict[str, str]] = []
    for line in text.splitlines():
        for piece in re.split(r"(?<=。)|(?<=[.?!])(?:\s+|$)", line):
            piece = GREETING_PREFIX.sub("", (piece or "").strip()).strip(" \t.。?!,")
            piece = re.sub(r"\s+", " ", piece)
            if len(piece) < MIN_PIECE_CHARS:
                continue
            lower = piece.lower()
            if not any(phrase in lower for phrase in TASK_PHRASES):
                continue
            tasks.append({"id": f"ext-{len(tasks) + 1}", "title": piece[:240], "source": source, "due": parse_due(piece, today)})
            if len(tasks) >= MAX_TASKS:
                return tasks
    return tasks


def _try_llm_extract(text: str, source: str) -> list[dict[str, str]] | None:
    config = _llm_config()
    if config is None:
        return None
    system = (
        "당신은 MoaFlow의 Intake Agent입니다. 사용자가 붙여 넣은 원문에서 할 일만 추출하세요. 인사말은 제외하세요. "
        "반드시 JSON 객체로만 답하세요: {\"tasks\": [{\"title\": string, \"due\": \"YYYY-MM-DD 또는 빈 문자열\"}]}"
    )
    try:
        candidate = _llm_json(config, system, {"today_kst": _kst_today().isoformat(), "text": text})
        if not isinstance(candidate, dict) or not isinstance(candidate.get("tasks"), list):
            return None
        items = [{"title": item["title"], "due": item.get("due", ""), "source": source}
                 for item in candidate["tasks"] if isinstance(item, dict) and isinstance(item.get("title"), str)]
        items = [{**item, "due": item["due"] if isinstance(item["due"], str) else ""} for item in items]
        tasks = normalize_tasks(items)
        if not tasks:
            return None
        return [{**task, "id": f"ext-{index + 1}"} for index, task in enumerate(tasks)]
    except Exception:
        return None


def extract_tasks(text: Any, source: Any = "기타") -> dict[str, Any]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    text = text[:MAX_EXTRACT_CHARS]
    source = source if isinstance(source, str) and source in SOURCE_VALUES else "기타"
    tasks = _try_llm_extract(text, source)
    if tasks is not None:
        return {"tasks": tasks, "engine": "configured-llm"}
    return {"tasks": extract_tasks_local(text, source), "engine": "local-demo"}


# ---- HTTP ---------------------------------------------------------------------

CLIENT_GONE = (BrokenPipeError, ConnectionResetError, TimeoutError)


class Handler(SimpleHTTPRequestHandler):
    timeout = 10  # seconds; a stalled client cannot hold a thread forever

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _static_path(self) -> str | None:
        path = urlsplit(self.path).path
        if path not in ALLOWED_STATIC_PATHS:
            return None
        return "/index.html" if path == "/" else path

    def do_GET(self) -> None:
        if urlsplit(self.path).path == STATUS_PATH:
            self._send_json(engine_status())
            return
        path = self._static_path()
        if path is None:
            self.send_error(404, "Not found")
            return
        self.path = path
        super().do_GET()

    def do_HEAD(self) -> None:
        if urlsplit(self.path).path == STATUS_PATH:
            encoded = json.dumps(engine_status(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        path = self._static_path()
        if path is None:
            self.send_error(404, "Not found")
            return
        self.path = path
        super().do_HEAD()

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlsplit(origin).netloc == self.headers.get("Host", "\0") and urlsplit(origin).scheme in ("http", "https")

    def _drain(self, length: int) -> None:
        remaining = min(length, 4 * 1024 * 1024)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route not in ("/api/plan", "/api/extract"):
            self.send_error(404, "Not found")
            return
        try:
            if not self._same_origin():
                self.send_error(403, "Cross-origin request rejected")
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error(415, "Content-Type must be application/json")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                self.send_error(400, "Invalid Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._drain(length)
                self.send_error(413, "Request too large")
                return
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if route == "/api/plan":
                result = build_plan(body.get("tasks", []))
            else:
                result = extract_tasks(body.get("text", ""), body.get("source", "기타"))
            self._send_json(result)
        except CLIENT_GONE:
            self.close_connection = True  # client left or stalled; nothing to answer
        except (ValueError, AttributeError, TypeError, RecursionError):
            self._safe_error(400, "Invalid request payload")
        except Exception:
            traceback.print_exc()  # once; the handler does not re-raise
            self._safe_error(500, "Internal error")

    def _safe_error(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except CLIENT_GONE:
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request lines only; task text is in the body and is never logged.
        super().log_message(fmt, *args)


def main() -> None:
    host = os.environ.get("MOAFLOW_HOST", "127.0.0.1")
    port = int(os.environ.get("MOAFLOW_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    status = engine_status()
    print(f"MoaFlow PoC running at http://{host}:{port}")
    if status["sends_text_to_provider"]:
        print("Mode: configured-llm. Input text IS SENT to the configured AI provider.")
    else:
        print("Mode: local-demo. No input text is sent to any external service.")
        print("(Set AI_API_KEY, AI_MODEL, and AI_BASE_URL together to enable an AI provider.)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
