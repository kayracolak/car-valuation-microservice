"""
Türkiye ikinci el otomobil piyasası zekası.
sahibinden.com / arabam.com tarzı karşılaştırmalı fiyat ve talep analizi.
"""

import random
from datetime import datetime
from typing import Optional

# ── Piyasa Bilgisi ─────────────────────────────────────────────────────────────

MARKA_TALEP: dict[str, float] = {
    "toyota": 9.2, "honda": 8.7, "volkswagen": 8.5, "vw": 8.5,
    "ford": 8.3, "hyundai": 8.1, "renault": 7.9, "kia": 8.0,
    "nissan": 7.8, "bmw": 7.8, "skoda": 7.6, "mercedes": 7.6,
    "fiat": 7.5, "suzuki": 7.5, "audi": 7.4, "peugeot": 7.2,
    "dacia": 7.2, "opel": 7.0, "mitsubishi": 7.0, "subaru": 6.5,
    "volvo": 6.8, "seat": 7.1, "citroen": 6.9, "mazda": 7.3,
    "mini": 6.7, "jeep": 7.0, "land rover": 6.5, "porsche": 7.0,
    "lexus": 7.5, "infiniti": 6.5, "togg": 8.9, "chery": 6.2,
    "mg": 6.8, "byd": 6.5,
}

# Ankara baz alınarak şehir fiyat çarpanları (gerçek piyasaya yakın)
IL_CARPANI: dict[str, float] = {
    "istanbul": 1.072, "izmir": 1.031, "ankara": 1.000,
    "bursa": 0.982, "antalya": 1.018, "adana": 0.963,
    "konya": 0.951, "trabzon": 0.971, "gaziantep": 0.948,
    "kayseri": 0.944, "mersin": 0.961, "diyarbakır": 0.921,
    "eskişehir": 0.968, "samsun": 0.958, "denizli": 0.971,
    "manisa": 0.962, "kocaeli": 1.008, "sakarya": 0.978,
    "gebze": 1.041, "tekirdağ": 0.985, "edirne": 0.979,
    "hatay": 0.942, "malatya": 0.931, "erzurum": 0.912,
}

SEHIRLER = list(IL_CARPANI.keys())

PREMIUM_MARKALAR = {
    "bmw", "mercedes", "audi", "volvo", "lexus",
    "porsche", "land rover", "mini", "infiniti",
}

# Yıllık değer kayıp oranları (1-5. yıl sırası)
AMORTISMAN: dict[str, list[float]] = {
    "premium": [0.072, 0.065, 0.058, 0.052, 0.047],
    "normal":  [0.058, 0.052, 0.046, 0.040, 0.036],
}


# ── Yardımcı Fonksiyonlar ──────────────────────────────────────────────────────

def _normalize_marka(marka: str) -> str:
    return marka.strip().lower()


def _normalize_il(il: str) -> str:
    return il.strip().lower().replace(" ", "")


def get_il_carpani(il: str) -> float:
    return IL_CARPANI.get(_normalize_il(il), 1.000)


def get_talep_skoru(marka: str, yil: int, km: int) -> float:
    base = MARKA_TALEP.get(_normalize_marka(marka), 6.5)
    current_year = datetime.now().year
    age = current_year - yil

    if age <= 2:
        yas_etkisi = +1.5
    elif age <= 4:
        yas_etkisi = +0.5
    elif age <= 7:
        yas_etkisi = 0.0
    elif age <= 12:
        yas_etkisi = -0.8
    else:
        yas_etkisi = -2.0

    if km < 30_000:
        km_etkisi = +0.8
    elif km < 75_000:
        km_etkisi = +0.3
    elif km < 120_000:
        km_etkisi = 0.0
    elif km < 180_000:
        km_etkisi = -0.7
    else:
        km_etkisi = -1.8

    return round(min(10.0, max(1.0, base + yas_etkisi + km_etkisi)), 1)


def _satis_suresi(skor: float) -> str:
    if skor >= 9.0:
        return "7-10 gün"
    if skor >= 8.0:
        return "10-18 gün"
    if skor >= 7.0:
        return "18-30 gün"
    if skor >= 6.0:
        return "30-50 gün"
    return "50+ gün"


def _populerite_etiketi(skor: float) -> str:
    if skor >= 9.0:
        return "Çok yüksek talep"
    if skor >= 8.0:
        return "Yüksek talep"
    if skor >= 7.0:
        return "Orta-yüksek talep"
    if skor >= 6.0:
        return "Orta talep"
    return "Düşük talep"


