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
