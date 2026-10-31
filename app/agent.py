"""상담 에이전트 코어: 세션 관리 + LLM 툴 루프."""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import action_api, catalog, llm, router

ROOT = Path(__file__).resolve().parent.parent
MAX_TOOL_ROUNDS = 8
MAX_REPLY_CHARS = 480  # 500자 초과 시 평가 제외 → 여유 두고 자름

DOC_TOOL = {
    "type": "function",
    "function": {
        "name": "read_doc",
        "description": "조합 공식 문서 원문을 읽는다. name은 '# 조합 공식 문서 목록'의 파일명. 운영 지식에 없는 정책·수치를 판단할 때만 호출.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "call_api",
        "description": "교동몰 액션 API 호출. path는 실제 값을 채운 경로(쿼리스트링 제외), "
                       "검색 API는 query에 파라미터 하나만 넣는다. 접수(POST)는 정책 확인 후에만 호출.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST"]},
                "path": {"type": "string", "description": "예: /api/orders/100100003"},
                "query": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "object"},
            },
            "required": ["method", "path"],
        },
    },
}]

URL_RE = re.compile(r"https?://\S+")
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".mp4")
TC_LINE_RE = re.compile(r"(?im)^\s*[-*]?\s*(?:id|tc[ _-]?id|test[ _-]?case[ _-]?id)\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]{0,99})\s*$")
META_RE = {
    "inbound_at": re.compile(r"상담\s*인입\s*일시\s*[:：]\s*(.+)"),
    "channel": re.compile(r"인입\s*창구\s*[:：]\s*(.+)"),
    "account": re.compile(r"인입\s*계정\s*[:：]\s*(.+)"),
}


def docs_mode():
    import os
    return os.getenv("DOCS_MODE", "index")  # index: 목록만 넣고 read_doc 도구로 / full: 원문 전체를 프롬프트에


def doc_index():
    d = ROOT / "docs"
    out = []
    for p in sorted(d.glob("*.md")):
        x = p.read_text(encoding="utf-8")
        if len(x) <= 300:
            continue
        first = next((l.strip("# ").strip() for l in x.splitlines() if l.strip()), "")
        out.append(f"- {p.name} — {first[:60]} ({len(x)}자)")
    return "\n".join(out)


def read_doc(name):
    d = ROOT / "docs"
    p = d / name
    if not p.exists():
        cands = [q for q in d.glob("*.md") if name.replace(".md", "") in q.name]
        if not cands:
            return "문서 없음. 목록의 파일명을 정확히 쓸 것."
        p = cands[0]
    x = p.read_text(encoding="utf-8")
    return x[:20000]


def load_docs():
    d = ROOT / "docs"
    return "\n\n".join(x for x in (p.read_text(encoding="utf-8") for p in sorted(d.glob("*.md"))) if len(x) > 300) if d.exists() else ""


def load_text(name):
    p = ROOT / "config" / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


@dataclass
class Session:
    sid: str
    tc_id: str | None = None
    meta: dict = field(default_factory=dict)
    history: list = field(default_factory=list)   # openai messages (system 제외)
    trace: list = field(default_factory=list)     # API 호출 기록
    turns: list = field(default_factory=list)     # (user, reply, ms, chars)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def parse_incoming(text: str):
    """고객 메시지에서 TC ID·메타·첨부(이미지/음성)를 분리."""
    tc_id = None
    m = TC_LINE_RE.search(text)
    if m:
        tc_id = m.group(1)
    meta = {}
    for k, rx in META_RE.items():
        mm = rx.search(text)
        if mm:
            meta[k] = mm.group(1).strip()
    images, audios = [], []
    for u in URL_RE.findall(text):
        low = u.lower().split("?")[0]
        if low.endswith(IMG_EXT):
            images.append(u)
        elif low.endswith(AUDIO_EXT):
            audios.append(u)
    # 본문: 메타/URL 라인 제거
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if TC_LINE_RE.match(line) or any(rx.search(s) for rx in META_RE.values()):
            continue
        if s.lower().startswith("case metadata"):
            continue
        if re.match(r"^(image|audio|이미지|음성)\s*[:：]", s, re.I):
            continue
        keep.append(line)
    body = "\n".join(keep)
    body = URL_RE.sub("", body).strip()
    return tc_id, meta, body, images, audios


def system_prompt(s: Session):
    ctx = [
        f"- TC ID: {s.tc_id or '(미확인)'} — API 헤더에 자동 포함됨",
        f"- 상담 인입 일시(=오늘): {s.meta.get('inbound_at', '(미확인)')}",
        f"- 인입 창구: {s.meta.get('channel', '(미확인)')}",
        f"- 인입 계정: {s.meta.get('account', '(미확인)')}",
    ]
    return "\n\n".join([
        load_text("system_prompt.md"),
        "# 운영 지식\n" + load_text("knowledge.md"),
        ("# 조합 공식 문서 (관계자에게 받은 원문 — 정책 판단의 최우선 근거)\n" + load_docs()) if docs_mode() == "full" else
        ("# 조합 공식 문서 목록 (원문은 read_doc 도구로 읽기. 운영 지식에 답이 있으면 읽지 말 것)\n" + doc_index()),
        "# 사용 가능한 API (id method path — 설명, *=필수)\n" + catalog.catalog_text(),
        "# 현재 상담 컨텍스트\n" + "\n".join(ctx),
    ])