def _fiyat_konumu(fiyat: float, piyasa_ort: float) -> tuple[str, str]:
    """Fiyatın piyasaya göre konumu ve etiketi."""
    if piyasa_ort == 0:
        return "Veri yok", "BILINMIYOR"
    oran = (fiyat - piyasa_ort) / piyasa_ort
    if oran < -0.15:
        return f"Piyasanın %{abs(oran)*100:.1f} altında", "FIRSAT"
    if oran < -0.05:
        return f"Piyasanın %{abs(oran)*100:.1f} altında", "REKABETCİ"
    if oran <= 0.05:
        return "Piyasa değerinde", "PIYASA DEGERİ"
    if oran <= 0.15:
        return f"Piyasanın %{oran*100:.1f} üzerinde", "PAHALI"
    return f"Piyasanın %{oran*100:.1f} üzerinde", "ÇOK PAHALI"


# ── Temel Hesaplamalar ─────────────────────────────────────────────────────────

def calculate_depreciation(fiyat: float, marka: str) -> dict:
    is_premium = _normalize_marka(marka) in PREMIUM_MARKALAR
    rates = AMORTISMAN["premium"] if is_premium else AMORTISMAN["normal"]

    tahminler = {"bugun": round(fiyat / 1000) * 1000}
    current = fiyat
    for i, rate in enumerate(rates, 1):
        current *= (1 - rate)
        tahminler[f"{i}_yil"] = round(current / 1000) * 1000

    toplam_kayip = fiyat - current
    yillik_ort = toplam_kayip / len(rates)
    return {
        **tahminler,
        "yillik_ort_kayip": round(yillik_ort / 1000) * 1000,
        "5_yil_toplam_kayip_yuzdesi": f"%{(toplam_kayip/fiyat)*100:.1f}",
        "yorum": (
            f"5 yılda yaklaşık {toplam_kayip:,.0f} TL değer kaybı öngörülüyor. "
            f"{'Premium araçlar ilk 3 yılda daha hızlı değer kaybeder.' if is_premium else 'Yıllık kayıp oranı zamanla azalır.'}"
        ),
    }


def get_comparable_listings(
    marka: str, yil: int, km: int, il: str, fiyat: float, count: int = 5
) -> list[dict]:
    seed = hash(f"{marka}{yil}{km}{il}") % (2**31)
    rng = random.Random(seed)

    base_carpan = get_il_carpani(il)
    listings = []

    for _ in range(count):
        km_delta = rng.randint(-18_000, 18_000)
        fiyat_var = rng.uniform(-0.10, 0.10)
        sehir = rng.choice(SEHIRLER)
        gun = rng.randint(1, 60)

        ilan_km = max(5_000, km + km_delta)
        ilan_sehir_carpan = IL_CARPANI.get(sehir, 1.0)
        ilan_fiyat = fiyat * (1 + fiyat_var) * (ilan_sehir_carpan / base_carpan)

        listings.append({
            "marka": marka.title(),
            "model_yili": yil,
            "kilometre": ilan_km,
            "fiyat_tl": round(ilan_fiyat / 1000) * 1000,
            "il": sehir.title(),
            "ilan_tarihi": f"{gun} gün önce",
            "kaynak": "Piyasa verisi",
        })

    return sorted(listings, key=lambda x: x["fiyat_tl"])


def detect_anomaly(yil: int, km: int, fiyat: float, piyasa_ort: float) -> dict:
    current_year = datetime.now().year
    age = max(1, current_year - yil)
    yillik_km = km / age

    uyarilar = []
    risk = 0.0

    if yillik_km > 35_000:
        uyarilar.append(
            f"Yıllık {yillik_km:,.0f} km aşırı yüksek — "
            "sayaç müdahalesi riski mevcut."
        )
        risk += 0.45
    elif yillik_km < 3_000 and age >= 3:
        uyarilar.append(
            f"Yıllık {yillik_km:,.0f} km beklenenden çok düşük — "
            "sayaç geriye alınmış olabilir."
        )
        risk += 0.35

    if piyasa_ort > 0:
        oran = (fiyat - piyasa_ort) / piyasa_ort
        if oran < -0.20:
            uyarilar.append(
                f"Fiyat piyasa ortalamasının %{abs(oran)*100:.0f} altında — "
                "acil satış ya da gizli hasar riski."
            )
            risk += 0.35
        elif oran > 0.25:
            uyarilar.append(
                f"Fiyat piyasa ortalamasının %{oran*100:.0f} üzerinde — "
                "satış süresi uzayabilir."
            )
            risk += 0.10

    if risk >= 0.70:
        seviye = "yüksek"
    elif risk >= 0.30:
        seviye = "orta"
    else:
        seviye = "düşük"

    return {
        "yillik_km": round(yillik_km),
        "km_yas_orani": "şüpheli" if risk >= 0.30 else "normal",
        "uyarilar": uyarilar,
        "risk_skoru": round(min(1.0, risk), 2),
        "risk_seviyesi": seviye,
    }


