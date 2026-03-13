from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import logging
import pika
import os
from openai import OpenAI

from models import AracOzellikleri
from services import fiyat_hesapla

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Araç Değerleme Servisi (Valuation Service)")

RABBITMQ_URL = "amqp://guest:guest@rabbitmq:5672/"
QUEUE_NAME = "valuation_events"

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def publish_event(message: str):
    try:
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        channel = connection.channel()
        channel.queue_declare(queue=QUEUE_NAME, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=message.encode(),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        logger.info(f"RabbitMQ event gönderildi: {message}")
    except Exception as e:
        logger.warning(f"RabbitMQ'ya event gönderilemedi (servis devam ediyor): {e}")


def ai_analiz_yap(arac: AracOzellikleri, fiyat: float) -> dict:
    try:
        prompt = (
            f"Araç: {arac.marka} {arac.model_yili}, {arac.kilometre}km, "
            f"hasar={'var' if arac.hasar_kaydi else 'yok'}, fiyat={fiyat:.0f}TL. "
            f"JSON ver: {{\"piyasa_yorumu\":\"fiyat makul mu (1 cümle)\",\"ozet\":\"güçlü/zayıf yön (1 cümle)\"}}"
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=150,
        )
        import json
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.warning(f"AI analizi yapılamadı: {e}")
        return {"piyasa_yorumu": "AI analizi şu an kullanılamıyor.", "ozet": ""}


@app.get("/", response_class=HTMLResponse)
def ana_sayfa():
    return """
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Araç Değerleme</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f5f5f5;
      display: flex;
      justify-content: center;
      padding: 40px 16px;
    }
    .container { width: 100%; max-width: 480px; }
    h1 { font-size: 22px; font-weight: 600; margin-bottom: 24px; color: #111; }
    .card {
      background: #fff;
      border-radius: 12px;
      padding: 24px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.1);
      margin-bottom: 16px;
    }
    label { display: block; font-size: 13px; color: #555; margin-bottom: 4px; margin-top: 16px; }
    label:first-of-type { margin-top: 0; }
    input[type=text], input[type=number] {
      width: 100%;
      padding: 10px 12px;
      border: 1px solid #ddd;
      border-radius: 8px;
      font-size: 15px;
      outline: none;
      transition: border-color 0.2s;
    }
    input:focus { border-color: #555; }
    .checkbox-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 16px;
    }
    .checkbox-row input { width: auto; }
    .checkbox-row label { margin: 0; font-size: 15px; color: #111; }
    button {
      width: 100%;
      margin-top: 20px;
      padding: 12px;
      background: #111;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
    }
    button:disabled { background: #999; cursor: not-allowed; }
    .result { display: none; }
    .fiyat { font-size: 28px; font-weight: 700; color: #111; }
    .fiyat span { font-size: 16px; font-weight: 400; color: #777; margin-left: 4px; }
    .section-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #999; margin-bottom: 8px; }
    .analiz-metin { font-size: 14px; color: #333; line-height: 1.6; }
    .divider { border: none; border-top: 1px solid #eee; margin: 16px 0; }
    .error { color: #c00; font-size: 14px; display: none; padding: 10px; background: #fff0f0; border-radius: 8px; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Araç Değerleme</h1>

    <div class="card">
      <label>Marka</label>
      <input type="text" id="marka" placeholder="Toyota, BMW, Ford...">

      <label>Model Yılı</label>
      <input type="number" id="model_yili" placeholder="2020" min="1990" max="2025">

      <label>Kilometre</label>
      <input type="number" id="kilometre" placeholder="80000" min="0">

      <div class="checkbox-row">
        <input type="checkbox" id="hasar_kaydi">
        <label for="hasar_kaydi">Hasar kaydı var</label>
      </div>

      <button id="btn" onclick="degerle()">Değerle</button>
      <p class="error" id="error">Bir hata oluştu, tekrar deneyin.</p>
    </div>

    <div class="card result" id="result">
      <p class="section-title">Tahmini Değer</p>
      <p class="fiyat" id="fiyat-text">— <span>TL</span></p>
      <hr class="divider">
      <p class="section-title">Piyasa Yorumu</p>
      <p class="analiz-metin" id="piyasa-yorumu">—</p>
      <hr class="divider">
      <p class="section-title">Araç Özeti</p>
      <p class="analiz-metin" id="ozet">—</p>
    </div>
  </div>

  <script>
    async function degerle() {
      const btn = document.getElementById('btn');
      const error = document.getElementById('error');
      const result = document.getElementById('result');
      error.style.display = 'none';
      result.style.display = 'none';

      const marka = document.getElementById('marka').value.trim();
      const model_yili = parseInt(document.getElementById('model_yili').value);
      const kilometre = parseInt(document.getElementById('kilometre').value);
      const hasar_kaydi = document.getElementById('hasar_kaydi').checked;

      if (!marka || !model_yili || isNaN(kilometre)) {
        error.textContent = 'Lütfen tüm alanları doldurun.';
        error.style.display = 'block';
        return;
      }

      btn.disabled = true;
      btn.textContent = 'Hesaplanıyor...';

      try {
        const res = await fetch('/api/v1/degerleme', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ marka, model_yili, kilometre, hasar_kaydi })
        });
        const data = await res.json();

        document.getElementById('fiyat-text').innerHTML =
          Number(data.hesaplanan_fiyat_tl).toLocaleString('tr-TR') + ' <span>TL</span>';
        document.getElementById('piyasa-yorumu').textContent = data.ai_analizi?.piyasa_yorumu || '—';
        document.getElementById('ozet').textContent = data.ai_analizi?.ozet || '—';

        result.style.display = 'block';
      } catch (e) {
        error.textContent = 'Bir hata oluştu, tekrar deneyin.';
        error.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Değerle';
      }
    }
  </script>
</body>
</html>
"""


@app.post("/api/v1/degerleme")
def arac_degerle(arac: AracOzellikleri):
    logger.info(f"Değerleme isteği alındı: {arac.marka} {arac.model_yili}")

    hesaplanan_fiyat = fiyat_hesapla(arac)

    ai_analizi = ai_analiz_yap(arac, hesaplanan_fiyat)

    publish_event(f"Araç değerlemesi yapıldı: {arac.marka}")

    return {
        "arac_bilgisi": arac,
        "hesaplanan_fiyat_tl": hesaplanan_fiyat,
        "ai_analizi": ai_analizi,
    }
