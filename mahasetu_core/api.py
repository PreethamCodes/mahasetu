"""HTTP surface of MahaSetu Core. Mirrors contracts/v1/openapi.json (the authoritative contract)."""
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import outbox
from .errors import ApiError
from .security import bearer
from .service import Core
from .settings import Settings

logger = logging.getLogger("uvicorn.error")
ProviderPath = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,40}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRequest(Strict):
    provider_id: str = Field(min_length=1)
    evidence_categories: list[str] = Field(min_length=1)


class CreateVerificationRequest(Strict):
    service_id: str = Field(min_length=1)
    application_reference: str = Field(min_length=1)
    applicant_reference: str = Field(min_length=1)
    applicant_mobile: str = Field(pattern=r"^[6-9][0-9]{9}$")
    purpose: str = Field(min_length=1)
    requested_evidence: list[EvidenceRequest] = Field(min_length=1)


class LaunchExchange(Strict):
    launch_reference: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class OtpVerify(Strict):
    challenge_reference: UUID
    code: str = Field(pattern=r"^[0-9]{6}$")


def create_app(core: Core | None = None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.core = core or Core(Settings.from_env())
        try:
            outbox.dispatch(app.state.core.db, app.state.core.sender, app.state.core.now())
        except Exception:  # noqa: BLE001 - a cold database must not stop the service from starting
            logger.warning("could not flush queued messages at startup")
        yield
        app.state.core.close()

    app = FastAPI(title="MahaSetu Core", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def service(request: Request) -> Core:
        return request.app.state.core

    def requester(request: Request, authorization: Annotated[str | None, Header()] = None):
        return service(request).authenticate_requester(bearer(authorization))

    def dashboard(request: Request, authorization: Annotated[str | None, Header()] = None):
        service(request).authenticate_dashboard(bearer(authorization))

    def applicant(request: Request, authorization: Annotated[str | None, Header()] = None):
        return service(request).session(bearer(authorization))

    Core_ = Annotated[Core, Depends(service)]
    Requester = Annotated[str, Depends(requester)]
    Applicant = Annotated[object, Depends(applicant)]

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return ApiError(400, "INVALID_REQUEST", f"The request is not valid{': ' + where if where else ''}.").response()

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return ApiError(exc.status_code, codes.get(exc.status_code, "REQUEST_REJECTED"), "The request was rejected.",
                        category="MISSING" if exc.status_code == 404 else "INPUT").response()

    @app.middleware("http")
    async def envelope(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            logger.exception("unhandled error")
            response = ApiError(500, "INTERNAL_ERROR", "Unexpected Core error.", retryable=True).response()
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        logger.info(json.dumps({"event": "request", "method": request.method,
                                "route": getattr(route, "path", "unmatched"), "status": response.status_code}))
        return response

    # ---- requesting service ------------------------------------------------------------------

    @app.post("/v1/verification-requests", status_code=201)
    def create(body: CreateVerificationRequest, svc: Core_, who: Requester,
               idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
        if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header of 1 to 128 characters.")
        return svc.create_request(who, body.model_dump(), idempotency_key.strip())

    @app.get("/v1/verification-requests/{request_id}")
    def read(request_id: UUID, svc: Core_, who: Requester):
        return svc.get_request(who, request_id)

    @app.post("/v1/verification-requests/{request_id}/cancel")
    def cancel(request_id: UUID, svc: Core_, who: Requester):
        return svc.cancel(who, request_id)

    @app.get("/v1/verification-requests/{request_id}/results")
    def results(request_id: UUID, svc: Core_, who: Requester):
        return svc.results(who, request_id)

    @app.get("/v1/services/{service_id}/requirements")
    def requirements(service_id: Annotated[str, Path(min_length=1, max_length=64)], svc: Core_, who: Requester):
        if service_id != who:
            raise ApiError(403, "SERVICE_MISMATCH", "The credential belongs to a different service.")
        return svc.requirements(service_id)

    # ---- MahaSetu website (server-to-server) --------------------------------------------------

    @app.post("/v1/applicant/sessions", status_code=201, dependencies=[Depends(dashboard)])
    def exchange(body: LaunchExchange, svc: Core_):
        return svc.exchange(body.launch_reference)

    @app.get("/v1/messages", dependencies=[Depends(dashboard)])
    def messages(svc: Core_, transaction_reference: UUID | None = None,
                 kind: Literal["OTP", "VERIFICATION_RESULT"] | None = None,
                 limit: Annotated[int, Query(ge=1, le=100)] = 25,
                 cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None):
        return svc.messages(transaction_reference, kind, limit, cursor)

    # ---- applicant session ------------------------------------------------------------------------

    @app.get("/v1/applicant/session")
    def overview(svc: Core_, session: Applicant):
        return svc.overview(session)

    @app.post("/v1/applicant/session/departments/{provider_id}/otp", status_code=202)
    def send_otp(provider_id: ProviderPath, svc: Core_, session: Applicant):
        return svc.send_otp(session, provider_id)

    @app.post("/v1/applicant/session/departments/{provider_id}/otp/verify")
    def verify_otp(provider_id: ProviderPath, body: OtpVerify, svc: Core_, session: Applicant):
        return svc.verify_otp(session, provider_id, body.challenge_reference, body.code)

    @app.post("/v1/applicant/session/departments/{provider_id}/verify", status_code=202)
    def verify(provider_id: ProviderPath, svc: Core_, session: Applicant, background: BackgroundTasks):
        card, run_id = svc.start_check(session, provider_id)
        background.add_task(svc.run_retrieval, session.request_id, provider_id, run_id)
        return card

    @app.post("/v1/applicant/session/departments/{provider_id}/decline")
    def decline(provider_id: ProviderPath, svc: Core_, session: Applicant):
        return svc.decline(session, provider_id)

    @app.post("/v1/applicant/session/complete")
    def complete(svc: Core_, session: Applicant):
        return svc.complete(session)

    @app.get("/v1/applicant/session/result")
    def result(svc: Core_, session: Applicant):
        return svc.applicant_result(session)

    @app.get("/health")
    def health(svc: Core_):
        ready = svc.health()
        return JSONResponse({"status": "ok" if ready else "unavailable"}, status_code=200 if ready else 503)

    return app
