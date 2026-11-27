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

.env.example을 참고해 프로세스에 필요한 환경을 주입한다. .env를 자동 탐색하거나 DWP 설정 파일을 자동으로 복사하지 않는다. 기본 라이브 실행은 false다. 서버 /readyz는 Azure 설정·live enable·기계 자격증명을 확인한다. 분산 모드에서는 실제 PostgreSQL 연결·테이블·DML 권한과 역할 격리까지 검사하며 실패 시 503이다. Azure 외부 연결을 호출하지 않으므로 회사망에서 별도 확인한다.

## Azure OpenAI와 DWP 호환

AZURE_OPENAI_API_KEY와 AZURE_OPENAI_ENDPOINT를 같은 이름으로 사용한다. ARCHDECK_OPENAI_MODEL은 Azure 배포명이며 없으면 DWP_OPENAI_MODEL을 사용한다. provider·base URL·timeout·출력 token도 ARCHDECK 우선, DWP 별칭을 지원한다. 리소스 URL은 /openai/v1로 정규화하고 api-key 헤더와 /responses, store=false, JSON Schema 출력으로 요청한다. 날짜형 api-version은 추가하지 않는다.

실제 키·endpoint·오류 body·prompt를 로그·UI·Git에 남기지 않는다. 기본 외부 tracing은 꺼져 있다. Azure 리소스 quota와 비용이 DWP와 공유될 수 있으므로 운영 예산을 별도로 정한다. 응답 usage token을 기록하되 가격표가 없어 금액을 0원으로 단정하지 않는다. 분산 모드는 run 전체에서 fence·프로세스 재시작을 통틀어 모델 예약이 최대 2회이며 실패/응답 저장 전 장애에도 소비된다. 분산 결과의 usage.modelAttemptsReserved는 run 전체에서 소비한 예약 수이며 실제 응답 token 합계나 확정 비용과 구분한다. 단독 SQLite는 run/fence당 2회다. 외부 호출의 exactly-once는 보장하지 않는다.

## 내부 API

BE만 Bearer ARCHDECK_AGENT_SERVICE_TOKEN(32자 이상)으로 호출한다. 공개 사용자 세션은 받지 않는다. 브라우저에 4010 포트를 직접 공개하지 않는다.

- POST /internal/runs/{runId}/execute: runId, leaseFence, asOf, policy, 선택 targetTopic. 정책은 domain_id/version/lookback_days/max_documents/max_input_characters/sources/auto_publish=false다.
- source는 host/publisher_group/feed_url/reuse_allowed=true가 필요하다. 권리·이용 조건을 확인한 정책만 활성화한다.
- 결과는 status needs_review 또는 held, draft·documents·discarded·issues·usage·published=false다. BE가 이를 저장하고 관리자 검토를 거쳐 게시한다.
- POST /internal/runs/{runId}/cancel: JSON {"leaseFence": 현재 fence}로 실행 취소를 기록한다. 분산 모드에서는 아직 도착하지 않은 실행에도 영속 취소 tombstone을 남긴다. 낮은 fence 취소는 409다. 실행 중 I/O는 bounded timeout으로 끝나며 다음 단계에서 취소를 확인한다.
- 단독 모드의 동일 runId/fence/body 재전송은 저장 결과를 반환한다. 다른 fence·본문은 409다. 단독 한 프로세스의 동시 실행은 worker_busy다. 분산 모드는 아래 계약을 따른다.
- targetTopic={targetContentId,baseLockVersion,topic}은 BE가 실행 생성 시 고정한 정규 토픽 snapshot이다. 개인 메모·계정 필드는 거부한다. 개정 초안의 revisionPatch는 title/summary/overview/keyTakeaways만 허용하며 BE가 나머지 정규 콘텐츠를 보존·병합하고 변경 비교와 승인 게시를 수행한다.

## 수집과 검증 경계

공개 RSS/Atom의 요약만 정적으로 추출하며 기사 HTML·JS·shell은 실행하지 않는다. HTTPS exact-host allowlist, 공인 DNS 전체 검증, 검증된 IP로 연결하면서 TLS SNI/인증서 host 검증, 리다이렉트 거부, MIME/압축/크기/시간 제한을 적용한다. robots.txt를 먼저 확인하고 미확인·거부·crawl-delay는 보류한다. robots 허용은 콘텐츠 재사용 권한을 뜻하지 않는다.

