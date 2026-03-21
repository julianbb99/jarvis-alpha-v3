#!/usr/bin/env python3
"""
JARVIS ALPHA BOT V4 — Self-Learning Multi-Coin Trading Bot
──────────────────────────────────────────────────────────
NEU in V4:
  • Täglicher Performance-Report (08:00 UTC)
  • Drawdown-Schutz: Bot pausiert bei >15% Verlust
  • Cooldown: Kein Re-Entry in gleichen Coin innerhalb 2h
  • MACD-Konfirmation als zusätzlicher Indikator
  • 4H-Trend-Filter: Nur Trades in Richtung des 4H-Trends
  • Trailing Stop Logik (simuliert via Memory-Monitoring)
  • Duplicate-Trade-Guard: Kein doppelter Entry in gleiche Richtung
  • API-Retry mit Backoff bei Verbindungsfehlern
  • Startup-Validierung: Prüft API-Keys und Verbindung
  • Tägliches Memory-Cleanup (max 500 Trades gespeichert)
  • Verbessertes Logging mit Zeitstempel
  • STOCH RSI als Zusatzfilter
  • Mindest-RR jetzt konfigurierbar
  • Balance-Tracking: Equity-Kurve wird gespeichert
"""

import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── HEALTH CHECK SERVER ────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

class ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    for p in [port, 10001, 10002, 8080, 8000]:
        try:
            server = ReuseHTTPServer(("0.0.0.0", p), HealthHandler)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            return p
        except OSError:
            continue
    return None

import requests, json, time, hmac, hashlib, base64, logging

# Coin-spezifische Präzision (wird beim Start + alle 6h geladen)
_PRICE_PLACES      = {}   # symbol → int (Dezimalstellen Preis)
_QTY_PLACES        = {}   # symbol → int (Dezimalstellen Menge)
_PRECISION_LOADED  = 0.0  # Timestamp letztes Laden

def load_contract_precision(force: bool = False):
    """Lädt pricePlace + volumePlace für alle Coins. Wird alle 6h automatisch erneuert."""
    global _PRICE_PLACES, _QTY_PLACES, _PRECISION_LOADED
    if not force and time.time() - _PRECISION_LOADED < 21600:  # 6h Cache
        return
    try:
        ts   = str(int(time.time() * 1000))
        path = '/api/v2/mix/market/contracts?productType=USDT-FUTURES'
        sig  = sign(ts, 'GET', path, '')
        hdrs = {
            'ACCESS-KEY': BITGET_API_KEY, 'ACCESS-SIGN': sig,
            'ACCESS-TIMESTAMP': ts, 'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
            'Content-Type': 'application/json',
        }
        r = requests.get(BASE_URL + path, headers=hdrs, timeout=15)
        for c in r.json().get('data', []):
            sym = c.get('symbol','')
            _PRICE_PLACES[sym] = int(c.get('pricePlace', 4))
            _QTY_PLACES[sym]   = int(c.get('volumePlace', 2))
        _PRECISION_LOADED = time.time()
        log.info(f"✅ Precision geladen für {len(_PRICE_PLACES)} Kontrakte")
    except Exception as e:
        log.warning(f"⚠️ Precision-Laden fehlgeschlagen: {e}")

def get_precision_live(symbol: str):
    """Holt Precision für einen einzelnen Coin live — Fallback wenn Cache leer."""
    try:
        ts   = str(int(time.time() * 1000))
        path = f'/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol={symbol}'
        sig  = sign(ts, 'GET', path, '')
        hdrs = {
            'ACCESS-KEY': BITGET_API_KEY, 'ACCESS-SIGN': sig,
            'ACCESS-TIMESTAMP': ts, 'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
            'Content-Type': 'application/json',
        }
        r = requests.get(BASE_URL + path, headers=hdrs, timeout=8)
        c = r.json().get('data', [{}])[0]
        _PRICE_PLACES[symbol] = int(c.get('pricePlace', 4))
        _QTY_PLACES[symbol]   = int(c.get('volumePlace', 2))
        log.info(f"  📐 Precision {symbol}: price={_PRICE_PLACES[symbol]} qty={_QTY_PLACES[symbol]}")
    except Exception as e:
        log.warning(f"  ⚠️ Precision Fallback für {symbol}: {e}")
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── LOGGING SETUP ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('JARVIS')

# ── CONFIG ────────────────────────────────────────────────────────────────────
BITGET_API_KEY    = os.getenv('BITGET_API_KEY', '')
BITGET_SECRET     = os.getenv('BITGET_API_SECRET', '')
BITGET_PASSPHRASE = os.getenv('BITGET_PASSPHRASE', '')
TELEGRAM_TOKEN    = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID  = os.getenv('NOTIFY_CHAT_ID', '')

BASE_URL          = 'https://api.bitget.com'
LEVERAGE          = 20
RISK_PCT          = 0.10       # Risiko pro Trade (% des Kapitals)
MAX_OPEN          = 3          # Max gleichzeitige Positionen
SCAN_INTERVAL     = 120        # Sekunden zwischen Scans
STATUS_INTERVAL   = 10800      # Status-Update alle 3h (nur stilles Lebenszeichen)
TIMEFRAME         = '1H'
TIMEFRAME_TREND   = '4H'       # Übergeordneter Trend-Filter
MEMORY_FILE       = '/tmp/trade_memory.json'
PARAMS_FILE       = '/tmp/learned_params.json'
EQUITY_FILE       = '/tmp/equity_curve.json'

MIN_RR            = 1.1        # Mindest Risk/Reward Ratio
MAX_DRAWDOWN_PCT  = 15.0       # Bot pausiert bei >X% Drawdown
COOLDOWN_MINUTES  = 120        # Kein Re-Entry in selben Coin für X Minuten
MAX_MEMORY_TRADES = 500        # Maximale gespeicherte Trades
DAILY_REPORT_HOUR = 8          # Uhrzeit für täglichen Report (UTC)

BLACKLIST = ['PAXGUSDT', 'XAUTUSDT', 'LYNUSDT', 'XAUUSDT']

DASHBOARD_URL    = 'https://jarvis-24cde2d6.base44.app/functions/saveScanResults'
DASHBOARD_SECRET = os.getenv('BOT_WEBHOOK_SECRET', 'jarvis2026')

# ── GLOBALS (Runtime State) ───────────────────────────────────────────────────
_cooldown_map   = {}          # symbol → datetime der letzten Position
_start_balance  = None        # Balance beim Start (für Drawdown-Tracking)
_last_report_day = None       # Tag des letzten Daily Reports
_paused          = False      # Drawdown-Pausierung

# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ── API RETRY ─────────────────────────────────────────────────────────────────

def _request_with_retry(method, url, retries=3, **kwargs):
    """HTTP-Request mit exponentiellem Backoff bei Fehlern."""
    for attempt in range(retries):
        try:
            r = requests.request(method, url, timeout=10, **kwargs)
            return r
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            log.warning(f"Request Fehler ({attempt+1}/{retries}): {e} — warte {wait}s")
            if attempt < retries - 1:
                time.sleep(wait)
    return None

# ── TELEGRAM ──────────────────────────────────────────────────────────────────

