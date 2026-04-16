import logging
import sys
import time

import pika
from pythonjsonlogger import jsonlogger
from tenacity import retry, stop_after_attempt, wait_exponential

from opentelemetry import trace, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# ── Tracing setup ──────────────────────────────────────────────────────────────
_resource = Resource.create({"service.name": "notification-service"})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://jaeger:4318/v1/traces"))
)
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer(__name__)

# ── Structured logging ─────────────────────────────────────────────────────────
SERVICE_NAME = "notification-service"


class _ServiceFilter(logging.Filter):
    def filter(self, record):
        record.service = SERVICE_NAME
        return True


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    jsonlogger.JsonFormatter(fmt="%(asctime)s %(levelname)s %(name)s %(message)s")
)
_handler.addFilter(_ServiceFilter())
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(SERVICE_NAME)

# ── Config ─────────────────────────────────────────────────────────────────────
RABBITMQ_URL = "amqp://guest:guest@rabbitmq:5672/"
QUEUE_NAME = "valuation_events"
DLQ_NAME = "valuation_events_dlq"


def callback(ch, method, properties, body):
    # Propagate trace context from message headers
    headers = dict(properties.headers or {})
    ctx = propagate.extract(headers)

    with tracer.start_as_current_span("process_valuation_event", context=ctx) as span:
        try:
            message = body.decode()
            span.set_attribute("message.content", message[:200])
            logger.info(
                "Kullanıcıya bildirim e-postası gönderildi",
                extra={"message": message},
            )
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            logger.error(
                "Mesaj işlenemedi, DLQ'ya gönderiliyor",
                extra={"error": str(e)},
            )
            span.record_exception(e)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _connect_and_consume():
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()

    # DLQ tanımla
    channel.queue_declare(queue=DLQ_NAME, durable=True)
    # Ana kuyruk — DLQ argümanıyla
    channel.queue_declare(
        queue=QUEUE_NAME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": DLQ_NAME,
        },
    )
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(
        queue=QUEUE_NAME,
        on_message_callback=callback,
        auto_ack=False,
    )
    logger.info(f"'{QUEUE_NAME}' kuyruğu dinleniyor")
    channel.start_consuming()


def main():
    logger.info("Notification Service başlatılıyor")
    while True:
        try:
            _connect_and_consume()
        except pika.exceptions.AMQPConnectionError as e:
            logger.warning(
                "RabbitMQ bağlantısı kesildi, yeniden bağlanılıyor",
                extra={"error": str(e)},
            )
            time.sleep(5)
        except Exception as e:
            logger.error("Beklenmeyen hata", extra={"error": str(e)})
            time.sleep(5)


if __name__ == "__main__":
    main()