발행일 없는 항목은 날짜를 만들지 않고 discarded로 기록한다. 현재 run에는 최신성 판단 가능한 자료만 들어간다. 발행일·사건일·수집일을 구분하고 future/기간밖/중복/인용누락은 검토 또는 보류한다. 문자열 인용 연결 검사는 주장 의미의 진위나 독립 출처 검증을 대신하지 않는다. 최종 지식의 정확성은 관리자 검토 책임으로 남긴다.

## 영속화와 재시작

단일 PC 기본은 artifacts/runtime.sqlite 실행 결과 캐시와 .checkpoints의 LangGraph SQLite 저장이다. 수집 완료 뒤 모델 실패 시 체크포인트에서 다시 시작하고 완료 결과는 재호출하지 않는다. 파일은 작업용 로컬 캐시이며 업무 원장은 BE PostgreSQL이다. artifacts는 Git에서 제외하고 디렉터리 권한 0700, 실행 캐시 0600으로 관리한다.

여러 worker/호스트는 ARCHDECK_AGENT_DISTRIBUTED=true와 ARCHDECK_AGENT_CHECKPOINT_DSN이 필수다. PostgreSQL이 없거나 격리되지 않은 역할이면 시작/준비를 거부하며 SQLite로 자동 전환하지 않는다. DB는 BE와 같은 PostgreSQL 인스턴스를 써도 업무 스키마와 Agent 실행 스키마·역할을 분리한다.

배포 관리자는 별도 스키마를 만들고 **배포용 DDL 자격증명**에 search_path=agent_runtime을 지정해 초기화한다. public/archdeck 스키마 초기화는 거부한다. 아래 SQL의 비밀번호는 서버 비밀 저장소에서 주입하고 기록하지 않는다. 앱 역할에 Agent 역할을 부여하지 않는다.

```sql
CREATE SCHEMA agent_runtime;
CREATE ROLE archdeck_agent_worker LOGIN PASSWORD '<server-managed-secret>';
GRANT USAGE ON SCHEMA agent_runtime TO archdeck_agent_worker;
-- 먼저 아래 setup 명령으로 테이블을 만든 뒤 실행한다.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA agent_runtime TO archdeck_agent_worker;
```

```sh
# DDL 전용 DSN을 안전하게 환경으로 주입한다(Agent schema search_path 필수).
uv run python -m archdeck_agent.checkpoint_setup
# 여기부터 CHECKPOINT_DSN을 archdeck_agent_worker DSN으로 교체한다.
# DSN 예시: postgresql://archdeck_agent_worker:<secret>@db/archdeck?options=-csearch_path%3Dagent_runtime
ARCHDECK_AGENT_DISTRIBUTED=true uv run uvicorn archdeck_agent.main:app --host 127.0.0.1 --port 4010 --workers 2
```

runtime 역할은 schema CREATE·superuser·CREATEDB·CREATEROLE·BYPASSRLS와 archdeck 업무 schema USAGE가 없어야 한다. 런타임에는 테이블 생성이 없다. 준비 검사는 registry·receipt·cancel·checkpoint 테이블의 SELECT/INSERT/UPDATE를 각각 확인한다. 배포 관리자가 setup 명령을 다시 실행하면 누락된 신규 Agent 테이블을 생성한다. 실행 캐시나 checkpoint는 업무 원장을 대체하지 않는다.

BE가 업무 lease/fence 권위이고 Agent는 공유 execution_registry에서 요청 해시·현재 fence·owner UUID·generation UUID·30초 lease·모델 예약·완료 결과를 원자적으로 관리한다. Agent heartbeat는 5초마다 lease를 갱신한다. 같은 run/fence 동시 요청은 execution_busy(409), 높은 fence는 기존 소유자를 즉시 무효화한다. 낮은 fence·이전 owner·만료 소유자의 완료는 CAS에서 거부한다. 완료 결과와 SHA-256 receipt는 같은 DB 트랜잭션으로 기록하며 높은 fence의 완료 재전송은 모델 재호출 없이 캐시를 반환하고 현재 fence를 승격한다.

요청 identity는 leaseFence를 제외한 policy/asOf/targetTopic 전체의 canonical SHA-256이다. 같은 run에서 정책·기준 시각·기존 토픽을 바꾸면 run_identity_conflict(409)다. BE는 재시도에도 snapshot을 고정한다. 정책을 바꾼 새 실행은 새 runId를 사용한다. fence 승격은 identity 변경 허용이 아니다.

