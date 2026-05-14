import os
import re
import random
from datetime import datetime

import httpx

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "vehicle-pricing-api.p.rapidapi.com"
DOVIZ_KURU = float(os.getenv("USD_TO_TRL", "35"))  # USD → TRL dönüşüm kuru
ORTALAMA_YILLIK_KM = 15_000  # Türkiye ortalama yıllık km

# Marka bazlı 2026 Türkiye sıfır araç baz fiyatları (TL)
# Orta segment model referans alınmıştır (Corolla, Golf, 3 Serisi vb.)
MARKA_BAZ_FIYAT: dict[str, int] = {
    "toyota": 2_400_000,
    "honda": 2_200_000,
    "volkswagen": 2_600_000,
    "vw": 2_600_000,
    "ford": 2_100_000,
    "hyundai": 2_000_000,
    "renault": 1_800_000,
    "kia": 2_100_000,
    "nissan": 2_000_000,
    "bmw": 4_500_000,
    "skoda": 2_200_000,
    "mercedes": 4_800_000,
    "fiat": 1_700_000,
    "suzuki": 1_600_000,
    "audi": 4_200_000,
    "peugeot": 2_000_000,
    "dacia": 1_500_000,
    "opel": 2_100_000,
    "mitsubishi": 2_000_000,
    "subaru": 2_200_000,
    "volvo": 4_000_000,
    "seat": 2_000_000,
    "citroen": 1_900_000,
    "mazda": 2_100_000,
    "mini": 3_200_000,
    "jeep": 3_000_000,
    "land rover": 6_000_000,
    "porsche": 8_000_000,
    "lexus": 4_000_000,
    "infiniti": 3_500_000,
    "togg": 2_200_000,
    "chery": 1_400_000,
    "mg": 1_600_000,
    "byd": 1_800_000,
}

DEFAULT_BAZ_FIYAT = 2_000_000

# Yıllık bileşik değer kayıp oranları (1. yıldan sonraki her yıl için)
YILLIK_AMORTISMAN = [0.18, 0.14, 0.11, 0.09, 0.08, 0.07, 0.07, 0.06, 0.06, 0.05]


def _parse_fiyat_araligi(price_str: str) -> float:
    """'$7,920 - $11,220' → ortalama değer float olarak döner."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+", price_str)]
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2
    elif nums:
        return nums[0]
    return 0.0


def fetch_api_fiyat(marka: str, model: str, yil: int) -> float | None:
    """
    RapidAPI Vehicle Pricing'den araç taban fiyatını TRL cinsinden çeker.
    API anahtarı yoksa, model boşsa veya hata oluşursa None döner (fallback devreye girer).
    """
    if not RAPIDAPI_KEY or not model or not model.strip():
        return None
    try:
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": RAPIDAPI_HOST,
            "Content-Type": "application/json",
        }
        params = {
            "maker": marka.strip().lower(),
            "model": model.strip().lower(),
            "year": str(yil),
        }
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                f"https://{RAPIDAPI_HOST}/2775/get%2Bvehicle%2Bvalue",
                headers=headers,
                params=params,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("status"):
            return None

        prices = []
        for body_type in data.get("body_type", []):
            for spec in body_type.get("modals", {}).get("specs", []):
                p = _parse_fiyat_araligi(spec.get("price_range", ""))
                if p > 0:
                    prices.append(p)

        if not prices:
            return None

        avg_foreign = sum(prices) / len(prices)
        return round(avg_foreign * DOVIZ_KURU)
    except Exception:
        return None


def _marka_baz_fiyati(marka: str) -> int:
    """Markaya göre 2026 Türkiye sıfır araç baz fiyatını döner."""
    return MARKA_BAZ_FIYAT.get(marka.strip().lower(), DEFAULT_BAZ_FIYAT)


def _amortize(baz: float, yas: int) -> float:
    """Bileşik yıllık amortisman uygulayarak ikinci el değeri hesaplar."""
    deger = float(baz)
    for i in range(min(yas, len(YILLIK_AMORTISMAN))):
        deger *= 1 - YILLIK_AMORTISMAN[i]
    # Tablonun ötesindeki yıllar için %5/yıl sabit
    if yas > len(YILLIK_AMORTISMAN):
        for _ in range(yas - len(YILLIK_AMORTISMAN)):
            deger *= 0.95
    return deger


def fiyat_hesapla(arac):
    return fiyat_hesapla_detay(arac)["fiyat"]


def fiyat_hesapla_detay(arac):
    """
    Araç fiyatını hesaplar ve her faktörün katkısını döner.

    Öncelik sırası:
    1. RapidAPI gerçek fiyat (model girilmişse + API erişimi varsa)
    2. Marka bazlı amortisman modeli (gerçekçi fallback)
    """
    guncel_yil = datetime.now().year
    yas = max(0, guncel_yil - arac.model_yili)
    model = getattr(arac, "model", None) or ""

    api_taban = fetch_api_fiyat(arac.marka, model, arac.model_yili)

    if api_taban:
        # ── Gerçek API verisi ──────────────────────────────────────────────
        taban = api_taban
        # API fiyatı zaten o yılın değerini içeriyor; tekrar yaş düşümü uygulanmaz.
        yas_etkisi = 0
        # Km etkisi: her 1000 km sapma başına tabanın %0.1'i
        beklenen_km = yas * ORTALAMA_YILLIK_KM
        km_fark = arac.kilometre - beklenen_km
        km_oran_1000km = taban * 0.001
        km_etkisi = -round((km_fark / 1000) * km_oran_1000km)
    else:
        # ── Marka bazlı akıllı fallback ────────────────────────────────────
        baz_sifir = _marka_baz_fiyati(arac.marka)
        taban = round(_amortize(baz_sifir, yas))
        yas_etkisi = 0  # Amortisman taban içinde zaten uygulandı

        # Km etkisi: her 1000 km sapma başına tabanın %0.1'i
        beklenen_km = yas * ORTALAMA_YILLIK_KM
        km_fark = arac.kilometre - beklenen_km
        km_oran_1000km = taban * 0.001  # TL / 1000 km
        km_etkisi = -round((km_fark / 1000) * km_oran_1000km)

    fiyat_oncesi = taban + yas_etkisi + km_etkisi
    hasar_etkisi = -round(fiyat_oncesi * 0.20) if arac.hasar_kaydi else 0
    piyasa = random.randint(-round(taban * 0.02), round(taban * 0.02))  # ±%2 dalgalanma

    fiyat = fiyat_oncesi + hasar_etkisi + piyasa
    if fiyat < 50_000:
        fiyat = 50_000

    return {
        "fiyat": round(fiyat),
        "faktorler": {
            "taban_fiyat": taban,
            "yas_etkisi": yas_etkisi,
            "kilometre_etkisi": km_etkisi,
            "hasar_etkisi": hasar_etkisi,
            "piyasa_dalgalanmasi": piyasa,
        },
    }
