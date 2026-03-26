import sys
import os
import importlib.util

os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")

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

from unittest.mock import patch, MagicMock

with patch("pika.BlockingConnection"):
    valuation_main = load_service("valuation-service", "valuation_main")

from fastapi.testclient import TestClient

client = TestClient(valuation_main.app)

GECERLI_ARAC = {
    "marka": "Toyota",
    "model_yili": 2020,
    "kilometre": 50000,
    "hasar_kaydi": False,
}


def _mock_ai_response(metin='{"piyasa_yorumu": "Makul fiyat.", "ozet": "İyi araç."}'):
    mock_choice = MagicMock()
    mock_choice.message.content = metin
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    return mock_completion


def test_health_check():
    response = client.get("/")
    assert response.status_code == 200


def test_degerleme_success():
    with patch.object(valuation_main.openai_client.chat.completions, "create", return_value=_mock_ai_response()), \
         patch("pika.BlockingConnection"):
        response = client.post("/api/v1/degerleme", json=GECERLI_ARAC)

    assert response.status_code == 200
    data = response.json()
    assert "hesaplanan_fiyat_tl" in data
    assert "ai_analizi" in data
    assert data["hesaplanan_fiyat_tl"] >= 50000


def test_degerleme_eksik_alan():
    """Zorunlu alan eksik olduğunda 422 döner."""
    response = client.post("/api/v1/degerleme", json={
        "marka": "BMW",
        # model_yili eksik
        "kilometre": 30000,
        "hasar_kaydi": False,
    })
    assert response.status_code == 422


def test_fiyat_hesapla_temel():
    """Yeni, az kilometreli araç 50.000 TL üzerinde olmalı."""
    from services import fiyat_hesapla
    from models import AracOzellikleri

    arac = AracOzellikleri(marka="Toyota", model_yili=2024, kilometre=0, hasar_kaydi=False)
    with patch("services.random.randint", return_value=0):
        fiyat = fiyat_hesapla(arac)
    assert fiyat > 50000


def test_fiyat_hesapla_hasar_indirimi():
    """Hasar kaydı olan araç daha ucuz olmalı."""
    service_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "valuation-service"))
    if service_path not in sys.path:
        sys.path.insert(0, service_path)

    from services import fiyat_hesapla
    from models import AracOzellikleri

    with patch("services.random.randint", return_value=0):
        fiyat_normal = fiyat_hesapla(AracOzellikleri(
            marka="BMW", model_yili=2020, kilometre=30000, hasar_kaydi=False))
        fiyat_hasarli = fiyat_hesapla(AracOzellikleri(
            marka="BMW", model_yili=2020, kilometre=30000, hasar_kaydi=True))

    assert fiyat_hasarli < fiyat_normal


def test_fiyat_minimum_sinir():
    """Fiyat asla 50.000 TL'nin altına düşmemeli."""
    from services import fiyat_hesapla
    from models import AracOzellikleri

    arac = AracOzellikleri(marka="Eski", model_yili=1990, kilometre=999999, hasar_kaydi=True)
    with patch("services.random.randint", return_value=-15000):
        fiyat = fiyat_hesapla(arac)
    assert fiyat >= 50000


def test_ai_analiz_fallback():
    """OpenAI başarısız olunca fallback mesajı döner."""
    with patch.object(valuation_main.openai_client.chat.completions, "create", side_effect=Exception("API hatası")), \
         patch("pika.BlockingConnection"):
        response = client.post("/api/v1/degerleme", json=GECERLI_ARAC)

    assert response.status_code == 200
    data = response.json()
    assert "kullanılamıyor" in data["ai_analizi"]["piyasa_yorumu"]
