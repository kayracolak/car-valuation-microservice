from fastapi import FastAPI, Header, HTTPException
import logging

from models import AracOzellikleri
from services import fiyat_hesapla

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Araç Değerleme Servisi (Valuation Service)")

@app.get("/")
def ana_sayfa():
    return {"mesaj": "Araç Değerleme Servisi tıkır tıkır çalışıyor!"}

@app.post("/api/v1/degerleme")
def arac_degerle(arac: AracOzellikleri, api_key: str = Header(None)):

    if api_key != "BENIM_GIZLI_SIFREM_123":
        logger.warning(f"Yetkisiz erişim denemesi! Marka: {arac.marka}")
        raise HTTPException(status_code=401, detail="Geçersiz API Anahtarı!")

    logger.info(f"Sistem başarıyla çalıştı. Değerlenen Araç: {arac.marka} - {arac.model_yili}")

    hesaplanan_fiyat = fiyat_hesapla(arac)

    return {
        "arac_bilgisi": arac,
        "hesaplanan_fiyat_tl": hesaplanan_fiyat
    }
