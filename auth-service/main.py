from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
import jwt
import hashlib
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Auth Service")

SECRET_KEY = "gizli_jwt_anahtari_degistir_123"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# In-memory kullanıcı veritabanı
users_db = {}


class UserRegister(BaseModel):
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


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
def register(user: UserRegister):
    if user.username in users_db:
        raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten alınmış!")
    users_db[user.username] = hash_password(user.password)
    logger.info(f"Yeni kullanıcı kaydedildi: {user.username}")
    return {"mesaj": f"'{user.username}' kullanıcısı başarıyla oluşturuldu!"}


@app.post("/login")
def login(user: UserLogin):
    stored_hash = users_db.get(user.username)
    if not stored_hash or stored_hash != hash_password(user.password):
        logger.warning(f"Başarısız giriş denemesi: {user.username}")
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre hatalı!")
    token = create_access_token({"sub": user.username})
    logger.info(f"Başarılı giriş: {user.username}")
    return {"access_token": token, "token_type": "bearer"}
