import logging
import sys
from contextvars import ContextVar

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger
from prometheus_fastapi_instrumentator import Instrumentator

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from market_engine import (
    get_piyasa_raporu,
    IL_CARPANI,
    MARKA_TALEP,
    SEHIRLER,
)

# ── Tracing ────────────────────────────────────────────────────────────────────
_resource = Resource.create({"service.name": "market-data-service"})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://jaeger:4318/v1/traces"))
)
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer(__name__)

# ── Structured logging ─────────────────────────────────────────────────────────
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
SERVICE_NAME = "market-data-service"


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.service = SERVICE_NAME
        record.trace_id = _trace_id_var.get("")
        return True


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    jsonlogger.JsonFormatter(fmt="%(asctime)s %(levelname)s %(name)s %(message)s")
)
_handler.addFilter(_ContextFilter())
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger(SERVICE_NAME)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Market Data Service",
    description="Türkiye ikinci el otomobil piyasası zekası — sahibinden.com / arabam.com benzeri analitik",
)
FastAPIInstrumentor.instrument_app(app)
try:
    Instrumentator().instrument(app).expose(app)
except Exception:
    pass


class PiyasaRaporuIstek(BaseModel):
    marka: str
    model_yili: int
    kilometre: int
    il: str = "ankara"
    hesaplanan_fiyat_tl: float


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/api/v1/piyasa-raporu")
def piyasa_raporu(istek: PiyasaRaporuIstek):
    with tracer.start_as_current_span("generate_market_report") as span:
        span.set_attribute("vehicle.brand", istek.marka)
        span.set_attribute("vehicle.year", istek.model_yili)
        span.set_attribute("vehicle.km", istek.kilometre)
        span.set_attribute("vehicle.city", istek.il)

        logger.info(
            "Piyasa raporu oluşturuluyor",
            extra={"marka": istek.marka, "il": istek.il},
        )

        rapor = get_piyasa_raporu(
            marka=istek.marka,
            yil=istek.model_yili,
            km=istek.kilometre,
            il=istek.il,
            fiyat=istek.hesaplanan_fiyat_tl,
        )
        return rapor


@app.get("/api/v1/iller")
def iller():
    """Şehir listesi ve fiyat çarpanları."""
    return {
        sehir.title(): {
            "carpan": carpan,
            "etki": f"+%{(carpan-1)*100:.1f}" if carpan >= 1 else f"-%{(1-carpan)*100:.1f}",
            "baz": "Ankara",
        }
        for sehir, carpan in sorted(IL_CARPANI.items(), key=lambda x: x[1], reverse=True)
    }


@app.get("/api/v1/markalar")
def markalar():
    """Marka talep skorları."""
    return {
        marka.title(): {"talep_skoru": skor, "kategori": "Premium" if marka in {"bmw", "mercedes", "audi", "volvo", "lexus", "porsche"} else "Standart"}
        for marka, skor in sorted(MARKA_TALEP.items(), key=lambda x: x[1], reverse=True)
    }


@app.get("/api/v1/sehir-kiyasi")
def sehir_kiyasi(fiyat: float = Query(..., description="Ankara baz fiyatı (TL)")):
    """Verilen fiyatı tüm şehirlerde hesaplar."""
    return {
        sehir.title(): round(fiyat * carpan / 1000) * 1000
        for sehir, carpan in sorted(IL_CARPANI.items(), key=lambda x: x[1], reverse=True)
    }
