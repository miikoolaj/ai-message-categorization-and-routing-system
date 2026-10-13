"""Liveness and readiness endpoints (used by the Docker healthcheck and scripts/wait-ready.sh)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_router.web.dependencies import ContainerDep
from ai_router.web.schemas import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/live", summary="Liveness probe - the process is running")
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/ready",
    summary="Readiness probe - Ollama, the model and SMTP are reachable",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is not ready"}},
)
async def ready(container: ContainerDep) -> JSONResponse:
    report = await container.readiness.check()
    status_code = 200 if report.status == "ready" else 503
    return JSONResponse(report.model_dump(mode="json", exclude_none=True), status_code=status_code)
