# 1. Hangi Python sürümünü kullanacağımızı seçiyoruz (Sihirli kutumuzun temeli)
FROM python:3.10-slim

# 2. Kutunun içindeki çalışma klasörümüzü belirliyoruz
WORKDIR /app

# 3. İhtiyacımız olan kütüphanelerin listesini kutuya kopyalıyoruz
COPY requirements.txt .

# 4. Kütüphaneleri kutunun içine kuruyoruz
RUN pip install --no-cache-dir -r requirements.txt

# 5. Kendi yazdığımız kodları (main.py vb.) kutuya kopyalıyoruz
COPY . .

# 6. Sistem çalıştığında API'nin yayın yapacağı portu açıyoruz
EXPOSE 8000

# 7. Kutu çalıştırıldığında sistemimizi başlatacak komutu veriyoruz
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]