"""LLM 호출 래퍼 — Anthropic Messages API 전용 (모델 기본: Sonnet).

에이전트 내부 대화 기록은 OpenAI 형식(role/content/tool_calls/tool)으로 유지하고,
호출 직전에 Anthropic 형식으로 변환한다. 반환도 OpenAI 형식 dict로 맞춘다.
"""
import json
import os

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1"  # 음성 전사(선택)용
_client: httpx.AsyncClient | None = None

# router.py / main.py 호환용
PROVIDERS = {"anthropic": (ANTHROPIC_URL, "ANTHROPIC_API_KEY", "claude-sonnet-5-5")}


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=45.0)
    return _client


def provider():
    return "anthropic"


def model_name():
    m = os.getenv("LLM_MODEL", "")
    return m if m.startswith("claude") else PROVIDERS["anthropic"][2]


# ---------- OpenAI 형식 → Anthropic 형식 ----------
def _user_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content or "(내용 없음)"}]
    out = []
    for p in content:
        if p.get("type") == "text":
            out.append({"type": "text", "text": p.get("text") or " "})
        elif p.get("type") == "image_url":
            out.append({"type": "image", "source": {"type": "url", "url": p["image_url"]["url"]}})
    return out or [{"type": "text", "text": "(내용 없음)"}]


def _convert(messages):
    system, out = "", []

    def push(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        r = m["role"]
        if r == "system":
            system += (m["content"] if isinstance(m["content"], str) else "") + "\n"
        elif r == "user":
            push("user", _user_blocks(m.get("content")))
        elif r == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
            if blocks:
                push("assistant", blocks)
        elif r == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(m.get("content", ""))}])
    return system.strip(), out


def _tools(tools):
    return [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"]["parameters"]} for t in tools or []]


async def chat(messages, tools=None, target=None):
    """returns OpenAI 형식 assistant message dict (content, tool_calls). target은 무시(호환용)."""
    if os.getenv("MOCK_LLM") == "1":
        last = next((m for m in reversed(messages) if m["role"] == "user"), {})
        c = last.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        return {"role": "assistant", "content": f"[MOCK] {str(c)[:80]}"}
    system, msgs = _convert(messages)
    payload = {
        "model": model_name(),
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "1024")),
        # 시스템 프롬프트 캐시 → 같은 세션의 툴 라운드·다음 턴 속도 개선
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": msgs,
    }
    if tools:
        payload["tools"] = _tools(tools)
    r = await client().post(
        ANTHROPIC_URL,
        headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=payload,
        timeout=float(os.getenv("LLM_TIMEOUT", "30")),
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:400]}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    msg = {"role": "assistant", "content": text or None}
    if calls:
        msg["tool_calls"] = calls
    return msg


async def transcribe(audio_url: str) -> str:
    """음성 URL → 텍스트. Anthropic은 음성 입력이 없어 OpenAI 키가 있을 때만 시도."""
    if os.getenv("MOCK_LLM") == "1":
        return "(MOCK 전사)"
    if not os.getenv("OPENAI_API_KEY"):
        return "(음성 내용을 확인할 수 없음 — 고객에게 요지를 글로 다시 확인할 것)"
    try:
        a = await client().get(audio_url)
        a.raise_for_status()
        name = audio_url.rsplit("/", 1)[-1] or "audio.m4a"
        r = await client().post(
            f"{OPENAI_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}"},
            data={"model": os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe"), "language": "ko"},
            files={"file": (name, a.content, "audio/mp4")},
        )
        r.raise_for_status()
        return r.json().get("text", "")
    except Exception:
        return "(음성 내용을 확인할 수 없음 — 고객에게 요지를 글로 다시 확인할 것)"
