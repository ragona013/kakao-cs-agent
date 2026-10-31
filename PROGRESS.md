# 진행 현황 (Claude 앱 ↔ Claude Code 공유)

규칙: 작업 시작 전에 읽고, 끝내거나 넘길 때 아래 3곳(체크리스트 / 막힌 것 / 작업 로그)을 갱신한다. 로그는 최신이 위로, `시각 · 작업자 · 한 일`.

## 마일스톤
- 13:00 개발 완료 → 제출(App ID + 디브리프) / 16:00 심사 시작 → 이후 수정 금지

## 체크리스트
### 1. 에이전트 코어
- [x] FastAPI 서버 + 웹 연습 UI (TC 선택, API 호출 로그, 모범대응 비교, 글자수·응답시간 표시)
- [x] LLM 툴 루프 (`call_api` 1개, actions.json 기반 경로 검증, reset 차단, 429 재시도)
- [x] 메시지 파싱: TC ID / 인입 일시·계정 메타 / 이미지·음성 URL
- [x] LLM: Anthropic 전용(claude-sonnet-5-5, Messages API 직접 호출 + 프롬프트 캐싱)으로 단일화
- [ ] 실제 키로 첫 실행 (L-03) — **맥에서 사용자 확인 대기**
- [x] Jev 라우터 구현 후 기본 꺼둠 (`LLM_ROUTER=jev`로만 켜짐)
- [ ] **ANTHROPIC_API_KEY 재입력** (현재 401, 62자 → 잘린 것으로 보임. 정상 키는 훨씬 김)

### 2. 지식·정책
- [x] 공개 TC 14개 규칙을 config/knowledge.md에 일반화
- [x] Y-16.md 추가됨
- [~] 관계자 인터뷰 진행 중 — 7/10명(이민준·구방만·홍보라·김청년·고길동·홍이장·박수복), 박민아 연락처 추적 중, 문서 22건 확보, 문서 10건 docs/ 저장, 지식 config/knowledge.md 누적 (상세: interview/LOG.md, interview/PLAYBOOK.md)
- [x] 회신 기한 확보(영업일 3일, 날짜로 안내)
- [x] 판매자 사업자정보 공시값 확보(docs/사업자정보.md)

### 3. 검증 (웹 연습장)
| TC | 결과 | 메모 |
|---|---|---|
| L-03 | - | |
| L-05 | - | |
| L-11 | - | |
| L-20 | - | |
| L-23 | - | |
| L-32 | - | |
| L-33 | - | |
| L-34 | - | |
| L-40 | - | |
| L-47 | - | |
| L-60 | - | |
| R-05-a | - | |
| Y-07-b | - | |
| Y-16 | - | |
| Y-24 | - | 공시값 미확보 |
(결과: ✅ 통과 / ⚠️ 부분 / ❌ 실패. 같은 TC 재검증 전 "액션 데이터 초기화")

### 4. 카카오톡 봇 연동·배포
- [x] `/webhook` 구현 (204 즉시 응답 → kapi 콜백)
- [x] 카카오디벨로퍼스 앱·봇 생성, BOT_TOKEN 발급(사용자)
- [x] Railway 배포(https://cs-agent-production-e354.up.railway.app, 12:10 첫 배포 성공)
- [ ] Railway 비밀값 4개 입력(사용자) → 재푸시 → 웹훅 URL 등록
- [ ] 채널 설정: 검색 허용 끄기(채널 공개는 켬), 채팅 기능 끄기
- [ ] 실제 카톡으로 왕복 확인 + logs/bot_raw.jsonl에서 TC ID·첨부 필드 위치 확인
- [ ] 제출: App ID + 디브리프

## 막힌 것 / 확인 필요
- 심사 때 TC ID가 웹훅 어디에 실려 오는지 미확인 (헤더 / payload / 메시지 본문 모두 탐지하도록 해둠)
- 웹훅으로 이미지·음성이 어떤 필드로 오는지 미확인 (attachments 등 추정 처리)
- ANTHROPIC_API_KEY 401 (62자, 잘렸을 가능성) → 콘솔에서 다시 복사해 넣어야 첫 실행 가능
- 음성 TC(Y-07-b): Anthropic은 음성 입력 불가 → OpenAI 키가 정상일 때만 전사, 아니면 고객에게 글로 재확인
- 미검증: claude-sonnet-5-5 실호출(키 교체 후)

## 작업 로그
- 12:15 · Claude 앱 · Anthropic API 한도 초과 발생 → 사용자 콘솔 한도 상향. 안전장치로 Anthropic 실패 시 OpenAI(gpt-4.1) 자동 전환 추가. 문서는 프롬프트에 목록만 넣고 read_doc 도구로 읽게 바꿔 속도 개선(턴당 2~10초). TC 9개 검증(TEST_LOG.md)
- 12:06 · Claude 앱 · 배포 Railway 결정(상주 서버·HTTPS·MCP 배포). GitHub 저장소는 사용자 생성/푸시
- 11:55 · Claude 앱 · 카톡 PC앱 자동 인터뷰 22분: 관계자 6명·문서 10건 확보. docs/ 자동 수집(fetchdoc/ocr 엔드포인트), interview/PLAYBOOK.md에 상황별 대처·소요 시간 기록
- 11:35 · Claude 앱 · 실제 키로 L-03 통과(API-11→API-24 접수, 턴당 2~6초). temperature 제거(Sonnet 5.5 미지원), load_dotenv override
- 11:30 · Claude 앱 · 사용자 요청으로 LLM을 Anthropic Sonnet 단일화: llm.py를 Messages API 직접 호출로 교체(OpenAI형 기록↔Anthropic 변환, 이미지 url, 프롬프트 캐싱), Jev 라우터 기본 off, .env.example 정리. 모의 테스트로 툴 2회 왕복·이미지 변환 확인
- 11:21 · Claude Code · Jev 라우터 추가(router.py, llm.py target 인자, agent.py 연결, scripts/test_router.py). 모의 8/8 통과, 실호출은 Gemini만 성공(나머지 3개 키 401)
- 11:15 · Claude 앱 · PROGRESS.md 생성, CLAUDE.md는 고정 정보만 두고 진행 현황은 여기로 분리
- 11:13 · Claude 앱 · LLM 제공자 3종 전환 지원(llm.py), .env.example에 키 4종
- 11:10 · Claude 앱 · 대회 페이지 확인: 액션 토큰 = 문제 풀이 가이드 > 제작 탭 / 봇은 카카오디벨로퍼스 카카오톡 봇(웹훅+콜백) → `/webhook` 추가
- 10:55 · Claude 앱 · 카카오CS 1차 구현 (app/, web/, config/, CLAUDE.md). 실제 API 호출은 미검증, mock으로 흐름만 확인
