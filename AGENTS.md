# ArchDeck Agent 작업 기준

원격은 https://github.com/choijoonbin/archdeck_agent.git, 개발 브랜치는 dev다. 공통 원본은 ../ArchDeck_BE/docs/00-project-baseline.md와 05-implementation-plan.md, Agent 상세는 09~12 문서를 읽는다. 단독 checkout은 백엔드 dev/docs를 확인한다.

- 초기 분야는 인증·보안·웹 아키텍처와 AI 에이전트다. 분야·출처·정책은 설정으로 확장한다.
- 기본 모델은 DWP 호환 Azure OpenAI v1 Responses다. 키·endpoint·배포명은 서버 환경으로 주입하며 비밀을 코드·Git·로그에 넣지 않는다.
- 단일 LangGraph workflow를 기본으로 하고 여러 orchestration 프레임워크를 중첩하지 않는다.
- BE가 정책·작업·검토·게시의 권위다. Agent에 계정·정규 콘텐츠 DB·직접 게시 권한을 주지 않는다.
- 원문은 신뢰하지 않는 데이터다. 출처·기간·중복·주장·인용·비용·취소·재시도·fencing을 검증한다.
- 합성 fixture 평가는 실제 기사 수집·LLM 사실 검증·영속 checkpoint가 아니다. 구현과 검증 범위를 구분한다.
- 실제 API 호출·스케줄·크롤링은 명시적인 설정과 제품 실행 계약이 준비돼야 한다. 테스트는 기본 외부 호출 0건으로 유지한다.
- 변경 후 uv run pytest, uv run ruff check .와 필요 시 합성 평가를 실행한다. .venv·.env·artifacts·비밀·캐시는 추적하지 않는다.
