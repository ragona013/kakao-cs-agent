# 카카오CS — AI TOP 100 예선 (교동마을 산지직송 CS 에이전트)

Claude Code / Claude 앱이 함께 보는 공유 컨텍스트 문서. 작업 전에 이 파일부터 읽고, 구조·결정이 바뀌면 이 파일을 갱신한다.

## 목표와 일정
- 교동마을 산지직송 조합몰 고객응대 AI 에이전트를 **카카오디벨로퍼스 카카오톡 봇**으로 연동해 App ID 제출(+디브리프).
- **개발 완료 목표 13:00 (10/31)**, 심사 10/31 16:00 ~ 11/1 24:00. 제출 후 코드·프롬프트 수정 금지.
- 채점: ① 관계자 인터뷰 지식 수 ② 응답속도 p95 ③ 평균 말풍선 수(적을수록↑) ④ 말풍선당 글자 수(적을수록↑) ⑤ 케이스 정확도(조회·처리·이관 API 수행 포함). ②③④는 ⑤에 가중.
- 심사 규칙: SimpleText 외 응답이면 케이스 종료 / 60초 무응답 종료 / 케이스당 응답 10건 / 말풍선 500자 초과 시 평가 제외.

## 구조
```
카카오CS/
├ CLAUDE.md              ← 이 문서
├ actions.json           ← 액션 API OpenAPI 3.1 (원본, 수정 금지)
├ L-03.md … Y-24.md      ← 공개 TC 15개
├ config/
│  ├ system_prompt.md    ← 역할·답변형식·절차·절대규칙
│  └ knowledge.md        ← 운영 지식(정책). 인터뷰 지식은 여기에 추가
├ app/
│  ├ main.py             ← FastAPI: 웹 연습 UI(/), /api/*, 카카오톡 봇 웹훅(/webhook), 예비 오픈빌더(/kakao)
│  ├ agent.py            ← 세션 + LLM 툴 루프(call_api 하나), 메시지 파싱, 답변 후처리
│  ├ catalog.py          ← actions.json → API 목록·호출 검증(reset 차단)
│  ├ action_api.py       ← 액션 API 클라이언트(429 Retry-After 재시도)
│  ├ router.py           ← Jev 라우터 (기본 꺼짐)
│  └ llm.py              ← Anthropic Messages API 호출(Sonnet), 음성 전사(선택)
├ scripts/test_router.py ← 라우팅 점검(모의 / --live)
├ web/index.html         ← 연습 화면(TC 선택 → 대화 → API 로그/모범대응 비교)
└ logs/                  ← web.jsonl, bot_raw.jsonl(웹훅 원본), bot.jsonl, bot_cb.jsonl(콜백 결과)
```

## 실행
```bash
cd 카카오CS
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # ACTION_TOKEN, OPENAI_API_KEY 채우기 (채팅에 붙이지 말 것)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# 브라우저: http://localhost:8000
```
- 키 없이 화면만 볼 땐 `.env`에 `MOCK_LLM=1`.
- 연습 데이터는 모든 TC가 공유 → 같은 TC 재연습 전 화면 상단 "액션 데이터 초기화".

## 핵심 설계 결정
- LLM 도구는 `call_api(method, path, query, body)` 하나. 허용 경로는 actions.json 기준으로 검증, `/api/action-data/reset`은 에이전트 호출 차단.
- TC ID: 웹은 TC 선택으로 지정. 봇은 ①요청 헤더 `X-Aitop100-Test-Case-Id` ②payload 내 test_case 계열 키 ③메시지 속 `id: XXX` 줄 순서로 탐지. **실제 위치는 logs/bot_raw.jsonl로 확인 필요(TODO)**.
- 메시지 속 `상담 인입 일시 / 인입창구 / 인입 계정` 줄은 메타로 분리해 시스템 프롬프트에 주입(인입 일시 = 오늘).
- 이미지 URL → vision 입력, 음성 URL(.m4a 등) → OpenAI 전사 후 텍스트로.
- 답변 후처리: 마크다운 제거, 480자 컷. 말풍선은 항상 1개.
- 카카오톡 봇(카카오디벨로퍼스): 웹훅 POST `{op:0, t:'MESSAGE_CREATE', d:{content, botUserKey, botGroupKey, callbackToken}}` → **1초 내 204 응답** → 비동기로 `POST https://kapi.kakao.com/v1/bot/callback` (헤더 `Authorization: KakaoAK {BOT_TOKEN}`, `X-Bot-Callback-Token`, 본문 `{skillResponse:{version:'2.0',template:{outputs:[{simpleText:{text}}]}}}`), 콜백 토큰 5분 만료. 세션 키 = botUserKey.
- 액션 토큰 위치: 대회 페이지 > 문제 풀이 가이드 > '제작' 탭 > 나의 API 토큰.
- LLM: **Anthropic 전용, 모델 claude-sonnet-5-5** (`.env` LLM_MODEL로 변경 가능). `llm.py`가 Messages API(x-api-key)를 직접 호출하고, 내부 대화 기록(OpenAI 형식)을 호출 직전에 Anthropic 형식(tool_use/tool_result, image url)으로 변환. 시스템 프롬프트는 prompt caching 적용. 음성 전사는 OPENAI_API_KEY가 있을 때만, 없으면 "고객에게 글로 재확인".
- 사용 키: ANTHROPIC_API_KEY(필수), ACTION_TOKEN(필수), BOT_TOKEN(배포 시), OPENAI_API_KEY(선택·음성).
- Jev 라우터(`app/router.py`)는 **기본 꺼짐**(LLM_ROUTER=jev일 때만 판단 호출, 모델은 어차피 Anthropic). 단일 모델로 판단 지연을 없앰.

## 배포 (DigitalOcean 139.59.118.225)
- 같은 코드를 서버에 올려 `uvicorn app.main:app --host 0.0.0.0 --port 8000` (또는 기존 nginx 뒤).
- 카카오디벨로퍼스 > 앱 > 카카오톡 봇 > 봇 > 웹훅 URL에 `https://<서버>/webhook` 등록, 봇 인증 토큰을 .env BOT_TOKEN에.
- 카카오비즈니스 채널: 검색 허용 끄기('채널 공개'는 켠 상태), 채팅 기능 끄기 (대회 가이드).
- 제출: 대회 페이지 Step1에 봇 App ID + Step2 디브리프.

## 진행 현황
**PROGRESS.md**에 기록한다. 작업을 시작할 때 읽고, 끝낼 때(또는 다른 도구로 넘길 때) 체크리스트와 작업 로그를 갱신한다.
- 사람 할 일은 HUMAN_TASKS.md (근거 포함)