def clean_reply(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    if len(t) > MAX_REPLY_CHARS:
        cut = t[:MAX_REPLY_CHARS]
        idx = max(cut.rfind(". "), cut.rfind("다."), cut.rfind("요."), cut.rfind("\n"))
        t = cut[: idx + 2].strip() if idx > 200 else cut.strip()
    return t or "확인 후 다시 안내드리겠습니다."


async def _run_tool(s: Session, args: dict):
    method = str(args.get("method", "GET")).upper()
    path = str(args.get("path", "")).split("?")[0]
    query = args.get("query") or None
    body = args.get("body")
    ep, err = catalog.match(method, path, query)
    rec = {"method": method, "path": path, "query": query, "body": body, "api": ep["id"] if ep else None}
    if err:
        rec.update(status="BLOCKED", ms=0, result=err)
        s.trace.append(rec)
        return f"호출 안 됨: {err}"
    if not s.tc_id:
        rec.update(status="NO_TC", ms=0, result="TC ID 미확인")
        s.trace.append(rec)
        return "호출 안 됨: 상담 TC ID가 확인되지 않았습니다."
    res = await action_api.call(method, path, query, body if method != "GET" else None, s.tc_id)
    out = action_api.describe_for_llm(res)
    rec.update(status=res["status"], ms=res["ms"], result=out[:1500])
    s.trace.append(rec)
    return out


async def handle(s: Session, raw_text: str):
    """고객 메시지 1건 처리 → dict(reply, ms, new_trace)"""
    t0 = time.perf_counter()
    trace_start = len(s.trace)
    tc_id, meta, body, images, audios = parse_incoming(raw_text)
    if tc_id and tc_id != s.tc_id:
        if s.tc_id is not None:  # 새 케이스 → 세션 초기화
            s.history.clear(); s.trace.clear(); s.turns.clear(); s.meta.clear()
            trace_start = 0
        s.tc_id = tc_id
    s.meta.update(meta)

    text_part = body or ""
    if audios:
        trs = await asyncio.gather(*(llm.transcribe(u) for u in audios))
        text_part += "".join(f"\n[고객 음성 메시지 전사] {t}" for t in trs)
    if images:
        text_part += f"\n[고객이 사진 {len(images)}장 첨부]"
    if not text_part.strip():
        text_part = "(내용 없음)"
    if images:
        content = [{"type": "text", "text": text_part}] + [
            {"type": "image_url", "image_url": {"url": u}} for u in images]
    else:
        content = text_part
    route = await router.decide(text_part, bool(images), s.history)
    target = None
    if route:
        target = (route["provider"], route["model"])
        s.trace.append({"method": "ROUTE", "path": f"{route['provider']}/{route['model']}", "api": route["route"],
                        "status": 200, "ms": route["ms"],
                        "result": f"source={route['source']} confidence={route['confidence']}"})
    s.history.append({"role": "user", "content": content})

    reply = None
    for _ in range(MAX_TOOL_ROUNDS):
        msgs = [{"role": "system", "content": system_prompt(s)}] + s.history
        try:
            msg, target = await router.chat(msgs, TOOLS + ([DOC_TOOL] if docs_mode() != "full" else []), target, s.trace)
        except Exception as e:
            reply = "죄송합니다. 잠시 확인이 지연되고 있어요. 한 번만 다시 말씀해 주시겠어요?"
            s.trace.append({"method": "LLM", "path": "", "status": "ERR", "ms": 0, "result": str(e)[:500]})
            break
        tcs = msg.get("tool_calls") or []
        s.history.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls") and v is not None})
        if not tcs:
            reply = msg.get("content")
            break
        async def one(tc):
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            if tc["function"].get("name") == "read_doc":
                out = read_doc(str(args.get("name", "")))
                s.trace.append({"method": "DOC", "path": str(args.get("name", "")), "api": "read_doc", "status": 200, "ms": 0, "result": out[:200]})
                return tc["id"], out
            return tc["id"], await _run_tool(s, args)
        results = await asyncio.gather(*(one(tc) for tc in tcs))
        for tid, out in results:
            s.history.append({"role": "tool", "tool_call_id": tid, "content": out})
    else:
        reply = "확인에 시간이 걸리고 있어요. 담당자 확인 후 다시 안내드리겠습니다."

    reply = clean_reply(reply)
    if s.history and s.history[-1].get("role") == "assistant" and not s.history[-1].get("tool_calls"):
        s.history[-1]["content"] = reply
    else:
        s.history.append({"role": "assistant", "content": reply})
    ms = int((time.perf_counter() - t0) * 1000)
    s.turns.append({"user": text_part, "reply": reply, "ms": ms, "chars": len(reply)})
    return {"reply": reply, "ms": ms, "chars": len(reply), "trace": s.trace[trace_start:],
            "tc_id": s.tc_id, "meta": s.meta, "route": route, "answered_by": "/".join(target) if target else None}
