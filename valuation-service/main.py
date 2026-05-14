import json
import logging
import os
import sys
from contextvars import ContextVar
from typing import Optional

import httpx
import pika
import pybreaker
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from openai import OpenAI
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from pythonjsonlogger import jsonlogger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from models import AracOzellikleri
from services import fiyat_hesapla, fiyat_hesapla_detay

# ── Tracing ────────────────────────────────────────────────────────────────────
_resource = Resource.create({"service.name": "valuation-service"})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://jaeger:4318/v1/traces"))
)
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer(__name__)

# ── Structured logging ─────────────────────────────────────────────────────────
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
SERVICE_NAME = "valuation-service"


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

# ── Prometheus ─────────────────────────────────────────────────────────────────
class _Noop:
    def observe(self, *a, **kw): pass
    def inc(self, *a, **kw): pass
    def set(self, *a, **kw): pass
    def labels(self, **kw): return self


try:
    valuation_price_histogram = Histogram(
        "valuation_price_tl", "Hesaplanan araç fiyatı (TL)",
        buckets=[100_000, 200_000, 300_000, 400_000, 500_000, 600_000, 700_000, 800_000, 1_000_000],
    )
    openai_calls_counter = Counter(
        "openai_api_calls_total", "OpenAI API çağrı sayısı", ["status"],
    )
    circuit_breaker_state_gauge = Gauge(
        "circuit_breaker_state", "Circuit breaker durumu (0=kapalı, 1=açık, 2=yarı-açık)",
    )
    market_calls_counter = Counter(
        "market_data_calls_total", "Piyasa servisi çağrı sayısı", ["status"],
    )
except ValueError:
    valuation_price_histogram = _Noop()
    openai_calls_counter = _Noop()
    circuit_breaker_state_gauge = _Noop()
    market_calls_counter = _Noop()

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Araç Değerleme Servisi")
FastAPIInstrumentor.instrument_app(app)
try:
    Instrumentator().instrument(app).expose(app)
except Exception:
    pass

RABBITMQ_URL = "amqp://guest:guest@rabbitmq:5672/"
QUEUE_NAME = "valuation_events"
DLQ_NAME = "valuation_events_dlq"
MARKET_DATA_URL = "http://market-data-service:8002"

# Model sabitleri — env ile override edilebilir
MODEL_ANALIZ = os.getenv("OPENAI_MODEL_ANALIZ", "gpt-4o-mini")
MODEL_CHAT = os.getenv("OPENAI_MODEL_CHAT", "gpt-4o-mini")
MODEL_PAZARLIK = os.getenv("OPENAI_MODEL_PAZARLIK", "gpt-4.1-nano")

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

_openai_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30, name="openai")
_chatbot_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name="chatbot")


class PazarlikIstegi(BaseModel):
    marka: str
    model: Optional[str] = ""
    model_yili: int
    kilometre: int
    hasar_kaydi: bool
    il: Optional[str] = "ankara"
    satis_fiyati: float
    hesaplanan_fiyat: float


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-ID", "")
    token = _trace_id_var.set(trace_id)
    response = await call_next(request)
    _trace_id_var.reset(token)
    return response


# ── RabbitMQ ───────────────────────────────────────────────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
       retry=retry_if_exception_type(Exception), reraise=False)
def _publish_to_rabbitmq(message: str):
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()
    channel.queue_declare(queue=DLQ_NAME, durable=True)
    channel.queue_declare(
        queue=QUEUE_NAME, durable=True,
        arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": DLQ_NAME},
    )
    channel.basic_publish(
        exchange="", routing_key=QUEUE_NAME, body=message.encode(),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    connection.close()


def publish_event(message: str):
    try:
        _publish_to_rabbitmq(message)
        logger.info("RabbitMQ event gönderildi", extra={"message": message})
    except Exception as e:
        logger.warning("RabbitMQ'ya event gönderilemedi", extra={"error": str(e)})


# ── OpenAI ─────────────────────────────────────────────────────────────────────
@_openai_breaker
def _call_openai(prompt: str, max_tokens: int = 300, model: str = "gpt-4o-mini") -> dict:
    resp = openai_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
    )
    return json.loads(resp.choices[0].message.content)


