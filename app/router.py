"""Jev(TypeSafe 판단 모델)로 메시지를 분류해 제공자·모델을 고르고, 실패 시 다른 제공자로 넘긴다.

- 턴(고객 메시지 1건)마다 한 번 판단하고, 그 턴의 툴 루프는 같은 모델로 돈다.
- Jev 오류·타임아웃·낮은 확신도 → ROUTE_DEFAULT로 진행 (응답을 막지 않음).
- TYPESAFE_API_KEY 없음 또는 LLM_ROUTER=off → 기존처럼 LLM_PROVIDER/LLM_MODEL 고정.
- 경로별 모델은 .env의 ROUTE_SIMPLE 등에 "provider:model" 형식으로 덮어쓴다.
"""
import os
import time

from . import llm

JEV_URL = "https://api.typesafe.ai/v1/systemone"

# 경로 → (기본 대상, Jev에게 주는 판단 기준). vision은 Jev가 텍스트 전용이라 규칙으로만 선택한다.
ROUTES = {
    "simple": ("google:gemini-2.5-flash", "인사, 감사, 영업시간·배송 정책 같은 일반 안내처럼 주문 조회 없이 바로 답할 수 있는 메시지"),
    "lookup": ("openai:gpt-4.1-mini", "주문·배송 조회, 주소 변경, 단순 취소처럼 시스템을 조회하거나 정해진 처리를 하면 되는 메시지"),
    "complex": ("anthropic:claude-sonnet-5-5", "상품 하자, 환불·보상 분쟁, 강한 불만, 예외 판단, 담당자 이관 여부 판단이 필요한 메시지"),
}
VISION_DEFAULT = "google:gemini-2.5-flash"
INSTRUCTIONS = "산지직송 쇼핑몰 고객의 마지막 메시지를 처리하려면 어떤 수준의 상담이 필요한지 고르세요."


def enabled():
    # Anthropic 단일 모델로 운영 → 기본 꺼짐. LLM_ROUTER=jev 일 때만 동작(대상 모델은 무시되고 Anthropic으로 호출됨)
    if os.getenv("MOCK_LLM") == "1" or os.getenv("LLM_ROUTER", "off").lower() != "jev":
        return False
    return bool(os.getenv("TYPESAFE_API_KEY"))


def _target(route):
    default = VISION_DEFAULT if route == "vision" else ROUTES[route][0]
    provider, _, model = os.getenv(f"ROUTE_{route.upper()}", default).partition(":")
    return provider, model


def _default_route():
    r = os.getenv("ROUTE_DEFAULT", "lookup")
    return r if r in ROUTES else "lookup"


def _user_text(m):
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
    return c or ""


def _state(text, history):
    """직전 고객 메시지 2건 + 현재 메시지. 현재 메시지는 history 추가 전 값을 받는다."""
    prev = [_user_text(m) for m in history if m.get("role") == "user"][-2:]
    lines = [f"이전 고객 메시지: {p[:300]}" for p in prev]
    lines.append(f"마지막 고객 메시지: {text[:1000]}")
    return "\n".join(lines)


async def _ask_jev(state):
    r = await llm.client().post(
        JEV_URL,
        headers={"Authorization": f"Bearer {os.getenv('TYPESAFE_API_KEY', '')}"},
        json={
            "model": os.getenv("JEV_MODEL", "jev-latest"),
            "state": state,
            "questions": {"route": {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": {k: v[1] for k, v in ROUTES.items()},
            }},
        },
        timeout=float(os.getenv("JEV_TIMEOUT", "3")),
    )
    r.raise_for_status()
    a = r.json()["answers"]["route"]
    return a["choice"], float(a.get("confidence") or 0)


async def decide(text, has_image, history):
    """이번 턴의 경로를 정한다. 라우터가 꺼져 있으면 None."""
    if not enabled():
        return None
    t0 = time.perf_counter()
    if has_image:
        route, confidence, source = "vision", None, "rule:image"
    else:
        try:
            route, confidence = await _ask_jev(_state(text, history))
            source = "jev"
            if route not in ROUTES:
                route, source = _default_route(), f"fallback:unknown_choice({route})"
            elif confidence < float(os.getenv("JEV_MIN_CONFIDENCE", "0.5")):
                route, source = _default_route(), f"fallback:low_confidence({route})"
        except Exception as e:
            route, confidence, source = _default_route(), None, f"fallback:{_err(e)}"
    provider, model = _target(route)
    return {"route": route, "provider": provider, "model": model, "confidence": confidence,
            "source": source, "ms": int((time.perf_counter() - t0) * 1000)}


def _err(e):
    """키가 섞일 수 있는 예외 메시지 대신 상태 코드·예외 이름만 남긴다."""
    resp = getattr(e, "response", None)
    return f"HTTP{resp.status_code}" if resp is not None else type(e).__name__


def _candidates(target):
    """선택된 대상 + 키가 있는 나머지 제공자의 기본 모델."""
    out = [target]
    for p, (_, key_env, default_model) in llm.PROVIDERS.items():
        if p != target[0] and os.getenv(key_env):
            out.append((p, default_model))
    return out


async def chat(messages, tools, target, trace):
    """returns (assistant message, 실제 응답한 target). 실패가 기록된 대상은 건너뛰고 다음으로 넘어간다."""
    if target is None:
        return await llm.chat(messages, tools), None
    last = None
    for cand in _candidates(target):
        t0 = time.perf_counter()
        try:
            return await llm.chat(messages, tools, target=cand), cand
        except Exception as e:
            last = e
            trace.append({"method": "LLM", "path": f"{cand[0]}/{cand[1]}", "api": "failover", "status": "ERR",
                          "ms": int((time.perf_counter() - t0) * 1000), "result": _err(e)})
    raise last
