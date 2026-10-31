"""FastAPI 서버: 웹 연습 UI + 카카오 스킬 엔드포인트.

실행: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402

from . import action_api, agent  # noqa: E402

app = FastAPI(title="교동마을 CS 에이전트")
SESSIONS: dict[str, agent.Session] = {}
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def log(name, obj):
    with open(LOG_DIR / name, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **obj}, ensure_ascii=False) + "\n")


def get_session(sid):
    if sid not in SESSIONS:
        SESSIONS[sid] = agent.Session(sid=sid)
    return SESSIONS[sid]


# ---------------- 공개 TC 로딩 ----------------
def parse_tc(path: Path):
    t = path.read_text(encoding="utf-8")
    head, _, rest = t.partition("#### 시나리오 및 기대 대응")
    meta_lines = re.findall(r"(?m)^- (id|상담 인입 일시|인입창구|인입 계정):\s*(.+)$", head)
    meta = {k: v.strip() for k, v in meta_lines}
    after = head.split("case metadata", 1)[-1]
    first_msg = "\n".join(
        l for l in after.splitlines()
        if l.strip() and not l.startswith("- ")
    ).strip()
    title = re.search(r"(?m)^##### (.+)$", rest)
    expected = rest.split("직접 실행", 1)[0].strip()
    return {
        "id": meta.get("id", path.stem),
        "title": title.group(1).strip() if title else "",
        "meta": meta,
        "first_message": first_msg,
        "expected": expected,
    }


@app.get("/api/tcs")
def list_tcs():
    tcs = []
    for p in sorted(ROOT.glob("*.md")):
        if p.name.upper() in ("CLAUDE.MD", "README.MD"):
            continue
        try:
            tc = parse_tc(p)
            if tc["meta"].get("id"):
                tcs.append(tc)
        except Exception:
            pass
    return tcs


# ---------------- 웹 연습 UI ----------------
@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/api/session/new")
async def new_session(req: Request):
    b = await req.json()
    sid = uuid.uuid4().hex[:12]
    s = get_session(sid)
    s.tc_id = (b.get("tc_id") or "").strip() or None
    m = b.get("meta") or {}
    s.meta = {"inbound_at": m.get("상담 인입 일시", ""), "channel": m.get("인입창구", ""),
              "account": m.get("인입 계정", "")}
    s.meta = {k: v for k, v in s.meta.items() if v}
    return {"session_id": sid, "tc_id": s.tc_id, "meta": s.meta}


@app.post("/api/chat")
async def chat(req: Request):
    b = await req.json()
    s = get_session(b["session_id"])
    async with s.lock:
        res = await agent.handle(s, b.get("message", ""))
    log("web.jsonl", {"sid": s.sid, "tc": s.tc_id, "in": b.get("message"), "out": res["reply"],
                      "ms": res["ms"], "trace": res["trace"]})
    return res


@app.post("/api/action-reset")
async def action_reset():
    return await action_api.reset_all()


@app.get("/health")
def health():
    from . import llm
    return {"ok": True, "model": f"{llm.provider()}:{llm.model_name()}", "mock": os.getenv("MOCK_LLM") == "1",
            "token_set": bool(os.getenv("ACTION_TOKEN")),
            "openai_set": bool(os.getenv(llm.PROVIDERS[llm.provider()][1]))}


# ---------------- 카카오디벨로퍼스 카카오톡 봇 (웹훅 → 204 → 콜백 API) ----------------
KAPI = "https://kapi.kakao.com"


async def bot_callback(callback_token: str, text: str, key: str):
    body = {"skillResponse": simple_text(text)}
    headers = {"Authorization": f"KakaoAK {os.getenv('BOT_TOKEN', '')}",
               "X-Bot-Callback-Token": callback_token}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{KAPI}/v1/bot/callback", json=body, headers=headers)
        log("bot_cb.jsonl", {"key": key, "status": r.status_code, "body": r.text[:300]})
        print("CALLBACK", key, r.status_code, r.text[:200], flush=True)
    except Exception as e:
        log("bot_cb.jsonl", {"key": key, "error": str(e)})


@app.post("/webhook")
async def webhook(req: Request):
    payload = await req.json()
    log("bot_raw.jsonl", {"headers": dict(req.headers), "payload": payload})
    print("WEBHOOK", json.dumps({"h": {k: v for k, v in req.headers.items() if k.lower().startswith(("x-", "user-agent"))}, "p": payload}, ensure_ascii=False)[:1500], flush=True)
    secret = os.getenv("WEBHOOK_SECRET_HEADER")  # "헤더이름:값" 형식, 설정했을 때만 검증
    if secret and ":" in secret:
        hn, hv = secret.split(":", 1)
        if req.headers.get(hn.strip().lower()) != hv.strip():
            return Response(status_code=204)
    d = payload.get("d") or {}
    if payload.get("op") == 0 and payload.get("t") == "MESSAGE_CREATE" and d.get("callbackToken"):
        key = d.get("botUserKey") or d.get("botGroupKey") or "anon"
        s = get_session(f"bot:{key}")
        tc = find_tc_in_payload(payload, req.headers)
        now = time.time()
        idle = now - getattr(s, "_last_ts", now)
        s._last_ts = now
        if idle > float(os.getenv("SESSION_IDLE_RESET", "600")):  # 10분 이상 공백 → 새 상담으로 보고 대화 기록 초기화
            s.history.clear(); s.trace.clear(); s.turns.clear(); s.meta.clear(); s.tc_id = None
        if tc and tc != s.tc_id:
            if s.tc_id:
                s.history.clear(); s.trace.clear(); s.turns.clear(); s.meta.clear()
            s.tc_id = tc
        text = d.get("content") or ""
        # 첨부(이미지/음성)가 별도 필드로 오면 URL을 본문에 붙여 agent가 처리하게 함
        for k in ("attachments", "attachment", "media", "files"):
            v = d.get(k)
            if v:
                text += "\n" + " ".join(re.findall(r"https?://[^\s\"']+", json.dumps(v)))

        async def work():
            async with s.lock:
                res = await agent.handle(s, text)
            log("bot.jsonl", {"key": key, "tc": s.tc_id, "in": text, "out": res["reply"],
                              "ms": res["ms"], "trace": res["trace"]})
            print("TURN", json.dumps({"key": key, "tc": s.tc_id, "ms": res["ms"], "in": text[:200], "out": res["reply"][:300],
                                      "api": [f"{x.get('api')}:{x.get('status')}" for x in res["trace"]]}, ensure_ascii=False), flush=True)
            await bot_callback(d["callbackToken"], res["reply"], key)

        asyncio.create_task(work())
    return Response(status_code=204)


# ---------------- (예비) 오픈빌더 스킬 형식 ----------------
def simple_text(text):
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}


def find_tc_in_payload(payload, headers):
    h = headers.get("x-aitop100-test-case-id")
    if h:
        return h
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = str(k).lower()
                if isinstance(v, str) and ("test_case" in kl or "testcase" in kl or kl in ("tc_id", "tcid")):
                    return v
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


@app.post("/kakao")
async def kakao(req: Request):
    payload = await req.json()
    log("kakao_raw.jsonl", {"headers": dict(req.headers), "payload": payload})
    ur = payload.get("userRequest", {})
    uid = ur.get("user", {}).get("id", "anon")
    text = ur.get("utterance", "")
    callback = ur.get("callbackUrl")
    s = get_session(f"kakao:{uid}")
    tc = find_tc_in_payload(payload, req.headers)
    if tc and tc != s.tc_id:
        if s.tc_id:
            s.history.clear(); s.trace.clear(); s.turns.clear(); s.meta.clear()
        s.tc_id = tc

    async def work():
        async with s.lock:
            res = await agent.handle(s, text)
        log("kakao.jsonl", {"uid": uid, "tc": s.tc_id, "in": text, "out": res["reply"],
                            "ms": res["ms"], "trace": res["trace"]})
        return res["reply"]

    task = asyncio.create_task(work())
    sync_wait = float(os.getenv("KAKAO_SYNC_WAIT", "3.8"))
    try:
        reply = await asyncio.wait_for(asyncio.shield(task), timeout=sync_wait if callback else 4.6)
        return JSONResponse(simple_text(reply))
    except asyncio.TimeoutError:
        if not callback:
            return JSONResponse(simple_text("확인 중이에요. 잠시 후 한 번만 다시 말씀해 주세요."))

    async def send_callback():
        reply = await task
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(callback, json=simple_text(reply))
            log("kakao_cb.jsonl", {"uid": uid, "status": r.status_code, "body": r.text[:300]})
        except Exception as e:
            log("kakao_cb.jsonl", {"uid": uid, "error": str(e)})

    asyncio.create_task(send_callback())
    return JSONResponse({"version": "2.0", "useCallback": True})


# ---------------- 개발용: 테스트 고객 역할에 필요한 데이터 조회 (GET만, DEV_PROXY=1일 때만) ----------------
@app.get("/api/dev/get")
async def dev_get(path: str, tc: str, q: str = ""):
    if os.getenv("DEV_PROXY", "1") != "1" or not path.startswith("/api/"):
        return JSONResponse({"error": "disabled"}, status_code=403)
    query = dict(x.split("=", 1) for x in q.split("&") if "=" in x) if q else None
    return await action_api.call("GET", path, query, None, tc)


# ---------------- 개발용: 관계자가 공유한 공식 문서(t1.kakaocdn.net) 저장 → docs/ ----------------
def _xlsx_text(data: bytes) -> str:
    import io
    try:
        import openpyxl
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "openpyxl"])
        import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    out = []
    for ws in wb:
        out.append(f"## {ws.title}")
        for r in ws.iter_rows(values_only=True):
            if any(c is not None for c in r):
                out.append(" | ".join("" if c is None else str(c) for c in r))
    return "\n".join(out)


@app.get("/api/dev/fetchdoc")
async def dev_fetchdoc(url: str):
    import html as _html
    if not url.startswith("https://t1.kakaocdn.net/"):
        return JSONResponse({"error": "kakaocdn만 허용"}, status_code=400)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url)
    if r.status_code != 200:
        return JSONResponse({"error": r.status_code}, status_code=404)
    if url.lower().endswith(".xlsx"):
        import io, zipfile
        zz = zipfile.ZipFile(io.BytesIO(r.content))
        ss = re.findall(r"<si>(.*?)</si>", zz.read("xl/sharedStrings.xml").decode("utf-8", "ignore"), re.S) if "xl/sharedStrings.xml" in zz.namelist() else []
        ss = ["".join(re.findall(r"<t[^>]*>([^<]*)</t>", s)) for s in ss]
        rows_out = []
        for nm in sorted(n for n in zz.namelist() if n.startswith("xl/worksheets/sheet")):
            xml = zz.read(nm).decode("utf-8", "ignore")
            rows_out.append(f"## {nm}")
            for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
                cells = []
                for attrs, inner in re.findall(r"<c([^>]*)>(.*?)</c>", row, re.S):
                    v = re.search(r"<v>(.*?)</v>", inner, re.S)
                    isv = re.search(r"<t[^>]*>(.*?)</t>", inner, re.S)
                    if 't="s"' in attrs and v:
                        cells.append(ss[int(v.group(1))])
                    elif v:
                        cells.append(v.group(1))
                    elif isv:
                        cells.append(isv.group(1))
                    else:
                        cells.append("")
                rows_out.append(" | ".join(cells))
        text = "\n".join(rows_out)
        name = re.sub(r"[^\w가-힣·\-]+", "_", url.rsplit("/", 1)[-1].rsplit(".", 1)[0])[:60]
        (ROOT / "docs" / f"{name}.md").write_text(f"# 엑셀 문서\n출처: {url}\n\n{text}\n", encoding="utf-8")
        return {"saved": f"{name}.md", "chars": len(text)}
    if url.lower().endswith(".zip"):
        import io, zipfile
        saved = []
        z = zipfile.ZipFile(io.BytesIO(r.content))
        for nm in z.namelist():
            if nm.endswith("/") or "__MACOSX" in nm:
                continue
            data = z.read(nm)
            low = nm.lower()
            try:
                if low.endswith(".pdf"):
                    import subprocess, sys
                    try:
                        import pypdf
                    except ImportError:
                        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pypdf"], check=False)
                        import pypdf
                    text = "\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(io.BytesIO(data)).pages)
                elif low.endswith((".html", ".htm")):
                    import html as _h
                    hh = data.decode("utf-8", "ignore")
                    hh = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", hh)
                    hh = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|table)>", "\n", hh)
                    text = _h.unescape(re.sub(r"<[^>]+>", "", re.sub(r"(?i)</t[dh]>", " | ", hh)))
                elif low.endswith((".md", ".txt", ".csv", ".json")):
                    text = data.decode("utf-8", "ignore")
                elif low.endswith(".docx"):
                    import zipfile as _zf, io as _io
                    xml = _zf.ZipFile(_io.BytesIO(data)).read("word/document.xml").decode("utf-8", "ignore")
                    text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
                elif low.endswith(".xlsx"):
                    text = _xlsx_text(data)
                elif low.endswith((".png", ".jpg", ".jpeg")):
                    (ROOT / "docs" / "raw").mkdir(exist_ok=True)
                    (ROOT / "docs" / "raw" / nm.rsplit("/", 1)[-1]).write_bytes(data)
                    saved.append({"file": nm, "image_saved": True})
                    continue
                else:
                    saved.append({"file": nm, "skipped": True, "bytes": len(data)})
                    continue
            except Exception as e:
                text = f"(추출 실패: {e})"
            text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
            base = re.sub(r"[^\w가-힣·\-]+", "_", nm.rsplit("/", 1)[-1].rsplit(".", 1)[0])[:60]
            (ROOT / "docs" / f"{base}.md").write_text(f"# {nm}\n출처: {url}\n\n{text}\n", encoding="utf-8")
            saved.append({"file": nm, "chars": len(text)})
        return {"zip": saved}
    if url.lower().endswith(".pdf") or r.headers.get("content-type", "").startswith("application/pdf"):
        import io, subprocess, sys
        try:
            import pypdf
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pypdf"], check=False)
            import pypdf
        rd = pypdf.PdfReader(io.BytesIO(r.content))
        text = "\n".join((pg.extract_text() or "") for pg in rd.pages)
        first = next((l.strip() for l in text.splitlines() if l.strip()), "문서")
        name = re.sub(r"[^\w가-힣·\-]+", "_", first)[:60]
        (ROOT / "docs" / f"{name}.md").write_text(f"# {first}\n출처: {url}\n\n{text}\n", encoding="utf-8")
        return {"saved": f"{name}.md", "chars": len(text), "pages": len(rd.pages)}
    h = r.text
    title = re.search(r"<title>(.*?)</title>", h, re.S)
    title = _html.unescape(title.group(1).strip()) if title else url.rsplit("/", 1)[-1]
    body = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", h)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|table|thead|tbody)>", "\n", body)
    body = re.sub(r"(?i)</t[dh]>", " | ", body)
    body = _html.unescape(re.sub(r"<[^>]+>", "", body))
    body = "\n".join(l.strip() for l in body.splitlines() if l.strip())
    name = re.sub(r"[^\w가-힣·\-]+", "_", title.split("|")[0].strip())[:60]
    p = ROOT / "docs" / f"{name}.md"
    p.write_text(f"# {title}\n출처: {url}\n\n{body}\n", encoding="utf-8")
    return {"saved": str(p.name), "chars": len(body)}


@app.get("/api/dev/docs")
def dev_docs():
    return [{"name": p.name, "chars": len(p.read_text(encoding="utf-8"))} for p in sorted((ROOT / "docs").glob("*.md"))]


# ---------------- 개발용: docs/raw 이미지 문서 → Claude 비전으로 전사 → docs/<prefix>.md ----------------
@app.get("/api/dev/ocr")
async def dev_ocr(prefix: str, title: str = ""):
    import base64, subprocess, tempfile
    imgs = sorted((ROOT / "docs" / "raw").glob(f"{prefix}*.png")) + sorted((ROOT / "docs" / "raw").glob(f"{prefix}*.jpg"))
    if not imgs:
        return JSONResponse({"error": "no images"}, status_code=404)
    content = []
    for p in imgs:
        out = Path(tempfile.gettempdir()) / (p.stem + "_s.jpg")
        subprocess.run(["sips", "-Z", "1800", "-s", "format", "jpeg", str(p), "--out", str(out)], capture_output=True)
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(out.read_bytes()).decode()}})
    content.append({"type": "text", "text": "위 이미지들은 한 문서의 연속 페이지다. 내용을 빠짐없이 한국어 원문 그대로 마크다운으로 전사하라. 표는 마크다운 표로. 요약·해설 금지."})
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"},
                         json={"model": os.getenv("LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 8000,
                               "messages": [{"role": "user", "content": content}]})
    if r.status_code >= 400:
        return JSONResponse({"error": r.text[:500]}, status_code=500)
    text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    name = re.sub(r"[^\w가-힣·\-]+", "_", title or prefix)[:60]
    (ROOT / "docs" / f"{name}.md").write_text(f"# {title or prefix} (이미지 전사)\n\n{text}\n", encoding="utf-8")
    return {"saved": f"{name}.md", "chars": len(text), "pages": len(imgs)}


# ---------------- 운영 배포 시 개발용 엔드포인트 차단 (DEV_PROXY=0) ----------------
@app.middleware("http")
async def _block_dev(request: Request, call_next):
    p = request.url.path
    if os.getenv("DEV_PROXY", "1") != "1" and (p.startswith("/api/dev") or p == "/api/action-reset"):
        return JSONResponse({"error": "disabled in production"}, status_code=403)
    return await call_next(request)
