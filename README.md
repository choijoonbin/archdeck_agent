# ArchDeck Agent

FastAPI·LangGraph 기반 제한된 리서치 worker다. BE가 사용자 권한·정책·실행 원장·검토·게시를 소유하고 Agent는 RSS/Atom 출처 수집, 날짜·중복 필터, Azure OpenAI 초안 생성과 인용 연결 검증을 수행한다. 직접 게시 기능은 없다.

## 실행

Python 3.11~3.14와 uv를 사용한다.

```sh
uv sync --locked
uv run uvicorn archdeck_agent.main:app --host 127.0.0.1 --port 4010 --workers 1
uv run pytest
uv run ruff check .
```

.env.example을 참고해 프로세스에 필요한 환경을 주입한다. .env를 자동 탐색하거나 DWP 설정 파일을 자동으로 복사하지 않는다. 기본 라이브 실행은 false다. 서버 /readyz는 Azure 설정·live enable·기계 자격증명 여부를 확인하며, 실제 외부 서비스 연결 상태를 보장하지 않는다.

## Azure OpenAI와 DWP 호환

AZURE_OPENAI_API_KEY와 AZURE_OPENAI_ENDPOINT를 같은 이름으로 사용한다. ARCHDECK_OPENAI_MODEL은 Azure 배포명이며 없으면 DWP_OPENAI_MODEL을 사용한다. provider·base URL·timeout·출력 token도 ARCHDECK 우선, DWP 별칭을 지원한다. 리소스 URL은 /openai/v1로 정규화하고 api-key 헤더와 /responses, store=false, JSON Schema 출력으로 요청한다. 날짜형 api-version은 추가하지 않는다.

실제 키·endpoint·오류 body·prompt를 로그·UI·Git에 남기지 않는다. 기본 외부 tracing은 꺼져 있다. Azure 리소스 quota와 비용이 DWP와 공유될 수 있으므로 운영 예산을 별도로 정한다. 응답 usage token을 기록하되 가격표가 없어 금액을 0원으로 단정하지 않는다. 한 run/fence에서 모델 시도는 최대 2회이며 실패/응답 저장 전 장애에도 예약은 소비된다.

## 내부 API

BE만 Bearer ARCHDECK_AGENT_SERVICE_TOKEN(32자 이상)으로 호출한다. 공개 사용자 세션은 받지 않는다. 브라우저에 4010 포트를 직접 공개하지 않는다.

- POST /internal/runs/{runId}/execute: runId, leaseFence, asOf, policy. 정책은 domain_id/version/lookback_days/max_documents/max_input_characters/sources/auto_publish=false다.
- source는 host/publisher_group/feed_url/reuse_allowed=true가 필요하다. 권리·이용 조건을 확인한 정책만 활성화한다.
- 결과는 status needs_review 또는 held, draft·documents·discarded·issues·usage·published=false다. BE가 이를 저장하고 관리자 검토를 거쳐 게시한다.
- POST /internal/runs/{runId}/cancel: 실행 취소를 기록한다. 실행 중 I/O는 bounded timeout으로 끝나며 다음 단계에서 취소를 확인한다.
- 동일 runId/fence/body 재전송은 저장 결과를 반환한다. 다른 fence·본문은 409다. 한 프로세스에서 실행은 1개이며 동시 요청은 worker_busy로 거절한다.

## 수집과 검증 경계

공개 RSS/Atom의 요약만 정적으로 추출하며 기사 HTML·JS·shell은 실행하지 않는다. HTTPS exact-host allowlist, 공인 DNS 전체 검증, 검증된 IP로 연결하면서 TLS SNI/인증서 host 검증, 리다이렉트 거부, MIME/압축/크기/시간 제한을 적용한다. robots.txt를 먼저 확인하고 미확인·거부·crawl-delay는 보류한다. robots 허용은 콘텐츠 재사용 권한을 뜻하지 않는다.

발행일 없는 항목은 날짜를 만들지 않고 discarded로 기록한다. 현재 run에는 최신성 판단 가능한 자료만 들어간다. 발행일·사건일·수집일을 구분하고 future/기간밖/중복/인용누락은 검토 또는 보류한다. 문자열 인용 연결 검사는 주장 의미의 진위나 독립 출처 검증을 대신하지 않는다. 최종 지식의 정확성은 관리자 검토 책임으로 남긴다.

