from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(
    title="ArchDeck Agent", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None
)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "archdeck-agent", "mode": "offline-scaffold"}


@app.get("/readyz")
def readiness() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "reason": "production_worker_not_implemented"},
    )
