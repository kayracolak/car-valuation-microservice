import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timedelta

import bcrypt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
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

# ── Tracing setup ──────────────────────────────────────────────────────────────
_resource = Resource.create({"service.name": "auth-service"})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://jaeger:4318/v1/traces"))
)
trace.set_tracer_provider(_provider)

# ── Structured logging ─────────────────────────────────────────────────────────
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")

SERVICE_NAME = "auth-service"


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

# ── App ────────────────────────────────────────────────────────────────────────
_limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Auth Service")
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
try:
    Instrumentator().instrument(app).expose(app)
except Exception:
    pass

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "gizli_jwt_anahtari_degistir_123")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

if len(SECRET_KEY) < 32:
    logger.warning("JWT_SECRET_KEY 32 karakterden kısa — production için güvenli bir key kullanın!")

users_db: dict[str, str] = {}


class UserRegister(BaseModel):
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-ID", "")
    token = _trace_id_var.set(trace_id)
    response = await call_next(request)
    _trace_id_var.reset(token)
    return response


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "users_registered": len(users_db),
    }


@app.get("/", response_class=HTMLResponse)
def ana_sayfa():
    return """
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Giriş / Kayıt</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f5f5f5;
      display: flex;
      justify-content: center;
      padding: 40px 16px;
    }
    .container { width: 100%; max-width: 420px; }
    h1 { font-size: 22px; font-weight: 600; margin-bottom: 24px; color: #111; }
    .tabs { display: flex; gap: 4px; margin-bottom: 16px; }
    .tab {
      flex: 1; padding: 10px; text-align: center;
      border: 1px solid #ddd; border-radius: 8px;
      cursor: pointer; font-size: 14px; font-weight: 500;
      background: #fff; color: #555;
    }
    .tab.active { background: #111; color: #fff; border-color: #111; }
    .card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
    label { display: block; font-size: 13px; color: #555; margin-bottom: 4px; margin-top: 16px; }
    label:first-of-type { margin-top: 0; }
    input {
      width: 100%; padding: 10px 12px;
      border: 1px solid #ddd; border-radius: 8px;
      font-size: 15px; outline: none; transition: border-color 0.2s;
    }
    input:focus { border-color: #555; }
    button {
      width: 100%; margin-top: 20px; padding: 12px;
      background: #111; color: #fff; border: none;
      border-radius: 8px; font-size: 15px; font-weight: 500; cursor: pointer;
    }
    button:disabled { background: #999; cursor: not-allowed; }
    .msg { margin-top: 12px; font-size: 14px; padding: 10px; border-radius: 8px; display: none; }
    .msg.success { background: #f0fdf4; color: #166534; }
    .msg.error { background: #fff0f0; color: #c00; }
    .token-box {
      margin-top: 12px; padding: 10px 12px;
      background: #f5f5f5; border-radius: 8px;
      font-size: 12px; color: #333; word-break: break-all;
      display: none;
    }
    .token-label { font-size: 11px; color: #999; margin-bottom: 4px; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Kullanıcı İşlemleri</h1>

    <div class="tabs">
      <div class="tab active" onclick="switchTab('login')">Giriş Yap</div>
      <div class="tab" onclick="switchTab('register')">Kayıt Ol</div>
    </div>

    <div class="card">
      <label>Kullanıcı Adı</label>
      <input type="text" id="username" placeholder="kullanici_adi">

      <label>Şifre</label>
      <input type="password" id="password" placeholder="••••••••">

      <button id="btn" onclick="submit()">Giriş Yap</button>

      <div class="msg" id="msg"></div>
      <div class="token-box" id="token-box">
        <div class="token-label">JWT TOKEN</div>
        <div id="token-text"></div>
      </div>
    </div>
  </div>

  <script>
    let mode = 'login';

    function switchTab(m) {
      mode = m;
      document.querySelectorAll('.tab').forEach((t, i) => {
        t.classList.toggle('active', (m === 'login' && i === 0) || (m === 'register' && i === 1));
      });
      document.getElementById('btn').textContent = m === 'login' ? 'Giriş Yap' : 'Kayıt Ol';
      document.getElementById('msg').style.display = 'none';
      document.getElementById('token-box').style.display = 'none';
    }

    async function submit() {
      const btn = document.getElementById('btn');
      const msg = document.getElementById('msg');
      const tokenBox = document.getElementById('token-box');
      const username = document.getElementById('username').value.trim();
      const password = document.getElementById('password').value;

      if (!username || !password) {
        showMsg('Kullanıcı adı ve şifre gerekli.', 'error');
        return;
      }

      btn.disabled = true;
      tokenBox.style.display = 'none';
      msg.style.display = 'none';

      const url = mode === 'login' ? '/login' : '/register';
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password })
        });
        const data = await res.json();

        if (!res.ok) {
          showMsg(data.detail || 'Bir hata oluştu.', 'error');
        } else if (mode === 'login') {
          showMsg('Giriş başarılı!', 'success');
          document.getElementById('token-text').textContent = data.access_token;
          tokenBox.style.display = 'block';
        } else {
          showMsg(data.mesaj || 'Kayıt başarılı!', 'success');
        }
      } catch (e) {
        showMsg('Sunucuya ulaşılamadı.', 'error');
      } finally {
        btn.disabled = false;
      }
    }

    function showMsg(text, type) {
      const msg = document.getElementById('msg');
      msg.textContent = text;
      msg.className = 'msg ' + type;
      msg.style.display = 'block';
    }
  </script>
</body>
</html>
"""


@app.post("/register", status_code=201)
@_limiter.limit("10/minute")
def register(request: Request, user: UserRegister):
    if user.username in users_db:
        raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten alınmış!")
    users_db[user.username] = hash_password(user.password)
    logger.info("Yeni kullanıcı kaydedildi", extra={"username": user.username})
    return {"mesaj": f"'{user.username}' kullanıcısı başarıyla oluşturuldu!"}


@app.post("/login")
@_limiter.limit("20/minute")
def login(request: Request, user: UserLogin):
    stored_hash = users_db.get(user.username)
    if not stored_hash or not verify_password(user.password, stored_hash):
        logger.warning("Başarısız giriş denemesi", extra={"username": user.username})
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre hatalı!")
    token = create_access_token({"sub": user.username})
    logger.info("Başarılı giriş", extra={"username": user.username})
    return {"access_token": token, "token_type": "bearer"}
