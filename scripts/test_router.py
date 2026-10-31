"""라우팅 파이프라인 점검. 액션 API는 호출하지 않는다(가짜 도구만 사용).

  .venv/bin/python scripts/test_router.py          # 모의 응답으로 분기·장애 전환 검증 (키 불필요)
  .venv/bin/python scripts/test_router.py --live   # 실제 키로 Jev + 각 LLM 호출
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import llm, router  # noqa: E402

TOOL = [{"type": "function", "function": {
    "name": "lookup_order", "description": "주문 번호로 배송 상태를 조회한다",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]
SAMPLES = [
    "안녕하세요, 배송 정책이 어떻게 되나요?",
    "주문번호 A123 배송 어디까지 왔는지 조회해 주세요.",
    "받은 사과가 다 썩었어요. 환불도 안 해주면 소비자원에 신고할 겁니다.",
]


async def run_turn(text, has_image=False):
    """agent.handle과 같은 순서로 판단 → 호출 → (도구 호출 시) 결과 전달까지 한 턴 실행."""
    trace, history = [], []
    route = await router.decide(text, has_image, history)
    target = (route["provider"], route["model"]) if route else None
    history.append({"role": "user", "content": text})
    system = {"role": "system", "content": "쇼핑몰 상담원. 주문 조회는 lookup_order 도구를 쓰고 한 문장으로 답한다."}
    msg, target = await router.chat([system] + history, TOOL, target, trace)
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        history.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls") and v is not None})
        for tc in tool_calls:
            history.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps({"status": "배송중"}, ensure_ascii=False)})
        msg, target = await router.chat([system] + history, TOOL, target, trace)
    return route, target, bool(tool_calls), msg.get("content"), trace


def chat_response(content="답변"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


async def mock_tests():
    os.environ.update(MOCK_LLM="0", LLM_ROUTER="jev", TYPESAFE_API_KEY="x", OPENAI_API_KEY="x",
                      ANTHROPIC_API_KEY="x", GOOGLE_API_KEY="x")
    for k in ("ROUTE_DEFAULT", "ROUTE_SIMPLE", "ROUTE_LOOKUP", "ROUTE_COMPLEX", "ROUTE_VISION"):
        os.environ.pop(k, None)
    jev = {"answer": None}
    down = set()
    hosts = []

    def handler(req):
        host = req.url.host
        hosts.append(host)
        if host == "api.typesafe.ai":
            if isinstance(jev["answer"], int):
                return httpx.Response(jev["answer"], json={})
            choice, conf = jev["answer"]
            return httpx.Response(200, json={"answers": {"route": {"type": "choice", "choice": choice, "confidence": conf}}})
        return httpx.Response(500, json={}) if host in down else chat_response()

    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    G, O, A = "google", "openai", "anthropic"
    cases = [
        ("simple 선택 → Gemini", ("simple", 0.9), False, set(), "simple", G),
        ("lookup 선택 → OpenAI", ("lookup", 0.9), False, set(), "lookup", O),
        ("complex 선택 → Anthropic", ("complex", 0.9), False, set(), "complex", A),
        ("이미지 → Jev 없이 vision", ("complex", 0.9), True, set(), "vision", G),
        ("낮은 확신도 → 기본 경로", ("complex", 0.2), False, set(), "lookup", O),
        ("Jev 401 → 기본 경로", 401, False, set(), "lookup", O),
        ("Anthropic 장애 → 다른 제공자", ("complex", 0.9), False, {"api.anthropic.com"}, "complex", O),
    ]
    failed = 0
    for name, answer, image, broken, want_route, want_provider in cases:
        jev["answer"], down = answer, broken
        hosts.clear()
        route, target, _, _, _ = await run_turn("테스트 메시지", image)
        ok = route["route"] == want_route and target[0] == want_provider
        if image:
            ok = ok and "api.typesafe.ai" not in hosts
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {name}: route={route['route']} source={route['source']} 응답={target[0]}")

    # 제공자가 바뀌어도 이어지도록 Gemini 전용 필드가 다른 제공자에 나가지 않는지
    tc = {"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}, "extra_content": {"google": {}}, "index": 0}
    msgs = [{"role": "assistant", "tool_calls": [tc]}]
    ok = "extra_content" not in llm._for_provider(msgs, O)[0]["tool_calls"][0] \
        and "extra_content" in llm._for_provider(msgs, G)[0]["tool_calls"][0] \
        and "index" not in llm._for_provider(msgs, G)[0]["tool_calls"][0]
    failed += not ok
    print(f"{'OK  ' if ok else 'FAIL'} 제공자별 tool_calls 정리")
    print(f"\n모의 테스트: {len(cases) + 1 - failed}/{len(cases) + 1} 통과")
    return failed


async def live_tests():
    load_dotenv(ROOT / ".env")
    os.environ["MOCK_LLM"] = "0"  # 이 스크립트 안에서만 실제 호출
    print("[경로별 대상 직접 호출]")
    for name in list(router.ROUTES) + ["vision"]:
        target = router._target(name)
        try:
            msg = await llm.chat([{"role": "user", "content": "한 단어로 인사해 줘"}], target=target)
            print(f"  OK   {name}: {target[0]}/{target[1]} → {(msg.get('content') or '')[:30]!r}")
        except Exception as e:
            print(f"  FAIL {name}: {target[0]}/{target[1]} → {router._err(e)}")
    print("\n[전체 파이프라인: Jev 판단 → LLM → 도구 왕복]")
    for text in SAMPLES:
        try:
            route, target, used_tool, content, trace = await run_turn(text)
        except Exception as e:
            print(f"  FAIL {text[:24]} → {router._err(e)}\n")
            continue
        print(f"  메시지: {text}")
        if route:
            print(f"  판단: {route['route']} (source={route['source']}, confidence={route['confidence']}, {route['ms']}ms)")
        for t in trace:
            print(f"  전환: {t['path']} 실패 {t['result']} ({t['ms']}ms)")
        print(f"  응답 모델: {'/'.join(target) if target else '고정 설정'}, 도구 호출={used_tool}")
        print(f"  답변: {content}\n")


if __name__ == "__main__":
    if "--live" in sys.argv:
        asyncio.run(live_tests())
    else:
        sys.exit(1 if asyncio.run(mock_tests()) else 0)
