import random
from datetime import datetime


def fiyat_hesapla(arac):
    taban_fiyat = 800000
    guncel_yil = datetime.now().year
    yas = guncel_yil - arac.model_yili
    fiyat = taban_fiyat - (yas * 25000)
    fiyat = fiyat - (arac.kilometre * 1.2)

    if arac.hasar_kaydi:
        fiyat = fiyat * 0.80

    fiyat = fiyat + random.randint(-15000, 15000)

    if fiyat < 50000:
        fiyat = 50000

    return round(fiyat, 2)


def fiyat_hesapla_detay(arac):
    """Fiyatı hesaplar ve her faktörün katkısını döner."""
    taban = 800000
    guncel_yil = datetime.now().year
    yas = guncel_yil - arac.model_yili
    yas_etkisi = -(yas * 25000)
    km_etkisi = -round(arac.kilometre * 1.2)

    fiyat_oncesi = taban + yas_etkisi + km_etkisi
    hasar_etkisi = 0
    if arac.hasar_kaydi:
        hasar_etkisi = -round(fiyat_oncesi * 0.20)

    piyasa = random.randint(-15000, 15000)

    fiyat = taban + yas_etkisi + km_etkisi + hasar_etkisi + piyasa
    if fiyat < 50000:
        fiyat = 50000

    return {
        "fiyat": round(fiyat, 2),
        "faktorler": {
            "taban_fiyat": taban,
            "yas_etkisi": yas_etkisi,
            "kilometre_etkisi": km_etkisi,
            "hasar_etkisi": hasar_etkisi,
            "piyasa_dalgalanmasi": piyasa,
        },
    }
