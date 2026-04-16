import json
import logging
import os
import sys
from contextvars import ContextVar

import httpx
import pika
import pybreaker
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from openai import OpenAI
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
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
from services import fiyat_hesapla

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

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

_openai_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30, name="openai")


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


# ── OpenAI (circuit breaker) ───────────────────────────────────────────────────
@_openai_breaker
def _call_openai(prompt: str) -> dict:
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=150,
    )
    return json.loads(response.choices[0].message.content)


def ai_analiz_yap(arac: AracOzellikleri, fiyat: float) -> dict:
    prompt = (
        f"Araç: {arac.marka} {arac.model_yili}, {arac.kilometre}km, "
        f"hasar={'var' if arac.hasar_kaydi else 'yok'}, fiyat={fiyat:.0f}TL, şehir={arac.il}. "
        f'JSON ver: {{"piyasa_yorumu":"fiyat makul mu (1 cümle)","ozet":"güçlü/zayıf yön (1 cümle)"}}'
    )
    with tracer.start_as_current_span("openai_analysis") as span:
        span.set_attribute("vehicle.brand", arac.marka)
        try:
            result = _call_openai(prompt)
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
    return {"piyasa_yorumu": "AI analizi şu an kullanılamıyor.", "ozet": ""}


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
                "circuit_breaker": cb_state,
            },
        },
    )


