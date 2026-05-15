import sys
import os
import importlib.util

# Her servisin main.py'si aynı isimde - importlib ile benzersiz isimle yükle
def load_service(service_dir, module_name):
    path = os.path.join(os.path.dirname(__file__), "..", service_dir, "main.py")
    spec = importlib.util.spec_from_file_location(module_name, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", service_dir)))
    spec.loader.exec_module(mod)
    return mod

import tempfile

auth_main = load_service("auth-service", "auth_main")
app = auth_main.app

from fastapi.testclient import TestClient

client = TestClient(app)

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
auth_main.DB_PATH = _tmp_db.name


def _db_users():
    conn = auth_main._get_db()
    rows = conn.execute("SELECT username, password_hash FROM users").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def setup_function():
    """Her testten önce kullanıcı veritabanını temizle."""
    conn = auth_main._get_db()
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()


def test_health_check():
    response = client.get("/")
    assert response.status_code == 200


def test_register_success():
    response = client.post("/register", json={"username": "ali", "password": "sifre123"})
    assert response.status_code == 201
    assert "mesaj" in response.json()
    assert "ali" in response.json()["mesaj"]


def test_register_duplicate_user():
    client.post("/register", json={"username": "ayse", "password": "pass1"})
    response = client.post("/register", json={"username": "ayse", "password": "pass2"})
    assert response.status_code == 400
    assert "zaten alınmış" in response.json()["detail"]


def test_login_success():
    client.post("/register", json={"username": "mehmet", "password": "sifre456"})
    response = client.post("/login", json={"username": "mehmet", "password": "sifre456"})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert len(data["access_token"]) > 0


def test_login_wrong_password():
    client.post("/register", json={"username": "zeynep", "password": "dogru"})
    response = client.post("/login", json={"username": "zeynep", "password": "yanlis"})
    assert response.status_code == 401


def test_login_nonexistent_user():
    response = client.post("/login", json={"username": "yok_kullanici", "password": "herhangi"})
    assert response.status_code == 401


def test_password_is_hashed():
    """Şifre düz metin olarak saklanmamalı."""
    client.post("/register", json={"username": "hasan", "password": "acik_sifre"})
    users = _db_users()
    assert users.get("hasan") != "acik_sifre"
    assert users.get("hasan") is not None
