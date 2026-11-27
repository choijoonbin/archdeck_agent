# ArchDeck Agent

Azure OpenAI(DWP 설정 호환) 기반 리서치 에이전트의 초기 실행 골격이다. 공통 아키텍처·UX·데이터 설계는 [백엔드 문서](https://github.com/choijoonbin/archdeck_be/tree/dev/docs)에 모아 관리한다.

현재 구현은 FastAPI 상태 확인, LangGraph의 합성 fixture 평가, Azure v1 Responses 어댑터와 단위 테스트다. 실제 기사 수집, LLM을 연결한 workflow, BE 통합·스케줄·PostgreSQL checkpoint·검토·게시는 아직 구현하지 않았다. health 성공을 운영 준비 완료로 보지 않으며 ready는 의도적으로 503을 반환한다.

## 실행과 평가

Python 3.11~3.14와 uv를 사용한다. 잠금 파일의 의존성을 그대로 설치한다.

```sh
uv sync --locked
uv run uvicorn archdeck_agent.main:app --host 127.0.0.1 --port 4010
uv run archdeck-agent-eval fixtures/research-request.json
uv run pytest
uv run ruff check .
```

/healthz는 골격의 생존 상태, /readyz는 미구현 운영 기능을 구분한다. 합성 평가에는 API 키가 필요 없으며 네트워크·모델 호출과 게시를 수행하지 않는다. in-memory checkpoint는 프로세스 재시작을 견디는 영속화가 아니다.

## DWP Azure OpenAI 설정 호환

기본 provider는 azure_openai다. DWP의 AZURE_OPENAI_API_KEY·AZURE_OPENAI_ENDPOINT를 같은 이름으로 받고, DWP_OPENAI_MODEL은 Azure의 배포명으로 사용한다. ARCHDECK_OPENAI_MODEL 등이 있으면 우선한다. 리소스 주소는 /openai/v1로 정규화하고 /responses에 api-key 헤더를 전달한다. v1 경로에는 별도의 날짜형 api-version 파라미터를 추가하지 않는다.

현재 DWP의 .env.local을 자동으로 탐색·복사하지 않는다. 개발 시 필요한 환경을 해당 프로세스에 주입할 수 있다. 공통 키·리소스의 quota·비용은 DWP와 합산될 수 있으므로 운영 단계에서 사용량과 제한을 분리한다. 키·원문 prompt·upstream 오류 body를 로그나 프론트에 노출하지 않는다. .env.example에는 자리표시자만 있다.

ARCHDECK_AGENT_LIVE_ENABLED는 기본 false다. 어댑터의 요청 구조만 MockTransport로 검사하며 live API·요금·현재 Azure 배포의 기능은 아직 검증하지 않았다. 공개 OpenAI 전용 web search나 managed agent 기능이 Azure에 동일하게 있다고 가정하지 않는다. 모델·지역별 Responses와 Structured Outputs 지원을 실제 통합 단계에서 확인한다.

## 하네스와 범위

- 분야 ID·출처·규칙은 데이터로 확장한다. config/domain-presets.json은 승인 전 정책 예시이며 실제 크롤링을 켜지 않는다.
- 평가 단계는 출처·기간·크기·중복 검사 → 주장별 인용문 연결 검사 → 관리자 검토 대기다.
- 문자열 근거 연결은 사실의 의미적 정확성을 증명하지 않는다. 출처 독립성·주장 검증·실수집 SSRF 방어·외부 원문 라이선스는 별도의 운영 검증 대상이다.
- instruction marker 검사는 triage용 휴리스틱이며 프롬프트 주입 방어의 보장이 아니다.
- Agent는 정규 콘텐츠·계정 DB에 접근하거나 직접 게시하지 않는다. 검토·게시의 유일 권위는 BE다.
- 합성 fixtures는 실제 기사·최신 뉴스의 근거가 아니며 BE에 게시하지 않는다.

## Docker

```sh
docker build -t archdeck-agent:dev .
docker run --rm -p 127.0.0.1:4010:4010 archdeck-agent:dev
```

이 이미지도 운영 worker가 아닌 상태 확인 골격이다. 사내 브라우저에 Agent 포트를 직접 공개하지 않는다. BE의 인증·인가를 통하는 내부 서비스 계약은 후속 구현이다.

## 확인한 범위

2026-11-27 기준 단위 검사 42개·ruff·잠금 파일 설치·Docker 이미지 빌드와 격리 기동을 확인했다. Docker 검사에서는 network none·읽기 전용 파일시스템·UID 10001로 healthz 200/readyz 503을 확인하고 임시 컨테이너를 제거했다. 실제 Azure API 호출과 기사 수집은 0건이다. 테스트 통과를 운영 worker·게시·의미적 사실 검증 완료로 해석하지 않는다.
