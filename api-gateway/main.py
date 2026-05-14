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

# ── Unified Single-Page UI ─────────────────────────────────────────────────────
_UI_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoVal — Türkiye Araç Değerleme</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f0f4ff;--card:#fff;--border:#e2e8f0;
  --p:#4f46e5;--pd:#3730a3;--pl:#e0e7ff;--pll:#f5f3ff;
  --g:#059669;--gl:#d1fae5;
  --r:#dc2626;--rl:#fee2e2;
  --a:#d97706;--al:#fef3c7;
  --t:#0f172a;--m:#475569;--f:#f8fafc;
  --sh:0 2px 16px rgba(79,70,229,.10);
  --sh2:0 8px 40px rgba(79,70,229,.18);
  --rad:16px;
  --header-h:64px;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--t);min-height:100vh}

/* ══ HEADER ══════════════════════════════════════════════════════════════════ */
.hdr{height:var(--header-h);background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 60%,#312e81 100%);
  display:flex;align-items:center;justify-content:space-between;padding:0 28px;
  position:sticky;top:0;z-index:200;box-shadow:0 2px 20px rgba(0,0,0,.4)}
.hdr-logo{display:flex;align-items:center;gap:12px;text-decoration:none}
.hdr-icon{width:38px;height:38px;background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:10px;
  display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 4px 12px rgba(79,70,229,.4)}