@app.get("/", response_class=HTMLResponse)
def ana_sayfa():
    return """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Araç Değerleme</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;padding:32px 16px;color:#111}
    .wrap{max-width:900px;margin:0 auto}
    h1{font-size:24px;font-weight:700;margin-bottom:6px}
    .subtitle{color:#666;font-size:14px;margin-bottom:28px}
    .card{background:#fff;border-radius:14px;padding:24px;box-shadow:0 1px 6px rgba(0,0,0,.08);margin-bottom:16px}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    @media(max-width:600px){.grid2{grid-template-columns:1fr}}
    label{display:block;font-size:12px;color:#666;margin-bottom:5px;margin-top:14px;font-weight:500;text-transform:uppercase;letter-spacing:.4px}
    label:first-of-type{margin-top:0}
    input,select{width:100%;padding:10px 12px;border:1.5px solid #e0e0e0;border-radius:9px;font-size:15px;outline:none;transition:.2s}
    input:focus,select:focus{border-color:#4f46e5}
    .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .checkbox-row{display:flex;align-items:center;gap:10px;margin-top:14px}
    .checkbox-row input{width:auto}
    .checkbox-row label{margin:0;font-size:15px;text-transform:none;letter-spacing:0;color:#111}
    button{width:100%;margin-top:20px;padding:13px;background:#4f46e5;color:#fff;border:none;border-radius:9px;font-size:15px;font-weight:600;cursor:pointer;transition:.2s}
    button:disabled{background:#a5b4fc;cursor:not-allowed}
    button:hover:not(:disabled){background:#4338ca}
    .hidden{display:none}
    .fiyat-buyuk{font-size:36px;font-weight:800;color:#111;margin-bottom:4px}
    .fiyat-buyuk span{font-size:18px;font-weight:400;color:#888;margin-left:4px}
    .badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;letter-spacing:.5px}
    .badge-firsat{background:#d1fae5;color:#065f46}
    .badge-rekabetci{background:#dbeafe;color:#1e40af}
    .badge-piyasa{background:#f3f4f6;color:#374151}
    .badge-pahali{background:#fef3c7;color:#92400e}
    .badge-cokpahali{background:#fee2e2;color:#991b1b}
    .skor-ring{font-size:42px;font-weight:800;color:#4f46e5;line-height:1}
    .skor-label{font-size:12px;color:#888;margin-top:4px}
    .section-title{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#999;margin-bottom:10px;font-weight:600}
    .analiz-text{font-size:14px;line-height:1.7;color:#444}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th{text-align:left;color:#999;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:8px 0;border-bottom:1px solid #f0f0f0}
    td{padding:9px 0;border-bottom:1px solid #f8f8f8;color:#333}
    tr:last-child td{border-bottom:none}
    .il-list{display:flex;flex-direction:column;gap:8px}
    .il-row{display:flex;justify-content:space-between;font-size:14px}
    .il-name{color:#555}
    .il-price{font-weight:700;color:#111}
    .il-bar{height:4px;background:#e9e9e9;border-radius:2px;margin-top:3px}
    .il-bar-fill{height:4px;background:#4f46e5;border-radius:2px}
    .dep-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #f5f5f5}
    .dep-row:last-child{border-bottom:none}
    .dep-year{font-size:13px;color:#666}
    .dep-val{font-weight:700;font-size:14px}
    .dep-loss{font-size:11px;color:#ef4444;margin-left:6px}
    .advice-box{background:#f5f3ff;border-left:4px solid #4f46e5;border-radius:0 10px 10px 0;padding:14px 16px}
    .advice-row{display:flex;gap:10px;margin-bottom:10px;font-size:14px}
    .advice-row:last-child{margin-bottom:0}
    .advice-icon{font-size:16px;flex-shrink:0;margin-top:1px}
    .warning-box{background:#fff7ed;border-left:4px solid #f97316;border-radius:0 10px 10px 0;padding:14px 16px}
    .warning-item{font-size:13px;color:#7c2d12;margin-bottom:6px;display:flex;gap:8px}
    .warning-item:last-child{margin-bottom:0}
    .error-card{background:#fff0f0;border-radius:14px;padding:16px;color:#c00;font-size:14px;display:none}
    .stat-row{display:flex;gap:24px;flex-wrap:wrap;margin-top:12px}
    .stat{text-align:center}
    .stat-val{font-size:18px;font-weight:700;color:#111}
    .stat-lbl{font-size:11px;color:#999;margin-top:2px}
  </style>
</head>
<body>
<div class="wrap">
  <h1>Araç Değerleme</h1>
  <p class="subtitle">Piyasa analizi · Şehir bazlı fiyat · Talep skoru · Değer tahmini</p>

  <div class="card">
    <div class="row2">
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
          <option value="trabzon">Trabzon</option>
          <option value="samsun">Samsun</option>
          <option value="kocaeli">Kocaeli</option>
          <option value="denizli">Denizli</option>
        </select>
      </div>
    </div>
    <div class="row2">
      <div>
        <label>Model Yılı</label>
        <input type="number" id="model_yili" placeholder="2020" min="1990" max="2025">
      </div>
      <div>
        <label>Kilometre</label>
        <input type="number" id="kilometre" placeholder="80000" min="0">
      </div>
    </div>
    <div class="checkbox-row">
      <input type="checkbox" id="hasar_kaydi">
      <label for="hasar_kaydi">Hasar kaydı var</label>
    </div>
    <button id="btn" onclick="degerle()">Değerle</button>
    <div class="error-card" id="error"></div>
  </div>

  <div id="results" class="hidden">

    <!-- Fiyat + Piyasa Skoru -->
    <div class="grid2">
      <div class="card">
        <div class="section-title">Tahmini Değer</div>
        <div class="fiyat-buyuk" id="fiyat-text">— <span>TL</span></div>
        <div style="margin-top:8px" id="konum-badge"></div>
        <div class="stat-row" id="piyasa-stats" style="display:none">
          <div class="stat"><div class="stat-val" id="stat-ort">—</div><div class="stat-lbl">Piyasa Ort.</div></div>
          <div class="stat"><div class="stat-val" id="stat-min">—</div><div class="stat-lbl">En Düşük</div></div>
          <div class="stat"><div class="stat-val" id="stat-max">—</div><div class="stat-lbl">En Yüksek</div></div>
        </div>
      </div>
      <div class="card" style="text-align:center;display:flex;flex-direction:column;justify-content:center">
        <div class="section-title">Talep Skoru</div>
        <div class="skor-ring" id="talep-skor">—</div>
        <div class="skor-label">/10</div>
        <div style="margin-top:10px;font-size:13px;color:#555" id="talep-sure">—</div>
        <div style="margin-top:4px;font-size:12px;color:#888" id="talep-label">—</div>
      </div>
    </div>

    <!-- AI Analiz -->
    <div class="card">
      <div class="section-title">AI Piyasa Analizi</div>
      <p class="analiz-text" id="piyasa-yorumu">—</p>
      <p class="analiz-text" style="margin-top:8px;color:#666" id="ozet">—</p>
    </div>

    <!-- Benzer İlanlar -->
    <div class="card" id="card-ilanlar" style="display:none">
      <div class="section-title">Benzer İlanlar (Piyasa Verisi)</div>
      <table>
        <thead><tr><th>Marka / Yıl</th><th>Kilometre</th><th>Şehir</th><th>Fiyat</th><th>İlan</th></tr></thead>
        <tbody id="ilanlar-body"></tbody>
      </table>
    </div>

    <!-- Şehir Fiyatları + Değer Tahmini -->
    <div class="grid2">
      <div class="card" id="card-iller" style="display:none">
        <div class="section-title">Şehre Göre Fiyat</div>
        <div class="il-list" id="il-list"></div>
      </div>
      <div class="card" id="card-dep" style="display:none">
        <div class="section-title">Değer Tahmini</div>
        <div id="dep-list"></div>
      </div>
    </div>

    <!-- Tavsiyeler -->
    <div class="card" id="card-tavsiye" style="display:none">
      <div class="section-title">Satıcı Tavsiyeleri</div>
      <div class="advice-box" id="advice-box"></div>
    </div>

    <!-- Uyarılar -->
    <div class="card" id="card-uyari" style="display:none">
      <div class="section-title">⚠ Dikkat Edilmesi Gerekenler</div>
      <div class="warning-box" id="uyari-box"></div>
    </div>

  </div>
</div>

<script>
const fmt = n => Number(n).toLocaleString('tr-TR') + ' TL';

function badgeClass(etiket) {
  const m = {'FIRSAT':'firsat','REKABETCİ':'rekabetci','PIYASA DEGERİ':'piyasa','PAHALI':'pahali','ÇOK PAHALI':'cokpahali'};
  return 'badge badge-' + (m[etiket] || 'piyasa');
}

async function degerle() {
  const btn = document.getElementById('btn');
  const errEl = document.getElementById('error');
  errEl.style.display = 'none';
  document.getElementById('results').classList.add('hidden');

  const marka = document.getElementById('marka').value.trim();
  const model_yili = parseInt(document.getElementById('model_yili').value);
  const kilometre = parseInt(document.getElementById('kilometre').value);
  const hasar_kaydi = document.getElementById('hasar_kaydi').checked;
  const il = document.getElementById('il').value;

  if (!marka || !model_yili || isNaN(kilometre)) {
    errEl.textContent = 'Lütfen tüm alanları doldurun.';
    errEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Hesaplanıyor…';

  try {
    const res = await fetch('/api/v1/degerleme', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({marka, model_yili, kilometre, hasar_kaydi, il})
    });
    const d = await res.json();
    renderResults(d);
    document.getElementById('results').classList.remove('hidden');
  } catch(e) {
    errEl.textContent = 'Sunucuya ulaşılamadı, tekrar deneyin.';
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Değerle';
  }
}

function renderResults(d) {
  const fiyat = d.hesaplanan_fiyat_tl;
  document.getElementById('fiyat-text').innerHTML =
    Number(fiyat).toLocaleString('tr-TR') + ' <span>TL</span>';

  // AI
  document.getElementById('piyasa-yorumu').textContent = d.ai_analizi?.piyasa_yorumu || '—';
  document.getElementById('ozet').textContent = d.ai_analizi?.ozet || '—';

  const pr = d.piyasa_raporu;
  if (!pr) return;

  // Piyasa badge
  const etiket = pr.istatistikler?.konum_etiketi || '';
  const konum = pr.istatistikler?.fiyat_konumu || '';
  if (etiket) {
    document.getElementById('konum-badge').innerHTML =
      `<span class="${badgeClass(etiket)}">${etiket}</span> <span style="font-size:12px;color:#888;margin-left:6px">${konum}</span>`;
  }

  // Piyasa stats
  if (pr.istatistikler?.ortalama_fiyat) {
    document.getElementById('stat-ort').textContent = Number(pr.istatistikler.ortalama_fiyat).toLocaleString('tr-TR');
    document.getElementById('stat-min').textContent = Number(pr.istatistikler.en_dusuk).toLocaleString('tr-TR');
    document.getElementById('stat-max').textContent = Number(pr.istatistikler.en_yuksek).toLocaleString('tr-TR');
    document.getElementById('piyasa-stats').style.display = 'flex';
  }

  // Talep
  if (pr.talep) {
    document.getElementById('talep-skor').textContent = pr.talep.skor;
    document.getElementById('talep-sure').textContent = '⏱ ' + pr.talep.ortalama_satis_suresi;
    document.getElementById('talep-label').textContent = pr.talep.populerite;
  }

  // Benzer İlanlar
  if (pr.benzer_ilanlar?.length) {
    const tbody = document.getElementById('ilanlar-body');
    tbody.innerHTML = pr.benzer_ilanlar.map(i =>
      `<tr>
        <td><strong>${i.marka}</strong> ${i.model_yili}</td>
        <td>${Number(i.kilometre).toLocaleString('tr-TR')} km</td>
        <td>${i.il}</td>
        <td><strong>${Number(i.fiyat_tl).toLocaleString('tr-TR')} TL</strong></td>
        <td style="color:#999;font-size:12px">${i.ilan_tarihi}</td>
      </tr>`
    ).join('');
    document.getElementById('card-ilanlar').style.display = 'block';
  }

  // Şehir fiyatları
  if (pr.il_karsilastirmasi) {
    const prices = Object.entries(pr.il_karsilastirmasi);
    const maxP = Math.max(...prices.map(([,v]) => v));
    const html = prices.sort(([,a],[,b]) => b-a).map(([il, p]) =>
      `<div>
        <div class="il-row">
          <span class="il-name">${il}</span>
          <span class="il-price">${Number(p).toLocaleString('tr-TR')} TL</span>
        </div>
        <div class="il-bar"><div class="il-bar-fill" style="width:${(p/maxP*100).toFixed(1)}%"></div></div>
      </div>`
    ).join('');
    document.getElementById('il-list').innerHTML = html;
    document.getElementById('card-iller').style.display = 'block';
  }

  // Değer tahmini
  if (pr.deger_tahmini) {
    const dt = pr.deger_tahmini;
    const bugun = dt.bugun || fiyat;
    const rows = [
      ['Bugün', bugun, null],
      ['1 Yıl', dt['1_yil'], dt['1_yil']],
      ['2 Yıl', dt['2_yil'], dt['2_yil']],
      ['3 Yıl', dt['3_yil'], dt['3_yil']],
      ['5 Yıl', dt['5_yil'], dt['5_yil']],
    ].filter(([,v]) => v);
    const html = rows.map(([label, val], idx) => {
      const loss = idx > 0 ? ` <span class="dep-loss">-${Number(bugun-val).toLocaleString('tr-TR')}</span>` : '';
      return `<div class="dep-row">
        <span class="dep-year">${label}</span>
        <span><span class="dep-val">${Number(val).toLocaleString('tr-TR')} TL</span>${loss}</span>
      </div>`;
    }).join('');
    document.getElementById('dep-list').innerHTML = html
      + `<div style="font-size:11px;color:#999;margin-top:10px">${dt.yorum || ''}</div>`;
    document.getElementById('card-dep').style.display = 'block';
  }

  // Tavsiyeler
  if (pr.musteri_tavsiyeleri) {
    const t = pr.musteri_tavsiyeleri;
    const items = [
      ['💰', `Liste fiyatı önerisi: <strong>${Number(t.liste_fiyati_onerisi).toLocaleString('tr-TR')} TL</strong> — ${t.muzakere_marji}`],
      ['📍', t.en_iyi_sehir],
      ['📅', t.ilkbahar_tavsiyesi],
      ['🤝', t.alici_icin],
    ].filter(([,v]) => v);
    document.getElementById('advice-box').innerHTML = items.map(([icon, text]) =>
      `<div class="advice-row"><span class="advice-icon">${icon}</span><span>${text}</span></div>`
    ).join('');
    document.getElementById('card-tavsiye').style.display = 'block';
  }

  // Uyarılar
  if (pr.dogrulama?.uyarilar?.length) {
    const html = pr.dogrulama.uyarilar.map(u =>
      `<div class="warning-item"><span>⚠</span><span>${u}</span></div>`
    ).join('');
    document.getElementById('uyari-box').innerHTML = html;
    document.getElementById('card-uyari').style.display = 'block';
  }
}
</script>
</body>
</html>"""


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

        hesaplanan_fiyat = fiyat_hesapla(arac)
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
            "ai_analizi": ai_analizi,
            "piyasa_raporu": piyasa_raporu,
        }