checkpoint thread는 runId:fence:generation으로 분리한다. 만료·프로세스 강제 종료 후에는 새 generation에서 수집부터 다시 수행하고 이전 실행의 불완전 checkpoint를 재사용하지 않는다. 이전 namespace의 잔여 checkpoint는 새 소유자의 결과를 덮어쓰지 못하며 stale 완료는 저장되지 않는다. 이 보수적인 재시작에서도 모델 예약은 run 전체 2회를 넘지 않는다. 이미 완료된 결과는 공유 registry에서 복구한다. 취소나 fence 상실은 진행 중 외부 I/O를 강제 중단하지 않고 제한 시간 후 다음 단계 및 최종 저장에서 차단한다.

## 검증 상태

단위 검사에서 Azure 모의 요청, 실제 workflow·checkpoint 재시작/재전송/취소, SSRF·XML entity·날짜·근거·인증/입력 크기를 검사했다. 실제 openai.com robots/RSS 1회 수집과 2개 항목 파싱은 통과했다. 실제 Azure smoke는 기존 DWP 설정 endpoint의 DNS 해석 실패로 연결 전에 실패했다. 사용자가 회사망에서만 접근 가능한 환경임을 확인했으므로 추가 라이브 호출은 중단했으며 회사망 연결 후 검증할 항목으로 남긴다. 따라서 실제 Azure 모델 응답 성공은 확인하지 않았다. 키·호스트값은 출력하지 않았다.

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


## 실제 PostgreSQL 다중 프로세스 검사

별도 검증 DB의 배포용 DSN을 ARCHDECK_TEST_PG_DSN으로 주입한 뒤 실행한다. 테스트는 임의 이름의 schema와 DML 전용 역할을 만들고 종료 시 해당 객체만 제거한다. 운영 DB를 지정하지 않는다. 기본 pytest에서는 이 환경이 없으면 PG 검사만 skip한다.

```sh
uv run pytest -q
uv run pytest -q tests/test_distributed.py
uv run ruff check .
uv run ruff format --check .
```

별도 archdeck_app_verify에서 spawned Python 프로세스의 동일 실행 경쟁·새 fence 승격·늦은 완료/취소·취소 후 지연 도착·프로세스 강제 종료와 만료 후 재시작·실제 heartbeat·전역 모델 예약·요청 변조·완료 replay·최소 권한을 실제 PostgreSQL로 검사한다. 수집/모델은 합성 fixture이며 Azure 네트워크 호출은 하지 않는다.

2026-11-27 최종 실행 결과: 실제 PostgreSQL 선택 검사를 포함한 76개 검사 통과, ruff check/format 및 uv lock 검사 통과. Docker 이미지 빌드와 UID 10001·health 200·미설정 ready 503을 확인했다. 합성 모델/수집으로 실제 PostgreSQL 역할을 사용하는 Uvicorn worker 2개의 health/ready 200도 확인했다. 실제 Azure 모델 호출은 회사망 검증으로 유예했다.

분산 컨테이너는 PostgreSQL과 같은 Docker 네트워크에 연결한다. 이 저장소의 예제 DB는 archdeck_default 네트워크의 db:5432로 접근하며 컨테이너 내부 127.0.0.1은 호스트 DB가 아니다. 컨테이너용 runtime DSN에 해당 주소를 지정하고 ARCHDECK_AGENT_DISTRIBUTED=true를 주입한 뒤 이미지 명령을 `.venv/bin/uvicorn archdeck_agent.main:app --host 0.0.0.0 --port 4010 --workers 2`로 지정한다.

동일 검증에서 BE 4500의 정규 토픽 snapshot을 실제 4010 worker 2개에 전달해 topic_revision 후보·diff·게시를 연결했다. Agent 공유 PG에서 각 run의 fence 2·model 예약 1회·receipt 및 checkpoint 저장을 대조했다. BE 측 원자적 개정 게시·기존 메타데이터 보존·stale 409와 FE 검토 화면 확인 후 합성 서버를 중지하고 이 검증에서만 만든 임시 Agent schema/role과 비공개 환경 파일을 제거했다. 기존 앱·DWP 데이터와 스키마는 유지했다.
