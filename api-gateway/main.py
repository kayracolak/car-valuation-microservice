import os
import uuid
import logging
import sys
from contextvars import ContextVar

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
import jwt
from pythonjsonlogger import jsonlogger
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

# ── Tracing ────────────────────────────────────────────────────────────────────
_resource = Resource.create({"service.name": "api-gateway"})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://jaeger:4318/v1/traces"))
)
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer(__name__)
HTTPXClientInstrumentor().instrument()

# ── Structured logging ─────────────────────────────────────────────────────────
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
SERVICE_NAME = "api-gateway"


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.service = SERVICE_NAME
        record.trace_id = _trace_id_var.get("")
        return True


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    jsonlogger.JsonFormatter(fmt="%(asctime)s %(levelname)s %(name)s %(message)s")
)
_handler.addFilter(_ContextFilter())
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(SERVICE_NAME)

# ── Rate limiter ───────────────────────────────────────────────────────────────
_limiter = Limiter(key_func=get_remote_address)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="API Gateway")
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
try:
    Instrumentator().instrument(app).expose(app)
except Exception:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "gizli_jwt_anahtari_degistir_123")
ALGORITHM = "HS256"
AUTH_SERVICE_URL = "http://auth-service:8001"
VALUATION_SERVICE_URL = "http://valuation-service:8000"
_TIMEOUT = httpx.Timeout(30.0)

if len(SECRET_KEY) < 32:
    logger.warning(
        "JWT_SECRET_KEY 32 karakterden kısa — production ortamında güvenli bir key kullanın!",
        extra={"key_length": len(SECRET_KEY)},
    )


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())
    token = _trace_id_var.set(trace_id)
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-ID"] = trace_id
    _trace_id_var.reset(token)
    return response


def verify_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token süresi dolmuş!")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Geçersiz token!")


@app.get("/")
def root():
    return {"mesaj": "API Gateway çalışıyor!"}


@app.get("/health")
async def health():
    deps = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        for name, url in [
            ("auth-service", AUTH_SERVICE_URL),
            ("valuation-service", VALUATION_SERVICE_URL),
        ]:
            try:
                resp = await client.get(f"{url}/health")
                deps[name] = "ok" if resp.status_code == 200 else "degraded"
            except Exception:
                deps[name] = "unreachable"

    overall = "ok" if all(v == "ok" for v in deps.values()) else "degraded"
    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={"status": overall, "service": SERVICE_NAME, "dependencies": deps},
    )


@app.post("/register")
@_limiter.limit("10/minute")
async def register(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{AUTH_SERVICE_URL}/register",
            json=body,
            headers={"X-Trace-ID": request.state.trace_id},
        )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/login")
@_limiter.limit("20/minute")
async def login(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{AUTH_SERVICE_URL}/login",
            json=body,
            headers={"X-Trace-ID": request.state.trace_id},
        )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/api/v1/degerleme")
@_limiter.limit("30/minute")
async def degerleme(request: Request, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header eksik! Format: 'Bearer <token>'",
        )
    token = authorization.split(" ")[1]
    payload = verify_jwt(token)
    user = payload.get("sub")
    logger.info(
        "Yetkili istek yönlendiriliyor",
        extra={"user": user, "endpoint": "/api/v1/degerleme"},
    )
    body = await request.json()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{VALUATION_SERVICE_URL}/api/v1/degerleme",
            json=body,
            headers={"X-Trace-ID": request.state.trace_id},
        )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/api/v1/arac-asistan")
@_limiter.limit("10/minute")
async def arac_asistan(request: Request, authorization: str = Header(None)):
    """AI araç danışmanlık chatbot — JWT zorunlu, dakikada 10 istek."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header eksik! Format: 'Bearer <token>'",
        )
    token = authorization.split(" ")[1]
    payload = verify_jwt(token)
    logger.info(
        "Asistan isteği yönlendiriliyor",
        extra={"user": payload.get("sub")},
    )
    body = await request.json()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{VALUATION_SERVICE_URL}/api/v1/arac-asistan",
            json=body,
            headers={"X-Trace-ID": request.state.trace_id},
        )
    return JSONResponse(status_code=response.status_code, content=response.json())