@_chatbot_breaker
def _call_chat(messages: list, max_tokens: int = 500) -> str:
    resp = openai_client.chat.completions.create(
        model=MODEL_CHAT,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return resp.choices[0].message.content


def ai_analiz_yap(arac: AracOzellikleri, fiyat: float) -> dict:
    prompt = (
        f"Araç: {arac.marka} {arac.model_yili}, {arac.kilometre}km, "
        f"hasar={'var' if arac.hasar_kaydi else 'yok'}, fiyat={fiyat:.0f}TL, sehir={arac.il}. "
        f"JSON ver: {{\"piyasa_yorumu\":\"fiyat degerlendirmesi 1 cumle\","
        f"\"ozet\":\"guclu veya zayif yon 1 cumle\","
        f"\"ongoru\":\"6 ay fiyat tahmini 1 cumle\","
        f"\"alici_profili\":\"ideal alici profili 1 cumle\","
        f"\"satis_taktigi\":\"en hizli satis icin 1 cumle\"}}"
    )
    with tracer.start_as_current_span("openai_analysis") as span:
        span.set_attribute("vehicle.brand", arac.marka)
        try:
            result = _call_openai(prompt, max_tokens=350, model=MODEL_ANALIZ)
            openai_calls_counter.labels(status="success").inc()
            circuit_breaker_state_gauge.set(0)
            return result
        except pybreaker.CircuitBreakerError:
            openai_calls_counter.labels(status="circuit_open").inc()
            circuit_breaker_state_gauge.set(1)
            logger.warning("Circuit breaker açık — OpenAI çağrısı atlandı")
            span.set_attribute("circuit_breaker", "open")
        except Exception as e:
            openai_calls_counter.labels(status="failure").inc()
            logger.warning("AI analizi yapılamadı", extra={"error": str(e)})
            span.record_exception(e)
    return {
        "piyasa_yorumu": "AI analizi şu an kullanılamıyor.",
        "ozet": "", "ongoru": "", "alici_profili": "", "satis_taktigi": "",
    }


def pazarlik_argumanlari_yap(istek: PazarlikIstegi) -> dict:
    fark = istek.satis_fiyati - istek.hesaplanan_fiyat
    prompt = (
        f"Arac: {istek.marka} {istek.model_yili}, {istek.kilometre}km, "
        f"hasar={'var' if istek.hasar_kaydi else 'yok'}, sehir={istek.il}. "
        f"Satici fiyati: {istek.satis_fiyati:.0f}TL, piyasa degeri: {istek.hesaplanan_fiyat:.0f}TL "
        f"(fark: {fark:.0f}TL). "
        f"Alici perspektifinden 4 somut pazarlik argumani uret. Her biri icin gercekci indirim TL belirt. "
        f"JSON: {{\"argumanlar\":[{{\"baslik\":\"...\",\"detay\":\"...\",\"indirim_tl\":15000}}],"
        f"\"hedef_fiyat\":{int(istek.hesaplanan_fiyat*0.98)},"
        f"\"alt_sinir\":{int(istek.hesaplanan_fiyat*0.94)},"
        f"\"ozet\":\"tek cumle\"}}"
    )
    with tracer.start_as_current_span("pazarlik_kocu") as span:
        span.set_attribute("vehicle.brand", istek.marka)
        try:
            result = _call_openai(prompt, max_tokens=450, model=MODEL_PAZARLIK)
            logger.info("Pazarlık argümanları üretildi", extra={"marka": istek.marka})
            return result
        except pybreaker.CircuitBreakerError:
            logger.warning("Pazarlık koçu — circuit breaker açık")
        except Exception as e:
            logger.warning("Pazarlık koçu hata", extra={"error": str(e)})
            span.record_exception(e)
    return {
        "argumanlar": [],
        "hedef_fiyat": int(istek.hesaplanan_fiyat),
        "alt_sinir": int(istek.hesaplanan_fiyat * 0.95),
        "ozet": "AI şu an kullanılamıyor, lütfen daha sonra tekrar deneyin.",
    }


# ── Market Data Service ────────────────────────────────────────────────────────
def _get_piyasa_raporu(arac: AracOzellikleri, fiyat: float) -> dict | None:
    with tracer.start_as_current_span("market_data_call") as span:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(
                    f"{MARKET_DATA_URL}/api/v1/piyasa-raporu",
                    json={
                        "marka": arac.marka,
                        "model_yili": arac.model_yili,
                        "kilometre": arac.kilometre,
                        "il": arac.il or "ankara",
                        "hesaplanan_fiyat_tl": fiyat,
                    },
                )
                market_calls_counter.labels(status="success").inc()
                return resp.json()
        except Exception as e:
            market_calls_counter.labels(status="failure").inc()
            logger.warning("Piyasa servisi ulaşılamadı", extra={"error": str(e)})
            span.record_exception(e)
            return None


# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    rabbitmq_ok = True
    try:
        params = pika.URLParameters(RABBITMQ_URL)
        params.socket_timeout = 3
        params.connection_attempts = 1
        conn = pika.BlockingConnection(params)
        conn.close()
    except Exception:
        rabbitmq_ok = False

    market_ok = True
    try:
        with httpx.Client(timeout=3.0) as client:
            client.get(f"{MARKET_DATA_URL}/health")
    except Exception:
        market_ok = False

    cb_state = _openai_breaker.current_state
    overall = "ok" if rabbitmq_ok else "degraded"
    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={
            "status": overall,
            "service": SERVICE_NAME,
            "dependencies": {
                "rabbitmq": "ok" if rabbitmq_ok else "unreachable",
                "market_data_service": "ok" if market_ok else "unreachable",
                "openai_key": "set" if os.getenv("OPENAI_API_KEY") else "missing",
                "rapidapi_key": "set" if os.getenv("RAPIDAPI_KEY") else "missing",
                "circuit_breaker": cb_state,
            },
        },
    )


@app.post("/api/v1/pazarlik-kocu")
def pazarlik_kocu(istek: PazarlikIstegi):
    return pazarlik_argumanlari_yap(istek)


@app.post("/api/v1/arac-asistan")
async def arac_asistan(request: Request):
    body = await request.json()
    soru = body.get("soru", "").strip()
    gecmis = body.get("gecmis", [])
    arac_ctx = body.get("arac_kontekst")

    if not soru:
        raise HTTPException(status_code=400, detail="'soru' alanı boş olamaz.")

    sistem = (
        "Sen Turkiye arac piyasasi uzmani bir danismansin. "
        "Kullanicilara arac alim, satim, fiyat pazarligi ve piyasa trendleri konusunda "
        "pratik, kisa ve guvenilir Turkce tavsiyeler veriyorsun. "
        "Cevaplarin en fazla 3 paragraf olsun."
    )
    if arac_ctx:
        sistem += (
            f" Degerlenen arac: {arac_ctx.get('marka','')} {arac_ctx.get('model_yili','')},"
            f" {arac_ctx.get('km','')}km, deger: {arac_ctx.get('fiyat','')}TL,"
            f" sehir: {arac_ctx.get('il','')}."
        )

    mesajlar = [{"role": "system", "content": sistem}]
    for m in gecmis[-8:]:
        rol = "user" if m.get("rol") == "kullanici" else "assistant"
        mesajlar.append({"role": rol, "content": m.get("icerik", "")})
    mesajlar.append({"role": "user", "content": soru})

    with tracer.start_as_current_span("chatbot_request") as span:
        span.set_attribute("chatbot.question_len", len(soru))
        try:
            cevap = _call_chat(mesajlar)
            logger.info("Asistan yanıtı üretildi", extra={"soru_len": len(soru)})
            return {"cevap": cevap, "durum": "ok"}
        except pybreaker.CircuitBreakerError:
            span.set_attribute("circuit_breaker", "open")
            logger.warning("Chatbot circuit breaker açık")
            return {
                "cevap": "AI asistan şu an yoğunluk nedeniyle geçici olarak kullanılamıyor. Lütfen birkaç dakika sonra tekrar deneyin.",
                "durum": "circuit_open",
            }
        except Exception as e:
            span.record_exception(e)
            logger.warning("Asistan yanıt üretemedi", extra={"error": str(e)})
            return {
                "cevap": "AI asistana şu an ulaşılamıyor. Lütfen daha sonra tekrar deneyin.",
                "durum": "error",
            }


