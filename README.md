# Araç Değerleme Servisi (Valuation Microservice) - HW1

Bu proje, araçların marka, model yılı, kilometre ve hasar durumu gibi özelliklerini alarak tahmini güncel piyasa değerini hesaplayan bir mikroservistir.

## Kullanılan Teknolojiler ve Konseptler
- **FastAPI & Python:** REST API tasarımı için kullanıldı.
- **Clean Architecture:** `main.py`, `models.py` ve `services.py` olarak kodlar ayrıştırıldı.
- **Security:** API Key ile yetkilendirme sağlandı.
- **Observability:** İşlemler ve hatalar için Loglama entegre edildi.
- **Containerization:** Sistem `Dockerfile` ile paketlendi.
- **AI Integration (Opsiyonel):** Basit bir fiyatlandırma/değer kaybı algoritması eklendi.
