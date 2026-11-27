from __future__ import annotations

import hmac
import os
from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .provider import ProviderConfiguration, ProviderError
from .runtime import ExecutionRequest, ResearchRuntime, RuntimeErrorCode

app = FastAPI(
    title="ArchDeck Agent", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None
)


@app.middleware("http")
async def request_boundaries(request: Request, call_next):
    if request.url.path.startswith("/internal/"):
        expected = os.getenv("ARCHDECK_AGENT_SERVICE_TOKEN", "")
        supplied = request.headers.get("authorization", "")
        if len(expected) < 32 or not hmac.compare_digest(
            supplied.encode(), ("Bearer " + expected).encode()
        ):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 100_000:
                return JSONResponse(status_code=413, content={"error": "request_limit"})
        request._body = bytes(body)
    result = await call_next(request)
    result.headers["Cache-Control"] = "no-store"
    return result


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    return JSONResponse(status_code=422, content={"error": "invalid_request"})


@lru_cache
def runtime():
    return ResearchRuntime()


def authorize(authorization: str | None = Header(default=None)):
    expected = os.getenv("ARCHDECK_AGENT_SERVICE_TOKEN", "")
    if len(expected) < 32 or not hmac.compare_digest(
        (authorization or "").encode(), ("Bearer " + expected).encode()
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "archdeck-agent", "mode": "bounded-research"}


@app.get("/readyz")
def readiness():
    try:
        config = ProviderConfiguration.from_environment()
        ready = (
            config.configured
            and config.live_enabled
            and len(os.getenv("ARCHDECK_AGENT_SERVICE_TOKEN", "")) >= 32
        )
    except ProviderError:
        ready = False
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "configured": ready},
    )


@app.post("/internal/runs/{run_id}/execute", dependencies=[Depends(authorize)])
def execute(run_id: str, body: ExecutionRequest):
    if run_id != body.runId:
        raise HTTPException(status_code=409, detail="run_identity_conflict")
    try:
        config = ProviderConfiguration.from_environment()
        if not config.live_enabled or not config.configured:
            raise HTTPException(status_code=503, detail="provider_not_ready")
        return runtime().execute(body)
    except RuntimeErrorCode as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None


@app.post("/internal/runs/{run_id}/cancel", dependencies=[Depends(authorize)])
def cancel(run_id: str):
    runtime().cancel(run_id)
    return {"runId": run_id, "status": "cancel_requested"}
