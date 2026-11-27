from fastapi.testclient import TestClient

from archdeck_agent.main import app


def test_health_is_not_production_readiness():
    with TestClient(app) as client:
        assert client.get("/healthz").json()["mode"] == "offline-scaffold"
        assert client.get("/readyz").status_code == 503
        assert client.post("/internal/runs").status_code == 404