@app.post("/api/v1/degerleme")
def arac_degerle(arac: AracOzellikleri):
    with tracer.start_as_current_span("valuation_request") as span:
        span.set_attribute("vehicle.brand", arac.marka)
        span.set_attribute("vehicle.year", arac.model_yili)
        span.set_attribute("vehicle.km", arac.kilometre)
        span.set_attribute("vehicle.city", arac.il or "")

        logger.info(
            "Değerleme isteği alındı",
            extra={"marka": arac.marka, "model_yili": arac.model_yili, "il": arac.il},
        )

        detay = fiyat_hesapla_detay(arac)
        hesaplanan_fiyat = detay["fiyat"]
        faktorler = detay["faktorler"]

        valuation_price_histogram.observe(hesaplanan_fiyat)
        span.set_attribute("valuation.price", hesaplanan_fiyat)

        ai_analizi = ai_analiz_yap(arac, hesaplanan_fiyat)
        piyasa_raporu = _get_piyasa_raporu(arac, hesaplanan_fiyat)

        publish_event(
            f"Araç değerlemesi: {arac.marka} {arac.model_yili} "
            f"— {hesaplanan_fiyat:.0f} TL — {arac.il}"
        )

        return {
            "arac_bilgisi": arac,
            "hesaplanan_fiyat_tl": hesaplanan_fiyat,
            "faktorler": faktorler,
            "ai_analizi": ai_analizi,
            "piyasa_raporu": piyasa_raporu,
        }


# ── UI ─────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def ana_sayfa():
    return _UI_HTML


