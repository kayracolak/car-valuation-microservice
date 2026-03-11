from fastapi import FastAPI, HTTPException
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


@app.get("/")
def health_check():
    return {"mesaj": "Auth Service çalışıyor!"}


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
