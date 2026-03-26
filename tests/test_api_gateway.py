import sys
import os
import importlib.util

# Her servisin main.py'si aynı isimde - importlib ile benzersiz isimle yükle
def load_service(service_dir, module_name):
    service_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", service_dir))
    if service_path not in sys.path:
        sys.path.insert(0, service_path)
    path = os.path.join(service_path, "main.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

gateway_main = load_service("api-gateway", "gateway_main")

from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime, timedelta
import jwt

client = TestClient(gateway_main.app)

SECRET_KEY = "gizli_jwt_anahtari_degistir_123"


def token_olustur(username="testuser", dakika=30):
    payload = {"sub": username, "exp": datetime.utcnow() + timedelta(minutes=dakika)}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def test_health_check():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["mesaj"] == "API Gateway çalışıyor!"


def test_degerleme_auth_header_yok():
    response = client.post("/api/v1/degerleme", json={"marka": "Toyota"})
    assert response.status_code == 401


def test_degerleme_gecersiz_token():
    response = client.post(
        "/api/v1/degerleme",
        json={"marka": "Toyota"},
        headers={"Authorization": "Bearer gecersiz_token_xyz"},
    )
    assert response.status_code == 401


def test_degerleme_suresi_dolmus_token():
    suresi_dolmus = token_olustur(dakika=-1)
    response = client.post(
        "/api/v1/degerleme",
        json={"marka": "Toyota"},
        headers={"Authorization": f"Bearer {suresi_dolmus}"},
    )
    assert response.status_code == 401
    assert "süresi dolmuş" in response.json()["detail"]


def test_degerleme_gecerli_token():
    token = token_olustur()

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "hesaplanan_fiyat_tl": 500000,
        "ai_analizi": {"piyasa_yorumu": "Makul.", "ozet": "İyi."},
    }

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_http):
        response = client.post(
            "/api/v1/degerleme",
            json={"marka": "Toyota", "model_yili": 2020, "kilometre": 50000, "hasar_kaydi": False},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["hesaplanan_fiyat_tl"] == 500000


def test_register_proxy():
    """Register isteği auth-service'e iletilmeli."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"mesaj": "'testuser' kullanıcısı başarıyla oluşturuldu!"}

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_http):
        response = client.post("/register", json={"username": "testuser", "password": "pass"})

    assert response.status_code == 200


def test_login_proxy():
    """Login isteği auth-service'e iletilmeli."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "eyJxxx", "token_type": "bearer"}

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_http):
        response = client.post("/login", json={"username": "testuser", "password": "pass"})

    assert response.status_code == 200
    assert "access_token" in response.json()
