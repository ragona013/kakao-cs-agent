"""교동몰 액션 API 클라이언트."""
import asyncio
import json
import os
import time

import httpx

BASE = os.getenv("ACTION_BASE_URL", "https://gyodong-mall-action.aitop100.org")
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE, timeout=15.0)
    return _client


def _headers(tc_id: str | None):
    h = {"Authorization": f"Bearer {os.getenv('ACTION_TOKEN', '')}"}
    if tc_id:
        h["X-Aitop100-Test-Case-Id"] = tc_id
    return h


async def call(method: str, path: str, query: dict | None, body: dict | None, tc_id: str | None):
    """returns dict(status, data, ms)"""
    t0 = time.perf_counter()
    for attempt in range(3):
        try:
            r = await client().request(
                method.upper(), path,
                params=query or None,
                json=(body if body is not None else ({} if method.upper() == "POST" else None)),
                headers=_headers(tc_id),
            )
        except httpx.HTTPError as e:
            if attempt == 2:
                return {"status": 0, "data": f"네트워크 오류: {e}", "ms": int((time.perf_counter() - t0) * 1000)}
            await asyncio.sleep(0.5)
            continue
        if r.status_code == 429:
            try:
                msg = r.json().get("message", "")
            except Exception:
                msg = ""
            if "누적" in msg:
                break
            wait = min(float(r.headers.get("Retry-After", "2") or 2), 10)
            await asyncio.sleep(wait)
            continue
        if r.status_code in (500, 503) and attempt < 2:
            await asyncio.sleep(0.7)
            continue
        break
    ms = int((time.perf_counter() - t0) * 1000)
    if not r.content:
        data = None
    else:
        try:
            data = r.json()
        except Exception:
            data = r.text
    return {"status": r.status_code, "data": data, "ms": ms}


async def reset_all():
    r = await client().post("/api/action-data/reset", headers=_headers(None))
    return {"status": r.status_code, "data": (r.text or None)}


def describe_for_llm(res: dict) -> str:
    """LLM에 돌려줄 짧은 결과 문자열."""
    st = res["status"]
    data = res["data"]
    if st == 404:
        note = "404: 대상 없음(경로·메서드·식별자 확인)"
        return note
    if st == 400 and not data:
        return "400: 요청 파라미터/본문 형식 오류(필수 필드·타입·허용값 확인)"
    txt = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    if len(txt) > 6000:
        txt = txt[:6000] + "...(생략)"
    return f"{st}: {txt}"
