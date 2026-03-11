from fastapi import FastAPI, Header, HTTPException, Request
import httpx
import jwt
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="API Gateway")

SECRET_KEY = "gizli_jwt_anahtari_degistir_123"
ALGORITHM = "HS256"

AUTH_SERVICE_URL = "http://auth-service:8001"
VALUATION_SERVICE_URL = "http://valuation-service:8000"


def verify_jwt(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token süresi dolmuş!")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Geçersiz token!")


@app.get("/")
def health_check():
    return {"mesaj": "API Gateway çalışıyor!"}


@app.post("/register")
async def register(request: Request):
    body = await request.json()
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{AUTH_SERVICE_URL}/register", json=body)
    return response.json()


@app.post("/login")
async def login(request: Request):
    body = await request.json()
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{AUTH_SERVICE_URL}/login", json=body)
    return response.json()


@app.post("/api/v1/degerleme")
async def degerleme(request: Request, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header eksik! Format: 'Bearer <token>'"
        )

    token = authorization.split(" ")[1]
    payload = verify_jwt(token)
    logger.info(f"Yetkili istek: kullanıcı='{payload.get('sub')}' -> Valuation Service'e yönlendiriliyor.")

    body = await request.json()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{VALUATION_SERVICE_URL}/api/v1/degerleme",
            json=body
        )
    return response.json()
