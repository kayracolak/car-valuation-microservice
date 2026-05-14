import os
import uuid
import logging
import sys
from contextvars import ContextVar

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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


@app.get("/", response_class=HTMLResponse)
def root():
    return _UI_HTML


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


@app.post("/api/v1/pazarlik-kocu")
@_limiter.limit("15/minute")
async def pazarlik_kocu(request: Request, authorization: str = Header(None)):
    """Pazarlık koçu — JWT zorunlu, dakikada 15 istek."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header eksik! Format: 'Bearer <token>'",
        )
    token = authorization.split(" ")[1]
    payload = verify_jwt(token)
    logger.info(
        "Pazarlık koçu isteği yönlendiriliyor",
        extra={"user": payload.get("sub")},
    )
    body = await request.json()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{VALUATION_SERVICE_URL}/api/v1/pazarlik-kocu",
            json=body,
            headers={"X-Trace-ID": request.state.trace_id},
        )
    return JSONResponse(status_code=response.status_code, content=response.json())


# ── Unified Single-Page UI ─────────────────────────────────────────────────────
_UI_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Araç Değerleme Sistemi</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f1f5f9;--card:#fff;--border:#e2e8f0;
  --primary:#4f46e5;--primary-d:#4338ca;--primary-l:#ede9fe;
  --green:#10b981;--red:#ef4444;--amber:#f59e0b;
  --text:#0f172a;--muted:#64748b;--faint:#f8fafc;
  --r:14px;--sh:0 1px 8px rgba(0,0,0,.08);
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

/* ── Header ── */
.header{background:#fff;border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:0 1px 6px rgba(0,0,0,.06)}
.header-logo{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:800;color:var(--text);letter-spacing:-.3px}
.header-logo-icon{width:32px;height:32px;background:var(--primary);border-radius:8px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px}
.header-right{display:flex;align-items:center;gap:10px}
.user-chip{background:var(--primary-l);color:var(--primary);font-size:13px;font-weight:600;padding:5px 12px;border-radius:20px}
.btn-logout{padding:6px 14px;background:none;border:1.5px solid var(--border);border-radius:8px;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;transition:.2s}
.btn-logout:hover{border-color:var(--red);color:var(--red)}

/* ── Wrap ── */
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 80px}

/* ── Auth Screen ── */
#auth-screen{max-width:440px;margin:60px auto 0}
.auth-title{font-size:24px;font-weight:800;text-align:center;margin-bottom:6px}
.auth-sub{color:var(--muted);font-size:14px;text-align:center;margin-bottom:28px}
.auth-tabs{display:flex;gap:6px;margin-bottom:16px}
.auth-tab{flex:1;padding:10px;text-align:center;border:1.5px solid var(--border);border-radius:9px;cursor:pointer;font-size:14px;font-weight:600;background:#fff;color:var(--muted);transition:.2s}
.auth-tab.active{background:var(--primary);color:#fff;border-color:var(--primary)}

/* ── Card ── */
.card{background:var(--card);border-radius:var(--r);box-shadow:var(--sh);padding:22px}
.card+.card{margin-top:14px}
.section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:12px}

/* ── Forms ── */
.field-group{margin-bottom:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:640px){.grid2,.grid3{grid-template-columns:1fr}}
label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:5px;text-transform:uppercase;letter-spacing:.3px}
input,select{width:100%;padding:10px 13px;border:1.5px solid var(--border);border-radius:9px;font-size:15px;outline:none;transition:border-color .2s,box-shadow .2s;background:#fff;color:var(--text)}
input:focus,select:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(79,70,229,.1)}
.check-row{display:flex;align-items:center;gap:10px;padding:12px 0}
.check-row input{width:18px;height:18px;accent-color:var(--primary);cursor:pointer}
.check-row label{margin:0;font-size:15px;text-transform:none;letter-spacing:0;color:var(--text);font-weight:400;cursor:pointer}
.btn-primary{width:100%;margin-top:10px;padding:13px;background:var(--primary);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;transition:background .2s,transform .1s;letter-spacing:.2px}
.btn-primary:hover:not(:disabled){background:var(--primary-d)}
.btn-primary:active:not(:disabled){transform:scale(.99)}
.btn-primary:disabled{background:#a5b4fc;cursor:not-allowed}
.btn-secondary{padding:10px 18px;background:var(--primary-l);color:var(--primary);border:none;border-radius:9px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s;white-space:nowrap}
.btn-secondary:hover:not(:disabled){background:#ddd6fe}
.btn-secondary:disabled{opacity:.5;cursor:not-allowed}

/* ── Page title ── */
.page-title{font-size:22px;font-weight:800;letter-spacing:-.4px;margin-bottom:2px}
.page-sub{color:var(--muted);font-size:14px;margin-bottom:22px}

/* ── Tabs ── */
.tabs{display:flex;gap:6px;margin-bottom:14px;border-bottom:2px solid var(--border);padding-bottom:0}
.tab-btn{padding:10px 18px;background:none;border:none;border-bottom:3px solid transparent;margin-bottom:-2px;font-size:14px;font-weight:600;color:var(--muted);cursor:pointer;transition:.2s;border-radius:8px 8px 0 0}
.tab-btn:hover{color:var(--primary);background:var(--primary-l)}
.tab-btn.active{color:var(--primary);border-bottom-color:var(--primary)}
.tab-panel{display:none;animation:fadeIn .25s ease}
.tab-panel.active{display:block}

/* ── Price Hero ── */
.price-hero{font-size:44px;font-weight:900;letter-spacing:-1px;line-height:1}
.price-sub{font-size:15px;color:var(--muted);margin-top:4px}
.badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;letter-spacing:.4px}
.badge-firsat{background:#d1fae5;color:#065f46}
.badge-rekabetci{background:#dbeafe;color:#1e40af}
.badge-piyasa{background:#f3f4f6;color:#374151}
.badge-pahali{background:#fef3c7;color:#92400e}
.badge-cokpahali{background:#fee2e2;color:#991b1b}

/* ── Market Bar ── */
.mktbar{position:relative;height:8px;background:#e2e8f0;border-radius:4px;margin:18px 0 6px}
.mktbar-fill{position:absolute;top:0;left:0;height:8px;background:linear-gradient(90deg,var(--green),var(--primary));border-radius:4px;transition:width 1s cubic-bezier(.4,0,.2,1)}
.mktbar-dot{position:absolute;top:50%;transform:translate(-50%,-50%);width:18px;height:18px;background:var(--primary);border:3px solid #fff;border-radius:50%;box-shadow:0 2px 8px rgba(79,70,229,.4);transition:left 1s cubic-bezier(.4,0,.2,1)}
.mktbar-labels{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px}
.mktbar-center{text-align:center;font-size:13px;font-weight:700;color:var(--primary);margin-top:2px}

/* ── Gauge ── */
.gauge-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%}
.gauge-num{font-size:38px;font-weight:900;color:var(--primary);line-height:1;margin-top:-16px}
.gauge-lbl{font-size:12px;color:var(--muted);margin-top:2px}
.gauge-detail{font-size:13px;color:var(--text);margin-top:8px;text-align:center}
.gauge-detail-sub{font-size:12px;color:var(--muted);margin-top:2px;text-align:center}

/* ── DNA Bars ── */
.dna-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.dna-label{font-size:12px;color:var(--muted);width:160px;flex-shrink:0}
.dna-bar-track{flex:1;height:8px;background:#f1f5f9;border-radius:4px;overflow:hidden}
.dna-bar-fill{height:8px;border-radius:4px;width:0;transition:width 1s cubic-bezier(.4,0,.2,1)}
.dna-val{font-size:13px;font-weight:700;width:110px;text-align:right;flex-shrink:0}
.dna-divider{border:none;border-top:1px solid var(--border);margin:10px 0 8px}
.dna-total{display:flex;justify-content:space-between;font-size:14px;font-weight:700;padding:4px 0}

/* ── Insight Cards ── */
.insight-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:640px){.insight-grid{grid-template-columns:1fr}}
.insight-card{background:var(--faint);border:1px solid var(--border);border-radius:12px;padding:16px}
.insight-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:6px}
.insight-text{font-size:13px;line-height:1.6;color:var(--text)}

/* ── Piyasa ── */
.ai-text{font-size:14px;line-height:1.7;color:var(--muted);margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:8px 0;border-bottom:1px solid var(--border)}
td{padding:9px 0;border-bottom:1px solid var(--faint);color:var(--text)}
tr:last-child td{border-bottom:none}
.il-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.il-name{font-size:13px;color:var(--muted);width:100px;flex-shrink:0}
.il-track{flex:1;height:6px;background:#f1f5f9;border-radius:3px;overflow:hidden}
.il-fill{height:6px;background:var(--primary);border-radius:3px;width:0;transition:width .8s ease}
.il-price{font-size:13px;font-weight:700;width:100px;text-align:right}
.dep-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--faint)}
.dep-row:last-child{border-bottom:none}
.dep-yr{font-size:13px;color:var(--muted)}
.dep-val{font-weight:700;font-size:14px}
.dep-loss{font-size:11px;color:var(--red);margin-left:6px}

/* ── Pazarlık Koçu ── */
.koc-input-row{display:flex;gap:10px;margin-bottom:16px}
.koc-input-row input{flex:1}
.koc-summary-bar{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.koc-stat{flex:1;min-width:120px;background:var(--faint);border:1px solid var(--border);border-radius:10px;padding:12px 14px;text-align:center}
.koc-stat strong{display:block;font-size:16px;font-weight:800;color:var(--text);margin-bottom:2px}
.koc-stat span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
.koc-stat.savings strong{color:var(--green)}
.koc-ozet{font-size:14px;color:var(--muted);line-height:1.6;margin-bottom:14px;font-style:italic}
.koc-arg{display:flex;align-items:flex-start;gap:12px;padding:14px;background:var(--faint);border:1px solid var(--border);border-radius:10px;margin-bottom:10px}
.koc-num{width:28px;height:28px;border-radius:50%;background:var(--primary);color:#fff;font-size:13px;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px}
.koc-body{flex:1}
.koc-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:3px}
.koc-detail{font-size:13px;color:var(--muted);line-height:1.5}
.koc-save{font-size:13px;font-weight:700;color:var(--red);white-space:nowrap;padding-top:2px}

/* ── Chat ── */
.chat-ctx{background:var(--primary-l);border:1px solid #c7d2fe;border-radius:10px;padding:12px 14px;font-size:13px;color:var(--primary);margin-bottom:14px;font-weight:500}
.chat-msgs{height:340px;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding:4px 0;margin-bottom:14px}
.chat-msg{max-width:80%;padding:11px 14px;border-radius:12px;font-size:14px;line-height:1.55;white-space:pre-wrap}
.chat-user{align-self:flex-end;background:var(--primary);color:#fff;border-bottom-right-radius:4px}
.chat-bot{align-self:flex-start;background:var(--faint);border:1px solid var(--border);color:var(--text);border-bottom-left-radius:4px}
.chat-input-row{display:flex;gap:8px}
.chat-input-row input{flex:1}
.chat-send{padding:10px 18px;background:var(--primary);color:#fff;border:none;border-radius:9px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;transition:.2s}
.chat-send:hover{background:var(--primary-d)}
.chat-send:disabled{background:#a5b4fc;cursor:not-allowed}
.thinking{display:flex;gap:5px;align-items:center;padding:4px 0}
.thinking span{width:7px;height:7px;border-radius:50%;background:#94a3b8;animation:bounce 1.2s infinite}
.thinking span:nth-child(2){animation-delay:.2s}
.thinking span:nth-child(3){animation-delay:.4s}

/* ── Advice / Warn ── */
.advice-box{background:#f5f3ff;border-left:4px solid var(--primary);border-radius:0 10px 10px 0;padding:14px 16px}
.advice-row{display:flex;gap:10px;margin-bottom:10px;font-size:14px;color:var(--text)}
.advice-row:last-child{margin-bottom:0}
.warn-box{background:#fff7ed;border-left:4px solid var(--amber);border-radius:0 10px 10px 0;padding:14px 16px}
.warn-item{font-size:13px;color:#7c2d12;margin-bottom:6px;display:flex;gap:8px}
.warn-item:last-child{margin-bottom:0}

/* ── Monitoring Panel ── */
.monitoring-section{margin-top:36px;padding-top:28px;border-top:2px solid var(--border)}
.monitoring-title{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:14px}
.mon-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:640px){.mon-grid{grid-template-columns:repeat(2,1fr)}}
.mon-card{display:flex;flex-direction:column;align-items:center;gap:6px;padding:14px 10px;background:#fff;border:1.5px solid var(--border);border-radius:12px;text-decoration:none;color:var(--text);transition:.2s;cursor:pointer}
.mon-card:hover{border-color:var(--primary);background:var(--primary-l);transform:translateY(-2px);box-shadow:0 4px 12px rgba(79,70,229,.15)}
.mon-icon{font-size:22px}
.mon-name{font-size:12px;font-weight:700;color:var(--text)}
.mon-port{font-size:11px;color:var(--muted)}
.mon-dot{width:7px;height:7px;border-radius:50%;background:#d1d5db}
.mon-dot.ok{background:var(--green)}

/* ── Health Strip ── */
.health-strip{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
.health-chip{display:flex;align-items:center;gap:6px;padding:5px 12px;background:#fff;border:1px solid var(--border);border-radius:20px;font-size:12px;font-weight:600;color:var(--muted)}
.health-chip .dot{width:8px;height:8px;border-radius:50%;background:#d1d5db;flex-shrink:0}
.health-chip .dot.ok{background:var(--green)}
.health-chip .dot.err{background:var(--red)}

/* ── Utils ── */
.hidden{display:none!important}
.error-msg{background:#fff0f0;border-radius:10px;padding:12px 14px;color:var(--red);font-size:14px}
.loading-dots{display:flex;gap:6px;align-items:center;padding:16px;justify-content:center;color:var(--muted);font-size:14px}
.loading-dots span{width:7px;height:7px;border-radius:50%;background:var(--primary);animation:bounce 1.2s infinite}
.loading-dots span:nth-child(2){animation-delay:.2s}
.loading-dots span:nth-child(3){animation-delay:.4s}
.slide-in{animation:slideIn .4s ease both}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
</style>
</head>
<body>

<!-- ── Header ────────────────────────────────────────────────────────────── -->
<div class="header">
  <div class="header-logo">
    <div class="header-logo-icon">🚗</div>
    Araç Değerleme Sistemi
  </div>
  <div class="header-right" id="header-right">
    <span class="user-chip hidden" id="user-chip"></span>
    <button class="btn-logout hidden" id="logout-btn" onclick="logout()">Çıkış</button>
  </div>
</div>

<div class="wrap">

<!-- ── Auth Screen ───────────────────────────────────────────────────────── -->
<div id="auth-screen">
  <div class="auth-title">Hoş Geldiniz</div>
  <p class="auth-sub">Devam etmek için giriş yapın veya yeni hesap oluşturun</p>

  <div class="auth-tabs">
    <div class="auth-tab active" id="auth-tab-login" onclick="authSwitchTab('login')">Giriş Yap</div>
    <div class="auth-tab" id="auth-tab-register" onclick="authSwitchTab('register')">Kayıt Ol</div>
  </div>

  <div class="card">
    <div class="field-group">
      <label>Kullanıcı Adı</label>
      <input type="text" id="auth-username" placeholder="kullanici_adi" onkeydown="if(event.key==='Enter')authSubmit()">
    </div>
    <div class="field-group">
      <label>Şifre</label>
      <input type="password" id="auth-password" placeholder="••••••••" onkeydown="if(event.key==='Enter')authSubmit()">
    </div>
    <button class="btn-primary" id="auth-btn" onclick="authSubmit()">Giriş Yap</button>
    <div class="error-msg hidden" id="auth-error" style="margin-top:12px"></div>
    <div style="margin-top:12px;padding:12px;background:#f0fdf4;border-radius:10px;font-size:13px;color:#166534;display:none" id="auth-success"></div>
  </div>
</div>

<!-- ── Main App ──────────────────────────────────────────────────────────── -->
<div id="app-screen" class="hidden">

  <!-- Health Strip -->
  <div class="health-strip" id="health-strip">
    <div class="health-chip"><div class="dot" id="hc-gateway"></div> API Gateway</div>
    <div class="health-chip"><div class="dot" id="hc-auth"></div> Auth Service</div>
    <div class="health-chip"><div class="dot" id="hc-valuation"></div> Valuation Service</div>
  </div>

  <h1 class="page-title">Araç Değerleme</h1>
  <p class="page-sub">Piyasa analizi · Talep skoru · AI koçu · Pazarlık argümanları</p>

  <!-- Form Card -->
  <div class="card">
    <div class="grid2">
      <div>
        <label>Marka</label>
        <input type="text" id="marka" placeholder="Toyota, BMW, Ford…">
      </div>
      <div>
        <label>Şehir</label>
        <select id="il">
          <option value="ankara">Ankara</option>
          <option value="istanbul">İstanbul</option>
          <option value="izmir">İzmir</option>
          <option value="bursa">Bursa</option>
          <option value="antalya">Antalya</option>
          <option value="adana">Adana</option>
          <option value="konya">Konya</option>
          <option value="gaziantep">Gaziantep</option>
          <option value="kayseri">Kayseri</option>
          <option value="mersin">Mersin</option>
          <option value="eskişehir">Eskişehir</option>
          <option value="kocaeli">Kocaeli</option>
          <option value="trabzon">Trabzon</option>
          <option value="samsun">Samsun</option>
          <option value="denizli">Denizli</option>
        </select>
      </div>
    </div>
    <div class="grid2" style="margin-top:14px">
      <div>
        <label>Model Yılı</label>
        <input type="number" id="model_yili" placeholder="2020" min="1990" max="2026">
      </div>
      <div>
        <label>Kilometre</label>
        <input type="number" id="kilometre" placeholder="80000" min="0">
      </div>
    </div>
    <div class="check-row">
      <input type="checkbox" id="hasar_kaydi">
      <label for="hasar_kaydi">Hasar kaydı var</label>
    </div>
    <button class="btn-primary" id="main-btn" onclick="degerle()">Değerle</button>
    <div class="error-msg hidden" id="form-error" style="margin-top:10px"></div>
  </div>

  <!-- Results -->
  <div id="results" class="hidden" style="margin-top:20px">
    <div class="tabs">
      <button class="tab-btn active" id="t-ozet" onclick="switchTab('ozet')">Özet</button>
      <button class="tab-btn" id="t-piyasa" onclick="switchTab('piyasa')">Piyasa</button>
      <button class="tab-btn" id="t-koc" onclick="switchTab('koc')">AI Koç</button>
      <button class="tab-btn" id="t-asistan" onclick="switchTab('asistan')">Asistan</button>
    </div>

    <!-- Tab: Özet -->
    <div class="tab-panel active" id="p-ozet">
      <div class="grid2">
        <div class="card">
          <div class="section-title">Tahmini Piyasa Değeri</div>
          <div class="price-hero"><span id="price-counter">0</span> TL</div>
          <div class="price-sub" id="price-sub"></div>
          <div style="margin-top:10px" id="price-badge-wrap"></div>
          <div id="mktbar-wrap" class="hidden">
            <div class="mktbar"><div class="mktbar-fill" id="mktbar-fill"></div><div class="mktbar-dot" id="mktbar-dot"></div></div>
            <div class="mktbar-labels"><span id="mktbar-min">—</span><span>Piyasa Aralığı</span><span id="mktbar-max">—</span></div>
            <div class="mktbar-center" id="mktbar-center"></div>
          </div>
        </div>
        <div class="card" id="gauge-card" style="display:none">
          <div class="section-title">Talep Skoru</div>
          <div class="gauge-wrap">
            <svg viewBox="0 0 200 120" width="180" height="108" style="overflow:visible">
              <defs><linearGradient id="gGrad" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" stop-color="#ef4444"/><stop offset="50%" stop-color="#f59e0b"/><stop offset="100%" stop-color="#10b981"/></linearGradient></defs>
              <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e2e8f0" stroke-width="16" stroke-linecap="round"/>
              <path id="gauge-arc" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="url(#gGrad)" stroke-width="16" stroke-linecap="round" stroke-dasharray="251" stroke-dashoffset="251"/>
            </svg>
            <div class="gauge-num" id="gauge-num">0</div>
            <div class="gauge-lbl">/ 10</div>
            <div class="gauge-detail" id="gauge-detail"></div>
            <div class="gauge-detail-sub" id="gauge-sub"></div>
          </div>
        </div>
      </div>
      <div class="card slide-in">
        <div class="section-title">Fiyat Faktör Analizi</div>
        <div id="dna-bars"></div>
      </div>
    </div>

    <!-- Tab: Piyasa -->
    <div class="tab-panel" id="p-piyasa">
      <div class="card">
        <div class="section-title">AI Piyasa Yorumu</div>
        <p class="ai-text" id="ai-piyasa-yorumu"></p>
        <p style="font-size:14px;color:#555;line-height:1.6" id="ai-ozet"></p>
      </div>
      <div class="card hidden" id="card-ilanlar">
        <div class="section-title">Benzer İlanlar</div>
        <table><thead><tr><th>Marka / Yıl</th><th>Kilometre</th><th>Şehir</th><th>Fiyat</th><th>İlan</th></tr></thead><tbody id="ilanlar-body"></tbody></table>
      </div>
      <div class="grid2">
        <div class="card hidden" id="card-iller"><div class="section-title">Şehre Göre Fiyat</div><div id="il-list"></div></div>
        <div class="card hidden" id="card-dep"><div class="section-title">Değer Tahmini</div><div id="dep-list"></div></div>
      </div>
      <div class="card hidden" id="card-tavsiye"><div class="section-title">Satıcı Tavsiyeleri</div><div class="advice-box" id="advice-box"></div></div>
      <div class="card hidden" id="card-uyari"><div class="section-title">Dikkat Edilmesi Gerekenler</div><div class="warn-box" id="uyari-box"></div></div>
    </div>

    <!-- Tab: AI Koç -->
    <div class="tab-panel" id="p-koc">
      <div class="card">
        <div class="section-title">AI İçgörüler</div>
        <div class="insight-grid">
          <div class="insight-card"><div class="insight-title">6 Aylık Öngörü</div><div class="insight-text" id="ins-ongoru">—</div></div>
          <div class="insight-card"><div class="insight-title">İdeal Alıcı Profili</div><div class="insight-text" id="ins-alici">—</div></div>
          <div class="insight-card"><div class="insight-title">Satış Taktiği</div><div class="insight-text" id="ins-taktik">—</div></div>
        </div>
      </div>
      <div class="card">
        <div class="section-title">Pazarlık Koçu</div>
        <p style="font-size:14px;color:var(--muted);margin-bottom:14px;line-height:1.6">Satıcının istediği fiyatı girin — AI size güçlü, somut pazarlık argümanları hazırlasın.</p>
        <div class="koc-input-row">
          <input type="number" id="satis-fiyati" placeholder="Satıcının istediği fiyat (TL)" min="0">
          <button class="btn-secondary" id="koc-btn" onclick="loadKoc()">Argümanları Oluştur</button>
        </div>
        <div id="koc-results"></div>
      </div>
    </div>

    <!-- Tab: Asistan -->
    <div class="tab-panel" id="p-asistan">
      <div class="card">
        <div class="section-title">AI Araç Danışmanı</div>
        <div class="chat-ctx" id="chat-ctx">Değerleme yapıldı. Bu araç veya Türkiye araba piyasası hakkında her şeyi sorabilirsiniz.</div>
        <div class="chat-msgs" id="chat-msgs"></div>
        <div class="chat-input-row">
          <input type="text" id="chat-input" placeholder="Soru sorun…" onkeydown="if(event.key==='Enter')sendChat()">
          <button class="chat-send" id="chat-send" onclick="sendChat()">Gönder</button>
        </div>
      </div>
    </div>
  </div><!-- /results -->

  <!-- ── Monitoring Panel ──────────────────────────────────────────────── -->
  <div class="monitoring-section">
    <div class="monitoring-title">İzleme ve Gözlemlenebilirlik Araçları</div>
    <div class="mon-grid">
      <a class="mon-card" href="http://localhost:3000" target="_blank">
        <span class="mon-icon">📊</span>
        <span class="mon-name">Grafana</span>
        <span class="mon-port">:3000</span>
      </a>
      <a class="mon-card" href="http://localhost:16686" target="_blank">
        <span class="mon-icon">🔍</span>
        <span class="mon-name">Jaeger</span>
        <span class="mon-port">:16686</span>
      </a>
      <a class="mon-card" href="http://localhost:5601" target="_blank">
        <span class="mon-icon">📋</span>
        <span class="mon-name">Kibana</span>
        <span class="mon-port">:5601</span>
      </a>
      <a class="mon-card" href="http://localhost:9090" target="_blank">
        <span class="mon-icon">🎯</span>
        <span class="mon-name">Prometheus</span>
        <span class="mon-port">:9090</span>
      </a>
      <a class="mon-card" href="http://localhost:15672" target="_blank">
        <span class="mon-icon">🐇</span>
        <span class="mon-name">RabbitMQ</span>
        <span class="mon-port">:15672</span>
      </a>
    </div>
  </div>

</div><!-- /app-screen -->
</div><!-- /wrap -->

<script>
// ── State ──────────────────────────────────────────────────────────────────
let token = localStorage.getItem('token') || null;
let username = localStorage.getItem('username') || null;
let currentResult = null;
let chatHistory = [];
let authMode = 'login';

// ── Init ───────────────────────────────────────────────────────────────────
(function init() {
  if (token) {
    showApp();
  } else {
    showAuth();
  }
})();

// ── Auth ───────────────────────────────────────────────────────────────────
function showAuth() {
  document.getElementById('auth-screen').classList.remove('hidden');
  document.getElementById('app-screen').classList.add('hidden');
  document.getElementById('user-chip').classList.add('hidden');
  document.getElementById('logout-btn').classList.add('hidden');
}

function showApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  if (username) {
    const chip = document.getElementById('user-chip');
    chip.textContent = username;
    chip.classList.remove('hidden');
    document.getElementById('logout-btn').classList.remove('hidden');
  }
  checkHealth();
}

function logout() {
  localStorage.removeItem('token');
  localStorage.removeItem('username');
  token = null; username = null; currentResult = null; chatHistory = [];
  document.getElementById('results').classList.add('hidden');
  showAuth();
}

function authSwitchTab(mode) {
  authMode = mode;
  document.getElementById('auth-tab-login').classList.toggle('active', mode === 'login');
  document.getElementById('auth-tab-register').classList.toggle('active', mode === 'register');
  document.getElementById('auth-btn').textContent = mode === 'login' ? 'Giriş Yap' : 'Kayıt Ol';
  document.getElementById('auth-error').classList.add('hidden');
  document.getElementById('auth-success').style.display = 'none';
}

async function authSubmit() {
  const btn = document.getElementById('auth-btn');
  const errEl = document.getElementById('auth-error');
  const sucEl = document.getElementById('auth-success');
  const uname = document.getElementById('auth-username').value.trim();
  const pass = document.getElementById('auth-password').value;
  errEl.classList.add('hidden');
  sucEl.style.display = 'none';
  if (!uname || !pass) { showAuthErr('Kullanıcı adı ve şifre gerekli.'); return; }
  btn.disabled = true;
  btn.textContent = 'Bekleyin…';
  const url = authMode === 'login' ? '/login' : '/register';
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: uname, password: pass})
    });
    const data = await res.json();
    if (!res.ok) { showAuthErr(data.detail || 'Bir hata oluştu.'); return; }
    if (authMode === 'login') {
      token = data.access_token;
      username = uname;
      localStorage.setItem('token', token);
      localStorage.setItem('username', username);
      showApp();
    } else {
      sucEl.textContent = data.mesaj || 'Kayıt başarılı! Şimdi giriş yapabilirsiniz.';
      sucEl.style.display = 'block';
      authSwitchTab('login');
    }
  } catch(e) {
    showAuthErr('Sunucuya ulaşılamadı. Lütfen tekrar deneyin.');
  } finally {
    btn.disabled = false;
    btn.textContent = authMode === 'login' ? 'Giriş Yap' : 'Kayıt Ol';
  }
}

function showAuthErr(msg) {
  const el = document.getElementById('auth-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

// ── Health Check ───────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const res = await fetch('/health');
    const data = await res.json();
    const deps = data.dependencies || {};
    setDot('hc-gateway', data.status === 'ok');
    setDot('hc-auth', deps['auth-service'] === 'ok');
    setDot('hc-valuation', deps['valuation-service'] === 'ok');
  } catch(e) {
    setDot('hc-gateway', false);
  }
}

function setDot(id, ok) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'dot ' + (ok ? 'ok' : 'err');
}

// ── Format ─────────────────────────────────────────────────────────────────
const fmtTL = n => Number(Math.round(n)).toLocaleString('tr-TR') + ' TL';

// ── Counter animation ──────────────────────────────────────────────────────
function animateCounter(el, target, duration=900) {
  const start = performance.now();
  function tick(now) {
    const p = Math.min((now-start)/duration,1);
    const eased = 1-Math.pow(1-p,3);
    el.textContent = Number(Math.round(target*eased)).toLocaleString('tr-TR');
    if(p<1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Gauge ──────────────────────────────────────────────────────────────────
function animateGauge(score) {
  const arc = document.getElementById('gauge-arc');
  const numEl = document.getElementById('gauge-num');
  if (!arc) return;
  setTimeout(() => {
    arc.style.transition = 'stroke-dashoffset 1.2s cubic-bezier(.4,0,.2,1)';
    arc.setAttribute('stroke-dashoffset', 251*(1-score/10));
  }, 200);
  const start = performance.now();
  function tick(now) {
    const p = Math.min((now-start)/1200,1);
    numEl.textContent = (score*(1-Math.pow(1-p,3))).toFixed(1);
    if(p<1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(name) {
  ['ozet','piyasa','koc','asistan'].forEach(t => {
    document.getElementById('t-'+t).classList.toggle('active', t===name);
    document.getElementById('p-'+t).classList.toggle('active', t===name);
  });
}

// ── Valuation ──────────────────────────────────────────────────────────────
async function degerle() {
  const btn = document.getElementById('main-btn');
  const errEl = document.getElementById('form-error');
  errEl.classList.add('hidden');
  document.getElementById('results').classList.add('hidden');

  const marka = document.getElementById('marka').value.trim();
  const model_yili = parseInt(document.getElementById('model_yili').value);
  const kilometre = parseInt(document.getElementById('kilometre').value);
  const hasar_kaydi = document.getElementById('hasar_kaydi').checked;
  const il = document.getElementById('il').value;

  if (!marka || !model_yili || isNaN(kilometre)) {
    errEl.textContent = 'Lütfen tüm alanları doldurun.';
    errEl.classList.remove('hidden');
    return;
  }

  btn.disabled = true; btn.textContent = 'Hesaplanıyor…';
  try {
    const res = await fetch('/api/v1/degerleme', {
      method: 'POST',
      headers: {'Content-Type':'application/json','Authorization':'Bearer '+token},
      body: JSON.stringify({marka, model_yili, kilometre, hasar_kaydi, il})
    });
    if (res.status === 401) { logout(); return; }
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    currentResult = data; chatHistory = [];
    renderResults(data);
    document.getElementById('results').classList.remove('hidden');
    document.getElementById('results').scrollIntoView({behavior:'smooth',block:'start'});
    switchTab('ozet');
    const ctx = `${marka} ${model_yili} | ${Number(kilometre).toLocaleString('tr-TR')} km | ${fmtTL(data.hesaplanan_fiyat_tl)} | ${il}`;
    document.getElementById('chat-ctx').textContent = ctx;
    document.getElementById('chat-msgs').innerHTML = '';
    addBotMsg(`${marka} ${model_yili} modelinizin değerlemesi tamamlandı. Bu araç, fiyatlandırma stratejisi veya Türkiye araba piyasası hakkında aklınıza takılan her soruyu sorabilirsiniz.`);
  } catch(e) {
    errEl.textContent = 'Sunucuya ulaşılamadı. Lütfen tekrar deneyin.';
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false; btn.textContent = 'Değerle';
  }
}

// ── Render results ─────────────────────────────────────────────────────────
function renderResults(d) {
  const fiyat = d.hesaplanan_fiyat_tl;
  const pr = d.piyasa_raporu;
  const ai = d.ai_analizi || {};

  animateCounter(document.getElementById('price-counter'), fiyat);
  document.getElementById('price-sub').textContent = (d.arac_bilgisi?.marka||'') + ' ' + (d.arac_bilgisi?.model_yili||'');

  const etiket = pr?.istatistikler?.konum_etiketi || '';
  if (etiket) {
    const bm = {'FIRSAT':'firsat','REKABETCİ':'rekabetci','PIYASA DEGERİ':'piyasa','PAHALI':'pahali','ÇOK PAHALI':'cokpahali'};
    document.getElementById('price-badge-wrap').innerHTML = `<span class="badge badge-${bm[etiket]||'piyasa'}">${etiket}</span>`;
  }

  if (pr?.istatistikler?.en_dusuk && pr?.istatistikler?.en_yuksek) {
    const mn=pr.istatistikler.en_dusuk, mx=pr.istatistikler.en_yuksek;
    const pct = Math.min(94,Math.max(6,(fiyat-mn)/(mx-mn)*100));
    document.getElementById('mktbar-wrap').classList.remove('hidden');
    document.getElementById('mktbar-min').textContent = Number(mn).toLocaleString('tr-TR');
    document.getElementById('mktbar-max').textContent = Number(mx).toLocaleString('tr-TR');
    document.getElementById('mktbar-center').textContent = fmtTL(fiyat);
    setTimeout(()=>{
      document.getElementById('mktbar-fill').style.width = pct+'%';
      document.getElementById('mktbar-dot').style.left = pct+'%';
    },300);
  }

  if (pr?.talep?.skor) {
    document.getElementById('gauge-card').style.display = '';
    animateGauge(parseFloat(pr.talep.skor));
    document.getElementById('gauge-detail').textContent = pr.talep.ortalama_satis_suresi || '';
    document.getElementById('gauge-sub').textContent = pr.talep.populerite || '';
  }

  renderDNA(d.faktorler);

  document.getElementById('ai-piyasa-yorumu').textContent = ai.piyasa_yorumu || '—';
  document.getElementById('ai-ozet').textContent = ai.ozet || '';

  if (pr?.benzer_ilanlar?.length) {
    document.getElementById('ilanlar-body').innerHTML = pr.benzer_ilanlar.map(i =>
      `<tr><td><strong>${i.marka}</strong> ${i.model_yili}</td><td>${Number(i.kilometre).toLocaleString('tr-TR')} km</td><td>${i.il}</td><td><strong>${Number(i.fiyat_tl).toLocaleString('tr-TR')} TL</strong></td><td style="font-size:12px;color:var(--muted)">${i.ilan_tarihi}</td></tr>`
    ).join('');
    document.getElementById('card-ilanlar').classList.remove('hidden');
  }

  if (pr?.il_karsilastirmasi) {
    const prices = Object.entries(pr.il_karsilastirmasi);
    const maxP = Math.max(...prices.map(([,v])=>v));
    document.getElementById('il-list').innerHTML = prices.sort(([,a],[,b])=>b-a).map(([il,p])=>
      `<div class="il-row"><div class="il-name">${il}</div><div class="il-track"><div class="il-fill" data-w="${(p/maxP*100).toFixed(1)}"></div></div><div class="il-price">${Number(p).toLocaleString('tr-TR')} TL</div></div>`
    ).join('');
    setTimeout(()=>{ document.querySelectorAll('.il-fill').forEach(el=>{ el.style.width=el.dataset.w+'%'; }); },100);
    document.getElementById('card-iller').classList.remove('hidden');
  }

  if (pr?.deger_tahmini) {
    const dt=pr.deger_tahmini, bugun=dt.bugun||fiyat;
    const rows=[['Bugün',bugun],['1 Yıl',dt['1_yil']],['2 Yıl',dt['2_yil']],['3 Yıl',dt['3_yil']],['5 Yıl',dt['5_yil']]].filter(([,v])=>v);
    document.getElementById('dep-list').innerHTML = rows.map(([lbl,val],i)=>{
      const loss=i>0?`<span class="dep-loss">-${Number(bugun-val).toLocaleString('tr-TR')}</span>`:'';
      return `<div class="dep-row"><span class="dep-yr">${lbl}</span><span><span class="dep-val">${Number(val).toLocaleString('tr-TR')} TL</span>${loss}</span></div>`;
    }).join('')+`<div style="font-size:11px;color:var(--muted);margin-top:10px">${dt.yorum||''}</div>`;
    document.getElementById('card-dep').classList.remove('hidden');
  }

  if (pr?.musteri_tavsiyeleri) {
    const t=pr.musteri_tavsiyeleri;
    const items=[
      t.liste_fiyati_onerisi?`Liste fiyatı önerisi: <strong>${Number(t.liste_fiyati_onerisi).toLocaleString('tr-TR')} TL</strong> — ${t.muzakere_marji||''}`:null,
      t.en_iyi_sehir||null, t.ilkbahar_tavsiyesi||null, t.alici_icin||null,
    ].filter(Boolean);
    document.getElementById('advice-box').innerHTML = items.map(x=>`<div class="advice-row"><span>${x}</span></div>`).join('');
    document.getElementById('card-tavsiye').classList.remove('hidden');
  }

  if (pr?.dogrulama?.uyarilar?.length) {
    document.getElementById('uyari-box').innerHTML = pr.dogrulama.uyarilar.map(u=>`<div class="warn-item"><span>—</span><span>${u}</span></div>`).join('');
    document.getElementById('card-uyari').classList.remove('hidden');
  }

  document.getElementById('ins-ongoru').textContent = ai.ongoru||'—';
  document.getElementById('ins-alici').textContent = ai.alici_profili||'—';
  document.getElementById('ins-taktik').textContent = ai.satis_taktigi||'—';
  document.getElementById('koc-results').innerHTML = '';
  document.getElementById('satis-fiyati').value = '';
}

// ── Price DNA ──────────────────────────────────────────────────────────────
function renderDNA(f) {
  if (!f) return;
  const items=[
    {label:'Taban Fiyat',val:f.taban_fiyat},{label:'Yaş Etkisi',val:f.yas_etkisi},
    {label:'Kilometre Etkisi',val:f.kilometre_etkisi},{label:'Hasar Etkisi',val:f.hasar_etkisi},
    {label:'Piyasa Dalgalanması',val:f.piyasa_dalgalanmasi},
  ].filter(i=>i.val!==0);
  const maxAbs=Math.max(...items.map(i=>Math.abs(i.val)));
  const net=items.reduce((s,i)=>s+i.val,0);
  document.getElementById('dna-bars').innerHTML = items.map(item=>{
    const pct=(Math.abs(item.val)/maxAbs*100).toFixed(1);
    const color=item.val>=0?'var(--green)':'var(--red)';
    return `<div class="dna-row"><div class="dna-label">${item.label}</div><div class="dna-bar-track"><div class="dna-bar-fill" data-w="${pct}" style="background:${color}"></div></div><div class="dna-val" style="color:${color}">${item.val>0?'+':''}${Number(item.val).toLocaleString('tr-TR')}</div></div>`;
  }).join('')+`<hr class="dna-divider"><div class="dna-total"><span style="color:var(--muted)">Hesaplanan Net Değer</span><span style="color:var(--primary)">${Number(Math.max(net,50000)).toLocaleString('tr-TR')} TL</span></div>`;
  setTimeout(()=>{ document.querySelectorAll('.dna-bar-fill').forEach(el=>{ el.style.transition='width .9s cubic-bezier(.4,0,.2,1)'; el.style.width=el.dataset.w+'%'; }); },100);
}

// ── Pazarlık Koçu ──────────────────────────────────────────────────────────
async function loadKoc() {
  if (!currentResult) return;
  const btn=document.getElementById('koc-btn');
  const inp=document.getElementById('satis-fiyati');
  const resultsEl=document.getElementById('koc-results');
  const satisFiyati=parseFloat(inp.value);
  if (!satisFiyati||satisFiyati<=0) { resultsEl.innerHTML='<div class="error-msg">Lütfen satıcının istediği fiyatı girin.</div>'; return; }
  btn.disabled=true; btn.textContent='Analiz ediliyor…';
  resultsEl.innerHTML='<div class="loading-dots"><span></span><span></span><span></span><span style="margin-left:8px">AI argümanlar hazırlıyor…</span></div>';
  try {
    const arac=currentResult.arac_bilgisi;
    const res=await fetch('/api/v1/pazarlik-kocu',{
      method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify({marka:arac.marka,model_yili:arac.model_yili,kilometre:arac.kilometre,hasar_kaydi:arac.hasar_kaydi,il:arac.il||'ankara',satis_fiyati:satisFiyati,hesaplanan_fiyat:currentResult.hesaplanan_fiyat_tl})
    });
    if (res.status===401) { logout(); return; }
    renderKoc(await res.json(), satisFiyati);
  } catch(e) {
    resultsEl.innerHTML='<div class="error-msg">AI servisine ulaşılamıyor. Lütfen tekrar deneyin.</div>';
  } finally {
    btn.disabled=false; btn.textContent='Argümanları Oluştur';
  }
}

function renderKoc(data, satisFiyati) {
  const args=data.argumanlar||data['argümanlar']||[];
  const toplam=args.reduce((s,a)=>s+(a.indirim_tl||0),0);
  document.getElementById('koc-results').innerHTML=`
    <div class="koc-summary-bar">
      <div class="koc-stat"><strong>${Number(data.hedef_fiyat).toLocaleString('tr-TR')} TL</strong><span>Hedef Fiyat</span></div>
      <div class="koc-stat"><strong>${Number(data.alt_sinir).toLocaleString('tr-TR')} TL</strong><span>Alt Sınır</span></div>
      <div class="koc-stat savings"><strong>-${Number(toplam).toLocaleString('tr-TR')} TL</strong><span>Toplam İndirim Potansiyeli</span></div>
    </div>
    ${data.ozet?`<p class="koc-ozet">${data.ozet}</p>`:''}
    ${args.map((a,i)=>`<div class="koc-arg slide-in" style="animation-delay:${i*.08}s"><div class="koc-num">${i+1}</div><div class="koc-body"><div class="koc-title">${a.baslik}</div><div class="koc-detail">${a.detay}</div></div><div class="koc-save">-${Number(a.indirim_tl).toLocaleString('tr-TR')} TL</div></div>`).join('')}
  `;
}

// ── Chat ───────────────────────────────────────────────────────────────────
function addBotMsg(text) {
  const el=document.createElement('div'); el.className='chat-msg chat-bot'; el.textContent=text;
  const chat=document.getElementById('chat-msgs'); chat.appendChild(el); chat.scrollTop=chat.scrollHeight; return el;
}
function addUserMsg(text) {
  const el=document.createElement('div'); el.className='chat-msg chat-user'; el.textContent=text;
  const chat=document.getElementById('chat-msgs'); chat.appendChild(el); chat.scrollTop=chat.scrollHeight;
}
function addThinking() {
  const el=document.createElement('div'); el.className='chat-msg chat-bot';
  el.innerHTML='<div class="thinking"><span></span><span></span><span></span></div>';
  const chat=document.getElementById('chat-msgs'); chat.appendChild(el); chat.scrollTop=chat.scrollHeight; return el;
}

async function sendChat() {
  const input=document.getElementById('chat-input');
  const sendBtn=document.getElementById('chat-send');
  const soru=input.value.trim(); if(!soru) return;
  addUserMsg(soru); chatHistory.push({rol:'kullanici',icerik:soru}); input.value=''; sendBtn.disabled=true;
  const thinking=addThinking();
  let aracKontekst=null;
  if(currentResult){
    const a=currentResult.arac_bilgisi;
    aracKontekst={marka:a?.marka,model_yili:a?.model_yili,km:a?.kilometre,fiyat:currentResult.hesaplanan_fiyat_tl,il:a?.il};
  }
  try {
    const res=await fetch('/api/v1/arac-asistan',{
      method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify({soru,gecmis:chatHistory.slice(0,-1),arac_kontekst:aracKontekst})
    });
    if(res.status===401){logout();return;}
    const data=await res.json();
    thinking.remove();
    const cevap=data.cevap||'Yanıt alınamadı.';
    addBotMsg(cevap); chatHistory.push({rol:'asistan',icerik:cevap});
  } catch(e) {
    thinking.remove(); addBotMsg('Asistana ulaşılamadı. Lütfen tekrar deneyin.');
  } finally { sendBtn.disabled=false; }
}
</script>
</body>
</html>"""
