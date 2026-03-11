from fastapi import FastAPI
import logging
import pika

from models import AracOzellikleri
from services import fiyat_hesapla

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Araç Değerleme Servisi (Valuation Service)")

RABBITMQ_URL = "amqp://guest:guest@rabbitmq:5672/"
QUEUE_NAME = "valuation_events"


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


@app.get("/")
def ana_sayfa():
    return {"mesaj": "Araç Değerleme Servisi çalışıyor!"}


@app.post("/api/v1/degerleme")
def arac_degerle(arac: AracOzellikleri):
    logger.info(f"Değerleme isteği alındı: {arac.marka} {arac.model_yili}")

    hesaplanan_fiyat = fiyat_hesapla(arac)

    publish_event(f"Araç değerlemesi yapıldı: {arac.marka}")

    return {
        "arac_bilgisi": arac,
        "hesaplanan_fiyat_tl": hesaplanan_fiyat,
    }
