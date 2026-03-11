import pika
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RABBITMQ_URL = "amqp://guest:guest@rabbitmq:5672/"
QUEUE_NAME = "valuation_events"


def callback(ch, method, properties, body):
    message = body.decode()
    logger.info(f"[BILDIRIM] Kullanıcıya bildirim e-postası gönderildi: Araç başarıyla değerlendi - '{message}'")


def main():
    logger.info("Notification Service başlatılıyor...")
    while True:
        try:
            connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            logger.info(f"'{QUEUE_NAME}' kuyruğu dinleniyor. Mesaj bekleniyor...")
            channel.basic_consume(
                queue=QUEUE_NAME,
                on_message_callback=callback,
                auto_ack=True
            )
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as e:
            logger.warning(f"RabbitMQ bağlantısı kurulamadı, 5 saniye sonra tekrar deneniyor... Hata: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