_UI_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Araç Değerleme</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --c-bg:#f1f5f9;--c-card:#fff;--c-border:#e2e8f0;
  --c-primary:#4f46e5;--c-primary-dark:#4338ca;--c-primary-light:#ede9fe;
  --c-green:#10b981;--c-red:#ef4444;--c-amber:#f59e0b;
  --c-text:#0f172a;--c-muted:#64748b;--c-faint:#f8fafc;
  --radius:14px;--shadow:0 1px 8px rgba(0,0,0,.08);
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--c-bg);color:var(--c-text);padding:28px 16px 60px}
.wrap{max-width:960px;margin:0 auto}
.page-title{font-size:26px;font-weight:800;letter-spacing:-.5px;margin-bottom:4px}
.page-sub{color:var(--c-muted);font-size:14px;margin-bottom:24px}
.card{background:var(--c-card);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px}
.card+.card{margin-top:14px}
.section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--c-muted);margin-bottom:12px}
/* Form */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
@media(max-width:640px){.grid2,.grid3{grid-template-columns:1fr}}
label{display:block;font-size:12px;font-weight:600;color:var(--c-muted);margin-bottom:5px;text-transform:uppercase;letter-spacing:.3px}
input,select{width:100%;padding:10px 13px;border:1.5px solid var(--c-border);border-radius:9px;font-size:15px;outline:none;transition:border-color .2s,box-shadow .2s;background:#fff}
input:focus,select:focus{border-color:var(--c-primary);box-shadow:0 0 0 3px rgba(79,70,229,.1)}
.check-row{display:flex;align-items:center;gap:10px;padding:12px 0}
.check-row input{width:auto;width:18px;height:18px;accent-color:var(--c-primary);cursor:pointer}
.check-row label{margin:0;font-size:15px;text-transform:none;letter-spacing:0;color:var(--c-text);font-weight:400;cursor:pointer}
.btn-primary{width:100%;margin-top:16px;padding:13px;background:var(--c-primary);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;transition:background .2s,transform .1s;letter-spacing:.2px}
.btn-primary:hover:not(:disabled){background:var(--c-primary-dark)}
.btn-primary:active:not(:disabled){transform:scale(.99)}
.btn-primary:disabled{background:#a5b4fc;cursor:not-allowed}
.btn-secondary{padding:10px 18px;background:var(--c-primary-light);color:var(--c-primary);border:none;border-radius:9px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s}
.btn-secondary:hover:not(:disabled){background:#ddd6fe}
.btn-secondary:disabled{opacity:.5;cursor:not-allowed}
/* Tabs */
.tabs{display:flex;gap:6px;margin-bottom:14px;border-bottom:2px solid var(--c-border);padding-bottom:0}
.tab-btn{padding:10px 18px;background:none;border:none;border-bottom:3px solid transparent;margin-bottom:-2px;font-size:14px;font-weight:600;color:var(--c-muted);cursor:pointer;transition:.2s;border-radius:8px 8px 0 0}
.tab-btn:hover{color:var(--c-primary);background:var(--c-primary-light)}
.tab-btn.active{color:var(--c-primary);border-bottom-color:var(--c-primary)}
.tab-panel{display:none;animation:fadeIn .25s ease}
.tab-panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
/* Price hero */
.price-hero{font-size:44px;font-weight:900;letter-spacing:-1px;color:var(--c-text);line-height:1}
.price-sub{font-size:15px;color:var(--c-muted);margin-top:4px}
/* Badges */
.badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;letter-spacing:.4px}
.badge-firsat{background:#d1fae5;color:#065f46}
.badge-rekabetci{background:#dbeafe;color:#1e40af}
.badge-piyasa{background:#f3f4f6;color:#374151}
.badge-pahali{background:#fef3c7;color:#92400e}
.badge-cokpahali{background:#fee2e2;color:#991b1b}
/* Market position bar */
.mktbar{position:relative;height:8px;background:#e2e8f0;border-radius:4px;margin:18px 0 6px}
.mktbar-fill{position:absolute;top:0;left:0;height:8px;background:linear-gradient(90deg,var(--c-green),var(--c-primary));border-radius:4px;transition:width 1s cubic-bezier(.4,0,.2,1)}
.mktbar-dot{position:absolute;top:50%;transform:translate(-50%,-50%);width:18px;height:18px;background:var(--c-primary);border:3px solid #fff;border-radius:50%;box-shadow:0 2px 8px rgba(79,70,229,.4);transition:left 1s cubic-bezier(.4,0,.2,1)}
.mktbar-labels{display:flex;justify-content:space-between;font-size:11px;color:var(--c-muted);margin-top:4px}
.mktbar-center{text-align:center;font-size:13px;font-weight:700;color:var(--c-primary);margin-top:2px}
/* Gauge */
.gauge-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%}
.gauge-num{font-size:38px;font-weight:900;color:var(--c-primary);line-height:1;margin-top:-16px}
.gauge-lbl{font-size:12px;color:var(--c-muted);margin-top:2px}
.gauge-detail{font-size:13px;color:var(--c-text);margin-top:8px;text-align:center}
.gauge-detail-sub{font-size:12px;color:var(--c-muted);margin-top:2px;text-align:center}
/* Price DNA */
.dna-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.dna-label{font-size:12px;color:var(--c-muted);width:160px;flex-shrink:0}
.dna-bar-track{flex:1;height:8px;background:#f1f5f9;border-radius:4px;overflow:hidden}
.dna-bar-fill{height:8px;border-radius:4px;width:0;transition:width 1s cubic-bezier(.4,0,.2,1)}
.dna-val{font-size:13px;font-weight:700;width:110px;text-align:right;flex-shrink:0}
.dna-divider{border:none;border-top:1px solid var(--c-border);margin:10px 0 8px}
.dna-total{display:flex;justify-content:space-between;font-size:14px;font-weight:700;padding:4px 0}
/* AI Insight cards */
.insight-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:640px){.insight-grid{grid-template-columns:1fr}}
.insight-card{background:var(--c-faint);border:1px solid var(--c-border);border-radius:12px;padding:16px}
.insight-icon{font-size:20px;margin-bottom:8px}
.insight-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--c-muted);margin-bottom:6px}
.insight-text{font-size:13px;line-height:1.6;color:var(--c-text)}
/* Piyasa */
.ai-text{font-size:14px;line-height:1.7;color:var(--c-muted);margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--c-muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:8px 0;border-bottom:1px solid var(--c-border)}
td{padding:9px 0;border-bottom:1px solid var(--c-faint);color:var(--c-text)}
tr:last-child td{border-bottom:none}
.il-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.il-name{font-size:13px;color:var(--c-muted);width:100px;flex-shrink:0}
.il-track{flex:1;height:6px;background:#f1f5f9;border-radius:3px;overflow:hidden}
.il-fill{height:6px;background:var(--c-primary);border-radius:3px;width:0;transition:width .8s ease}
.il-price{font-size:13px;font-weight:700;width:100px;text-align:right}
.dep-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--c-faint)}
.dep-row:last-child{border-bottom:none}
.dep-yr{font-size:13px;color:var(--c-muted)}
.dep-val{font-weight:700;font-size:14px}
.dep-loss{font-size:11px;color:var(--c-red);margin-left:6px}
/* Pazarlık Koçu */
.koc-input-row{display:flex;gap:10px;margin-bottom:16px}
.koc-input-row input{flex:1}
.koc-summary-bar{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.koc-stat{flex:1;min-width:120px;background:var(--c-faint);border:1px solid var(--c-border);border-radius:10px;padding:12px 14px;text-align:center}
.koc-stat strong{display:block;font-size:16px;font-weight:800;color:var(--c-text);margin-bottom:2px}
.koc-stat span{font-size:11px;color:var(--c-muted);text-transform:uppercase;letter-spacing:.3px}
.koc-stat.savings strong{color:var(--c-green)}
.koc-ozet{font-size:14px;color:var(--c-muted);line-height:1.6;margin-bottom:14px;font-style:italic}
.koc-arg{display:flex;align-items:flex-start;gap:12px;padding:14px;background:var(--c-faint);border:1px solid var(--c-border);border-radius:10px;margin-bottom:10px}
.koc-num{width:28px;height:28px;border-radius:50%;background:var(--c-primary);color:#fff;font-size:13px;font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px}
.koc-body{flex:1}
.koc-title{font-size:14px;font-weight:700;color:var(--c-text);margin-bottom:3px}
.koc-detail{font-size:13px;color:var(--c-muted);line-height:1.5}
.koc-save{font-size:13px;font-weight:700;color:var(--c-red);white-space:nowrap;padding-top:2px}
.loading-dots{display:flex;gap:6px;align-items:center;padding:16px;justify-content:center;color:var(--c-muted);font-size:14px}
.loading-dots span{width:7px;height:7px;border-radius:50%;background:var(--c-primary);animation:bounce 1.2s infinite}
.loading-dots span:nth-child(2){animation-delay:.2s}
.loading-dots span:nth-child(3){animation-delay:.4s}
/* Chat */
.chat-ctx{background:var(--c-primary-light);border:1px solid #c7d2fe;border-radius:10px;padding:12px 14px;font-size:13px;color:var(--c-primary);margin-bottom:14px;font-weight:500}
.chat-msgs{height:340px;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding:4px 0;margin-bottom:14px}
.chat-msg{max-width:80%;padding:11px 14px;border-radius:12px;font-size:14px;line-height:1.55;white-space:pre-wrap}
.chat-user{align-self:flex-end;background:var(--c-primary);color:#fff;border-bottom-right-radius:4px}
.chat-bot{align-self:flex-start;background:var(--c-faint);border:1px solid var(--c-border);color:var(--c-text);border-bottom-left-radius:4px}
.chat-input-row{display:flex;gap:8px}
.chat-input-row input{flex:1}
.chat-send{padding:10px 18px;background:var(--c-primary);color:#fff;border:none;border-radius:9px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;transition:.2s}
.chat-send:hover{background:var(--c-primary-dark)}
.chat-send:disabled{background:#a5b4fc;cursor:not-allowed}
.thinking{display:flex;gap:5px;align-items:center;padding:4px 0}
.thinking span{width:7px;height:7px;border-radius:50%;background:#94a3b8;animation:bounce 1.2s infinite}
.thinking span:nth-child(2){animation-delay:.2s}
.thinking span:nth-child(3){animation-delay:.4s}
/* Advice */
.advice-box{background:#f5f3ff;border-left:4px solid var(--c-primary);border-radius:0 10px 10px 0;padding:14px 16px}
.advice-row{display:flex;gap:10px;margin-bottom:10px;font-size:14px;color:var(--c-text)}
.advice-row:last-child{margin-bottom:0}
.warn-box{background:#fff7ed;border-left:4px solid var(--c-amber);border-radius:0 10px 10px 0;padding:14px 16px}
.warn-item{font-size:13px;color:#7c2d12;margin-bottom:6px;display:flex;gap:8px}
.warn-item:last-child{margin-bottom:0}
/* Utils */
.hidden{display:none!important}
.error-msg{background:#fff0f0;border-radius:10px;padding:12px 14px;color:var(--c-red);font-size:14px}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
.slide-in{animation:slideIn .4s ease both}
@keyframes slideIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<div class="wrap">
  <h1 class="page-title">Araç Değerleme</h1>
  <p class="page-sub">Piyasa analizi · Talep skoru · AI koçu · Pazarlık argümanları</p>

  <!-- ── Form ── -->
  <div class="card">
    <div class="grid2">
      <div>
        <label>Marka</label>
        <input type="text" id="marka" placeholder="Toyota, BMW, Ford…">
      </div>
      <div>
        <label>Şehir</label>
        <select id="il">
          <option value="ankara">Ankara</option>
          <option value="istanbul">İstanbul</option>
          <option value="izmir">İzmir</option>
          <option value="bursa">Bursa</option>
          <option value="antalya">Antalya</option>
          <option value="adana">Adana</option>
          <option value="konya">Konya</option>
          <option value="gaziantep">Gaziantep</option>
          <option value="kayseri">Kayseri</option>
          <option value="mersin">Mersin</option>
          <option value="eskişehir">Eskişehir</option>
          <option value="kocaeli">Kocaeli</option>
          <option value="trabzon">Trabzon</option>
          <option value="samsun">Samsun</option>
          <option value="denizli">Denizli</option>
        </select>
      </div>
    </div>
    <div class="grid2" style="margin-top:14px">
      <div>
        <label>Model Yılı</label>
        <input type="number" id="model_yili" placeholder="2020" min="1990" max="2026">
      </div>
      <div>
        <label>Kilometre</label>
        <input type="number" id="kilometre" placeholder="80000" min="0">
      </div>
    </div>
    <div class="check-row">
      <input type="checkbox" id="hasar_kaydi">
      <label for="hasar_kaydi">Hasar kaydı var</label>
    </div>
    <button class="btn-primary" id="main-btn" onclick="degerle()">Değerle</button>
    <div class="error-msg hidden" id="form-error"></div>
  </div>

  <!-- ── Results ── -->
  <div id="results" class="hidden" style="margin-top:20px">

    <!-- Tabs -->
    <div class="tabs">
      <button class="tab-btn active" id="t-ozet" onclick="switchTab('ozet')">Özet</button>
      <button class="tab-btn" id="t-piyasa" onclick="switchTab('piyasa')">Piyasa</button>
      <button class="tab-btn" id="t-koc" onclick="switchTab('koc')">AI Koç</button>
      <button class="tab-btn" id="t-asistan" onclick="switchTab('asistan')">Asistan</button>
    </div>

    <!-- ── Tab: Özet ── -->
    <div class="tab-panel active" id="p-ozet">
      <div class="grid2">
        <!-- Price + Market Bar -->
        <div class="card">
          <div class="section-title">Tahmini Piyasa Değeri</div>
          <div class="price-hero"><span id="price-counter">0</span> TL</div>
          <div class="price-sub" id="price-sub"></div>
          <div style="margin-top:10px" id="price-badge-wrap"></div>
          <div id="mktbar-wrap" class="hidden">
            <div class="mktbar"><div class="mktbar-fill" id="mktbar-fill"></div><div class="mktbar-dot" id="mktbar-dot"></div></div>
            <div class="mktbar-labels"><span id="mktbar-min">—</span><span>Piyasa Aralığı</span><span id="mktbar-max">—</span></div>
            <div class="mktbar-center" id="mktbar-center"></div>
          </div>
        </div>
        <!-- Demand Gauge -->
        <div class="card" id="gauge-card" style="display:none">
          <div class="section-title">Talep Skoru</div>
          <div class="gauge-wrap">
            <svg viewBox="0 0 200 120" width="180" height="108" style="overflow:visible">
              <defs>
                <linearGradient id="gGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                  <stop offset="0%" stop-color="#ef4444"/>
                  <stop offset="50%" stop-color="#f59e0b"/>
                  <stop offset="100%" stop-color="#10b981"/>
                </linearGradient>
              </defs>
              <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="#e2e8f0" stroke-width="16" stroke-linecap="round"/>
              <path id="gauge-arc" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="url(#gGrad)" stroke-width="16" stroke-linecap="round" stroke-dasharray="251" stroke-dashoffset="251"/>
            </svg>
            <div class="gauge-num" id="gauge-num">0</div>
            <div class="gauge-lbl">/ 10</div>
            <div class="gauge-detail" id="gauge-detail"></div>
            <div class="gauge-detail-sub" id="gauge-sub"></div>
          </div>
        </div>
      </div>
      <!-- Price DNA -->
      <div class="card slide-in">
        <div class="section-title">Fiyat Faktör Analizi</div>
        <div id="dna-bars"></div>
      </div>
    </div>

    <!-- ── Tab: Piyasa ── -->
    <div class="tab-panel" id="p-piyasa">
      <div class="card">
        <div class="section-title">AI Piyasa Yorumu</div>
        <p class="ai-text" id="ai-piyasa-yorumu"></p>
        <p style="font-size:14px;color:#555;line-height:1.6" id="ai-ozet"></p>
      </div>
      <div class="card hidden" id="card-ilanlar">
        <div class="section-title">Benzer İlanlar</div>
        <table>
          <thead><tr><th>Marka / Yıl</th><th>Kilometre</th><th>Şehir</th><th>Fiyat</th><th>İlan</th></tr></thead>
          <tbody id="ilanlar-body"></tbody>
        </table>
      </div>
      <div class="grid2">
        <div class="card hidden" id="card-iller">
          <div class="section-title">Şehre Göre Fiyat</div>
          <div id="il-list"></div>
        </div>
        <div class="card hidden" id="card-dep">
          <div class="section-title">Değer Tahmini</div>
          <div id="dep-list"></div>
        </div>
      </div>
      <div class="card hidden" id="card-tavsiye">
        <div class="section-title">Satıcı Tavsiyeleri</div>
        <div class="advice-box" id="advice-box"></div>
      </div>
      <div class="card hidden" id="card-uyari">
        <div class="section-title">Dikkat Edilmesi Gerekenler</div>
        <div class="warn-box" id="uyari-box"></div>
      </div>
    </div>

    <!-- ── Tab: AI Koç ── -->
    <div class="tab-panel" id="p-koc">
      <!-- AI Insights -->
      <div class="card" id="insight-card">
        <div class="section-title">AI İçgörüler</div>
        <div class="insight-grid" id="insight-grid">
          <div class="insight-card">
            <div class="insight-title">6 Aylık Öngörü</div>
            <div class="insight-text" id="ins-ongoru">—</div>
          </div>
          <div class="insight-card">
            <div class="insight-title">İdeal Alıcı Profili</div>
            <div class="insight-text" id="ins-alici">—</div>
          </div>
          <div class="insight-card">
            <div class="insight-title">Satış Taktiği</div>
            <div class="insight-text" id="ins-taktik">—</div>
          </div>
        </div>
      </div>
      <!-- Pazarlık Koçu -->
      <div class="card">
        <div class="section-title">Pazarlık Koçu</div>
        <p style="font-size:14px;color:var(--c-muted);margin-bottom:14px;line-height:1.6">
          Satıcının istediği fiyatı girin — AI size güçlü, somut pazarlık argümanları hazırlasın.
        </p>
        <div class="koc-input-row">
          <input type="number" id="satis-fiyati" placeholder="Satıcının istediği fiyat (TL)" min="0">
          <button class="btn-secondary" id="koc-btn" onclick="loadKoc()">Argümanları Oluştur</button>
        </div>
        <div id="koc-results"></div>
      </div>
    </div>

    <!-- ── Tab: Asistan ── -->
    <div class="tab-panel" id="p-asistan">
      <div class="card">
        <div class="section-title">AI Araç Danışmanı</div>
        <div class="chat-ctx" id="chat-ctx">Değerleme yapıldı. Bu araç veya Türkiye araba piyasası hakkında her şeyi sorabilirsiniz.</div>
        <div class="chat-msgs" id="chat-msgs"></div>
        <div class="chat-input-row">
          <input type="text" id="chat-input" placeholder="Soru sorun…" onkeydown="if(event.key==='Enter')sendChat()">
          <button class="chat-send" id="chat-send" onclick="sendChat()">Gönder</button>
        </div>
      </div>
    </div>

  </div><!-- /results -->
</div><!-- /wrap -->

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let currentResult = null;
let chatHistory = [];

// ── Format ────────────────────────────────────────────────────────────────────
const fmtTL = n => Number(Math.round(n)).toLocaleString('tr-TR') + ' TL';

// ── Counter animation ─────────────────────────────────────────────────────────
function animateCounter(el, target, duration = 900) {
  const start = performance.now();
  const from = 0;
  function tick(now) {
    const p = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Number(Math.round(from + (target - from) * eased)).toLocaleString('tr-TR');
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Gauge animation ───────────────────────────────────────────────────────────
function animateGauge(score) {
  const arcLen = 251;
  const arc = document.getElementById('gauge-arc');
  const numEl = document.getElementById('gauge-num');
  if (!arc) return;
  setTimeout(() => {
    arc.style.transition = 'stroke-dashoffset 1.2s cubic-bezier(.4,0,.2,1)';
    arc.setAttribute('stroke-dashoffset', arcLen * (1 - score / 10));
  }, 200);
  // Animate number
  const start = performance.now();
  function tick(now) {
    const p = Math.min((now - start) / 1200, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    numEl.textContent = (score * eased).toFixed(1);
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name) {
  ['ozet','piyasa','koc','asistan'].forEach(t => {
    document.getElementById('t-' + t).classList.toggle('active', t === name);
    document.getElementById('p-' + t).classList.toggle('active', t === name);
  });
}

// ── Main valuation ────────────────────────────────────────────────────────────
async function degerle() {
  const btn = document.getElementById('main-btn');
  const errEl = document.getElementById('form-error');
  errEl.classList.add('hidden');
  document.getElementById('results').classList.add('hidden');

  const marka = document.getElementById('marka').value.trim();
  const model_yili = parseInt(document.getElementById('model_yili').value);
  const kilometre = parseInt(document.getElementById('kilometre').value);
  const hasar_kaydi = document.getElementById('hasar_kaydi').checked;
  const il = document.getElementById('il').value;

  if (!marka || !model_yili || isNaN(kilometre)) {
    errEl.textContent = 'Lütfen tüm alanları doldurun.';
    errEl.classList.remove('hidden');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Hesaplanıyor…';

  try {
    const res = await fetch('/api/v1/degerleme', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({marka, model_yili, kilometre, hasar_kaydi, il})
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    currentResult = data;
    chatHistory = [];
    renderResults(data);
    document.getElementById('results').classList.remove('hidden');
    document.getElementById('results').scrollIntoView({behavior:'smooth', block:'start'});
    switchTab('ozet');
    // Init chat context
    const ctx = `${marka} ${model_yili} | ${Number(kilometre).toLocaleString('tr-TR')} km | ${fmtTL(data.hesaplanan_fiyat_tl)} | ${il}`;
    document.getElementById('chat-ctx').textContent = ctx;
    document.getElementById('chat-msgs').innerHTML = '';
    addBotMsg(`${marka} ${model_yili} modelinizin değerlemesi tamamlandı. Bu araç, fiyatlandırma stratejisi veya Türkiye araba piyasası hakkında aklınıza takılan her soruyu sorabilirsiniz.`);
  } catch(e) {
    errEl.textContent = 'Sunucuya ulaşılamadı. Lütfen tekrar deneyin.';
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Değerle';
  }
}

// ── Render results ────────────────────────────────────────────────────────────
function renderResults(d) {
  const fiyat = d.hesaplanan_fiyat_tl;
  const pr = d.piyasa_raporu;
  const ai = d.ai_analizi || {};

  // --- Özet tab ---
  animateCounter(document.getElementById('price-counter'), fiyat);
  document.getElementById('price-sub').textContent = d.arac_bilgisi?.marka + ' ' + d.arac_bilgisi?.model_yili;

  // Badge
  const etiket = pr?.istatistikler?.konum_etiketi || '';
  if (etiket) {
    const badgeMap = {'FIRSAT':'firsat','REKABETCİ':'rekabetci','PIYASA DEGERİ':'piyasa','PAHALI':'pahali','ÇOK PAHALI':'cokpahali'};
    document.getElementById('price-badge-wrap').innerHTML =
      `<span class="badge badge-${badgeMap[etiket]||'piyasa'}">${etiket}</span>`;
  }

  // Market position bar
  if (pr?.istatistikler?.en_dusuk && pr?.istatistikler?.en_yuksek) {
    const mn = pr.istatistikler.en_dusuk, mx = pr.istatistikler.en_yuksek;
    const pct = Math.min(94, Math.max(6, (fiyat - mn) / (mx - mn) * 100));
    document.getElementById('mktbar-wrap').classList.remove('hidden');
    document.getElementById('mktbar-min').textContent = Number(mn).toLocaleString('tr-TR');
    document.getElementById('mktbar-max').textContent = Number(mx).toLocaleString('tr-TR');
    document.getElementById('mktbar-center').textContent = fmtTL(fiyat);
    setTimeout(() => {
      document.getElementById('mktbar-fill').style.width = pct + '%';
      document.getElementById('mktbar-dot').style.left = pct + '%';
    }, 300);
  }

  // Gauge
  if (pr?.talep?.skor) {
    document.getElementById('gauge-card').style.display = '';
    animateGauge(parseFloat(pr.talep.skor));
    document.getElementById('gauge-detail').textContent = pr.talep.ortalama_satis_suresi || '';
    document.getElementById('gauge-sub').textContent = pr.talep.populerite || '';
  }

  // Price DNA
  renderDNA(d.faktorler);

  // --- Piyasa tab ---
  document.getElementById('ai-piyasa-yorumu').textContent = ai.piyasa_yorumu || '—';
  document.getElementById('ai-ozet').textContent = ai.ozet || '';

  if (pr?.benzer_ilanlar?.length) {
    document.getElementById('ilanlar-body').innerHTML = pr.benzer_ilanlar.map(i =>
      `<tr><td><strong>${i.marka}</strong> ${i.model_yili}</td>
           <td>${Number(i.kilometre).toLocaleString('tr-TR')} km</td>
           <td>${i.il}</td>
           <td><strong>${Number(i.fiyat_tl).toLocaleString('tr-TR')} TL</strong></td>
           <td style="font-size:12px;color:var(--c-muted)">${i.ilan_tarihi}</td></tr>`
    ).join('');
    document.getElementById('card-ilanlar').classList.remove('hidden');
  }

  if (pr?.il_karsilastirmasi) {
    const prices = Object.entries(pr.il_karsilastirmasi);
    const maxP = Math.max(...prices.map(([,v]) => v));
    document.getElementById('il-list').innerHTML = prices.sort(([,a],[,b]) => b-a).map(([il, p]) =>
      `<div class="il-row">
        <div class="il-name">${il}</div>
        <div class="il-track"><div class="il-fill" data-w="${(p/maxP*100).toFixed(1)}"></div></div>
        <div class="il-price">${Number(p).toLocaleString('tr-TR')} TL</div>
      </div>`
    ).join('');
    setTimeout(() => {
      document.querySelectorAll('.il-fill').forEach(el => {
        el.style.width = el.dataset.w + '%';
      });
    }, 100);
    document.getElementById('card-iller').classList.remove('hidden');
  }

  if (pr?.deger_tahmini) {
    const dt = pr.deger_tahmini;
    const bugun = dt.bugun || fiyat;
    const rows = [['Bugün', bugun],['1 Yıl', dt['1_yil']],['2 Yıl', dt['2_yil']],['3 Yıl', dt['3_yil']],['5 Yıl', dt['5_yil']]].filter(([,v])=>v);
    document.getElementById('dep-list').innerHTML = rows.map(([lbl, val], i) => {
      const loss = i > 0 ? `<span class="dep-loss">-${Number(bugun-val).toLocaleString('tr-TR')}</span>` : '';
      return `<div class="dep-row"><span class="dep-yr">${lbl}</span><span><span class="dep-val">${Number(val).toLocaleString('tr-TR')} TL</span>${loss}</span></div>`;
    }).join('') + `<div style="font-size:11px;color:var(--c-muted);margin-top:10px">${dt.yorum||''}</div>`;
    document.getElementById('card-dep').classList.remove('hidden');
  }

  if (pr?.musteri_tavsiyeleri) {
    const t = pr.musteri_tavsiyeleri;
    const items = [
      t.liste_fiyati_onerisi ? `Liste fiyatı önerisi: <strong>${Number(t.liste_fiyati_onerisi).toLocaleString('tr-TR')} TL</strong> — ${t.muzakere_marji||''}` : null,
      t.en_iyi_sehir || null,
      t.ilkbahar_tavsiyesi || null,
      t.alici_icin || null,
    ].filter(Boolean);
    document.getElementById('advice-box').innerHTML = items.map(x => `<div class="advice-row"><span>${x}</span></div>`).join('');
    document.getElementById('card-tavsiye').classList.remove('hidden');
  }

  if (pr?.dogrulama?.uyarilar?.length) {
    document.getElementById('uyari-box').innerHTML = pr.dogrulama.uyarilar.map(u =>
      `<div class="warn-item"><span>—</span><span>${u}</span></div>`).join('');
    document.getElementById('card-uyari').classList.remove('hidden');
  }

  // --- AI Koç tab ---
  document.getElementById('ins-ongoru').textContent = ai.ongoru || '—';
  document.getElementById('ins-alici').textContent = ai.alici_profili || '—';
  document.getElementById('ins-taktik').textContent = ai.satis_taktigi || '—';
  document.getElementById('koc-results').innerHTML = '';
  document.getElementById('satis-fiyati').value = '';
}

// ── Price DNA ─────────────────────────────────────────────────────────────────
function renderDNA(f) {
  if (!f) return;
  const items = [
    {label: 'Taban Fiyat', val: f.taban_fiyat},
    {label: 'Yaş Etkisi', val: f.yas_etkisi},
    {label: 'Kilometre Etkisi', val: f.kilometre_etkisi},
    {label: 'Hasar Etkisi', val: f.hasar_etkisi},
    {label: 'Piyasa Dalgalanması', val: f.piyasa_dalgalanmasi},
  ].filter(i => i.val !== 0);

  const maxAbs = Math.max(...items.map(i => Math.abs(i.val)));
  const net = items.reduce((s, i) => s + i.val, 0);

  const html = items.map(item => {
    const pct = (Math.abs(item.val) / maxAbs * 100).toFixed(1);
    const color = item.val >= 0 ? 'var(--c-green)' : 'var(--c-red)';
    const sign = item.val > 0 ? '+' : '';
    return `<div class="dna-row">
      <div class="dna-label">${item.label}</div>
      <div class="dna-bar-track"><div class="dna-bar-fill" data-w="${pct}" style="background:${color}"></div></div>
      <div class="dna-val" style="color:${color}">${sign}${Number(item.val).toLocaleString('tr-TR')}</div>
    </div>`;
  }).join('') + `
    <hr class="dna-divider">
    <div class="dna-total">
      <span style="color:var(--c-muted)">Hesaplanan Net Değer</span>
      <span style="color:var(--c-primary)">${Number(Math.max(net, 50000)).toLocaleString('tr-TR')} TL</span>
    </div>`;

  document.getElementById('dna-bars').innerHTML = html;
  setTimeout(() => {
    document.querySelectorAll('.dna-bar-fill').forEach(el => {
      el.style.transition = 'width .9s cubic-bezier(.4,0,.2,1)';
      el.style.width = el.dataset.w + '%';
    });
  }, 100);
}

// ── Pazarlık Koçu ─────────────────────────────────────────────────────────────
async function loadKoc() {
  if (!currentResult) return;
  const btn = document.getElementById('koc-btn');
  const inp = document.getElementById('satis-fiyati');
  const resultsEl = document.getElementById('koc-results');
  const satisFiyati = parseFloat(inp.value);

  if (!satisFiyati || satisFiyati <= 0) {
    resultsEl.innerHTML = '<div class="error-msg">Lütfen satıcının istediği fiyatı girin.</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Analiz ediliyor…';
  resultsEl.innerHTML = '<div class="loading-dots"><span></span><span></span><span></span><span style="margin-left:8px;color:var(--c-muted)">AI argümanlar hazırlıyor…</span></div>';

  try {
    const arac = currentResult.arac_bilgisi;
    const res = await fetch('/api/v1/pazarlik-kocu', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        marka: arac.marka, model_yili: arac.model_yili,
        kilometre: arac.kilometre, hasar_kaydi: arac.hasar_kaydi,
        il: arac.il || 'ankara', satis_fiyati: satisFiyati,
        hesaplanan_fiyat: currentResult.hesaplanan_fiyat_tl
      })
    });
    const data = await res.json();
    renderKoc(data, satisFiyati);
  } catch(e) {
    resultsEl.innerHTML = '<div class="error-msg">AI servisine ulasilamiyor. Lutfen tekrar deneyin.</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Argümanları Oluştur';
  }
}

function renderKoc(data, satisFiyati) {
  const args = data.argumanlar || data['argümanlar'] || [];
  const toplam = args.reduce((s, a) => s + (a.indirim_tl || 0), 0);
  const saved = satisFiyati - (data.hedef_fiyat || 0);

  const statsHtml = `
    <div class="koc-summary-bar">
      <div class="koc-stat"><strong>${Number(data.hedef_fiyat).toLocaleString('tr-TR')} TL</strong><span>Hedef Fiyat</span></div>
      <div class="koc-stat"><strong>${Number(data.alt_sinir).toLocaleString('tr-TR')} TL</strong><span>Alt Sınır</span></div>
      <div class="koc-stat savings"><strong>-${Number(toplam).toLocaleString('tr-TR')} TL</strong><span>Toplam İndirim Potansiyeli</span></div>
    </div>`;

  const ozetHtml = data.ozet ? `<p class="koc-ozet">${data.ozet}</p>` : '';

  const argsHtml = args.map((a, i) => `
    <div class="koc-arg slide-in" style="animation-delay:${i*0.08}s">
      <div class="koc-num">${i+1}</div>
      <div class="koc-body">
        <div class="koc-title">${a.baslik}</div>
        <div class="koc-detail">${a.detay}</div>
      </div>
      <div class="koc-save">-${Number(a.indirim_tl).toLocaleString('tr-TR')} TL</div>
    </div>`).join('');

  document.getElementById('koc-results').innerHTML = statsHtml + ozetHtml + argsHtml;
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function addBotMsg(text) {
  const el = document.createElement('div');
  el.className = 'chat-msg chat-bot';
  el.textContent = text;
  const chat = document.getElementById('chat-msgs');
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

function addUserMsg(text) {
  const el = document.createElement('div');
  el.className = 'chat-msg chat-user';
  el.textContent = text;
  const chat = document.getElementById('chat-msgs');
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function addThinking() {
  const el = document.createElement('div');
  el.className = 'chat-msg chat-bot';
  el.innerHTML = '<div class="thinking"><span></span><span></span><span></span></div>';
  const chat = document.getElementById('chat-msgs');
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('chat-send');
  const soru = input.value.trim();
  if (!soru) return;

  addUserMsg(soru);
  chatHistory.push({rol: 'kullanici', icerik: soru});
  input.value = '';
  sendBtn.disabled = true;

  const thinking = addThinking();

  let aracKontekst = null;
  if (currentResult) {
    const a = currentResult.arac_bilgisi;
    aracKontekst = {
      marka: a?.marka, model_yili: a?.model_yili,
      km: a?.kilometre, fiyat: currentResult.hesaplanan_fiyat_tl,
      il: a?.il
    };
  }

  try {
    const res = await fetch('/api/v1/arac-asistan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({soru, gecmis: chatHistory.slice(0,-1), arac_kontekst: aracKontekst})
    });
    const data = await res.json();
    thinking.remove();
    const cevap = data.cevap || 'Yanıt alınamadı.';
    addBotMsg(cevap);
    chatHistory.push({rol: 'asistan', icerik: cevap});
  } catch(e) {
    thinking.remove();
    addBotMsg('Asistana ulaşılamadı. Lütfen tekrar deneyin.');
  } finally {
    sendBtn.disabled = false;
  }
}
</script>
</body>
</html>"""