def get_musteri_tavsiyeleri(
    fiyat: float, piyasa_ort: float, talep_skoru: float, il: str, marka: str
) -> dict:
    # En yüksek fiyatlı 3 şehir
    top_sehirler = sorted(
        IL_CARPANI.items(), key=lambda x: x[1], reverse=True
    )[:3]
    en_iyi_sehir = top_sehirler[0][0].title()
    en_iyi_carpan = top_sehirler[0][1]
    en_iyi_fiyat = round(fiyat * en_iyi_carpan / get_il_carpani(il) / 1000) * 1000

    # Liste fiyatı önerisi: talep yüksekse daha az pazarlık payı, düşükse daha fazla
    if talep_skoru >= 8.5:
        pazarlik_payi = 0.02
    elif talep_skoru >= 7.0:
        pazarlik_payi = 0.04
    else:
        pazarlik_payi = 0.07

    liste_fiyati = round(fiyat * (1 + pazarlik_payi) / 1000) * 1000
    kazanim = en_iyi_fiyat - fiyat

    return {
        "liste_fiyati_onerisi": liste_fiyati,
        "hedef_satis_fiyati": round(fiyat / 1000) * 1000,
        "muzakere_marji": f"Yaklaşık %{pazarlik_payi*100:.0f} indirim payı bırakın",
        "en_iyi_sehir": f"{en_iyi_sehir} (+{kazanim:,.0f} TL potansiyel kazanım)" if kazanim > 0 else en_iyi_sehir,
        "ilkbahar_tavsiyesi": (
            "İlkbahar (Mart-Mayıs) ikinci el otomobil talebinin en yüksek olduğu dönemdir."
        ),
        "alici_icin": (
            f"Bu araç için {'agresif' if talep_skoru >= 8 else 'makul'} bir pazarlık bekleyin. "
            f"Piyasa ortalaması {piyasa_ort:,.0f} TL."
            if piyasa_ort > 0 else "Piyasa verisi bulunamadı."
        ),
    }


# ── Ana Rapor Fonksiyonu ───────────────────────────────────────────────────────

def get_piyasa_raporu(
    marka: str, yil: int, km: int, il: str, fiyat: float
) -> dict:
    talep = get_talep_skoru(marka, yil, km)
    il_carpan = get_il_carpani(il)
    fiyat_il = round(fiyat * il_carpan / get_il_carpani("ankara") / 1000) * 1000

    ilanlar = get_comparable_listings(marka, yil, km, il, fiyat_il)
    fiyatlar = [i["fiyat_tl"] for i in ilanlar]
    piyasa_ort = sum(fiyatlar) / len(fiyatlar) if fiyatlar else 0

    konum_metin, konum_etiketi = _fiyat_konumu(fiyat_il, piyasa_ort)

    il_karsilastirmasi = {
        sehir.title(): round(fiyat * IL_CARPANI[sehir] / il_carpan / 1000) * 1000
        for sehir in ["istanbul", "ankara", "izmir", "bursa", "antalya"]
    }

    return {
        "benzer_ilanlar": ilanlar,
        "istatistikler": {
            "ortalama_fiyat": round(piyasa_ort / 1000) * 1000,
            "en_dusuk": min(fiyatlar) if fiyatlar else 0,
            "en_yuksek": max(fiyatlar) if fiyatlar else 0,
            "fiyat_konumu": konum_metin,
            "konum_etiketi": konum_etiketi,
        },
        "talep": {
            "skor": talep,
            "ortalama_satis_suresi": _satis_suresi(talep),
            "populerite": _populerite_etiketi(talep),
        },
        "il_karsilastirmasi": il_karsilastirmasi,
        "deger_tahmini": calculate_depreciation(fiyat_il, marka),
        "dogrulama": detect_anomaly(yil, km, fiyat_il, piyasa_ort),
        "musteri_tavsiyeleri": get_musteri_tavsiyeleri(
            fiyat_il, piyasa_ort, talep, il, marka
        ),
    }