def tg(msg: str, silent: bool = False):
    """Telegram-Nachricht senden. silent=True für nächtliche Meldungen."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TG] {msg[:120]}")
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={
                'chat_id':              TELEGRAM_CHAT_ID,
                'text':                 msg,
                'parse_mode':           'HTML',
                'disable_notification': silent,
            },
            timeout=5
        )
    except Exception as e:
        log.warning(f"Telegram Fehler: {e}")

# ── BITGET AUTH ───────────────────────────────────────────────────────────────

def sign(ts, method, path, body=''):
    msg = f'{ts}{method}{path}{body}'
    sig = hmac.new(BITGET_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()

def hdrs(method, path, body=''):
    ts = str(int(time.time() * 1000))
    return {
        'ACCESS-KEY':        BITGET_API_KEY,
        'ACCESS-SIGN':       sign(ts, method, path, body),
        'ACCESS-TIMESTAMP':  ts,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
        'Content-Type':      'application/json',
        'locale':            'en-US'
    }

def api_get(path):
    r = _request_with_retry('GET', BASE_URL + path, headers=hdrs('GET', path))
    if r is None:
        return {}
    try:
        return r.json()
    except Exception as e:
        log.error(f"GET JSON Fehler {path}: {e}")
        return {}

def api_post(path, body):
    bs = json.dumps(body)
    r  = _request_with_retry('POST', BASE_URL + path, headers=hdrs('POST', path, bs), data=bs)
    if r is None:
        return {}
    try:
        return r.json()
    except Exception as e:
        log.error(f"POST JSON Fehler {path}: {e}")
        return {}

# ── STARTUP VALIDATION ────────────────────────────────────────────────────────

def validate_startup() -> bool:
    """Prüft API-Keys und Verbindung beim Start."""
    log.info("🔍 Validiere API-Verbindung...")

    if not BITGET_API_KEY or not BITGET_SECRET or not BITGET_PASSPHRASE:
        log.error("❌ Bitget API-Keys fehlen! Bitte BITGET_API_KEY, BITGET_API_SECRET und BITGET_PASSPHRASE setzen.")
        return False

    # Teste öffentliche API
    r = _request_with_retry('GET', f'{BASE_URL}/api/v2/mix/market/tickers?productType=USDT-FUTURES')
    if r is None or r.status_code != 200:
        log.error("❌ Bitget API nicht erreichbar!")
        return False

    # Teste authentifizierte API
    data = api_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if not data or data.get('code') != '00000':
        code = data.get('code', '?') if data else 'keine Antwort'
        log.error(f"❌ Bitget Auth fehlgeschlagen! Code: {code}")
        log.error("   Prüfe API-Key, Secret und Passphrase.")
        return False

    log.info("✅ API-Verbindung OK")
    load_contract_precision()

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            r = requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe', timeout=5)
            if r.json().get('ok'):
                log.info("✅ Telegram OK")
            else:
                log.warning("⚠️ Telegram Token ungültig!")
        except:
            log.warning("⚠️ Telegram nicht erreichbar")
    else:
        log.warning("⚠️ Telegram nicht konfiguriert — keine Benachrichtigungen")

    return True

# ═══════════════════════════════════════════════════════════════════════════════
#  MEMORY & PARAMS
# ═══════════════════════════════════════════════════════════════════════════════

def load_memory():
    try:
        with open(MEMORY_FILE, 'r') as f:
            data = json.load(f)
            if 'trades' not in data:
                data['trades'] = []
            return data
    except:
        return {'trades': []}

def save_memory(mem):
    # Memory-Cleanup: maximal MAX_MEMORY_TRADES behalten
    if len(mem.get('trades', [])) > MAX_MEMORY_TRADES:
        # Offene Trades immer behalten, älteste geschlossene löschen
        open_t   = [t for t in mem['trades'] if t.get('status') == 'open']
        closed_t = [t for t in mem['trades'] if t.get('status') != 'open']
        closed_t = closed_t[-(MAX_MEMORY_TRADES - len(open_t)):]
        mem['trades'] = closed_t + open_t
        log.info(f"🧹 Memory bereinigt: {len(mem['trades'])} Trades behalten")
    try:
        with open(MEMORY_FILE, 'w') as f:
            json.dump(mem, f, indent=2)
    except Exception as e:
        log.error(f"Memory Save Error: {e}")

def load_params():
    defaults = {
        'min_score':     45,   # war 55 — aggressiver
        'rsi_long':      32,   # war 30 — häufiger triggern
        'rsi_short':     68,   # war 70
        'rsi_extreme_l': 30,   # war 28
        'rsi_extreme_s': 70,   # war 72
        'tp_atr_mult':   1.8,  # war 1.5 — besserer TP
        'sl_atr_mult':   1.5,  # war 2.0 — engerer SL, besseres RR
        'min_atr_pct':   0.2,  # war 0.3 — mehr Coins zugelassen
        'bb_tight':      0.010, # war 0.005 — 1% statt 0.5%
        'bb_near':       0.025, # war 0.015 — 2.5% statt 1.5%
        'min_rr':        MIN_RR,
        'regime_scores': {
            'trending_up':   {'score_bonus': 8,   'tp_mult': 2.0, 'sl_mult': 1.5},
            'trending_down': {'score_bonus': 8,   'tp_mult': 2.0, 'sl_mult': 1.5},
            'ranging':       {'score_bonus': 12,  'tp_mult': 1.5, 'sl_mult': 1.3},
            'volatile':      {'score_bonus': 0,   'tp_mult': 2.2, 'sl_mult': 2.0},
            'dead':          {'score_bonus': -8,  'tp_mult': 1.2, 'sl_mult': 1.2},
        },
        'version':      1,
        'total_trades': 0,
        'win_rate':     0.0,
        'last_update':  None,
    }
    try:
        with open(PARAMS_FILE, 'r') as f:
            saved = json.load(f)
            # Deep merge für regime_scores
            if 'regime_scores' in saved:
                for k, v in saved['regime_scores'].items():
                    if k in defaults['regime_scores']:
                        defaults['regime_scores'][k].update(v)
                del saved['regime_scores']
            defaults.update(saved)
            # Sicherheitsnetz: min_score darf niemals über 50 starten
            if defaults.get('min_score', 55) > 50:
                defaults['min_score'] = 45
    except:
        pass
    return defaults

def save_params(p):
    try:
        with open(PARAMS_FILE, 'w') as f:
            json.dump(p, f, indent=2)
    except Exception as e:
        log.error(f"Params Save Error: {e}")

# ── EQUITY TRACKING ───────────────────────────────────────────────────────────

def load_equity():
    try:
        with open(EQUITY_FILE, 'r') as f:
            return json.load(f)
    except:
        return {'history': [], 'peak': 0.0}

def save_equity(eq):
    try:
        with open(EQUITY_FILE, 'w') as f:
            json.dump(eq, f, indent=2)
    except:
        pass

def update_equity(balance: float):
    """Equity-Kurve aktualisieren und Drawdown berechnen."""
    global _paused
    eq = load_equity()

    entry = {'time': ts_str(), 'balance': round(balance, 2)}
    eq['history'].append(entry)
    # Nur letzte 1000 Einträge behalten
    eq['history'] = eq['history'][-1000:]

    if balance > eq.get('peak', 0):
        eq['peak'] = balance

    peak    = eq.get('peak', balance)
    dd_pct  = ((peak - balance) / peak * 100) if peak > 0 else 0
    eq['current_drawdown'] = round(dd_pct, 2)

    save_equity(eq)

    if dd_pct >= MAX_DRAWDOWN_PCT and not _paused:
        _paused = True
        log.warning(f"🚨 DRAWDOWN-SCHUTZ aktiviert! DD: {dd_pct:.1f}% (Limit: {MAX_DRAWDOWN_PCT}%)")
        tg(
            f"🚨 <b>DRAWDOWN-SCHUTZ AKTIV!</b>\n"
            f"Aktueller Drawdown: <b>{dd_pct:.1f}%</b>\n"
            f"Limit: {MAX_DRAWDOWN_PCT}%\n"
            f"💤 Bot pausiert neue Trades bis Erholung"
        )
    elif dd_pct < MAX_DRAWDOWN_PCT * 0.7 and _paused:
        _paused = False
        log.info("✅ Drawdown erholt — Bot nimmt Trades wieder auf")
        tg(f"✅ <b>Drawdown erholt</b> ({dd_pct:.1f}%) — Trading wieder aktiv")

    return dd_pct

# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def calc_ema(data, p):
    if not data or len(data) < p:
        return []
    e = data[0]
    k = 2 / (p + 1)
    r = [e]
    for v in data[1:]:
        e = v * k + e * (1 - k)
        r.append(e)
    return r

def calc_rsi(cl, p=14):
    if len(cl) < p + 2:
        return []
    diffs = [cl[i] - cl[i-1] for i in range(1, len(cl))]
    ag = sum(max(d, 0)  for d in diffs[:p]) / p
    al = sum(max(-d, 0) for d in diffs[:p]) / p
    vals = [None] * p
    for i in range(p, len(diffs)):
        g = max(diffs[i], 0)
        l = max(-diffs[i], 0)
        ag = (ag * (p - 1) + g) / p
        al = (al * (p - 1) + l) / p
        rs = ag / al if al else 100
        vals.append(100 - (100 / (1 + rs)))
    vals.append(None)
    return vals

def calc_stoch_rsi(cl, rsi_p=14, stoch_p=14, k_smooth=3, d_smooth=3):
    """Stochastic RSI — gibt (K, D) Wert zurück."""
    rsi_vals = [v for v in calc_rsi(cl, rsi_p) if v is not None]
    if len(rsi_vals) < stoch_p:
        return None, None
    stoch = []
    for i in range(stoch_p - 1, len(rsi_vals)):
        window = rsi_vals[i - stoch_p + 1:i + 1]
        lo, hi = min(window), max(window)
        stoch.append((rsi_vals[i] - lo) / (hi - lo) * 100 if hi != lo else 50)
    if len(stoch) < k_smooth:
        return None, None
    k_line = sum(stoch[-k_smooth:]) / k_smooth
    if len(stoch) < k_smooth + d_smooth - 1:
        return k_line, None
    d_vals = [sum(stoch[i:i+k_smooth])/k_smooth for i in range(len(stoch)-k_smooth+1)]
    d_line = sum(d_vals[-d_smooth:]) / d_smooth if len(d_vals) >= d_smooth else None
    return k_line, d_line

def calc_macd(cl, fast=12, slow=26, signal_p=9):
    """MACD Linie, Signal und Histogram."""
    if len(cl) < slow + signal_p:
        return None, None, None
    ema_fast = calc_ema(cl, fast)
    ema_slow = calc_ema(cl, slow)
    if not ema_fast or not ema_slow:
        return None, None, None
    min_len  = min(len(ema_fast), len(ema_slow))
    macd_raw = [ema_fast[i] - ema_slow[i] for i in range(min_len)]
    if len(macd_raw) < signal_p:
        return None, None, None
    sig_line = calc_ema(macd_raw, signal_p)
    if not sig_line:
        return None, None, None
    hist     = macd_raw[-1] - sig_line[-1]
    return macd_raw[-1], sig_line[-1], hist

def calc_atr(hi, lo, cl, p=14):
    tr = [hi[0] - lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1])))
    v = sum(tr[:p]) / p
    r = [None] * (p - 1)
    r.append(v)
    for i in range(p, len(tr)):
        v = (v * (p-1) + tr[i]) / p
        r.append(v)
    return r

def calc_bb(cl, p=20, k=2.0):
    mid = [None] * (p - 1)
    up  = [None] * (p - 1)
    dn  = [None] * (p - 1)
    for i in range(p - 1, len(cl)):
        w   = cl[i - p + 1:i + 1]
        m   = sum(w) / p
        std = (sum((x - m) ** 2 for x in w) / p) ** 0.5
        mid.append(m)
        up.append(m + k * std)
        dn.append(m - k * std)
    return mid, up, dn

# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA
# ═══════════════════════════════════════════════════════════════════════════════

def get_candles(symbol, gran=TIMEFRAME, limit=100):
    url = (f'/api/v2/mix/market/candles?symbol={symbol}'
           f'&productType=USDT-FUTURES&granularity={gran}&limit={limit}')
    try:
        r    = _request_with_retry('GET', BASE_URL + url)
        if r is None:
            return None
        data = sorted(r.json().get('data', []), key=lambda x: int(x[0]))
        if len(data) < 30:
            return None
        return {
            'ts': [int(c[0])    for c in data],
            'op': [float(c[1])  for c in data],
            'hi': [float(c[2])  for c in data],
            'lo': [float(c[3])  for c in data],
            'cl': [float(c[4])  for c in data],
            'vo': [float(c[5])  for c in data],
        }
    except Exception as e:
        log.debug(f"Candles Fehler {symbol}: {e}")
        return None

def get_4h_trend(symbol) -> str:
    """Gibt den übergeordneten 4H-Trend zurück: 'up', 'down' oder 'neutral'."""
    d = get_candles(symbol, gran=TIMEFRAME_TREND, limit=60)
    if not d or len(d['cl']) < 50:
        return 'neutral'
    cl    = d['cl']
    ema20 = calc_ema(cl, 20)
    ema50 = calc_ema(cl, 50)
    if not ema20 or not ema50:
        return 'neutral'
    if ema20[-1] > ema50[-1] and cl[-1] > ema20[-1]:
        return 'up'
    elif ema20[-1] < ema50[-1] and cl[-1] < ema20[-1]:
        return 'down'
    return 'neutral'

def get_liquid_coins():
    try:
        r     = _request_with_retry('GET', f'{BASE_URL}/api/v2/mix/market/tickers?productType=USDT-FUTURES')
        if r is None:
            return []
        coins = []
        for c in r.json().get('data', []):
            vol = float(c.get('usdtVolume', 0))
            sym = c.get('symbol', '')
            if vol >= 5e6 and sym not in BLACKLIST and sym.endswith('USDT'):
                coins.append((sym, vol))
        coins.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in coins[:30]]
    except Exception as e:
        log.error(f"get_liquid_coins Fehler: {e}")
        return []

# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET REGIME
# ═══════════════════════════════════════════════════════════════════════════════

def detect_market_regime(closes, hi, lo) -> str:
    if len(closes) < 50:
        return 'ranging'

    ema20 = calc_ema(closes[-50:], 20)
    ema50 = calc_ema(closes[-50:], 50)
    if not ema20 or not ema50:
        return 'ranging'

    e20   = ema20[-1]
    e50   = ema50[-1]
    price = closes[-1]

    trs = [
        max(hi[i] - lo[i], abs(hi[i] - closes[i-1]), abs(lo[i] - closes[i-1]))
        for i in range(max(1, len(closes) - 20), len(closes))
    ]
    atr       = sum(trs) / len(trs) if trs else 0
    atr_pct   = atr / price * 100
    trend_str = abs(e20 - e50) / e50 * 100

    if atr_pct > 4.0:
        return 'volatile'
    elif atr_pct < 0.2:
        return 'dead'
    elif trend_str > 1.5 and e20 > e50 and price > e20:
        return 'trending_up'
    elif trend_str > 1.5 and e20 < e50 and price < e20:
        return 'trending_down'
    else:
        return 'ranging'

# ═══════════════════════════════════════════════════════════════════════════════
#  SELF-LEARNING
# ═══════════════════════════════════════════════════════════════════════════════

def learn_from_trades(mem, params):
    trades = mem.get('trades', [])
    closed = [t for t in trades if t.get('status') in ['win', 'loss']]
    if len(closed) < 10:
        return params

    recent = closed[-20:]
    wins   = [t for t in recent if t['status'] == 'win']
    wr     = len(wins) / len(recent)

    log.info(f"🧠 [LEARNING] {len(recent)} Trades | WR: {wr * 100:.1f}%")

    # Regime-Analyse
    regime_stats = defaultdict(lambda: {'wins': 0, 'total': 0})
    for t in recent:
        reg = t.get('regime', 'unknown')
        regime_stats[reg]['total'] += 1
        if t['status'] == 'win':
            regime_stats[reg]['wins'] += 1

    for reg, s in regime_stats.items():
        r_wr = s['wins'] / s['total'] if s['total'] > 0 else 0
        log.info(f"   {reg:15}: WR {r_wr*100:.0f}% ({s['total']} Trades)")
        if reg in params['regime_scores'] and s['total'] >= 3:
            if r_wr > 0.65:
                params['regime_scores'][reg]['score_bonus'] = min(
                    20, params['regime_scores'][reg]['score_bonus'] + 2
                )
            elif r_wr < 0.40:
                params['regime_scores'][reg]['score_bonus'] = max(
                    -25, params['regime_scores'][reg]['score_bonus'] - 3
                )

    # Score-Anpassung basierend auf Win-Rate
    if wr > 0.65:
        params['min_score'] = max(45, params['min_score'] - 1)
    elif wr < 0.40:
        params['min_score'] = min(75, params['min_score'] + 2)

    # ATR-Multiplikator anpassen
    avg_pnl = sum(t.get('pnl_pct', 0) for t in recent) / len(recent)
    if avg_pnl < -1.0:
        params['sl_atr_mult'] = max(1.5, params.get('sl_atr_mult', 2.0) - 0.1)
    elif avg_pnl > 2.0:
        params['tp_atr_mult'] = min(3.0, params.get('tp_atr_mult', 1.5) + 0.1)

    params['win_rate']     = round(wr * 100, 1)
    params['total_trades'] = len(closed)
    params['last_update']  = datetime.now().strftime('%Y-%m-%d %H:%M')
    params['version']     += 1
    save_params(params)

    regime_str = ", ".join(
        f"{k}:{v['score_bonus']:+d}" for k, v in params['regime_scores'].items()
    )
    tg(
        f"🧠 <b>Bot hat gelernt!</b> (Brain v{params['version']})\n"
        f"📊 WR letzte 20 Trades: <b>{wr*100:.1f}%</b>\n"
        f"🎯 Neuer Min-Score: {params['min_score']}\n"
        f"📐 TP-Mult: {params['tp_atr_mult']:.1f}x | SL-Mult: {params['sl_atr_mult']:.1f}x\n"
        f"📋 Regime-Boni: {regime_str}"
    )
    return params

# ═══════════════════════════════════════════════════════════════════════════════
#  COIN ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_coin(symbol, params):
    d = get_candles(symbol, TIMEFRAME, 100)
    if not d:
        return None

    cl = d['cl']; hi = d['hi']; lo = d['lo']; vo = d['vo']
    n  = len(cl)
    if n < 50:
        return None

    # Indikatoren
    rsi_v        = calc_rsi(cl)
    atr_v        = calc_atr(hi, lo, cl)
    _, bbu, bbl  = calc_bb(cl)
    ema50_v      = calc_ema(cl, 50)
    macd, macd_sig, macd_hist = calc_macd(cl)
    stoch_k, stoch_d          = calc_stoch_rsi(cl)

    i  = n - 2
    ip = n - 3

    r     = rsi_v[i]    if rsi_v   and i  < len(rsi_v)  else None
    rp    = rsi_v[ip]   if rsi_v   and ip < len(rsi_v)  else None
    at    = atr_v[i]    if atr_v   and i  < len(atr_v)  else None
    bbu_v = bbu[i]      if i < len(bbu)  else None
    bbl_v = bbl[i]      if i < len(bbl)  else None
    e50   = ema50_v[-1] if ema50_v else None

    if None in [r, rp, at, bbu_v, bbl_v, e50]:
        return None

    price   = cl[i]
    atr_pct = at / price * 100
    regime  = detect_market_regime(cl, hi, lo)

    reg_cfg   = params['regime_scores'].get(regime, {'score_bonus': 0, 'tp_mult': 1.5, 'sl_mult': 2.0})
    tp_mult   = reg_cfg['tp_mult']
    sl_mult   = reg_cfg['sl_mult']
    reg_bonus = reg_cfg['score_bonus']

    score   = 0
    signal  = None
    reasons = []

    # ── RSI Signale ───────────────────────────────────────────────────────────
    if rp < params['rsi_long'] and r >= params['rsi_long']:
        score += 45; signal = 'LONG';  reasons.append(f'RSI Bounce {rp:.0f}→{r:.0f}')
    elif rp > params['rsi_short'] and r <= params['rsi_short']:
        score += 45; signal = 'SHORT'; reasons.append(f'RSI Drop {rp:.0f}→{r:.0f}')
    elif r < params['rsi_extreme_l']:
        score += 35; signal = 'LONG';  reasons.append(f'RSI oversold {r:.0f}')  # war 25
    elif r > params['rsi_extreme_s']:
        score += 35; signal = 'SHORT'; reasons.append(f'RSI overbought {r:.0f}')  # war 25
    elif r < params['rsi_long'] + 5 and rp < r:  # RSI steigt aus überverkaufter Zone
        score += 20; signal = 'LONG';  reasons.append(f'RSI erholt {r:.0f}')
    elif r > params['rsi_short'] - 5 and rp > r:  # RSI fällt aus überkaufter Zone
        score += 20; signal = 'SHORT'; reasons.append(f'RSI schwächt {r:.0f}')
    else:
        return None

    # ── 4H-Trend-Filter ───────────────────────────────────────────────────────
    trend_4h = get_4h_trend(symbol)
    if signal == 'LONG'  and trend_4h == 'down':
        score -= 10; reasons.append('⚠️ 4H-Trend gegen Trade')
    elif signal == 'SHORT' and trend_4h == 'up':
        score -= 10; reasons.append('⚠️ 4H-Trend gegen Trade')
    elif signal == 'LONG'  and trend_4h == 'up':
        score += 15; reasons.append('4H-Trend ✅')
    elif signal == 'SHORT' and trend_4h == 'down':
        score += 15; reasons.append('4H-Trend ✅')

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    if signal == 'LONG':
        dist = (price - bbl_v) / price
        if dist < params['bb_tight']:  score += 30; reasons.append('BB-Low Touch')
        elif dist < params['bb_near']: score += 15; reasons.append('BB-Low Nähe')
    else:
        dist = (bbu_v - price) / price
        if dist < params['bb_tight']:  score += 30; reasons.append('BB-High Touch')
        elif dist < params['bb_near']: score += 15; reasons.append('BB-High Nähe')

    # ── EMA50 ─────────────────────────────────────────────────────────────────
    if signal == 'LONG'  and price > e50: score += 10; reasons.append('über EMA50')
    if signal == 'SHORT' and price < e50: score += 10; reasons.append('unter EMA50')

    # ── MACD Konfirmation ─────────────────────────────────────────────────────
    if macd is not None and macd_sig is not None:
        if signal == 'LONG'  and macd > macd_sig and macd_hist and macd_hist > 0:
            score += 12; reasons.append('MACD bullish')
        elif signal == 'SHORT' and macd < macd_sig and macd_hist and macd_hist < 0:
            score += 12; reasons.append('MACD bearish')
        elif signal == 'LONG'  and macd < macd_sig:
            score -= 8;  reasons.append('MACD Gegenwind')
        elif signal == 'SHORT' and macd > macd_sig:
            score -= 8;  reasons.append('MACD Gegenwind')

    # ── Stochastic RSI ────────────────────────────────────────────────────────
    if stoch_k is not None:
        if signal == 'LONG'  and stoch_k < 20:
            score += 10; reasons.append(f'StochRSI OS {stoch_k:.0f}')
        elif signal == 'SHORT' and stoch_k > 80:
            score += 10; reasons.append(f'StochRSI OB {stoch_k:.0f}')

    # ── Volumen ───────────────────────────────────────────────────────────────
    if len(vo) >= 10:
        avg_vol = sum(vo[-10:-1]) / 9
        vol_ratio = vo[-1] / avg_vol if avg_vol > 0 else 1
        if vol_ratio > 1.3:   score += 15; reasons.append(f'Volu +{vol_ratio:.1f}x')
        elif vol_ratio > 1.1: score += 8;  reasons.append(f'Volu +{vol_ratio:.1f}x')
        elif vol_ratio < 0.4: score -= 8;  reasons.append('Volu schwach')

    # ── ATR / Volatilität ─────────────────────────────────────────────────────
    if atr_pct > 2.0:   score += 15; reasons.append(f'Vola {atr_pct:.1f}%')
    elif atr_pct > 1.0: score += 8;  reasons.append(f'Vola {atr_pct:.1f}%')
    elif atr_pct < params['min_atr_pct']:
        return None

    # ── Regime Bonus ──────────────────────────────────────────────────────────
    if reg_bonus != 0:
        score += reg_bonus
        reasons.append(f'Regime:{regime}({reg_bonus:+d})')

    # ── TP/SL Berechnung ──────────────────────────────────────────────────────
    if signal == 'LONG':
        tp = price + at * tp_mult
        sl = price - at * sl_mult
    else:
        tp = price - at * tp_mult
        sl = price + at * sl_mult

    rr = abs(tp - price) / abs(sl - price) if abs(sl - price) > 0 else 0

    return {
        'symbol':   symbol,
        'name':     symbol.replace('USDT', ''),
        'signal':   signal,
        'score':    score,
        'price':    price,
        'rsi':      r,
        'rsi_prev': rp,
        'atr':      at,
        'atr_pct':  atr_pct,
        'tp':       tp,
        'sl':       sl,
        'rr':       rr,
        'regime':   regime,
        'trend_4h': trend_4h,
        'macd_hist': macd_hist,
        'stoch_k':  stoch_k,
        'reasons':  ' + '.join(reasons),
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT & ORDERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_balance() -> float:
    data = api_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    try:
        for acc in data.get('data', []):
            if acc.get('marginCoin') == 'USDT':
                av = float(acc.get('available', 0))
                eq = float(acc.get('accountEquity', 0))
                return eq if eq > av else av
    except:
        pass
    return 0.0

def get_open_positions():
    data = api_get('/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT')
    try:
        return [p for p in data.get('data', []) if float(p.get('total', 0)) > 0]
    except:
        return []

def set_leverage(symbol, lev):
    for side in ['long', 'short']:
        api_post('/api/v2/mix/account/set-leverage', {
            'symbol':      symbol,
            'productType': 'USDT-FUTURES',
            'marginCoin':  'USDT',
            'leverage':    str(lev),
            'holdSide':    side,
        })

def round_price(price: float, symbol: str = '') -> str:
    """Rundet Preis exakt auf Bitget pricePlace Dezimalstellen."""
    if symbol and symbol not in _PRICE_PLACES:
        get_precision_live(symbol)  # Live nachladen
    decimals = _PRICE_PLACES.get(symbol, None)
    if decimals is None:
        # Letzter Fallback nach Preisgröße
        if price >= 1000:   decimals = 1
        elif price >= 100:  decimals = 2
        elif price >= 10:   decimals = 3
        elif price >= 1:    decimals = 4
        elif price >= 0.1:  decimals = 4
        elif price >= 0.01: decimals = 5
        else:               decimals = 6
    return f"{price:.{decimals}f}"

def round_qty(qty: float, symbol: str = '') -> str:
    """Rundet Menge exakt auf Bitget volumePlace Dezimalstellen."""
    if symbol and symbol not in _QTY_PLACES:
        get_precision_live(symbol)  # Live nachladen
    decimals = _QTY_PLACES.get(symbol, 2)
    return f"{qty:.{decimals}f}"

def place_order(symbol, side, size_usdt, tp, sl, price):
    qty = size_usdt * LEVERAGE / price
    if qty <= 0:
        return None
    set_leverage(symbol, LEVERAGE)
    order_side = 'buy' if side == 'LONG' else 'sell'
    body = {
        'symbol':                 symbol,
        'productType':            'USDT-FUTURES',
        'marginMode':             'isolated',
        'marginCoin':             'USDT',
        'size':                   round_qty(qty, symbol),
        'side':                   order_side,
        'tradeSide':              'open',
        'orderType':              'market',
        'presetStopSurplusPrice': round_price(tp, symbol),
        'presetStopLossPrice':    round_price(sl, symbol),
    }
    return api_post('/api/v2/mix/order/place-order', body)

def partial_close(symbol: str, side: str, close_qty: float) -> bool:
    """Schließt einen Teil der Position (Flash Close)."""
    close_side = 'sell' if side == 'LONG' else 'buy'
    body = {
        'symbol':      symbol,
        'productType': 'USDT-FUTURES',
        'marginCoin':  'USDT',
        'size':        round_qty(close_qty, symbol),
        'side':        close_side,
        'tradeSide':   'close',
        'orderType':   'market',
    }
    resp = api_post('/api/v2/mix/order/place-order', body)
    if resp and resp.get('code') == '00000':
        log.info(f"  ✂️ Partial Close {symbol}: {close_qty:.2f} Kontrakte")
        return True
    log.warning(f"  ⚠️ Partial Close fehlgeschlagen: {resp}")
    return False

def set_tp2(symbol: str, side: str, tp2_price: float) -> bool:
    """Setzt einen neuen TP (Stop-Surplus) für die verbleibende Position."""
    hold_side = 'long' if side == 'LONG' else 'short'
    body = {
        'symbol':         symbol,
        'productType':    'USDT-FUTURES',
        'marginCoin':     'USDT',
        'planType':       'profit_loss',
        'stopSurplusPrice': round_price(tp2_price, symbol),
        'stopSurplusTriggerType': 'mark_price',
        'holdSide':       hold_side,
    }
    resp = api_post('/api/v2/mix/order/place-tpsl-order', body)
    if resp and resp.get('code') == '00000':
        log.info(f"  🎯 TP2 gesetzt: {symbol} @ {round_price(tp2_price, symbol)}")
        return True
    log.warning(f"  ⚠️ TP2 setzen fehlgeschlagen: {resp}")
    return False

# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def check_partial_tp(positions: list, mem: dict):
    """
    Partial TP System:
    - TP1 bei definiertem Ziel: 60-75% der Position schließen
    - TP2 für Rest: weiterer ATR-basierter Zielpreis
    - Trend-Check: Starker Trend → 60% schließen, schwacher → 75%
    """
    for pos in positions:
        symbol    = pos.get('symbol', '')
        hold_side = pos.get('holdSide', '')  # 'long' oder 'short'
        side      = 'LONG' if hold_side == 'long' else 'SHORT'
        total_qty = float(pos.get('total', 0))
        entry     = float(pos.get('openPriceAvg', 0))
        mark      = float(pos.get('markPrice', 0))
        unreal_pl = float(pos.get('unrealizedPL', 0))
        margin    = float(pos.get('marginSize', 1))

        if total_qty <= 0 or entry <= 0:
            continue

        # Trade im Memory suchen
        open_trades = [t for t in mem.get('trades', []) if t.get('status') == 'open' and t.get('symbol') == symbol]
        if not open_trades:
            continue
        t = open_trades[0]

        # Bereits partial TP ausgeführt? → überspringen
        if t.get('partial_done'):
            continue

        tp1   = float(t.get('tp', 0))
        atr   = float(t.get('atr', 0))
        if tp1 <= 0 or atr <= 0:
            continue

        # TP1 erreicht prüfen
        tp1_hit = (side == 'LONG'  and mark >= tp1) or                   (side == 'SHORT' and mark <= tp1)

        if not tp1_hit:
            continue

        # Trend noch intakt? → bestimmt wie viel wir schließen
        trend_4h = get_4h_trend(symbol)
        if side == 'LONG'  and trend_4h == 'up':
            close_pct = 0.60   # 60% schließen, 40% läuft weiter
        elif side == 'SHORT' and trend_4h == 'down':
            close_pct = 0.60
        else:
            close_pct = 0.75   # Trend neutral/gegen → 75% sichern

        close_qty = total_qty * close_pct
        keep_qty  = total_qty - close_qty

        if close_qty <= 0:
            continue

        log.info(f"🎯 TP1 erreicht: {symbol} {side} | Mark: ${mark:.4f} | Schließe {close_pct*100:.0f}% ({close_qty:.2f})")

        # Partial Close ausführen
        if partial_close(symbol, side, close_qty):
            t['partial_done'] = True
            t['partial_price'] = mark
            t['partial_qty']   = close_qty

            # TP2 setzen für verbleibende Position
            if side == 'LONG':
                tp2 = mark + atr * 1.5
            else:
                tp2 = mark - atr * 1.5

            set_tp2(symbol, side, tp2)
            t['tp2'] = tp2

            # Telegram Meldung
            profit_pct = (mark - entry) / entry * 100 if side == 'LONG' else (entry - mark) / entry * 100
            tg(
                f"✂️ <b>PARTIAL TP — {symbol.replace('USDT','')} {side}</b>\n"
                f"💰 TP1 @ ${round_price(mark, symbol)} ({profit_pct:+.1f}%)\n"
                f"📦 {close_pct*100:.0f}% geschlossen ({close_qty:.2f} Kontrakte)\n"
                f"🚀 Rest läuft weiter → TP2 @ ${round_price(tp2, symbol)}\n"
                f"📊 Trend 4H: {trend_4h}"
            )

    return mem

def is_in_cooldown(symbol: str) -> bool:
    """Prüft ob ein Coin gerade in der Cooldown-Phase ist."""
    if symbol not in _cooldown_map:
        return False
    elapsed = (datetime.now() - _cooldown_map[symbol]).total_seconds() / 60
    return elapsed < COOLDOWN_MINUTES

def is_duplicate_trade(symbol: str, signal: str, mem: dict) -> bool:
    """Verhindert doppelten Entry in gleiche Richtung."""
    open_trades = [t for t in mem.get('trades', []) if t.get('status') == 'open']
    for t in open_trades:
        if t.get('symbol') == symbol and t.get('signal') == signal:
            return True
    return False

def check_closed_trades(mem, params, open_symbols_prev, open_symbols_now):
    closed_syms = open_symbols_prev - open_symbols_now
    if not closed_syms:
        return params

    trades  = mem.get('trades', [])
    changed = False

    for sym in closed_syms:
        _cooldown_map[sym] = datetime.now()
        for t in reversed(trades):
            if t.get('symbol') == sym and t.get('status') == 'open':
                try:
                    r = _request_with_retry(
                        'GET',
                        f'{BASE_URL}/api/v2/mix/market/ticker?symbol={sym}&productType=USDT-FUTURES'
                    )
                    current_price = float(r.json()['data'][0]['lastPr'])
                    entry  = t['entry_price']
                    signal = t['signal']
                    pnl_pct = (
                        (current_price - entry) / entry * 100
                        if signal == 'LONG'
                        else (entry - current_price) / entry * 100
                    )
                    t['status']     = 'win' if pnl_pct > 0 else 'loss'
                    t['exit_price'] = current_price
                    t['pnl_pct']    = round(pnl_pct, 2)
                    t['closed_at']  = datetime.now().strftime('%Y-%m-%d %H:%M')

                    lev_pnl = round(pnl_pct * LEVERAGE, 1)
                    icon    = '✅ WIN' if t['status'] == 'win' else '❌ LOSS'
                    tg(
                        f"{icon} <b>{sym.replace('USDT','')}</b> {signal}\n"
                        f"Entry: ${entry:.4f} → Exit: ${current_price:.4f}\n"
                        f"PnL: {pnl_pct:+.2f}% ({lev_pnl:+.1f}% mit {LEVERAGE}x)\n"
                        f"Regime: {t.get('regime','?')} | Score: {t.get('score','?')}"
                    )
                    changed = True
                except Exception as e:
                    log.error(f"Close Check Error {sym}: {e}")
                break

    if changed:
        save_memory(mem)
        params = learn_from_trades(mem, params)

    return params

# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def push_scan_to_dashboard(results, scan_time):
    if not results:
        return
    try:
        payload = {
            'scan_time': scan_time,
            'results': [{
                'coin':      r.get('name', '').replace('USDT', ''),
                'score':     r.get('score', 0),
                'signal':    r.get('signal', 'NONE'),
                'rsi':       round(float(r.get('rsi') or 0), 1),
                'regime':    r.get('regime', 'unknown'),
                'price':     r.get('price', 0),
                'bb_dist':   round(float(r.get('bb_dist') or 0), 4),
                'volume_ok': bool(r.get('vol_ok', False)),
                'traded':    False,
                'trend_4h':  r.get('trend_4h', 'neutral'),
                'scan_time': scan_time,
            } for r in results[:30]]
        }
        resp = requests.post(
            DASHBOARD_URL,
            headers={'Content-Type': 'application/json', 'x-bot-secret': DASHBOARD_SECRET},
            json=payload, timeout=8
        )
        if resp.status_code == 200:
            log.info(f"📡 {len(results)} Coins → Dashboard ✅")
        else:
            log.warning(f"[Dashboard] Fehler: {resp.status_code}")
    except Exception as e:
        log.debug(f"[Dashboard] Fehler: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def send_daily_report(mem, params, balance):
    """Täglicher Performance-Report via Telegram."""
    trades  = mem.get('trades', [])
    today   = datetime.utcnow().strftime('%Y-%m-%d')
    eq      = load_equity()
    dd_pct  = eq.get('current_drawdown', 0)

    # Trades von heute
    today_trades = [
        t for t in trades
        if t.get('closed_at', '').startswith(today) and t.get('status') in ['win', 'loss']
    ]
    today_wins   = len([t for t in today_trades if t['status'] == 'win'])
    today_losses = len([t for t in today_trades if t['status'] == 'loss'])
    today_pnl    = sum(t.get('pnl_pct', 0) for t in today_trades)

    # Gesamt-Statistik
    all_closed = [t for t in trades if t.get('status') in ['win', 'loss']]
    all_wins   = len([t for t in all_closed if t['status'] == 'win'])
    all_wr     = (all_wins / len(all_closed) * 100) if all_closed else 0

    # Offene Positionen
    open_trades = [t for t in trades if t.get('status') == 'open']

    tg(
        f"📅 <b>TAGES-REPORT — {today}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: <b>${balance:.2f}</b>\n"
        f"📉 Drawdown: {dd_pct:.1f}% (Max {MAX_DRAWDOWN_PCT}%)\n"
        f"\n<b>Heute:</b>\n"
        f"✅ Wins: {today_wins}  ❌ Losses: {today_losses}\n"
        f"💸 Tages-PnL: {today_pnl:+.2f}%\n"
        f"\n<b>Gesamt:</b>\n"
        f"📊 Win-Rate: {all_wr:.1f}% ({len(all_closed)} Trades)\n"
        f"🔓 Offen: {len(open_trades)} Position(en)\n"
        f"🧠 Brain v{params['version']} | Min-Score: {params['min_score']}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    global _start_balance, _last_report_day
    # Alte params.json löschen damit neue Defaults greifen
    import os as _os
    if _os.path.exists(PARAMS_FILE):
        try:
            import json as _j
            old = _j.load(open(PARAMS_FILE))
            if old.get('min_score', 0) > 50:
                _os.remove(PARAMS_FILE)
                log.info("🔄 Alte params.json zurückgesetzt (min_score war zu hoch)")
        except:
            pass

    print(f"\n{'=' * 62}")
    print(f"  🤖 JARVIS ALPHA BOT V4 — Self-Learning Trading Bot")
    print(f"  {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"  Leverage: {LEVERAGE}x | Risk: {RISK_PCT*100:.0f}%/Trade | MaxPos: {MAX_OPEN}")
    print(f"  Drawdown-Limit: {MAX_DRAWDOWN_PCT}% | Cooldown: {COOLDOWN_MINUTES}min")
    print(f"{'=' * 62}\n")

    # Health-Check Server starten (Render braucht HTTP für Stabilität)
    hc_port = start_health_server()
    log.info(f"🌐 Health-Check Server läuft auf Port {hc_port}")

    # Startup-Validierung — mit Retry damit Render-Neustart klappt
    startup_ok = False
    for startup_attempt in range(5):
        if validate_startup():
            startup_ok = True
            break
        wait_sec = 30 * (startup_attempt + 1)
        log.warning(f"⚠️ Startup fehlgeschlagen (Versuch {startup_attempt+1}/5) — warte {wait_sec}s...")
        time.sleep(wait_sec)
    if not startup_ok:
        log.error("❌ Startup nach 5 Versuchen fehlgeschlagen.")
        tg("❌ <b>JARVIS konnte nicht starten</b> — API nicht erreichbar nach 5 Versuchen")
        return

    mem    = load_memory()
    params = load_params()

    closed_all = [t for t in mem['trades'] if t.get('status') in ['win', 'loss']]
    wins_all   = len([t for t in closed_all if t['status'] == 'win'])
    wr_all     = (wins_all / len(closed_all) * 100) if closed_all else 0

    _start_balance = get_balance()
    if _start_balance > 0:
        eq = load_equity()
        if eq.get('peak', 0) == 0:
            eq['peak'] = _start_balance
            save_equity(eq)

    tg(
        f"🤖 <b>JARVIS ALPHA V4 gestartet</b>\n"
        f"🧠 Brain v{params['version']} | Trades gesamt: {len(closed_all)}\n"
        f"📊 Win-Rate: {wr_all:.1f}% | Min-Score: {params['min_score']}\n"
        f"⚙️ {LEVERAGE}x Leverage | {RISK_PCT*100:.0f}% Risiko/Trade\n"
        f"🛡️ Drawdown-Limit: {MAX_DRAWDOWN_PCT}% | Cooldown: {COOLDOWN_MINUTES}min\n"
        f"💰 Start-Balance: ${_start_balance:.2f}\n"
        f"🔍 Scanning alle {SCAN_INTERVAL // 60} Min..."
    )

    open_symbols_prev       = set()
    scan_count              = 0
    last_status_tg          = 0   # Timestamp letzter Status-TG
    last_precision_refresh  = time.time()

    while True:
        try:
            scan_count += 1
            # Precision alle 6h automatisch erneuern
            if time.time() - last_precision_refresh > 21600:
                load_contract_precision(force=True)
                last_precision_refresh = time.time()
            now_str = datetime.now().strftime('%H:%M:%S')
            log.info(f"─── Scan #{scan_count} ───")

            # ── Balance & Equity ──────────────────────────────────────────────
            balance    = get_balance()
            trade_size = round(balance * RISK_PCT, 2) if balance > 0 else 0
            if balance > 0:
                dd = update_equity(balance)
                log.info(f"💰 Balance: ${balance:.2f} | TradeSize: ${trade_size:.2f} | DD: {dd:.1f}%")
            else:
                log.warning("⚠️ Kein Futures-Guthaben — nur Scan, keine Trades")

            # ── Daily Report Check ────────────────────────────────────────────
            today = datetime.utcnow().strftime('%Y-%m-%d')
            hour  = datetime.utcnow().hour
            if today != _last_report_day and hour >= DAILY_REPORT_HOUR:
                send_daily_report(mem, params, balance)
                _last_report_day = today

            # ── Offene Positionen ─────────────────────────────────────────────
            positions    = get_open_positions()
            open_symbols = {p['symbol'] for p in positions}
            open_count   = len(positions)
            log.info(f"📂 Positionen: {open_count}/{MAX_OPEN}")

            params            = check_closed_trades(mem, params, open_symbols_prev, open_symbols)
            open_symbols_prev = open_symbols.copy()

            # ── Partial TP Check ─────────────────────────────────────────
            if positions:
                mem = check_partial_tp(positions, mem)
                save_memory(mem)

            # ── Drawdown-Pause ────────────────────────────────────────────────
            if _paused:
                log.warning(f"🚨 Drawdown-Pause aktiv — überspringe Scan")
                time.sleep(SCAN_INTERVAL)
                continue

            if open_count >= MAX_OPEN:
                log.info("⏸️ Max Positionen — überspringe Scan")
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Coin-Scan ─────────────────────────────────────────────────────
            coins = get_liquid_coins()
            log.info(f"🔍 Scanne {len(coins)} Coins...")

            signals          = []
            all_scan_results = []
            scan_time        = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')

            for sym in coins:
                if sym in open_symbols:
                    continue
                if is_in_cooldown(sym):
                    log.debug(f"  ⏳ {sym} in Cooldown")
                    continue
                try:
                    result = analyze_coin(sym, params)
                except Exception as e:
                    log.warning(f"  ⚠️ analyze_coin Fehler für {sym}: {e}")
                    result = None
                if result:
                    all_scan_results.append(result)
                    if result['score'] >= params['min_score']:
                        signals.append(result)

                        log.info(
                            f"  ✅ {result['name']:10} {result['signal']:5} "
                            f"Score:{result['score']:3} RSI:{result['rsi']:.0f} "
                            f"RR:{result['rr']:.2f} Regime:{result['regime']} "
                            f"4H:{result['trend_4h']}"
                        )

            # Dashboard Push
            push_scan_to_dashboard(
                sorted(all_scan_results, key=lambda x: x['score'], reverse=True),
                scan_time
            )

            # ── Trade-Ausführung ──────────────────────────────────────────────
            signals.sort(key=lambda x: x['score'], reverse=True)
            slots_free = MAX_OPEN - open_count
            traded     = 0

            for sig in signals[:slots_free]:
                if balance <= 0 or trade_size <= 0:
                    log.warning("⚠️ Kein Balance für Trade")
                    break

                min_rr = params.get('min_rr', MIN_RR)
                if sig['rr'] < min_rr:
                    log.info(f"  ⏭️ {sig['name']} RR zu niedrig ({sig['rr']:.2f} < {min_rr}) — skip")
                    continue

                if is_duplicate_trade(sig['symbol'], sig['signal'], mem):
                    log.info(f"  ⏭️ {sig['name']} Duplikat-Trade — skip")
                    continue

                log.info(
                    f"\n  🚀 TRADE: {sig['name']} {sig['signal']} | "
                    f"Score:{sig['score']} | Entry:${sig['price']:.4f} | "
                    f"TP:${sig['tp']:.4f} | SL:${sig['sl']:.4f} | RR:{sig['rr']:.2f}"
                )

                resp = place_order(
                    sig['symbol'], sig['signal'], trade_size,
                    sig['tp'], sig['sl'], sig['price']
                )

                if resp and resp.get('code') == '00000':
                    order_id = resp.get('data', {}).get('orderId', '?')
                    log.info(f"  ✅ Order platziert: {order_id}")

                    mem['trades'].append({
                        'symbol':       sig['symbol'],
                        'signal':       sig['signal'],
                        'entry_price':  sig['price'],
                        'tp':           sig['tp'],
                        'sl':           sig['sl'],
                        'atr':          sig['atr'],
                        'rr':           round(sig['rr'], 2),
                        'score':        sig['score'],
                        'regime':       sig['regime'],
                        'trend_4h':     sig['trend_4h'],
                        'reasons':      sig['reasons'],
                        'rsi':          round(sig['rsi'], 1),
                        'atr_pct':      round(sig['atr_pct'], 2),
                        'stoch_k':      round(sig['stoch_k'], 1) if sig['stoch_k'] else None,
                        'status':       'open',
                        'opened_at':    datetime.now().strftime('%Y-%m-%d %H:%M'),
                        'order_id':     order_id,
                        'trade_size':   trade_size,
                        'partial_done': False,
                    })
                    save_memory(mem)

                    tg(
                        f"🚀 <b>TRADE ERÖFFNET</b>\n"
                        f"Coin: <b>{sig['name']}</b> {sig['signal']}\n"
                        f"Entry: ${sig['price']:.4f} | Score: {sig['score']}\n"
                        f"TP: ${sig['tp']:.4f} | SL: ${sig['sl']:.4f}\n"
                        f"RR: {sig['rr']:.2f} | Regime: {sig['regime']}\n"
                        f"4H-Trend: {sig['trend_4h']} | Kapital: ${trade_size:.2f}\n"
                        f"📋 {sig['reasons']}"
                    )
                    traded += 1
                else:
                    err = resp.get('msg', 'Unbekannt') if resp else 'Keine Antwort'
                    log.error(f"  ❌ Order fehlgeschlagen: {err}")
                    tg(f"❌ Order fehlgeschlagen: {sig['name']} — {err}")

            if not signals:
                log.info(f"💤 Keine Signale (Min-Score: {params['min_score']})")
                # Alle 30 Min Status-Update auf Telegram (nur wenn kein Signal)
                if time.time() - last_status_tg >= STATUS_INTERVAL:
                    try:
                        # Offene Positionen PnL sammeln
                        pos_summary = ''
                        try:
                            cur_pos = get_open_positions()
                            for p in cur_pos:
                                sym  = p.get('symbol','').replace('USDT','')
                                side = p.get('holdSide','').upper()
                                upnl = float(p.get('unrealizedPL', 0))
                                mg   = float(p.get('marginSize', 1))
                                pct  = upnl / mg * 100 if mg > 0 else 0
                                e    = "🟢" if upnl > 0 else "🔴"
                                pos_summary += f"\n{e} {sym} {side}: ${upnl:+.2f} ({pct:+.1f}%)"
                        except:
                            pass
                        tg(
                            f"🤖 <b>JARVIS — 3h Status</b>\n"
                            f"💰 Balance: ${balance:.2f} | Pos: {open_count}/{MAX_OPEN}"
                            + (pos_summary if pos_summary else "\n💤 Keine offenen Positionen") +
                            f"\n🔍 Scannt weiter...",
                            silent=True
                        )
                    except Exception as e:
                        log.warning(f"Status-TG Fehler: {e}")
                    last_status_tg = time.time()
            else:
                log.info(f"📊 {len(signals)} Signal(e) gefunden | {traded} Trade(s) eröffnet")

            log.info(f"⏳ Nächster Scan in {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n⛔ Bot gestoppt.")
            tg("⛔ <b>JARVIS ALPHA V4 gestoppt</b>")
            break
        except Exception as e:
            log.error(f"[MAIN LOOP ERROR] {e}", exc_info=True)
            tg(f"⚠️ <b>Bot Fehler:</b> {str(e)[:200]}")
            time.sleep(30)

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    retry = 0
    while True:
        try:
            run()
            log.warning("run() beendet — Neustart in 10s...")
        except SystemExit:
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            retry += 1
            log.error(f"Kritischer Fehler #{retry}: {e}", exc_info=True)
            try:
                tg(f"🔄 Bot-Neustart nach Fehler: {str(e)[:150]}")
            except:
                pass
            wait = min(30 * retry, 300)
            log.info(f"Neustart in {wait}s...")
            time.sleep(wait)