## 영속화와 재시작

단일 PC 기본은 artifacts/runtime.sqlite 실행 결과 캐시와 .checkpoints의 LangGraph SQLite 저장이다. 수집 완료 뒤 모델 실패 시 체크포인트에서 다시 시작하고 완료 결과는 재호출하지 않는다. 파일은 작업용 로컬 캐시이며 업무 원장은 BE PostgreSQL이다. artifacts는 Git에서 제외하고 디렉터리 권한 0700, 실행 캐시 0600으로 관리한다.

운영 PostgreSQL 체크포인트는 ARCHDECK_AGENT_CHECKPOINT_DSN을 별도 agent_runtime 역할·스키마로 지정한다. 배포 관리자가 스키마를 만든 뒤 아래 명시적 초기화 명령을 1회 실행한다. 실행 중 Agent는 테이블 생성 권한을 요구하지 않는다.

```sh
uv run python -m archdeck_agent.checkpoint_setup
```

현재 단일 프로세스 락과 run/fence 고정은 분산 lease 갱신을 대체하지 않는다. 여러 worker 배포나 자동 lease 승격은 아직 지원하지 않는다. SQLite와 PG checkpoint 모드를 동시에 원장으로 사용하지 않는다. 외부 모델 호출의 exactly-once는 보장하지 않는다.

## 검증 상태

60개 단위 검사에서 Azure 모의 요청, 실제 workflow·checkpoint 재시작/재전송/취소, SSRF·XML entity·날짜·근거·인증/입력 크기를 검사했다. 실제 openai.com robots/RSS 1회 수집과 2개 항목 파싱은 통과했다. 실제 Azure smoke는 기존 DWP 설정 endpoint의 DNS 해석 실패로 연결 전에 실패했다. 사용자가 회사망에서만 접근 가능한 환경임을 확인했으므로 추가 라이브 호출은 중단했으며 회사망 연결 후 검증할 항목으로 남긴다. 따라서 실제 Azure 모델 응답 성공은 확인하지 않았다. 키·호스트값은 출력하지 않았다.

오프라인 합성 fixture도 유지한다.

```sh
uv run archdeck-agent-eval fixtures/research-request.json
```

합성 fixture는 실제 뉴스가 아니며 게시하지 않는다. config/domain-presets.json은 비활성 정책 예시다. 제품 설정·스케줄·검토·게시와 화면 연결은 BE/FE 원장 구현과 함께 검증한다.

PostgreSQL checkpoint는 별도 archdeck_app_verify/agent_runtime에 실제 실행해 5개 graph checkpoint 행을 확인했다. SQLite checkpoint 파일이 생성되지 않음을 검사하고 로컬 결과 캐시를 제거한 뒤 PostgreSQL에서 완료 결과를 복구해 모델 mock 호출이 1회로 유지됨을 확인했다.

명시적 통합검증용 scripts/integration_server.py는 실제 collector와 모델을 합성 fixture로 바꾸는 별도 테스트 서버다. 운영 main 엔트리포인트는 이 파일을 가져오지 않는다. 실제 수집·모델 검증 결과로 혼동하지 않는다.

## Docker 실행

```sh
docker build -t archdeck-agent:dev .
docker run --rm -p 127.0.0.1:4010:4010 --env-file .env \
  -v archdeck_agent_state:/app/artifacts archdeck-agent:dev
```

컨테이너는 UID 10001로 실행하고 /app/artifacts에만 실행 캐시를 기록한다. 컨테이너 단독 삭제로 재시작 데이터가 사라지지 않도록 전용 볼륨을 사용한다. 실제 .env에는 서버 비밀이 있으므로 Git과 이미지에 포함하지 않는다.

별도 PostgreSQL 최소 권한 검증에서도 임시 Agent 역할에 checkpoint 스키마·테이블 DML만 허용했을 때 graph 저장이 성공했고 앱 계정·콘텐츠·세션 조회는 모두 거부됐다. 검증용 역할은 종료 후 제거했다.
