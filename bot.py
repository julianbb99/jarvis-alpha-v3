#!/usr/bin/env python3
"""
JARVIS XAU/USD BOT V1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Instrument: XAUUSDT Perpetual Futures
Strategie:  Schnelle Scalps auf 5min Chart
            EMA Cross + Momentum + Volatilität
Ziel:       3-6 Trades/Tag bei volatiler Session
TP:         $8-12 Gewinn pro Trade
SL:         direkt auf Bitget gesetzt
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('xau')

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv('BITGET_API_KEY', '')
API_SECRET = os.getenv('BITGET_API_SECRET', '')
API_PASS   = os.getenv('BITGET_PASSPHRASE', '')
TG_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TG_CHAT    = os.getenv('TELEGRAM_CHAT_ID', '')
B44_KEY    = os.getenv('BASE44_SERVICE_TOKEN', '')
B44_APP    = os.getenv('BASE44_APP_ID', '69a75a817485663824cde2d6')

BASE_URL   = 'https://api.bitget.com'
SYMBOL     = 'XAUUSDT'
PRODUCT    = 'USDT-FUTURES'

LEVERAGE   = 10        # 10x — Gold ist schon volatil genug
RISK_PCT   = 0.35      # 35% Balance als Margin
TP_PCT     = 0.003     # 0.3% Kursbewegung → ~$10 bei 10x auf $30 Margin ($300 Position)
SL_PCT     = 0.0015    # 0.15% SL → ~$5 Verlust (R/R 2:1)
MAX_HOLD_H = 1.0       # max 1h halten
SCAN_SEC   = 60        # jede Minute scannen

# Handelszeiten — Gold ist am volatilsten:
# London Open: 09:00-11:00 CET
# New York Open: 15:00-18:00 CET
# Overlap: 15:00-17:00 CET (beste Zeit)
TRADE_HOURS = [8,9,10,14,15,16,17,18,19]  # CET (UTC+1)

_active    = None   # aktuell offene Position
_last_trade = 0     # timestamp letzter Trade
COOLDOWN_MIN = 15   # 15min zwischen Trades

# ── BITGET API ────────────────────────────────────────────────────────────────
def _sign(ts, method, path, body=''):
    msg = f"{ts}{method}{path}{body}"
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def hdrs(method, path, body=''):
    ts = str(int(time.time() * 1000))
    return {
        'ACCESS-KEY':        API_KEY,
        'ACCESS-SIGN':       _sign(ts, method, path, body),
        'ACCESS-TIMESTAMP':  ts,
        'ACCESS-PASSPHRASE': API_PASS,
        'Content-Type':      'application/json',
        'locale':            'en-US',
    }

def bg_get(path, timeout=10):
    try:
        r = requests.get(BASE_URL + path, headers=hdrs('GET', path), timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"GET Fehler: {e}")
        return {}

def bg_post(path, body, timeout=10):
    bs = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=hdrs('POST', path, bs), data=bs, timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"POST Fehler: {e}")
        return {}

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[TG] {msg[:120]}")
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'},
            timeout=6
        )
    except Exception as e:
        log.warning(f"TG: {e}")

# ── BASE44 ────────────────────────────────────────────────────────────────────
def b44(entity, data):
    if not B44_KEY: return
    try:
        requests.post(
            f'https://api.base44.com/api/apps/{B44_APP}/entities/{entity}',
            headers={'x-api-key': B44_KEY, 'Content-Type': 'application/json'},
            json=data, timeout=5
        )
    except: pass

# ── INDIKATOREN ───────────────────────────────────────────────────────────────
def ema(values, period):
    if len(values) < period: return []
    k = 2 / (period + 1)
    e = [sum(values[:period]) / period]
    for v in values[period:]:
        e.append(v * k + e[-1] * (1 - k))
    return e

def rsi(closes, period=14):
    if len(closes) < period + 2: return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0: return 100.0
    return round(100 - (100 / (1 + ag / al)), 1)

def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]['h'], candles[i]['l'], candles[i-1]['c']
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    if not trs: return 0
    return sum(trs[-period:]) / min(len(trs), period)

# ── MARKTDATEN ────────────────────────────────────────────────────────────────
def get_candles(tf='5m', limit=100):
    # Bitget Granularität: 1m=1min, 5m=5min, 15m=15min
    gran_map = {'1m': '1min', '5m': '5min', '15m': '15min', '1h': '1H'}
    gran = gran_map.get(tf, '5min')
    path = f'/api/v2/mix/market/candles?symbol={SYMBOL}&granularity={gran}&limit={limit}&productType={PRODUCT}'
    d = bg_get(path)
    if not d or d.get('code') != '00000':
        log.warning(f"Candles Fehler: {d.get('msg','')}")
        return []
    candles = []
    for c in d.get('data', []):
        try:
            candles.append({'ts': int(c[0]), 'o': float(c[1]), 'h': float(c[2]),
                            'l': float(c[3]), 'c': float(c[4]), 'v': float(c[5])})
        except: pass
    return sorted(candles, key=lambda x: x['ts'])

def get_price():
    path = f'/api/v2/mix/market/ticker?symbol={SYMBOL}&productType={PRODUCT}'
    d = bg_get(path)
    if d.get('code') == '00000':
        data = d.get('data', [])
        if data:
            return float(data[0].get('lastPr', 0))
    return 0

def get_balance():
    d = bg_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if d.get('code') == '00000':
        for acc in d.get('data', []):
            if acc.get('marginCoin') == 'USDT':
                return float(acc.get('available', 0))
    return 0

def get_open_position():
    d = bg_get('/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT')
    if d.get('code') == '00000':
        for p in d.get('data', []):
            if p.get('symbol') == SYMBOL and float(p.get('total', 0)) > 0:
                return p
    return None

# ── SIGNAL ANALYSE (5min Chart) ───────────────────────────────────────────────
def analyse():
    candles_5m = get_candles('5m', 100)
    candles_15m = get_candles('15m', 50)

    if len(candles_5m) < 50 or len(candles_15m) < 20:
        return None, 0, ''

    closes_5m  = [c['c'] for c in candles_5m]
    closes_15m = [c['c'] for c in candles_15m]
    price      = closes_5m[-1]

    # ── Indikatoren 5min ──
    e8  = ema(closes_5m, 8)
    e21 = ema(closes_5m, 21)
    r5  = rsi(closes_5m[-20:], 14)
    a5  = atr(candles_5m[-20:], 14)

    # ── Indikatoren 15min (Trend-Bestätigung) ──
    e21_15 = ema(closes_15m, 21)
    r15    = rsi(closes_15m[-20:], 14)

    if not e8 or not e21 or not e21_15:
        return None, 0, ''

    # ── Volatilität prüfen ──
    atr_pct = (a5 / price) * 100
    if atr_pct < 0.05:  # Gold braucht mindestens 0.05% ATR auf 5min
        log.info(f"  ATR zu niedrig: {atr_pct:.3f}%")
        return None, 0, ''

    # ── Trend 15min ──
    trend_15_up   = closes_15m[-1] > e21_15[-1]
    trend_15_down = closes_15m[-1] < e21_15[-1]

    # ── EMA Cross 5min ──
    cross_up   = e8[-3] <= e21[-3] and e8[-1] > e21[-1]
    cross_down = e8[-3] >= e21[-3] and e8[-1] < e21[-1]

    # ── Momentum (letzte 3 Kerzen gleichgerichtet) ──
    mom_up   = closes_5m[-1] > closes_5m[-2] > closes_5m[-3]
    mom_down = closes_5m[-1] < closes_5m[-2] < closes_5m[-3]

    # ── Volumen Bestätigung ──
    vols    = [c['v'] for c in candles_5m]
    avg_vol = sum(vols[-11:-1]) / 10
    vol_ok  = vols[-1] > avg_vol * 1.3

    score   = 0
    signal  = None
    reasons = []

    # ── LONG ──
    if trend_15_up:
        score += 25; reasons.append("15m Trend ↑")
    if cross_up:
        score += 30; reasons.append("EMA8/21 Cross ↑")
    if mom_up:
        score += 20; reasons.append("Momentum ↑")
    if vol_ok:
        score += 15; reasons.append("Vol+")
    if 40 < r5 < 65:
        score += 10; reasons.append(f"RSI {r5}")

    if score >= 70 and trend_15_up and (cross_up or mom_up):
        signal = 'LONG'
    else:
        score = 0; reasons = []
        if trend_15_down:
            score += 25; reasons.append("15m Trend ↓")
        if cross_down:
            score += 30; reasons.append("EMA8/21 Cross ↓")
        if mom_down:
            score += 20; reasons.append("Momentum ↓")
        if vol_ok:
            score += 15; reasons.append("Vol+")
        if 35 < r5 < 60:
            score += 10; reasons.append(f"RSI {r5}")

        if score >= 70 and trend_15_down and (cross_down or mom_down):
            signal = 'SHORT'

    return signal, score, ' | '.join(reasons)

# ── LEVERAGE SETZEN ───────────────────────────────────────────────────────────
def set_leverage():
    for side in ['long', 'short']:
        r = bg_post('/api/v2/mix/account/set-leverage', {
            'symbol': SYMBOL, 'productType': PRODUCT,
            'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': side
        })
        if r.get('code') != '00000':
            log.warning(f"Leverage Fehler: {r.get('msg')}")

# ── TRADE PLATZIEREN ──────────────────────────────────────────────────────────
def place_trade(side, balance):
    global _active, _last_trade

    price = get_price()
    if price <= 0: return False

    margin   = round(balance * RISK_PCT, 2)
    notional = margin * LEVERAGE
    qty      = round(notional / price, 2)
    qty      = max(qty, 0.01)

    # TP/SL Preise (auf 2 Dezimalstellen — Gold)
    if side == 'LONG':
        tp = round(price * (1 + TP_PCT), 2)
        sl = round(price * (1 - SL_PCT), 2)
    else:
        tp = round(price * (1 - TP_PCT), 2)
        sl = round(price * (1 + SL_PCT), 2)

    profit_est = round(qty * price * TP_PCT, 2)
    loss_est   = round(qty * price * SL_PCT, 2)

    log.info(f"Trade: {side} | Qty: {qty} oz | Entry: ${price:.2f} | TP: ${tp:.2f} | SL: ${sl:.2f}")
    log.info(f"Margin: ${margin:.2f} | Notional: ${notional:.2f} | Gewinn: ~${profit_est:.2f}")

    # ── 1. Entry ──
    entry_body = {
        'symbol':      SYMBOL,
        'productType': PRODUCT,
        'marginMode':  'isolated',
        'marginCoin':  'USDT',
        'size':        str(qty),
        'side':        'buy' if side == 'LONG' else 'sell',
        'tradeSide':   'open',
        'orderType':   'market',
        'force':       'gtc',
    }
    er = bg_post('/api/v2/mix/order/place-order', entry_body)
    if er.get('code') != '00000':
        log.warning(f"Entry Fehler: {er.get('msg')} ({er.get('code')})")
        return False

    log.info(f"✅ Entry OK: {er.get('data',{}).get('orderId','')}")
    time.sleep(2)

    # ── 2. TP Plan Order ──
    tp_r = bg_post('/api/v2/mix/order/place-tpsl-order', {
        'symbol':       SYMBOL,
        'productType':  PRODUCT,
        'marginCoin':   'USDT',
        'planType':     'profit_plan',
        'triggerPrice': str(tp),
        'triggerType':  'mark_price',
        'executePrice': '0',
        'holdSide':     'long' if side == 'LONG' else 'short',
        'size':         str(qty),
        'tradeSide':    'close',
    })
    if tp_r.get('code') == '00000':
        log.info(f"✅ TP gesetzt: ${tp:.2f}")
    else:
        log.warning(f"⚠️ TP Fehler: {tp_r.get('msg')} ({tp_r.get('code')})")

    # ── 3. SL Plan Order ──
    sl_r = bg_post('/api/v2/mix/order/place-tpsl-order', {
        'symbol':       SYMBOL,
        'productType':  PRODUCT,
        'marginCoin':   'USDT',
        'planType':     'loss_plan',
        'triggerPrice': str(sl),
        'triggerType':  'mark_price',
        'executePrice': '0',
        'holdSide':     'long' if side == 'LONG' else 'short',
        'size':         str(qty),
        'tradeSide':    'close',
    })
    if sl_r.get('code') == '00000':
        log.info(f"✅ SL gesetzt: ${sl:.2f}")
    else:
        log.warning(f"⚠️ SL Fehler: {sl_r.get('msg')} ({sl_r.get('code')})")

    # ── Telegram Nachricht ──
    tg(
        f"⚡ <b>XAU/USD TRADE</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{side}</b> | {qty} oz Gold\n"
        f"💵 Entry: <b>${price:.2f}</b>\n"
        f"✅ TP: <b>${tp:.2f}</b> (+{TP_PCT*100:.2f}%)\n"
        f"🛑 SL: <b>${sl:.2f}</b> (-{SL_PCT*100:.2f}%)\n"
        f"💰 Margin: ${margin:.2f} × {LEVERAGE}x\n"
        f"🎯 Gewinn: ~<b>+${profit_est:.2f}</b> | Risiko: ~${loss_est:.2f}\n"
        f"⏱️ {datetime.now().strftime('%H:%M')} CET"
    )

    _active    = {'side': side, 'qty': qty, 'entry': price, 'tp': tp, 'sl': sl, 'time': time.time()}
    _last_trade = time.time()

    b44('BotTrade', {
        'symbol': SYMBOL, 'side': side, 'entry_price': price,
        'size': qty, 'pnl': 0, 'status': 'filled',
        'trade_time': datetime.utcnow().isoformat() + 'Z'
    })
    return True

def close_position(reason='timeout'):
    global _active
    if not _active: return
    side = _active['side']
    qty  = _active['qty']
    pos  = get_open_position()
    if not pos: _active = None; return
    upnl = float(pos.get('unrealizedPL', 0))

    bg_post('/api/v2/mix/order/place-order', {
        'symbol': SYMBOL, 'productType': PRODUCT,
        'marginMode': 'isolated', 'marginCoin': 'USDT',
        'size': str(qty),
        'side': 'sell' if side == 'LONG' else 'buy',
        'tradeSide': 'close', 'orderType': 'market', 'force': 'gtc',
    })
    tg(f"⏱️ <b>{reason.upper()}</b> — XAU/USD\n{side} | PnL: ${upnl:.2f}")
    _active = None

# ── HANDELSZEIT CHECK ─────────────────────────────────────────────────────────
def is_trading_hour():
    hour_cet = (datetime.utcnow().hour + 1) % 24  # UTC+1 (CET)
    return hour_cet in TRADE_HOURS

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global _active

    log.info("=" * 55)
    log.info("JARVIS XAU/USD BOT V1")
    log.info(f"Hebel: {LEVERAGE}x | Margin/Trade: {RISK_PCT*100:.0f}% | TP: {TP_PCT*100:.2f}% | SL: {SL_PCT*100:.2f}%")
    log.info("=" * 55)

    set_leverage()
    balance = get_balance()
    margin  = round(balance * RISK_PCT, 2)
    profit_est = round(margin * LEVERAGE * TP_PCT, 2)

    tg(
        f"🥇 <b>JARVIS XAU/USD BOT gestartet</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: ${balance:.2f}\n"
        f"⚡ {LEVERAGE}x Hebel | {RISK_PCT*100:.0f}% Margin/Trade\n"
        f"🎯 Ziel: ~${profit_est:.2f} pro Trade\n"
        f"📊 TP: +{TP_PCT*100:.2f}% | SL: -{SL_PCT*100:.2f}%\n"
        f"🕐 Aktiv: London + NY Session\n"
        f"🔒 TP/SL direkt auf Bitget"
    )

    scan = 0
    while True:
        scan += 1
        try:
            now_h = (datetime.utcnow().hour + 1) % 24
            log.info(f"── Scan #{scan} [{datetime.now().strftime('%H:%M:%S')}] CET {now_h}:xx ──")

            # Position Monitor
            pos = get_open_position()
            if _active and not pos:
                log.info(f"✅ Position geschlossen (TP/SL)")
                _active = None
                _last_trade = time.time()

            # Timeout Check
            if _active and pos:
                age_h = (time.time() - _active['time']) / 3600
                if age_h >= MAX_HOLD_H:
                    close_position('timeout')

            # Keine neue Position wenn aktiv
            if _active:
                upnl = float(pos.get('unrealizedPL', 0)) if pos else 0
                log.info(f"⏸️ Position offen — uPnL: ${upnl:.2f}")
                time.sleep(SCAN_SEC)
                continue

            # Außerhalb Handelszeiten
            if not is_trading_hour():
                log.info(f"😴 Außerhalb Handelszeiten (CET {now_h}:xx)")
                time.sleep(300)
                continue

            # Cooldown
            elapsed_min = (time.time() - _last_trade) / 60
            if elapsed_min < COOLDOWN_MIN:
                log.info(f"⏳ Cooldown: noch {COOLDOWN_MIN - elapsed_min:.0f}min")
                time.sleep(SCAN_SEC)
                continue

            # Balance check
            balance = get_balance()
            if balance < 5:
                log.warning(f"⚠️ Balance zu niedrig: ${balance:.2f}")
                time.sleep(SCAN_SEC)
                continue

            # Signal
            signal, score, reason = analyse()
            log.info(f"Signal: {signal or 'kein'} | Score: {score} | {reason}")

            if signal and score >= 70:
                log.info(f"🟢 Einstieg: {signal} | Score={score}")
                place_trade(signal, balance)
            else:
                log.info("💤 Kein Setup")

        except KeyboardInterrupt:
            tg("⛔ XAU Bot gestoppt")
            break
        except Exception as e:
            log.error(f"Fehler: {e}", exc_info=True)

        time.sleep(SCAN_SEC)

if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Kritischer Fehler: {e}")
            time.sleep(20)