.hdr-title{font-size:18px;font-weight:800;color:#fff;letter-spacing:-.5px}
.hdr-sub{font-size:11px;color:#94a3b8;font-weight:500;margin-top:1px}
.hdr-right{display:flex;align-items:center;gap:10px}
.chip-user{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#e2e8f0;
  font-size:13px;font-weight:600;padding:5px 14px;border-radius:20px;backdrop-filter:blur(8px)}
.btn-out{padding:6px 16px;background:transparent;border:1.5px solid rgba(255,255,255,.25);
  border-radius:8px;font-size:13px;font-weight:600;color:#94a3b8;cursor:pointer;transition:.2s}
.btn-out:hover{border-color:#ef4444;color:#f87171}

/* ══ AUTH ════════════════════════════════════════════════════════════════════ */
#auth-screen{max-width:460px;margin:72px auto 0;padding:0 16px}
.auth-hero{text-align:center;margin-bottom:32px}
.auth-hero h1{font-size:32px;font-weight:900;background:linear-gradient(135deg,#4f46e5,#7c3aed);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:8px}
.auth-hero p{color:var(--m);font-size:15px}
.auth-card{background:#fff;border-radius:20px;padding:28px;box-shadow:var(--sh2)}
.auth-tabs{display:flex;gap:4px;background:var(--bg);border-radius:12px;padding:4px;margin-bottom:22px}
.auth-tab{flex:1;padding:9px;text-align:center;border-radius:9px;cursor:pointer;font-size:14px;font-weight:600;
  color:var(--m);transition:.2s;border:none;background:none}
.auth-tab.active{background:#fff;color:var(--p);box-shadow:0 2px 8px rgba(79,70,229,.15)}
.field{margin-bottom:16px}
.field label{display:block;font-size:12px;font-weight:700;color:var(--m);margin-bottom:6px;
  text-transform:uppercase;letter-spacing:.4px}
.field input{width:100%;padding:12px 14px;border:1.5px solid var(--border);border-radius:10px;
  font-size:15px;outline:none;transition:.2s;background:#fff;color:var(--t)}
.field input:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(79,70,229,.12)}
.btn-main{width:100%;padding:14px;background:linear-gradient(135deg,var(--p),#7c3aed);color:#fff;
  border:none;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;
  transition:.2s;letter-spacing:.2px;box-shadow:0 4px 16px rgba(79,70,229,.35)}
.btn-main:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 6px 20px rgba(79,70,229,.45)}
.btn-main:disabled{opacity:.6;cursor:not-allowed;transform:none}
.msg-err{background:var(--rl);border-radius:10px;padding:11px 14px;color:var(--r);font-size:14px;margin-top:12px}
.msg-ok{background:var(--gl);border-radius:10px;padding:11px 14px;color:#065f46;font-size:14px;margin-top:12px}
.hidden{display:none!important}

/* ══ WRAP ════════════════════════════════════════════════════════════════════ */
.wrap{max-width:1060px;margin:0 auto;padding:28px 16px 80px}

/* ══ HEALTH STRIP ════════════════════════════════════════════════════════════ */
.hstrip{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px}
.hchip{display:flex;align-items:center;gap:7px;padding:6px 14px;background:#fff;
  border:1px solid var(--border);border-radius:20px;font-size:12px;font-weight:600;color:var(--m);
  box-shadow:0 1px 4px rgba(0,0,0,.05)}
.hdot{width:8px;height:8px;border-radius:50%;background:#cbd5e1;flex-shrink:0;transition:.4s}
.hdot.ok{background:var(--g);box-shadow:0 0 6px rgba(5,150,105,.5)}
.hdot.err{background:var(--r)}

/* ══ PAGE TITLE ══════════════════════════════════════════════════════════════ */
.ptitle{font-size:26px;font-weight:900;letter-spacing:-.6px;margin-bottom:4px}
.psub{color:var(--m);font-size:14px;margin-bottom:24px}

/* ══ FORM CARD ═══════════════════════════════════════════════════════════════ */
.form-card{background:#fff;border-radius:var(--rad);box-shadow:var(--sh);overflow:hidden}
.fsec{padding:22px 24px;border-bottom:1px solid #f1f5f9}
.fsec:last-child{border-bottom:none}
.fsec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:18px}
.fsec-icon{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0}
.fsec-title{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;color:var(--t)}
.fsec-sub{font-size:12px;color:var(--m);margin-top:2px}
.ic1{background:#ede9fe} .ic2{background:#dbeafe} .ic3{background:#dcfce7} .ic4{background:#fef3c7}

.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:640px){.g2,.g3{grid-template-columns:1fr}}

label.fl{display:block;font-size:11px;font-weight:700;color:var(--m);margin-bottom:6px;
  text-transform:uppercase;letter-spacing:.4px}
input.fi,select.fi{width:100%;padding:11px 13px;border:1.5px solid var(--border);border-radius:10px;
  font-size:14px;outline:none;transition:.2s;background:#fff;color:var(--t)}
input.fi:focus,select.fi:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(79,70,229,.1)}
input.fi::placeholder{color:#94a3b8}

/* Pill radio buttons */
.pills{display:flex;flex-wrap:wrap;gap:7px}
.pill input{display:none}
.pill span{display:inline-flex;align-items:center;gap:5px;padding:8px 14px;border:1.5px solid var(--border);
  border-radius:20px;font-size:13px;font-weight:600;color:var(--m);cursor:pointer;transition:.2s;
  background:#fff;white-space:nowrap;user-select:none}
.pill input:checked+span{background:var(--p);border-color:var(--p);color:#fff;box-shadow:0 2px 8px rgba(79,70,229,.3)}
.pill span:hover{border-color:var(--p);color:var(--p)}

/* Color picker */
.clrs{display:flex;flex-wrap:wrap;gap:8px}
.clr-opt input{display:none}
.clr-dot{width:30px;height:30px;border-radius:50%;cursor:pointer;border:3px solid transparent;
  transition:.2s;display:block;box-shadow:0 2px 6px rgba(0,0,0,.15)}
.clr-opt input:checked+.clr-dot{border-color:var(--p);transform:scale(1.18);box-shadow:0 0 0 2px #fff,0 0 0 4px var(--p)}

/* Checkbox row */
.chk-row{display:flex;align-items:center;gap:10px;padding:10px 0}
.chk-row input{width:18px;height:18px;accent-color:var(--p);cursor:pointer;flex-shrink:0}
.chk-row label{font-size:14px;color:var(--t);cursor:pointer}

/* Submit button */
.btn-degerle{width:100%;padding:15px;background:linear-gradient(135deg,var(--p),#7c3aed);color:#fff;
  border:none;border-radius:12px;font-size:16px;font-weight:800;cursor:pointer;
  transition:.2s;box-shadow:0 4px 16px rgba(79,70,229,.35);letter-spacing:.3px}
.btn-degerle:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 6px 24px rgba(79,70,229,.45)}
.btn-degerle:disabled{opacity:.6;cursor:not-allowed;transform:none}

.form-err{background:var(--rl);border-radius:10px;padding:11px 14px;color:var(--r);font-size:14px;margin-top:12px}

/* ══ RESULTS ═════════════════════════════════════════════════════════════════ */
#results{margin-top:24px}
.tabs-bar{display:flex;gap:4px;margin-bottom:16px;border-bottom:2px solid var(--border);padding-bottom:0}
.tb{padding:10px 20px;background:none;border:none;border-bottom:3px solid transparent;margin-bottom:-2px;
  font-size:14px;font-weight:700;color:var(--m);cursor:pointer;transition:.2s;border-radius:8px 8px 0 0}
.tb:hover{color:var(--p);background:var(--pll)}
.tb.active{color:var(--p);border-bottom-color:var(--p)}
.tp{display:none;animation:fIn .25s ease}
.tp.active{display:block}
@keyframes fIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* Cards */
.card{background:#fff;border-radius:var(--rad);box-shadow:var(--sh);padding:22px;margin-bottom:14px}
.card-title{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:var(--m);margin-bottom:14px}

/* Price Hero */
.price-hero-wrap{text-align:left}
.price-lbl{font-size:12px;font-weight:700;color:var(--m);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.price-main{font-size:52px;font-weight:900;letter-spacing:-2px;line-height:1;color:var(--t)}
.price-main span{color:var(--p)}
.price-tag{font-size:15px;color:var(--m);margin-top:6px;font-weight:500}
.badge{display:inline-flex;align-items:center;gap:5px;padding:5px 14px;border-radius:20px;
  font-size:12px;font-weight:700;letter-spacing:.4px;margin-top:10px}
.b-firsat{background:#d1fae5;color:#065f46}
.b-rekabetci{background:#dbeafe;color:#1e40af}
.b-piyasa{background:#f1f5f9;color:#475569}
.b-pahali{background:#fef3c7;color:#92400e}
.b-cokpahali{background:#fee2e2;color:#991b1b}

/* Market bar */
.mbar{position:relative;height:10px;background:#e2e8f0;border-radius:5px;margin:20px 0 8px}
.mbar-fill{position:absolute;inset:0;height:10px;background:linear-gradient(90deg,var(--g),var(--p));border-radius:5px;width:0;transition:width 1.2s cubic-bezier(.4,0,.2,1)}
.mbar-dot{position:absolute;top:50%;transform:translate(-50%,-50%);width:20px;height:20px;
  background:var(--p);border:3px solid #fff;border-radius:50%;box-shadow:0 2px 10px rgba(79,70,229,.5);left:50%;transition:left 1.2s cubic-bezier(.4,0,.2,1)}
.mbar-lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--m);margin-top:6px}
.mbar-ctr{text-align:center;font-size:13px;font-weight:800;color:var(--p);margin-top:4px}

/* Tax card */
.tax-card{background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 100%);border-radius:var(--rad);padding:22px;margin-bottom:14px;color:#fff}
.tax-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:#94a3b8;margin-bottom:16px}
.tax-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.08)}
.tax-row:last-child{border-bottom:none;padding-top:14px}
.tax-lbl{font-size:14px;color:#cbd5e1;display:flex;align-items:center;gap:8px}
.tax-val{font-size:15px;font-weight:700;color:#fff}
.tax-total .tax-lbl{font-size:16px;font-weight:700;color:#fff}
.tax-total .tax-val{font-size:22px;font-weight:900;color:#a5b4fc}
.tax-note{font-size:11px;color:#64748b;margin-top:12px;line-height:1.5}

/* Gauge */
.gauge-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;min-height:180px}
.gauge-num{font-size:42px;font-weight:900;color:var(--p);line-height:1;margin-top:-18px}
.gauge-lbl{font-size:12px;color:var(--m);margin-top:2px}
.gauge-det{font-size:13px;color:var(--t);margin-top:8px;text-align:center;font-weight:600}
.gauge-sub2{font-size:12px;color:var(--m);margin-top:3px;text-align:center}

/* DNA bars */
.dna-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.dna-lbl{font-size:12px;color:var(--m);width:170px;flex-shrink:0}
.dna-track{flex:1;height:9px;background:#f1f5f9;border-radius:5px;overflow:hidden}
.dna-fill{height:9px;border-radius:5px;width:0;transition:width 1s cubic-bezier(.4,0,.2,1)}
.dna-val{font-size:13px;font-weight:700;width:120px;text-align:right;flex-shrink:0}
.dna-div{border:none;border-top:1px solid var(--border);margin:10px 0 8px}
.dna-tot{display:flex;justify-content:space-between;font-size:15px;font-weight:800;padding:4px 0}

/* Insight grid */
.ins-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:640px){.ins-grid{grid-template-columns:1fr}}
.ins-card{background:var(--f);border:1px solid var(--border);border-radius:12px;padding:16px}
.ins-icon{font-size:22px;margin-bottom:8px}
.ins-t{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--m);margin-bottom:6px}
.ins-body{font-size:13px;line-height:1.6;color:var(--t)}

/* Koc */
.koc-row{display:flex;gap:10px;margin-bottom:16px}
.koc-row input{flex:1}
.btn-sec{padding:11px 20px;background:var(--pl);color:var(--p);border:none;border-radius:10px;
  font-size:14px;font-weight:700;cursor:pointer;transition:.2s;white-space:nowrap}
.btn-sec:hover:not(:disabled){background:#c7d2fe}
.btn-sec:disabled{opacity:.5;cursor:not-allowed}
.koc-stats{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.koc-stat{flex:1;min-width:120px;background:var(--f);border:1px solid var(--border);border-radius:12px;padding:14px;text-align:center}
.koc-stat strong{display:block;font-size:17px;font-weight:900;color:var(--t);margin-bottom:2px}
.koc-stat span{font-size:11px;color:var(--m);text-transform:uppercase;letter-spacing:.3px}
.koc-stat.sav strong{color:var(--g)}
.koc-ozet{font-size:14px;color:var(--m);line-height:1.7;margin-bottom:14px;font-style:italic;
  background:var(--f);padding:12px 16px;border-radius:10px;border-left:3px solid var(--p)}
.koc-arg{display:flex;align-items:flex-start;gap:12px;padding:14px;background:var(--f);
  border:1px solid var(--border);border-radius:12px;margin-bottom:10px;animation:fIn .3s ease both}
.koc-num{width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,var(--p),#7c3aed);
  color:#fff;font-size:13px;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.koc-body{flex:1}
.koc-ttl{font-size:14px;font-weight:700;color:var(--t);margin-bottom:4px}
.koc-dtl{font-size:13px;color:var(--m);line-height:1.5}
.koc-save{font-size:14px;font-weight:800;color:var(--r);white-space:nowrap;padding-top:2px}

/* Chat */
.chat-ctx{background:var(--pl);border:1px solid #c7d2fe;border-radius:10px;padding:12px 14px;
  font-size:13px;color:var(--p);margin-bottom:14px;font-weight:600;line-height:1.5}
.chat-msgs{height:360px;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding:4px 0;margin-bottom:14px}
.chat-msg{max-width:82%;padding:12px 15px;border-radius:14px;font-size:14px;line-height:1.55;white-space:pre-wrap}
.cm-u{align-self:flex-end;background:linear-gradient(135deg,var(--p),#7c3aed);color:#fff;border-bottom-right-radius:4px}
.cm-b{align-self:flex-start;background:var(--f);border:1px solid var(--border);color:var(--t);border-bottom-left-radius:4px}
.chat-inp-row{display:flex;gap:8px}
.chat-inp-row input{flex:1;padding:12px 14px;border:1.5px solid var(--border);border-radius:10px;font-size:14px;outline:none;transition:.2s}
.chat-inp-row input:focus{border-color:var(--p);box-shadow:0 0 0 3px rgba(79,70,229,.1)}
.btn-send{padding:12px 20px;background:linear-gradient(135deg,var(--p),#7c3aed);color:#fff;border:none;
  border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;transition:.2s;white-space:nowrap}
.btn-send:hover{transform:translateY(-1px)}
.btn-send:disabled{opacity:.5;cursor:not-allowed;transform:none}
.thinking{display:flex;gap:5px;align-items:center;padding:4px 0}
.thinking span{width:8px;height:8px;border-radius:50%;background:#94a3b8;animation:bounce 1.2s infinite}
.thinking span:nth-child(2){animation-delay:.2s}
.thinking span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-7px)}}

/* Piyasa */
.ai-text{font-size:14px;line-height:1.75;color:var(--m);margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--m);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.4px;
  padding:8px 0;border-bottom:1px solid var(--border)}
td{padding:10px 0;border-bottom:1px solid var(--f);color:var(--t)}
tr:last-child td{border-bottom:none}
.il-row{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.il-name{font-size:13px;color:var(--m);width:90px;flex-shrink:0}
.il-track{flex:1;height:7px;background:#f1f5f9;border-radius:4px;overflow:hidden}
.il-fill{height:7px;background:linear-gradient(90deg,var(--p),#7c3aed);border-radius:4px;width:0;transition:width .9s ease}
.il-price{font-size:13px;font-weight:700;width:110px;text-align:right}
.dep-row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--f)}
.dep-row:last-child{border-bottom:none}
.dep-yr{font-size:13px;color:var(--m)}
.dep-val{font-weight:700;font-size:14px}
.dep-loss{font-size:11px;color:var(--r);margin-left:6px}
.adv-box{background:var(--pll);border-left:4px solid var(--p);border-radius:0 12px 12px 0;padding:14px 16px}
.adv-row{display:flex;gap:10px;margin-bottom:8px;font-size:14px;color:var(--t)}
.adv-row:last-child{margin-bottom:0}
.warn-box{background:var(--al);border-left:4px solid var(--a);border-radius:0 12px 12px 0;padding:14px 16px}
.warn-item{font-size:13px;color:#7c2d12;margin-bottom:6px;display:flex;gap:8px}
.warn-item:last-child{margin-bottom:0}

/* Monitoring */
.mon-sec{margin-top:36px;padding-top:28px;border-top:2px solid var(--border)}
.mon-title{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:var(--m);margin-bottom:14px}
.mon-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:640px){.mon-grid{grid-template-columns:repeat(2,1fr)}}
.mon-card{display:flex;flex-direction:column;align-items:center;gap:6px;padding:16px 10px;
  background:#fff;border:1.5px solid var(--border);border-radius:14px;text-decoration:none;
  color:var(--t);transition:.2s;cursor:pointer}
.mon-card:hover{border-color:var(--p);background:var(--pll);transform:translateY(-3px);box-shadow:var(--sh)}
.mon-ico{font-size:24px}
.mon-nm{font-size:12px;font-weight:700}
.mon-pt{font-size:11px;color:var(--m)}

/* Loading */
.ldots{display:flex;gap:6px;align-items:center;padding:16px;justify-content:center;color:var(--m);font-size:14px}
.ldots span{width:8px;height:8px;border-radius:50%;background:var(--p);animation:bounce 1.2s infinite}
.ldots span:nth-child(2){animation-delay:.2s}
.ldots span:nth-child(3){animation-delay:.4s}

/* Adjustment pills (readonly display) */
.adj-pills{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.adj-pill{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700}
.adj-pos{background:var(--gl);color:#065f46}
.adj-neg{background:var(--rl);color:var(--r)}
.adj-neu{background:#f1f5f9;color:var(--m)}
</style>
</head>
<body>

<!-- ══ HEADER ══════════════════════════════════════════════════════════════ -->
<div class="hdr">
  <a class="hdr-logo" href="#">
    <div class="hdr-icon">🚗</div>
    <div>
      <div class="hdr-title">AutoVal</div>
      <div class="hdr-sub">Türkiye Araç Değerleme Sistemi</div>
    </div>
  </a>
  <div class="hdr-right" id="hdr-right">
    <span class="chip-user hidden" id="user-chip"></span>
    <button class="btn-out hidden" id="logout-btn" onclick="logout()">Çıkış Yap</button>
  </div>
</div>

<div class="wrap">

<!-- ══ AUTH SCREEN ═════════════════════════════════════════════════════════ -->
<div id="auth-screen">
  <div class="auth-hero">
    <h1>AutoVal'e Hoş Geldiniz</h1>
    <p>Yapay zeka destekli Türkiye araç değerleme platformu</p>
  </div>
  <div class="auth-card">
    <div class="auth-tabs">
      <button class="auth-tab active" id="atab-login" onclick="authTab('login')">Giriş Yap</button>
      <button class="auth-tab" id="atab-reg" onclick="authTab('register')">Kayıt Ol</button>
    </div>
    <div class="field"><label>Kullanıcı Adı</label>
      <input type="text" id="au" placeholder="kullanici_adi" onkeydown="if(event.key==='Enter')authDo()"></div>
    <div class="field"><label>Şifre</label>
      <input type="password" id="ap" placeholder="••••••••" onkeydown="if(event.key==='Enter')authDo()"></div>
    <button class="btn-main" id="auth-btn" onclick="authDo()">Giriş Yap</button>
    <div id="auth-err" class="msg-err hidden"></div>
    <div id="auth-ok" class="msg-ok hidden"></div>
  </div>
</div>

<!-- ══ MAIN APP ════════════════════════════════════════════════════════════ -->
<div id="app-screen" class="hidden">

  <!-- Health -->
  <div class="hstrip" id="hstrip">
    <div class="hchip"><div class="hdot" id="hc-gw"></div> API Gateway</div>
    <div class="hchip"><div class="hdot" id="hc-auth"></div> Auth Service</div>
    <div class="hchip"><div class="hdot" id="hc-val"></div> Valuation Service</div>
  </div>

  <h1 class="ptitle">Araç Değerleme</h1>
  <p class="psub">Gerçek piyasa verisi · AI analizi · Vergi hesabı · Pazarlık koçu</p>

  <!-- ══ FORM ══════════════════════════════════════════════════════════════ -->
  <div class="form-card">

    <!-- Bölüm 1: Araç Kimliği -->
    <div class="fsec">
      <div class="fsec-hdr">
        <div class="fsec-icon ic1">🏷️</div>
        <div><div class="fsec-title">Araç Kimliği</div><div class="fsec-sub">Marka, model ve üretim yılı</div></div>
      </div>
      <div class="g3">
        <div><label class="fl">Marka</label>
          <input class="fi" type="text" id="marka" placeholder="Toyota, BMW, Fiat…"></div>
        <div><label class="fl">Model <span style="color:#059669;font-size:10px">API için gerekli</span></label>
          <input class="fi" type="text" id="model" placeholder="Corolla, A3, Egea…"></div>
        <div><label class="fl">Model Yılı</label>
          <input class="fi" type="number" id="model_yili" placeholder="2020" min="1990" max="2026"></div>
      </div>
    </div>

    <!-- Bölüm 2: Teknik Özellikler -->
    <div class="fsec">
      <div class="fsec-hdr">
        <div class="fsec-icon ic2">⚙️</div>
        <div><div class="fsec-title">Teknik Özellikler</div><div class="fsec-sub">Motor, yakıt ve şanzıman — fiyatı etkiler</div></div>
      </div>
      <div style="margin-bottom:16px">
        <label class="fl">Yakıt Tipi</label>
        <div class="pills" id="yakut-pills">
          <label class="pill"><input type="radio" name="yakut" value="benzin" checked><span>⛽ Benzin</span></label>
          <label class="pill"><input type="radio" name="yakut" value="dizel"><span>🛢️ Dizel</span></label>
          <label class="pill"><input type="radio" name="yakut" value="lpg"><span>🔵 LPG</span></label>
          <label class="pill"><input type="radio" name="yakut" value="hibrit"><span>🌿 Hibrit</span></label>
          <label class="pill"><input type="radio" name="yakut" value="elektrik"><span>⚡ Elektrik</span></label>
        </div>
      </div>
      <div style="margin-bottom:16px">
        <label class="fl">Şanzıman</label>
        <div class="pills">
          <label class="pill"><input type="radio" name="vites" value="manuel" checked><span>🕹️ Manuel</span></label>
          <label class="pill"><input type="radio" name="vites" value="otomatik"><span>🤖 Otomatik</span></label>
          <label class="pill"><input type="radio" name="vites" value="yari"><span>⚙️ Yarı Otomatik</span></label>
        </div>
      </div>
      <div style="margin-bottom:16px">
        <label class="fl">Kasa Tipi</label>
        <div class="pills">
          <label class="pill"><input type="radio" name="kasa" value="sedan" checked><span>🚗 Sedan</span></label>
          <label class="pill"><input type="radio" name="kasa" value="hatchback"><span>🚙 Hatchback</span></label>
          <label class="pill"><input type="radio" name="kasa" value="suv"><span>🛻 SUV</span></label>
          <label class="pill"><input type="radio" name="kasa" value="crossover"><span>🚐 Crossover</span></label>
          <label class="pill"><input type="radio" name="kasa" value="coupe"><span>🏎️ Coupe</span></label>
          <label class="pill"><input type="radio" name="kasa" value="station"><span>🚌 Station</span></label>
          <label class="pill"><input type="radio" name="kasa" value="pickup"><span>🛻 Pickup</span></label>
          <label class="pill"><input type="radio" name="kasa" value="minivan"><span>🚐 Minivan</span></label>
        </div>
      </div>
      <div class="g3">
        <div>
          <label class="fl">Motor Hacmi (cc) <span style="color:#059669;font-size:10px">Vergi hesabı için</span></label>
          <select class="fi" id="motor_cc">
            <option value="">Seçiniz</option>
            <option value="900">900 cc</option>
            <option value="1000">1.0L (1000 cc)</option>
            <option value="1200">1.2L (1200 cc)</option>
            <option value="1400">1.4L (1400 cc)</option>
            <option value="1500">1.5L (1500 cc)</option>
            <option value="1600">1.6L (1600 cc)</option>
            <option value="1800">1.8L (1800 cc)</option>
            <option value="2000">2.0L (2000 cc)</option>
            <option value="2500">2.5L (2500 cc)</option>
            <option value="3000">3.0L (3000 cc)</option>
            <option value="3500">3.5L+ (3500+ cc)</option>
          </select>
        </div>
        <div>
          <label class="fl">Beygir Gücü (HP)</label>
          <select class="fi" id="beygir">
            <option value="">Seçiniz</option>
            <option value="70">70-90 HP</option>
            <option value="100">90-110 HP</option>
            <option value="120">110-130 HP</option>
            <option value="150">130-160 HP</option>
            <option value="180">160-200 HP</option>
            <option value="220">200-250 HP</option>
            <option value="280">250-300 HP</option>
            <option value="350">300+ HP</option>
          </select>
        </div>
        <div>
          <label class="fl">Çekiş Tipi</label>
          <div class="pills" style="margin-top:4px">
            <label class="pill"><input type="radio" name="celis" value="fwd" checked><span>Önden</span></label>
            <label class="pill"><input type="radio" name="celis" value="rwd"><span>Arkadan</span></label>
            <label class="pill"><input type="radio" name="celis" value="awd"><span>4x4/AWD</span></label>
          </div>
        </div>
      </div>
    </div>

    <!-- Bölüm 3: Durum & Görünüm -->
    <div class="fsec">
      <div class="fsec-hdr">
        <div class="fsec-icon ic3">🔍</div>
        <div><div class="fsec-title">Durum & Görünüm</div><div class="fsec-sub">Kilometre, renk ve hasar bilgileri</div></div>
      </div>
      <div class="g2" style="margin-bottom:16px">
        <div><label class="fl">Kilometre</label>
          <input class="fi" type="number" id="kilometre" placeholder="80,000" min="0"></div>
        <div>
          <label class="fl">Boyalı Panel Sayısı</label>
          <select class="fi" id="boyali">
            <option value="0">Boyasız (orjinal)</option>
            <option value="1">1 panel boyalı</option>
            <option value="2">2-3 panel boyalı</option>
            <option value="4">4+ panel boyalı</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:16px">
        <label class="fl">Renk</label>
        <div class="clrs">
          <label class="clr-opt" title="Beyaz"><input type="radio" name="renk" value="beyaz" checked><div class="clr-dot" style="background:#f5f5f5;border:1.5px solid #ddd"></div></label>
          <label class="clr-opt" title="Siyah"><input type="radio" name="renk" value="siyah"><div class="clr-dot" style="background:#1a1a1a"></div></label>
          <label class="clr-opt" title="Gri"><input type="radio" name="renk" value="gri"><div class="clr-dot" style="background:#6b7280"></div></label>
          <label class="clr-opt" title="Silver"><input type="radio" name="renk" value="silver"><div class="clr-dot" style="background:linear-gradient(135deg,#c0c0c0,#e8e8e8)"></div></label>
          <label class="clr-opt" title="Kırmızı"><input type="radio" name="renk" value="kirmizi"><div class="clr-dot" style="background:#dc2626"></div></label>
          <label class="clr-opt" title="Mavi"><input type="radio" name="renk" value="mavi"><div class="clr-dot" style="background:#2563eb"></div></label>
          <label class="clr-opt" title="Lacivert"><input type="radio" name="renk" value="lacivert"><div class="clr-dot" style="background:#1e3a5f"></div></label>
          <label class="clr-opt" title="Yeşil"><input type="radio" name="renk" value="yesil"><div class="clr-dot" style="background:#16a34a"></div></label>
          <label class="clr-opt" title="Kahverengi"><input type="radio" name="renk" value="kahve"><div class="clr-dot" style="background:#92400e"></div></label>
          <label class="clr-opt" title="Bej"><input type="radio" name="renk" value="bej"><div class="clr-dot" style="background:#d4b483"></div></label>
          <label class="clr-opt" title="Sarı/Turuncu"><input type="radio" name="renk" value="diger"><div class="clr-dot" style="background:linear-gradient(135deg,#f59e0b,#ec4899)"></div></label>
        </div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:4px">
        <div class="chk-row" style="margin-right:28px">
          <input type="checkbox" id="hasar_kaydi">
          <label for="hasar_kaydi">⚠️ Hasar kaydı var</label>
        </div>
        <div class="chk-row">
          <input type="checkbox" id="degisen_parca">
          <label for="degisen_parca">🔧 Değişen/yenilenmiş parça var</label>
        </div>
      </div>
    </div>

    <!-- Bölüm 4: Konum -->
    <div class="fsec">
      <div class="fsec-hdr">
        <div class="fsec-icon ic4">📍</div>
        <div><div class="fsec-title">Konum</div><div class="fsec-sub">Şehir fiyatları etkiler</div></div>
      </div>
      <select class="fi" id="il" style="max-width:340px">
        <option value="ankara">📍 Ankara</option>
        <option value="istanbul">📍 İstanbul (+%7.2)</option>
        <option value="izmir">📍 İzmir (+%3.1)</option>
        <option value="bursa">📍 Bursa (-1.8%)</option>
        <option value="antalya">📍 Antalya (+%1.8)</option>
        <option value="adana">📍 Adana (-3.7%)</option>
        <option value="konya">📍 Konya (-4.9%)</option>
        <option value="gaziantep">📍 Gaziantep (-5.2%)</option>
        <option value="kayseri">📍 Kayseri (-5.6%)</option>
        <option value="mersin">📍 Mersin (-3.9%)</option>
        <option value="eskişehir">📍 Eskişehir (-3.2%)</option>
        <option value="kocaeli">📍 Kocaeli (+%0.8)</option>
        <option value="trabzon">📍 Trabzon (-2.9%)</option>
        <option value="samsun">📍 Samsun (-4.2%)</option>
        <option value="denizli">📍 Denizli (-2.9%)</option>
      </select>
    </div>

    <!-- Submit -->
    <div class="fsec">
      <button class="btn-degerle" id="main-btn" onclick="degerle()">🔍 Değerle — Gerçek Piyasa Analizi</button>
      <div class="form-err hidden" id="form-err" style="margin-top:12px"></div>
    </div>
  </div>

  <!-- ══ RESULTS ══════════════════════════════════════════════════════════ -->
  <div id="results" class="hidden">
    <div class="tabs-bar">
      <button class="tb active" id="t-ozet" onclick="switchTab('ozet')">📊 Özet</button>
      <button class="tb" id="t-vergi" onclick="switchTab('vergi')">🏛️ Vergiler & Maliyet</button>
      <button class="tb" id="t-piyasa" onclick="switchTab('piyasa')">📈 Piyasa</button>
      <button class="tb" id="t-koc" onclick="switchTab('koc')">🧠 AI Koç</button>
      <button class="tb" id="t-asistan" onclick="switchTab('asistan')">💬 Asistan</button>
    </div>

    <!-- Özet -->
    <div class="tp active" id="p-ozet">
      <div class="g2">
        <div class="card">
          <div class="card-title">Tahmini Piyasa Değeri</div>
          <div class="price-hero-wrap">
            <div class="price-main"><span id="price-counter">0</span> <span style="font-size:28px;color:var(--m)">TL</span></div>
            <div class="price-tag" id="price-tag"></div>
            <div id="price-badge"></div>
            <div class="adj-pills" id="adj-pills"></div>
          </div>
          <div id="mbar-wrap" class="hidden" style="margin-top:18px">
            <div class="mbar"><div class="mbar-fill" id="mbar-fill"></div><div class="mbar-dot" id="mbar-dot"></div></div>
            <div class="mbar-lbl"><span id="mbar-min">—</span><span>Piyasa Aralığı</span><span id="mbar-max">—</span></div>
            <div class="mbar-ctr" id="mbar-ctr"></div>
          </div>
        </div>
        <div class="card" id="gauge-card" style="display:none">
          <div class="card-title">Talep Skoru</div>
          <div class="gauge-wrap">
            <svg viewBox="0 0 200 120" width="180" height="108" style="overflow:visible">
              <defs><linearGradient id="gG" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" stop-color="#ef4444"/>
                <stop offset="50%" stop-color="#f59e0b"/>
                <stop offset="100%" stop-color="#059669"/>
              </linearGradient></defs>
              <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e2e8f0" stroke-width="16" stroke-linecap="round"/>
              <path id="gauge-arc" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="url(#gG)" stroke-width="16" stroke-linecap="round" stroke-dasharray="251" stroke-dashoffset="251"/>
            </svg>
            <div class="gauge-num" id="gauge-num">0</div>
            <div class="gauge-lbl">/ 10</div>
            <div class="gauge-det" id="gauge-det"></div>
            <div class="gauge-sub2" id="gauge-sub"></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Fiyat Faktör Analizi</div>
        <div id="dna-bars"></div>
      </div>
    </div>

    <!-- Vergiler & Maliyet -->
    <div class="tp" id="p-vergi">
      <div class="tax-card" id="tax-card">
        <div class="tax-title">🏛️ Türkiye Vergi & Sahiplik Analizi</div>
        <div id="tax-rows"></div>
        <div class="tax-note" id="tax-note"></div>
      </div>
      <div class="card">
        <div class="card-title">📅 Yıllık Sahiplik Maliyetleri (Tahmini)</div>
        <div id="yillik-rows"></div>
      </div>
      <div class="card">
        <div class="card-title">📋 Devir & Satın Alma Masrafları</div>
        <div id="devir-rows"></div>
      </div>
    </div>

    <!-- Piyasa -->
    <div class="tp" id="p-piyasa">
      <div class="card">
        <div class="card-title">AI Piyasa Yorumu</div>
        <p class="ai-text" id="ai-yorum"></p>
        <p style="font-size:14px;color:#555;line-height:1.6" id="ai-ozet"></p>
      </div>
      <div class="card hidden" id="card-ilanlar">
        <div class="card-title">Benzer İlanlar</div>
        <table><thead><tr><th>Araç</th><th>KM</th><th>Şehir</th><th>Fiyat</th><th>İlan</th></tr></thead>
        <tbody id="ilanlar-body"></tbody></table>
      </div>
      <div class="g2">
        <div class="card hidden" id="card-iller"><div class="card-title">Şehre Göre Fiyat</div><div id="il-list"></div></div>
        <div class="card hidden" id="card-dep"><div class="card-title">Değer Tahmini</div><div id="dep-list"></div></div>
      </div>
      <div class="card hidden" id="card-tav"><div class="card-title">Satıcı Tavsiyeleri</div><div class="adv-box" id="adv-box"></div></div>
      <div class="card hidden" id="card-uya"><div class="card-title">⚠️ Dikkat Edilmesi Gerekenler</div><div class="warn-box" id="warn-box"></div></div>
    </div>

    <!-- AI Koç -->
    <div class="tp" id="p-koc">
      <div class="card">
        <div class="card-title">AI İçgörüler</div>
        <div class="ins-grid">
          <div class="ins-card"><div class="ins-icon">📅</div><div class="ins-t">6 Aylık Öngörü</div><div class="ins-body" id="ins-ongoru">—</div></div>
          <div class="ins-card"><div class="ins-icon">👤</div><div class="ins-t">İdeal Alıcı Profili</div><div class="ins-body" id="ins-alici">—</div></div>
          <div class="ins-card"><div class="ins-icon">🎯</div><div class="ins-t">Satış Taktiği</div><div class="ins-body" id="ins-taktik">—</div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Pazarlık Koçu</div>
        <p style="font-size:14px;color:var(--m);margin-bottom:14px;line-height:1.6">Satıcının istediği fiyatı girin — AI size güçlü, somut pazarlık argümanları hazırlasın.</p>
        <div class="koc-row">
          <input class="fi" type="number" id="satis-fiyati" placeholder="Satıcının fiyatı (TL)" min="0">
          <button class="btn-sec" id="koc-btn" onclick="loadKoc()">Argümanları Oluştur</button>
        </div>
        <div id="koc-results"></div>
      </div>
    </div>

    <!-- Asistan -->
    <div class="tp" id="p-asistan">
      <div class="card">
        <div class="card-title">💬 AI Araç Danışmanı</div>
        <div class="chat-ctx" id="chat-ctx">Değerleme yapıldı. Bu araç veya Türkiye araba piyasası hakkında her şeyi sorabilirsiniz.</div>
        <div class="chat-msgs" id="chat-msgs"></div>
        <div class="chat-inp-row">
          <input type="text" id="chat-input" placeholder="Soru sorun…" onkeydown="if(event.key==='Enter')sendChat()">
          <button class="btn-send" id="chat-send" onclick="sendChat()">Gönder</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ══ MONITORING ═══════════════════════════════════════════════════════ -->
  <div class="mon-sec">
    <div class="mon-title">İzleme & Gözlemlenebilirlik</div>
    <div class="mon-grid">
      <a class="mon-card" href="http://localhost:3000" target="_blank"><span class="mon-ico">📊</span><span class="mon-nm">Grafana</span><span class="mon-pt">:3000</span></a>
      <a class="mon-card" href="http://localhost:16686" target="_blank"><span class="mon-ico">🔍</span><span class="mon-nm">Jaeger</span><span class="mon-pt">:16686</span></a>
      <a class="mon-card" href="http://localhost:5601" target="_blank"><span class="mon-ico">📋</span><span class="mon-nm">Kibana</span><span class="mon-pt">:5601</span></a>
      <a class="mon-card" href="http://localhost:9090" target="_blank"><span class="mon-ico">🎯</span><span class="mon-nm">Prometheus</span><span class="mon-pt">:9090</span></a>
      <a class="mon-card" href="http://localhost:15672" target="_blank"><span class="mon-ico">🐇</span><span class="mon-nm">RabbitMQ</span><span class="mon-pt">:15672</span></a>
    </div>
  </div>

</div><!-- /app-screen -->
</div><!-- /wrap -->

<script>
// ══ STATE ═══════════════════════════════════════════════════════════════════
let token=localStorage.getItem('token')||null;
let username=localStorage.getItem('username')||null;
let currentResult=null;
let currentAdjFiyat=0;
let chatHistory=[];
let authMode='login';

// ══ FIYAT DÜZELTME KATSAYILARI ══════════════════════════════════════════════
const ADJ={
  yakut:{benzin:1,dizel:1.08,lpg:0.92,hibrit:1.13,elektrik:1.18},
  vites:{manuel:1,otomatik:1.09,yari:1.04},
  kasa:{sedan:1,hatchback:0.96,suv:1.16,crossover:1.08,coupe:1.06,station:0.97,pickup:1.11,minivan:0.94},
  renk:{beyaz:1.02,siyah:1.02,gri:1.01,silver:1.01,kirmizi:0.99,mavi:0.99,lacivert:0.99,yesil:0.98,kahve:0.97,bej:0.98,diger:0.94},
  celis:{fwd:1,rwd:1.04,awd:1.13},
  boyali:{'0':1,'1':0.97,'2':0.93,'4':0.87},
  degisen:{false:1,true:0.95}
};

function getAdj(){
  const y=document.querySelector('input[name="yakut"]:checked')?.value||'benzin';
  const v=document.querySelector('input[name="vites"]:checked')?.value||'manuel';
  const k=document.querySelector('input[name="kasa"]:checked')?.value||'sedan';
  const r=document.querySelector('input[name="renk"]:checked')?.value||'beyaz';
  const c=document.querySelector('input[name="celis"]:checked')?.value||'fwd';
  const b=document.getElementById('boyali')?.value||'0';
  const d=document.getElementById('degisen_parca')?.checked||false;
  return {
    mul:(ADJ.yakut[y]||1)*(ADJ.vites[v]||1)*(ADJ.kasa[k]||1)*(ADJ.renk[r]||1)*(ADJ.celis[c]||1)*(ADJ.boyali[b]||1)*(d?0.95:1),
    yakut:y,vites:v,kasa:k,renk:r,celis:c,boyali:b,degisen:d
  };
}

function adjLabel(key,val){
  const labels={
    yakut:{benzin:'⛽ Benzin',dizel:'🛢️ Dizel +8%',lpg:'🔵 LPG -8%',hibrit:'🌿 Hibrit +13%',elektrik:'⚡ Elektrik +18%'},
    vites:{manuel:'Manuel',otomatik:'🤖 Otomatik +9%',yari:'Yarı Oto +4%'},
    kasa:{sedan:'Sedan',hatchback:'Hatchback -4%',suv:'SUV +16%',crossover:'Crossover +8%',coupe:'Coupe +6%',station:'Station -3%',pickup:'Pickup +11%',minivan:'Minivan -6%'},
    celis:{fwd:'FWD',rwd:'RWD +4%',awd:'4x4/AWD +13%'},
    boyali:{'0':'Boyasız','1':'1 Boyalı -3%','2':'2-3 Boyalı -7%','4':'4+ Boyalı -13%'},
  };
  return labels[key]?.[val]||val;
}

// ══ ÖTV / VERGİ HESABI ══════════════════════════════════════════════════════
function calcOTV(cc,yakut,bazFiyat){
  cc=parseInt(cc)||0;
  let rate=0;
  if(!cc){return null;}
  if(yakut==='elektrik') rate=0.10;
  else if(yakut==='hibrit') rate=cc<=1600?0.50:1.30;
  else{
    if(cc<=1600) rate=0.60;
    else if(cc<=2000) rate=1.30;
    else rate=2.20;
  }
  const otv=bazFiyat*rate;
  const kdv=(bazFiyat+otv)*0.20;
  return{rate,otv,kdv,total:bazFiyat+otv+kdv};
}

function calcMTV(cc,yas){
  cc=parseInt(cc)||1400;
  let baz;
  if(cc<=1300) baz=3500;
  else if(cc<=1600) baz=6000;
  else if(cc<=1800) baz=10000;
  else if(cc<=2000) baz=16000;
  else baz=28000;
  const yasK=yas>15?0.4:yas>10?0.6:yas>7?0.75:yas>3?0.90:1;
  return Math.round(baz*yasK/500)*500;
}

// ══ INIT ════════════════════════════════════════════════════════════════════
(function(){token?showApp():showAuth();})();

// ══ AUTH ════════════════════════════════════════════════════════════════════
function showAuth(){
  document.getElementById('auth-screen').classList.remove('hidden');
  document.getElementById('app-screen').classList.add('hidden');
  document.getElementById('user-chip').classList.add('hidden');
  document.getElementById('logout-btn').classList.add('hidden');
}
function showApp(){
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  if(username){
    const c=document.getElementById('user-chip');
    c.textContent='👤 '+username;c.classList.remove('hidden');
    document.getElementById('logout-btn').classList.remove('hidden');
  }
  checkHealth();
}
function logout(){
  localStorage.removeItem('token');localStorage.removeItem('username');
  token=null;username=null;currentResult=null;chatHistory=[];
  document.getElementById('results').classList.add('hidden');
  showAuth();
}
function authTab(m){
  authMode=m;
  document.getElementById('atab-login').classList.toggle('active',m==='login');
  document.getElementById('atab-reg').classList.toggle('active',m==='register');
  document.getElementById('auth-btn').textContent=m==='login'?'Giriş Yap':'Kayıt Ol';
  document.getElementById('auth-err').classList.add('hidden');
  document.getElementById('auth-ok').classList.add('hidden');
}
async function authDo(){
  const btn=document.getElementById('auth-btn');
  const errEl=document.getElementById('auth-err');
  const okEl=document.getElementById('auth-ok');
  const u=document.getElementById('au').value.trim();
  const p=document.getElementById('ap').value;
  errEl.classList.add('hidden');okEl.classList.add('hidden');
  if(!u||!p){errEl.textContent='Kullanıcı adı ve şifre gerekli.';errEl.classList.remove('hidden');return;}
  btn.disabled=true;btn.textContent='Bekleyin…';
  try{
    const res=await fetch(authMode==='login'?'/login':'/register',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:u,password:p})
    });
    const data=await res.json();
    if(!res.ok){errEl.textContent=data.detail||'Hata oluştu.';errEl.classList.remove('hidden');return;}
    if(authMode==='login'){
      token=data.access_token;username=u;
      localStorage.setItem('token',token);localStorage.setItem('username',username);
      showApp();
    }else{
      okEl.textContent=data.mesaj||'Kayıt başarılı! Giriş yapabilirsiniz.';
      okEl.classList.remove('hidden');authTab('login');
    }
  }catch(e){errEl.textContent='Sunucuya ulaşılamadı.';errEl.classList.remove('hidden');}
  finally{btn.disabled=false;btn.textContent=authMode==='login'?'Giriş Yap':'Kayıt Ol';}
}

// ══ HEALTH ══════════════════════════════════════════════════════════════════
async function checkHealth(){
  try{
    const res=await fetch('/health');
    const d=await res.json();
    const deps=d.dependencies||{};
    setDot('hc-gw',d.status==='ok');
    setDot('hc-auth',deps['auth-service']==='ok');
    setDot('hc-val',deps['valuation-service']==='ok');
  }catch(e){setDot('hc-gw',false);}
}
function setDot(id,ok){
  const el=document.getElementById(id);
  if(!el)return;
  el.className='hdot '+(ok?'ok':'err');
}

// ══ UTILS ════════════════════════════════════════════════════════════════════
const fmtTL=n=>Number(Math.round(n)).toLocaleString('tr-TR')+' TL';
function animCounter(el,target,dur=900){
  const s=performance.now();
  function tick(now){
    const p=Math.min((now-s)/dur,1);
    const e=1-Math.pow(1-p,3);
    el.textContent=Number(Math.round(target*e)).toLocaleString('tr-TR');
    if(p<1)requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
function animGauge(score){
  const arc=document.getElementById('gauge-arc');
  const numEl=document.getElementById('gauge-num');
  if(!arc)return;
  setTimeout(()=>{
    arc.style.transition='stroke-dashoffset 1.2s cubic-bezier(.4,0,.2,1)';
    arc.setAttribute('stroke-dashoffset',251*(1-score/10));
  },200);
  const s=performance.now();
  function tick(now){
    const p=Math.min((now-s)/1200,1);
    numEl.textContent=(score*(1-Math.pow(1-p,3))).toFixed(1);
    if(p<1)requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
function switchTab(n){
  ['ozet','vergi','piyasa','koc','asistan'].forEach(t=>{
    document.getElementById('t-'+t).classList.toggle('active',t===n);
    document.getElementById('p-'+t).classList.toggle('active',t===n);
  });
}

// ══ DEĞERLEME ══════════════════════════════════════════════════════════════
async function degerle(){
  const btn=document.getElementById('main-btn');
  const errEl=document.getElementById('form-err');
  errEl.classList.add('hidden');
  document.getElementById('results').classList.add('hidden');

  const marka=document.getElementById('marka').value.trim();
  const model=document.getElementById('model').value.trim();
  const model_yili=parseInt(document.getElementById('model_yili').value);
  const kilometre=parseInt(document.getElementById('kilometre').value);
  const hasar_kaydi=document.getElementById('hasar_kaydi').checked;
  const il=document.getElementById('il').value;

  if(!marka||!model_yili||isNaN(kilometre)){
    errEl.textContent='Lütfen en az Marka, Model Yılı ve Kilometre alanlarını doldurun.';
    errEl.classList.remove('hidden');return;
  }
  btn.disabled=true;btn.textContent='⏳ Hesaplanıyor…';
  try{
    const res=await fetch('/api/v1/degerleme',{
      method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify({marka,model,model_yili,kilometre,hasar_kaydi,il})
    });
    if(res.status===401){logout();return;}
    if(!res.ok)throw new Error(await res.text());
    const data=await res.json();
    currentResult=data;chatHistory=[];

    // Çarpanları uygula
    const adj=getAdj();
    const bazFiyat=data.hesaplanan_fiyat_tl;
    const adjFiyat=Math.round(bazFiyat*adj.mul);
    currentAdjFiyat=adjFiyat;
    data._adjFiyat=adjFiyat;
    data._adj=adj;

    renderResults(data);
    renderVergi(data,adj);
    document.getElementById('results').classList.remove('hidden');
    document.getElementById('results').scrollIntoView({behavior:'smooth',block:'start'});
    switchTab('ozet');

    const ml=model?`${marka} ${model} ${model_yili}`:`${marka} ${model_yili}`;
    document.getElementById('chat-ctx').textContent=`${ml} | ${Number(kilometre).toLocaleString('tr-TR')} km | ${fmtTL(adjFiyat)} | ${il}`;
    document.getElementById('chat-msgs').innerHTML='';
    addBotMsg(`${ml} modelinizin değerlemesi tamamlandı. Araç, piyasa veya fiyatlandırma hakkında soru sorabilirsiniz.`);
  }catch(e){
    errEl.textContent='Sunucuya ulaşılamadı. Lütfen tekrar deneyin.';
    errEl.classList.remove('hidden');
  }finally{btn.disabled=false;btn.textContent='🔍 Değerle — Gerçek Piyasa Analizi';}
}

// ══ RENDER RESULTS ══════════════════════════════════════════════════════════
function renderResults(d){
  const fiyat=d._adjFiyat||d.hesaplanan_fiyat_tl;
  const adj=d._adj||{};
  const pr=d.piyasa_raporu;
  const ai=d.ai_analizi||{};

  // Price
  animCounter(document.getElementById('price-counter'),fiyat);
  document.getElementById('price-tag').textContent=(d.arac_bilgisi?.marka||'')+' '+(d.arac_bilgisi?.model||'')+' '+(d.arac_bilgisi?.model_yili||'');

  // Badge
  const etiket=pr?.istatistikler?.konum_etiketi||'';
  const bm={FIRSAT:'b-firsat','REKABETCİ':'b-rekabetci','PIYASA DEGERİ':'b-piyasa',PAHALI:'b-pahali','ÇOK PAHALI':'b-cokpahali'};
  if(etiket) document.getElementById('price-badge').innerHTML=`<span class="badge ${bm[etiket]||'b-piyasa'}">${etiket}</span>`;

  // Adjustment pills
  const adjPills=[];
  const yMap={dizel:'Dizel +8%',lpg:'LPG -8%',hibrit:'Hibrit +13%',elektrik:'Elektrik +18%'};
  const vMap={otomatik:'Otomatik +9%',yari:'Yarı Oto +4%'};
  const kMap={suv:'SUV +16%',crossover:'Crossover +8%',coupe:'Coupe +6%',pickup:'Pickup +11%',hatchback:'Hatchback -4%',station:'Station -3%',minivan:'Minivan -6%'};
  const cMap={rwd:'RWD +4%',awd:'AWD +13%'};
  const bMap={'1':'1 Boyalı -3%','2':'2-3 Boyalı -7%','4':'4+ Boyalı -13%'};
  if(yMap[adj.yakut]) adjPills.push({t:yMap[adj.yakut],pos:adj.yakut==='dizel'||adj.yakut==='hibrit'||adj.yakut==='elektrik'});
  if(vMap[adj.vites]) adjPills.push({t:vMap[adj.vites],pos:true});
  if(kMap[adj.kasa]) adjPills.push({t:kMap[adj.kasa],pos:adj.kasa==='suv'||adj.kasa==='crossover'||adj.kasa==='coupe'||adj.kasa==='pickup'});
  if(cMap[adj.celis]) adjPills.push({t:cMap[adj.celis],pos:true});
  if(bMap[adj.boyali]) adjPills.push({t:bMap[adj.boyali],pos:false});
  if(adj.degisen) adjPills.push({t:'Değişen Parça -5%',pos:false});
  document.getElementById('adj-pills').innerHTML=adjPills.map(p=>`<span class="adj-pill ${p.pos?'adj-pos':'adj-neg'}">${p.t}</span>`).join('');

  // Market bar
  if(pr?.istatistikler?.en_dusuk&&pr?.istatistikler?.en_yuksek){
    const mn=pr.istatistikler.en_dusuk,mx=pr.istatistikler.en_yuksek;
    const pct=Math.min(94,Math.max(6,(fiyat-mn)/(mx-mn)*100));
    document.getElementById('mbar-wrap').classList.remove('hidden');
    document.getElementById('mbar-min').textContent=Number(mn).toLocaleString('tr-TR');
    document.getElementById('mbar-max').textContent=Number(mx).toLocaleString('tr-TR');
    document.getElementById('mbar-ctr').textContent=fmtTL(fiyat);
    setTimeout(()=>{
      document.getElementById('mbar-fill').style.width=pct+'%';
      document.getElementById('mbar-dot').style.left=pct+'%';
    },300);
  }

  // Gauge
  if(pr?.talep?.skor){
    document.getElementById('gauge-card').style.display='';
    animGauge(parseFloat(pr.talep.skor));
    document.getElementById('gauge-det').textContent=pr.talep.ortalama_satis_suresi||'';
    document.getElementById('gauge-sub').textContent=pr.talep.populerite||'';
  }

  // DNA
  renderDNA(d.faktorler,adj,d.hesaplanan_fiyat_tl,fiyat);

  // Piyasa tab
  document.getElementById('ai-yorum').textContent=ai.piyasa_yorumu||'—';
  document.getElementById('ai-ozet').textContent=ai.ozet||'';

  if(pr?.benzer_ilanlar?.length){
    document.getElementById('ilanlar-body').innerHTML=pr.benzer_ilanlar.map(i=>
      `<tr><td><strong>${i.marka}</strong> ${i.model_yili}</td><td>${Number(i.kilometre).toLocaleString('tr-TR')} km</td><td>${i.il}</td><td><strong>${Number(i.fiyat_tl).toLocaleString('tr-TR')} TL</strong></td><td style="font-size:12px;color:var(--m)">${i.ilan_tarihi}</td></tr>`
    ).join('');
    document.getElementById('card-ilanlar').classList.remove('hidden');
  }
  if(pr?.il_karsilastirmasi){
    const prices=Object.entries(pr.il_karsilastirmasi);
    const maxP=Math.max(...prices.map(([,v])=>v));
    document.getElementById('il-list').innerHTML=prices.sort(([,a],[,b])=>b-a).map(([il,p])=>
      `<div class="il-row"><div class="il-name">${il}</div><div class="il-track"><div class="il-fill" data-w="${(p/maxP*100).toFixed(1)}"></div></div><div class="il-price">${Number(p).toLocaleString('tr-TR')} TL</div></div>`
    ).join('');
    setTimeout(()=>{document.querySelectorAll('.il-fill').forEach(el=>{el.style.width=el.dataset.w+'%';});},100);
    document.getElementById('card-iller').classList.remove('hidden');
  }
  if(pr?.deger_tahmini){
    const dt=pr.deger_tahmini,bugun=dt.bugun||fiyat;
    const rows=[['Bugün',bugun],['1 Yıl',dt['1_yil']],['2 Yıl',dt['2_yil']],['3 Yıl',dt['3_yil']],['5 Yıl',dt['5_yil']]].filter(([,v])=>v);
    document.getElementById('dep-list').innerHTML=rows.map(([lbl,val],i)=>{
      const loss=i>0?`<span class="dep-loss">-${Number(bugun-val).toLocaleString('tr-TR')}</span>`:'';
      return `<div class="dep-row"><span class="dep-yr">${lbl}</span><span><span class="dep-val">${Number(val).toLocaleString('tr-TR')} TL</span>${loss}</span></div>`;
    }).join('')+`<div style="font-size:11px;color:var(--m);margin-top:10px">${dt.yorum||''}</div>`;
    document.getElementById('card-dep').classList.remove('hidden');
  }
  if(pr?.musteri_tavsiyeleri){
    const t=pr.musteri_tavsiyeleri;
    const items=[
      t.liste_fiyati_onerisi?`Liste fiyatı: <strong>${Number(t.liste_fiyati_onerisi).toLocaleString('tr-TR')} TL</strong> — ${t.muzakere_marji||''}`:null,
      t.en_iyi_sehir||null,t.ilkbahar_tavsiyesi||null,t.alici_icin||null
    ].filter(Boolean);
    document.getElementById('adv-box').innerHTML=items.map(x=>`<div class="adv-row"><span>✓</span><span>${x}</span></div>`).join('');
    document.getElementById('card-tav').classList.remove('hidden');
  }
  if(pr?.dogrulama?.uyarilar?.length){
    document.getElementById('warn-box').innerHTML=pr.dogrulama.uyarilar.map(u=>`<div class="warn-item"><span>⚠</span><span>${u}</span></div>`).join('');
    document.getElementById('card-uya').classList.remove('hidden');
  }

  // AI Koç
  document.getElementById('ins-ongoru').textContent=ai.ongoru||'—';
  document.getElementById('ins-alici').textContent=ai.alici_profili||'—';
  document.getElementById('ins-taktik').textContent=ai.satis_taktigi||'—';
  document.getElementById('koc-results').innerHTML='';
  document.getElementById('satis-fiyati').value='';
}

// ══ DNA BARS ═════════════════════════════════════════════════════════════════
function renderDNA(f,adj,bazFiyat,adjFiyat){
  if(!f)return;
  const adjEtkisi=adjFiyat-bazFiyat;
  const items=[
    {label:'Taban Fiyat',val:f.taban_fiyat},
    {label:'Yaş Etkisi',val:f.yas_etkisi},
    {label:'Kilometre Etkisi',val:f.kilometre_etkisi},
    {label:'Hasar Etkisi',val:f.hasar_etkisi},
    {label:'Piyasa Dalgalanması',val:f.piyasa_dalgalanmasi},
  ].filter(i=>i.val!==0);
  if(adjEtkisi!==0) items.push({label:'Özellik Düzeltmesi',val:adjEtkisi});
  const maxAbs=Math.max(...items.map(i=>Math.abs(i.val)));
  document.getElementById('dna-bars').innerHTML=items.map(item=>{
    const pct=(Math.abs(item.val)/maxAbs*100).toFixed(1);
    const color=item.val>=0?'var(--g)':'var(--r)';
    return `<div class="dna-row"><div class="dna-lbl">${item.label}</div><div class="dna-track"><div class="dna-fill" data-w="${pct}" style="background:${color}"></div></div><div class="dna-val" style="color:${color}">${item.val>0?'+':''}${Number(item.val).toLocaleString('tr-TR')}</div></div>`;
  }).join('')+`<hr class="dna-div"><div class="dna-tot"><span style="color:var(--m)">Net Piyasa Değeri</span><span style="color:var(--p)">${Number(adjFiyat).toLocaleString('tr-TR')} TL</span></div>`;
  setTimeout(()=>{document.querySelectorAll('.dna-fill').forEach(el=>{el.style.transition='width .9s cubic-bezier(.4,0,.2,1)';el.style.width=el.dataset.w+'%';});},100);
}

// ══ VERGİ HESABI ═════════════════════════════════════════════════════════════
function renderVergi(d,adj){
  const fiyat=d._adjFiyat||d.hesaplanan_fiyat_tl;
  const yil=d.arac_bilgisi?.model_yili||2020;
  const yas=new Date().getFullYear()-yil;
  const cc=parseInt(document.getElementById('motor_cc').value)||0;
  const yakut=adj.yakut||'benzin';

  // ÖTV / Sıfır eşdeğer
  const otv=calcOTV(cc,yakut,fiyat);
  let taxRows='';
  if(otv){
    const rateP=Math.round(otv.rate*100);
    taxRows+=`<div class="tax-row"><div class="tax-lbl">💰 İkinci El Tahmini Değer</div><div class="tax-val">${fmtTL(fiyat)}</div></div>`;
    taxRows+=`<div class="tax-row"><div class="tax-lbl">📊 ÖTV Oranı (${cc}cc, ${yakut})</div><div class="tax-val">%${rateP}</div></div>`;
    taxRows+=`<div class="tax-row"><div class="tax-lbl">🏛️ ÖTV (Hesaplanan)</div><div class="tax-val">${fmtTL(otv.otv)}</div></div>`;
    taxRows+=`<div class="tax-row"><div class="tax-lbl">🏛️ KDV (%20)</div><div class="tax-val">${fmtTL(otv.kdv)}</div></div>`;
    taxRows+=`<div class="tax-row tax-total"><div class="tax-lbl">🚗 Sıfır Eşdeğer Türkiye Fiyatı</div><div class="tax-val">${fmtTL(otv.total)}</div></div>`;
    document.getElementById('tax-note').textContent='Sıfır araç fiyatı yalnızca referans amaçlıdır. İkinci el alımda ÖTV alıcıya yansımaz; piyasa değeri gerçek satın alma maliyetidir.';
  }else{
    taxRows=`<div class="tax-row tax-total"><div class="tax-lbl">💰 Tahmini Piyasa Değeri</div><div class="tax-val">${fmtTL(fiyat)}</div></div>`;
    document.getElementById('tax-note').textContent='Motor hacmi girilirse ÖTV oranı ve sıfır eşdeğer Türkiye fiyatı hesaplanır.';
  }
  document.getElementById('tax-rows').innerHTML=taxRows;

  // MTV
  const mtv=calcMTV(cc,yas);
  const sigorta=Math.round(fiyat*0.025/1000)*1000;
  const kasko=Math.round(fiyat*0.04/1000)*1000;
  const muayene=yas<=2?0:1800;
  document.getElementById('yillik-rows').innerHTML=[
    {lbl:'🏛️ MTV (Motorlu Taşıtlar Vergisi)',val:mtv,note:'Yaş ve motor hacmine göre'},
    {lbl:'🛡️ Zorunlu Trafik Sigortası',val:sigorta,note:'Tahmini (gerçek değişir)'},
    {lbl:'🔒 Kasko (Tahmini)',val:kasko,note:`Araç değerinin ~%4'ü`},
    {lbl:"🔧 Yıllık Bakım (Tahmini)",val:Math.round(fiyat*0.02/500)*500,note:"Marka ve km'ye göre değişir"},
    {lbl:'🔍 Muayene',val:muayene,note:muayene?'2 yılda bir':'Yeni araç — muayene yok'},
  ].map(r=>`<div class="dep-row"><span class="dep-yr">${r.lbl} <span style="font-size:11px;color:var(--m)">— ${r.note}</span></span><span class="dep-val">${fmtTL(r.val)}</span></div>`).join('');

  // Devir masrafları
  const noter=Math.round(fiyat*0.011/100)*100;
  const plaka=450;
  document.getElementById('devir-rows').innerHTML=[
    {lbl:"📜 Noter Devir Ücreti",val:noter,note:"Satış bedelinin ~%1.1'i"},
    {lbl:'🚗 Plaka Tescil',val:plaka,note:'Sabit ücret'},
    {lbl:'📋 Trafik Sigortası (İlk Yıl)',val:sigorta,note:''},
    {lbl:'💳 Toplam Alım Maliyeti',val:fiyat+noter+plaka+sigorta,note:''},
  ].map((r,i)=>`<div class="dep-row" ${i===3?'style="border-top:2px solid var(--border);margin-top:4px"':''}><span class="dep-yr">${r.lbl}${r.note?` <span style="font-size:11px;color:var(--m)">— ${r.note}</span>`:''}</span><span class="dep-val" ${i===3?'style="color:var(--p)"':''}>${fmtTL(r.val)}</span></div>`).join('');
}

// ══ PAZARLİK KOÇ ═════════════════════════════════════════════════════════════
async function loadKoc(){
  if(!currentResult)return;
  const btn=document.getElementById('koc-btn');
  const inp=document.getElementById('satis-fiyati');
  const resEl=document.getElementById('koc-results');
  const sf=parseFloat(inp.value);
  if(!sf||sf<=0){resEl.innerHTML='<div class="form-err">Lütfen satıcının istediği fiyatı girin.</div>';return;}
  btn.disabled=true;btn.textContent='Analiz…';
  resEl.innerHTML='<div class="ldots"><span></span><span></span><span></span><span style="margin-left:8px">AI argümanlar hazırlıyor…</span></div>';
  try{
    const arac=currentResult.arac_bilgisi;
    const res=await fetch('/api/v1/pazarlik-kocu',{
      method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify({marka:arac.marka,model:arac.model||'',model_yili:arac.model_yili,
        kilometre:arac.kilometre,hasar_kaydi:arac.hasar_kaydi,il:arac.il||'ankara',
        satis_fiyati:sf,hesaplanan_fiyat:currentAdjFiyat||currentResult.hesaplanan_fiyat_tl})
    });
    if(res.status===401){logout();return;}
    renderKoc(await res.json(),sf);
  }catch(e){resEl.innerHTML='<div class="form-err">AI servisine ulaşılamıyor.</div>';}
  finally{btn.disabled=false;btn.textContent='Argümanları Oluştur';}
}
function renderKoc(data,sf){
  const args=data.argumanlar||data['argümanlar']||[];
  const top=args.reduce((s,a)=>s+(a.indirim_tl||0),0);
  document.getElementById('koc-results').innerHTML=`
    <div class="koc-stats">
      <div class="koc-stat"><strong>${Number(data.hedef_fiyat).toLocaleString('tr-TR')} TL</strong><span>Hedef Fiyat</span></div>
      <div class="koc-stat"><strong>${Number(data.alt_sinir).toLocaleString('tr-TR')} TL</strong><span>Alt Sınır</span></div>
      <div class="koc-stat sav"><strong>-${Number(top).toLocaleString('tr-TR')} TL</strong><span>İndirim Potansiyeli</span></div>
    </div>
    ${data.ozet?`<p class="koc-ozet">${data.ozet}</p>`:''}
    ${args.map((a,i)=>`<div class="koc-arg" style="animation-delay:${i*.08}s">
      <div class="koc-num">${i+1}</div>
      <div class="koc-body"><div class="koc-ttl">${a.baslik}</div><div class="koc-dtl">${a.detay}</div></div>
      <div class="koc-save">-${Number(a.indirim_tl).toLocaleString('tr-TR')} TL</div>
    </div>`).join('')}`;
}

// ══ CHAT ══════════════════════════════════════════════════════════════════════
function addBotMsg(t){
  const el=document.createElement('div');el.className='chat-msg cm-b';el.textContent=t;
  const c=document.getElementById('chat-msgs');c.appendChild(el);c.scrollTop=c.scrollHeight;return el;
}
function addUserMsg(t){
  const el=document.createElement('div');el.className='chat-msg cm-u';el.textContent=t;
  const c=document.getElementById('chat-msgs');c.appendChild(el);c.scrollTop=c.scrollHeight;
}
function addThinking(){
  const el=document.createElement('div');el.className='chat-msg cm-b';
  el.innerHTML='<div class="thinking"><span></span><span></span><span></span></div>';
  const c=document.getElementById('chat-msgs');c.appendChild(el);c.scrollTop=c.scrollHeight;return el;
}
async function sendChat(){
  const inp=document.getElementById('chat-input');
  const sb=document.getElementById('chat-send');
  const soru=inp.value.trim();if(!soru)return;
  addUserMsg(soru);chatHistory.push({rol:'kullanici',icerik:soru});inp.value='';sb.disabled=true;
  const thinking=addThinking();
  let ctx=null;
  if(currentResult){
    const a=currentResult.arac_bilgisi;
    ctx={marka:a?.marka,model_yili:a?.model_yili,km:a?.kilometre,fiyat:currentAdjFiyat||currentResult.hesaplanan_fiyat_tl,il:a?.il};
  }
  try{
    const res=await fetch('/api/v1/arac-asistan',{
      method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify({soru,gecmis:chatHistory.slice(0,-1),arac_kontekst:ctx})
    });
    if(res.status===401){logout();return;}
    const data=await res.json();
    thinking.remove();
    const cevap=data.cevap||'Yanıt alınamadı.';
    addBotMsg(cevap);chatHistory.push({rol:'asistan',icerik:cevap});
  }catch(e){thinking.remove();addBotMsg('Asistana ulaşılamadı.');}
  finally{sb.disabled=false;}
}
</script>
</body>
</html>
"""
