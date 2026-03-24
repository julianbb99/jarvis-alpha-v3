#!/usr/bin/env python3
"""
JARVIS XAU/USD BOT V2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Instrument: XAUUSDT Perpetual Futures
Strategie:  Scalps + Pump/Dump Erkennung
            - Normal: EMA Cross + Momentum auf 5min
            - Pump/Dump: Kerzen-Ausbruch sofort reiten
Kein Tages-Limit — jede Gelegenheit mitnehmen
TP/SL: direkt als Bitget Plan-Orders
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('xau')

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv('BITGET_API_KEY', '')
API_SECRET = os.getenv('BITGET_API_SECRET', '')
API_PASS   = os.getenv('BITGET_PASSPHRASE', '')
TG_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TG_CHAT    = os.getenv('TELEGRAM_CHAT_ID', '')
B44_KEY    = os.getenv('BASE44_SERVICE_TOKEN', '')
B44_APP    = os.getenv('BASE44_APP_ID', '69a75a817485663824cde2d6')

BASE_URL = 'https://api.bitget.com'
SYMBOL   = 'XAUUSDT'
PRODUCT  = 'USDT-FUTURES'

LEVERAGE     = 10      # 10x Hebel
RISK_PCT     = 0.35    # 35% Balance als Margin pro Trade
SCAN_SEC     = 30      # alle 30 Sekunden scannen
COOLDOWN_MIN = 5       # nur 5min Cooldown — schnell wieder rein
MAX_HOLD_H   = 0.75    # max 45min halten

# ── TP/SL EINSTELLUNGEN ───────────────────────────────────────────────────────
# Normal Setup: 0.3% TP / 0.15% SL
TP_NORMAL  = 0.003
SL_NORMAL  = 0.0015

# Pump/Dump Setup: größeres TP weil Bewegung stark
TP_PUMP    = 0.005    # 0.5% TP — Pump läuft weiter
SL_PUMP    = 0.002    # 0.2% SL — enger Stop

# ── STATE ─────────────────────────────────────────────────────────────────────
_active     = None
_last_trade = 0

# ── BITGET ────────────────────────────────────────────────────────────────────
def _sign(ts, method, path, body=''):
    msg = f"{ts}{method}{path}{body}"
    return base64.b64encode(hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()

def hdrs(method, path, body=''):
    ts = str(int(time.time() * 1000))
    return {
        'ACCESS-KEY': API_KEY, 'ACCESS-SIGN': _sign(ts, method, path, body),
        'ACCESS-TIMESTAMP': ts, 'ACCESS-PASSPHRASE': API_PASS,
        'Content-Type': 'application/json', 'locale': 'en-US',
    }

def bg_get(path):
    try:
        return requests.get(BASE_URL + path, headers=hdrs('GET', path), timeout=10).json()
    except Exception as e:
        log.warning(f"GET: {e}"); return {}

def bg_post(path, body):
    bs = json.dumps(body)
    try:
        return requests.post(BASE_URL + path, headers=hdrs('POST', path, bs), data=bs, timeout=10).json()
    except Exception as e:
        log.warning(f"POST: {e}"); return {}

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[TG] {msg[:100]}"); return
    try:
        requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                      json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=6)
    except: pass

def b44(entity, data):
    if not B44_KEY: return
    try:
        requests.post(f'https://api.base44.com/api/apps/{B44_APP}/entities/{entity}',
                      headers={'x-api-key': B44_KEY, 'Content-Type': 'application/json'},
                      json=data, timeout=5)
    except: pass

# ── INDIKATOREN ───────────────────────────────────────────────────────────────
def ema(values, p):
    if len(values) < p: return []
    k = 2 / (p + 1)
    e = [sum(values[:p]) / p]
    for v in values[p:]: e.append(v * k + e[-1] * (1 - k))
    return e

def rsi(closes, p=14):
    if len(closes) < p + 2: return 50.0
    d = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ag = sum(max(x, 0) for x in d[-p:]) / p
    al = sum(max(-x, 0) for x in d[-p:]) / p
    return round(100 - (100 / (1 + ag / al)), 1) if al else 100.0

def atr(candles, p=14):
    trs = [max(c['h']-c['l'], abs(c['h']-candles[i-1]['c']), abs(c['l']-candles[i-1]['c']))
           for i, c in enumerate(candles) if i > 0]
    return sum(trs[-p:]) / min(len(trs), p) if trs else 0

# ── MARKTDATEN ────────────────────────────────────────────────────────────────
def get_candles(tf='5min', limit=100):
    path = f'/api/v2/mix/market/candles?symbol={SYMBOL}&granularity={tf}&limit={limit}&productType={PRODUCT}'
    d = bg_get(path)
    if d.get('code') != '00000': return []
    candles = []
    for c in d.get('data', []):
        try: candles.append({'ts': int(c[0]), 'o': float(c[1]), 'h': float(c[2]),
                             'l': float(c[3]), 'c': float(c[4]), 'v': float(c[5])})
        except: pass
    return sorted(candles, key=lambda x: x['ts'])

def get_price():
    d = bg_get(f'/api/v2/mix/market/ticker?symbol={SYMBOL}&productType={PRODUCT}')
    if d.get('code') == '00000':
        data = d.get('data', [])
        if data: return float(data[0].get('lastPr', 0))
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

def set_leverage():
    for side in ['long', 'short']:
        bg_post('/api/v2/mix/account/set-leverage', {
            'symbol': SYMBOL, 'productType': PRODUCT,
            'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': side
        })

# ── SIGNAL ANALYSE ────────────────────────────────────────────────────────────
def analyse():
    """
    Gibt zurück: (signal, score, mode, reason)
    mode: 'normal' | 'pump' | 'dump'
    """
    c5  = get_candles('5min', 100)
    c15 = get_candles('15min', 50)
    c1  = get_candles('1min', 30)   # für Pump/Dump Erkennung

    if len(c5) < 50: return None, 0, 'normal', ''

    closes5  = [c['c'] for c in c5]
    closes15 = [c['c'] for c in c15] if len(c15) > 20 else closes5
    price    = closes5[-1]

    e8   = ema(closes5, 8)
    e21  = ema(closes5, 21)
    e21_15 = ema(closes15, 21) if len(closes15) > 21 else e21
    r5   = rsi(closes5[-20:])
    a5   = atr(c5[-20:])
    atr_pct = (a5 / price) * 100

    vols    = [c['v'] for c in c5]
    avg_vol = sum(vols[-11:-1]) / 10 if len(vols) > 10 else 1

    # ── PUMP/DUMP ERKENNUNG (1min Kerzen) ──
    if len(c1) >= 5:
        last1 = c1[-1]
        prev1 = c1[-2]
        candle_body = abs(last1['c'] - last1['o']) / last1['o'] * 100
        vol_spike   = last1['v'] > avg_vol * 2.5

        # Pump: große grüne Kerze + Volumen Spike
        if candle_body > 0.15 and last1['c'] > last1['o'] and vol_spike:
            log.info(f"🚀 PUMP erkannt: Kerze {candle_body:.2f}% | Vol {last1['v']/avg_vol:.1f}x")
            return 'LONG', 85, 'pump', f"PUMP {candle_body:.2f}% | Vol {last1['v']/avg_vol:.1f}x"

        # Dump: große rote Kerze + Volumen Spike
        if candle_body > 0.15 and last1['c'] < last1['o'] and vol_spike:
            log.info(f"💥 DUMP erkannt: Kerze {candle_body:.2f}% | Vol {last1['v']/avg_vol:.1f}x")
            return 'SHORT', 85, 'dump', f"DUMP {candle_body:.2f}% | Vol {last1['v']/avg_vol:.1f}x"

    if not e8 or not e21: return None, 0, 'normal', ''

    # ── NORMALES SETUP (5min EMA Cross) ──
    trend_up   = closes5[-1] > e21_15[-1] if e21_15 else True
    trend_down = closes5[-1] < e21_15[-1] if e21_15 else True
    cross_up   = e8[-3] <= e21[-3] and e8[-1] > e21[-1]
    cross_down = e8[-3] >= e21[-3] and e8[-1] < e21[-1]
    mom_up     = closes5[-1] > closes5[-2] > closes5[-3]
    mom_down   = closes5[-1] < closes5[-2] < closes5[-3]
    vol_ok     = vols[-1] > avg_vol * 1.3

    # ATR Filter — Gold muss sich bewegen
    if atr_pct < 0.05:
        log.info(f"  ATR zu niedrig: {atr_pct:.3f}%")
        return None, 0, 'normal', ''

    score = 0; reasons = []

    if trend_up: score += 25; reasons.append("Trend↑")
    if cross_up: score += 30; reasons.append("Cross↑")
    if mom_up:   score += 20; reasons.append("Mom↑")
    if vol_ok:   score += 15; reasons.append("Vol+")
    if 40 < r5 < 65: score += 10; reasons.append(f"RSI{r5}")

    if score >= 65 and trend_up and (cross_up or mom_up):
        return 'LONG', score, 'normal', ' | '.join(reasons)

    score = 0; reasons = []
    if trend_down: score += 25; reasons.append("Trend↓")
    if cross_down: score += 30; reasons.append("Cross↓")
    if mom_down:   score += 20; reasons.append("Mom↓")
    if vol_ok:     score += 15; reasons.append("Vol+")
    if 35 < r5 < 60: score += 10; reasons.append(f"RSI{r5}")

    if score >= 65 and trend_down and (cross_down or mom_down):
        return 'SHORT', score, 'normal', ' | '.join(reasons)

    return None, score, 'normal', ' | '.join(reasons)

# ── TRADE PLATZIEREN ──────────────────────────────────────────────────────────
def place_trade(side, balance, mode='normal'):
    global _active, _last_trade

    price = get_price()
    if price <= 0: return False

    margin   = round(balance * RISK_PCT, 2)
    notional = margin * LEVERAGE
    qty      = round(notional / price, 2)
    qty      = max(qty, 0.01)

    # TP/SL je nach Modus
    tp_pct = TP_PUMP if mode in ('pump', 'dump') else TP_NORMAL
    sl_pct = SL_PUMP if mode in ('pump', 'dump') else SL_NORMAL

    if side == 'LONG':
        tp = round(price * (1 + tp_pct), 2)
        sl = round(price * (1 - sl_pct), 2)
    else:
        tp = round(price * (1 - tp_pct), 2)
        sl = round(price * (1 + sl_pct), 2)

    profit_est = round(qty * price * tp_pct, 2)
    loss_est   = round(qty * price * sl_pct, 2)

    log.info(f"{'🚀' if mode=='pump' else '💥' if mode=='dump' else '📊'} {side} | {qty}oz | Entry:{price:.2f} TP:{tp:.2f} SL:{sl:.2f}")

    # ── 1. Entry ──
    er = bg_post('/api/v2/mix/order/place-order', {
        'symbol': SYMBOL, 'productType': PRODUCT,
        'marginMode': 'isolated', 'marginCoin': 'USDT',
        'size': str(qty), 'side': 'buy' if side == 'LONG' else 'sell',
        'tradeSide': 'open', 'orderType': 'market', 'force': 'gtc',
    })
    if er.get('code') != '00000':
        log.warning(f"Entry Fehler: {er.get('msg')} ({er.get('code')})")
        return False

    log.info(f"✅ Entry OK: {er.get('data',{}).get('orderId','')}")
    time.sleep(2)

    # ── 2. TP ──
    tp_r = bg_post('/api/v2/mix/order/place-tpsl-order', {
        'symbol': SYMBOL, 'productType': PRODUCT, 'marginCoin': 'USDT',
        'planType': 'profit_plan', 'triggerPrice': str(tp),
        'triggerType': 'mark_price', 'executePrice': '0',
        'holdSide': 'long' if side == 'LONG' else 'short',
        'size': str(qty), 'tradeSide': 'close',
    })
    log.info(f"TP: {tp_r.get('code')} {tp_r.get('msg','')} → ${tp:.2f}")

    # ── 3. SL ──
    sl_r = bg_post('/api/v2/mix/order/place-tpsl-order', {
        'symbol': SYMBOL, 'productType': PRODUCT, 'marginCoin': 'USDT',
        'planType': 'loss_plan', 'triggerPrice': str(sl),
        'triggerType': 'mark_price', 'executePrice': '0',
        'holdSide': 'long' if side == 'LONG' else 'short',
        'size': str(qty), 'tradeSide': 'close',
    })
    log.info(f"SL: {sl_r.get('code')} {sl_r.get('msg','')} → ${sl:.2f}")

    mode_emoji = '🚀 PUMP' if mode == 'pump' else '💥 DUMP' if mode == 'dump' else '📊 SETUP'
    tg(
        f"⚡ <b>XAU/USD {mode_emoji}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{side}</b> | {qty} oz Gold\n"
        f"💵 Entry: <b>${price:.2f}</b>\n"
        f"✅ TP: <b>${tp:.2f}</b> (+{tp_pct*100:.2f}%)\n"
        f"🛑 SL: <b>${sl:.2f}</b> (-{sl_pct*100:.2f}%)\n"
        f"💰 Margin: ${margin:.2f} × {LEVERAGE}x\n"
        f"🎯 Ziel: ~<b>+${profit_est:.2f}</b> | Risiko: ~${loss_est:.2f}\n"
        f"⏱️ {datetime.now().strftime('%H:%M')} CET"
    )

    _active     = {'side': side, 'qty': qty, 'entry': price, 'tp': tp, 'sl': sl, 'time': time.time(), 'mode': mode}
    _last_trade = time.time()

    b44('BotTrade', {
        'symbol': SYMBOL, 'side': side, 'entry_price': price,
        'size': qty, 'pnl': 0, 'status': 'filled',
        'trade_time': datetime.utcnow().isoformat() + 'Z'
    })
    return True

def close_timeout():
    global _active
    if not _active: return
    pos = get_open_position()
    if not pos: _active = None; return
    upnl = float(pos.get('unrealizedPL', 0))
    side = _active['side']
    qty  = _active['qty']
    bg_post('/api/v2/mix/order/place-order', {
        'symbol': SYMBOL, 'productType': PRODUCT,
        'marginMode': 'isolated', 'marginCoin': 'USDT',
        'size': str(qty), 'side': 'sell' if side == 'LONG' else 'buy',
        'tradeSide': 'close', 'orderType': 'market', 'force': 'gtc',
    })
    tg(f"⏱️ <b>TIMEOUT</b> — XAU/USD\n{side} | PnL: ${upnl:.2f} | 45min abgelaufen")
    _active = None

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global _active

    log.info("=" * 55)
    log.info("JARVIS XAU/USD BOT V2 — Kein Trade-Limit")
    log.info(f"Hebel: {LEVERAGE}x | Margin: {RISK_PCT*100:.0f}% | Cooldown: {COOLDOWN_MIN}min")
    log.info("=" * 55)

    set_leverage()
    balance = get_balance()
    margin  = round(balance * RISK_PCT, 2)

    tg(
        f"🥇 <b>JARVIS XAU/USD V2 gestartet</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: ${balance:.2f} | Margin/Trade: ${margin:.2f}\n"
        f"⚡ {LEVERAGE}x Hebel | Scan: alle {SCAN_SEC}s\n"
        f"📊 Normal TP: +{TP_NORMAL*100:.2f}% | SL: -{SL_NORMAL*100:.2f}%\n"
        f"🚀 Pump/Dump TP: +{TP_PUMP*100:.2f}% | SL: -{SL_PUMP*100:.2f}%\n"
        f"⏱️ Cooldown: {COOLDOWN_MIN}min | Max Hold: 45min\n"
        f"🔒 TP/SL direkt auf Bitget | Kein Trade-Limit"
    )

    scan = 0
    while True:
        scan += 1
        try:
            log.info(f"── Scan #{scan} [{datetime.now().strftime('%H:%M:%S')}] ──")

            # Position Monitor
            pos = get_open_position()
            if _active and not pos:
                log.info(f"✅ Position geschlossen (TP/SL/Bitget)")
                _active = None
                _last_trade = time.time()

            # Timeout
            if _active and pos:
                age_h = (time.time() - _active['time']) / 3600
                if age_h >= MAX_HOLD_H:
                    close_timeout()

            # Position offen — warten
            if _active:
                upnl = float(pos.get('unrealizedPL', 0)) if pos else 0
                log.info(f"⏸️ Offen — uPnL: ${upnl:.2f}")
                time.sleep(SCAN_SEC)
                continue

            # Cooldown
            elapsed = (time.time() - _last_trade) / 60
            if elapsed < COOLDOWN_MIN:
                log.info(f"⏳ Cooldown: {COOLDOWN_MIN - elapsed:.0f}min")
                time.sleep(SCAN_SEC)
                continue

            # Balance
            balance = get_balance()
            if balance < 5:
                log.warning(f"⚠️ Balance: ${balance:.2f}")
                time.sleep(60)
                continue

            # Signal
            signal, score, mode, reason = analyse()
            log.info(f"Signal: {signal or 'kein'} | Score: {score} | Mode: {mode} | {reason}")

            if signal:
                log.info(f"🟢 TRADE: {signal} | {mode.upper()} | Score={score}")
                place_trade(signal, balance, mode)

        except KeyboardInterrupt:
            tg("⛔ XAU Bot V2 gestoppt")
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
