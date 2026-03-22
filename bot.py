#!/usr/bin/env python3
"""
JARVIS ALPHA BOT V6
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ziel: 1 großer Trade → $10 Gewinn
TP + SL: sofort als separate Bitget-Orders
Einstieg: nur bei starken, klaren Setups
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('jarvis')

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv('BITGET_API_KEY', '')
API_SECRET = os.getenv('BITGET_API_SECRET', '')
API_PASS   = os.getenv('BITGET_PASSPHRASE', '')
TG_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TG_CHAT    = os.getenv('TELEGRAM_CHAT_ID', '')
B44_KEY    = os.getenv('BASE44_SERVICE_TOKEN', '')
B44_APP    = os.getenv('BASE44_APP_ID', '69a75a817485663824cde2d6')

BASE_URL   = 'https://api.bitget.com'
B44_URL    = f'https://api.base44.com/api/apps/{B44_APP}/entities'

LEVERAGE     = 20       # 20x Hebel
RISK_PCT     = 0.40     # 40% Balance als Margin → bei $50 = $20 Margin × 20x = $400 Position
TP_PCT       = 0.025    # 2.5% Kursbewegung → ~$10 Gewinn auf $400
SL_PCT       = 0.012    # 1.2% SL → ~$4.80 Verlust (R/R 2:1)
MAX_HOLD_H   = 2.0      # Notfall-Timeout 2h
SCAN_SEC     = 120      # alle 2min scannen
COOLDOWN_MIN = 20       # 20min nach Trade kein Re-Entry

# Nur liquide Coins mit guten Bewegungen
COINS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT',
    'DOGEUSDT', 'XRPUSDT', 'ADAUSDT', 'AVAXUSDT',
    'LINKUSDT', 'DOTUSDT', 'LTCUSDT', 'MATICUSDT',
    'NEARUSDT', 'APTUSDT', 'INJUSDT', 'SUIUSDT',
]

_cooldown  = {}   # symbol → unix timestamp letzter Trade
_active    = {}   # symbol → position info

# ── BITGET API ────────────────────────────────────────────────────────────────
def _sign(ts, method, path, body=''):
    msg = f"{ts}{method}{path}{body}"
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def _hdrs(method, path, body=''):
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
        r = requests.get(BASE_URL + path, headers=_hdrs('GET', path), timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"GET Fehler: {e}")
        return {}

def bg_post(path, body, timeout=10):
    bs = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=_hdrs('POST', path, bs), data=bs, timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"POST Fehler: {e}")
        return {}

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg, silent=False):
    if not TG_TOKEN or not TG_CHAT:
        log.info(f"[TG] {msg[:100]}")
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg,
                  'parse_mode': 'HTML', 'disable_notification': silent},
            timeout=6
        )
    except Exception as e:
        log.warning(f"TG Fehler: {e}")

# ── BASE44 ────────────────────────────────────────────────────────────────────
def b44(entity, data):
    if not B44_KEY: return
    try:
        requests.post(
            f'{B44_URL}/{entity}',
            headers={'x-api-key': B44_KEY, 'Content-Type': 'application/json'},
            json=data, timeout=5
        )
    except: pass

# ── MARKTDATEN ────────────────────────────────────────────────────────────────
def get_candles(symbol, tf='15m', limit=100):
    """15min Kerzen holen."""
    path = f'/api/v2/mix/market/candles?symbol={symbol}&granularity={tf}&limit={limit}&productType=USDT-FUTURES'
    d = bg_get(path)
    if not d or d.get('code') != '00000':
        return []
    candles = []
    for c in d.get('data', []):
        try:
            candles.append({
                'ts': int(c[0]),
                'o':  float(c[1]),
                'h':  float(c[2]),
                'l':  float(c[3]),
                'c':  float(c[4]),
                'v':  float(c[5]),
            })
        except:
            pass
    return sorted(candles, key=lambda x: x['ts'])

def get_price(symbol):
    path = f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES'
    d = bg_get(path)
    if d.get('code') == '00000':
        data = d.get('data', [])
        if data:
            return float(data[0].get('lastPr', 0))
    return 0

def get_contract_info(symbol):
    """Min-Qty und Preis-Dezimalstellen holen."""
    path = f'/api/v2/mix/market/contracts?symbol={symbol}&productType=USDT-FUTURES'
    d = bg_get(path)
    if d.get('code') == '00000':
        for c in d.get('data', []):
            if c.get('symbol') == symbol:
                return {
                    'min_qty':   float(c.get('minTradeNum', 1)),
                    'qty_dp':    int(c.get('volumePlace', 0)),
                    'price_dp':  int(c.get('pricePlace', 4)),
                    'price_step': float(c.get('priceEndStep', 0.0001)),
                }
    return {'min_qty': 1, 'qty_dp': 0, 'price_dp': 4, 'price_step': 0.0001}

# ── INDIKATOREN ───────────────────────────────────────────────────────────────
def ema(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    e = [sum(values[:period]) / period]
    for v in values[period:]:
        e.append(v * k + e[-1] * (1 - k))
    return e

def rsi(closes, period=14):
    if len(closes) < period + 2:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0: return 100.0
    return round(100 - (100 / (1 + ag / al)), 1)

def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]['h']
        l = candles[i]['l']
        pc = candles[i-1]['c']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

# ── SIGNAL ANALYSE ────────────────────────────────────────────────────────────
def analyse(symbol):
    """
    Gibt zurück: (signal, score, reason)
    signal: 'LONG' | 'SHORT' | None
    score:  0-100
    reason: Text für Telegram
    """
    candles = get_candles(symbol, '15m', 100)
    if len(candles) < 60:
        return None, 0, ''

    closes = [c['c'] for c in candles]
    highs  = [c['h'] for c in candles]
    lows   = [c['l'] for c in candles]
    vols   = [c['v'] for c in candles]
    price  = closes[-1]

    # Indikatoren
    e9   = ema(closes, 9)
    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    r    = rsi(closes[-20:])
    a    = atr(candles[-20:])

    if not e9 or not e21 or not e50:
        return None, 0, ''

    # Trend bestimmen
    trend_up   = e9[-1] > e21[-1] > e50[-1]
    trend_down = e9[-1] < e21[-1] < e50[-1]

    # EMA Cross (letzte 3 Kerzen)
    cross_up   = e9[-3] < e21[-3] and e9[-1] > e21[-1]
    cross_down = e9[-3] > e21[-3] and e9[-1] < e21[-1]

    # Volumen — aktuelle Kerze vs 20er Durchschnitt
    avg_vol = sum(vols[-21:-1]) / 20
    vol_spike = vols[-1] > avg_vol * 1.5

    # Momentum — letzte 3 Kerzen
    momentum_up   = closes[-1] > closes[-2] > closes[-3]
    momentum_down = closes[-1] < closes[-2] < closes[-3]

    # Kein Einstieg in überkauftem/überverkauftem Bereich
    rsi_ok_long  = 35 < r < 65
    rsi_ok_short = 35 < r < 65

    # ATR-Filter — Coin muss sich bewegen (min 0.3% ATR)
    atr_pct = (a / price) * 100
    atr_ok  = atr_pct >= 0.3

    score  = 0
    signal = None
    reasons = []

    # ── LONG Setup ──
    if trend_up:
        score += 30
        reasons.append("Trend ↑")
    if cross_up:
        score += 25
        reasons.append("EMA Cross ↑")
    if momentum_up:
        score += 15
        reasons.append("Momentum ↑")
    if vol_spike:
        score += 15
        reasons.append("Volumen Spike")
    if rsi_ok_long and r < 55:
        score += 15
        reasons.append(f"RSI {r}")
    if atr_ok:
        score += 0  # Pflicht aber kein Score

    if score >= 65 and trend_up and atr_ok:
        signal = 'LONG'

    # ── SHORT Setup ──
    if signal is None:
        score = 0
        reasons = []
        if trend_down:
            score += 30
            reasons.append("Trend ↓")
        if cross_down:
            score += 25
            reasons.append("EMA Cross ↓")
        if momentum_down:
            score += 15
            reasons.append("Momentum ↓")
        if vol_spike:
            score += 15
            reasons.append("Volumen Spike")
        if rsi_ok_short and r > 45:
            score += 15
            reasons.append(f"RSI {r}")

        if score >= 65 and trend_down and atr_ok:
            signal = 'SHORT'

    return signal, score, ' | '.join(reasons)

# ── ACCOUNT ───────────────────────────────────────────────────────────────────
def get_balance():
    d = bg_get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if d.get('code') == '00000':
        for acc in d.get('data', []):
            if acc.get('marginCoin') == 'USDT':
                return float(acc.get('available', 0))
    return 0

def get_open_positions():
    d = bg_get('/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT')
    if d.get('code') == '00000':
        return [p for p in d.get('data', []) if float(p.get('total', 0)) > 0]
    return []

def set_leverage(symbol):
    for side in ['long', 'short']:
        bg_post('/api/v2/mix/account/set-leverage', {
            'symbol': symbol, 'productType': 'USDT-FUTURES',
            'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': side
        })

# ── ORDER PLATZIERUNG ─────────────────────────────────────────────────────────
def place_trade(symbol, side, balance):
    """
    Entry + sofortige TP/SL Orders als separate Plan-Orders.
    Gibt (success, qty, entry_price, tp_price, sl_price) zurück.
    """
    set_leverage(symbol)
    info  = get_contract_info(symbol)
    price = get_price(symbol)
    if price <= 0:
        return False, 0, 0, 0, 0

    margin    = round(balance * RISK_PCT, 2)
    notional  = margin * LEVERAGE
    raw_qty   = notional / price
    qty       = round(raw_qty, info['qty_dp'])
    qty       = max(qty, info['min_qty'])

    # TP und SL Preise
    dp = info['price_dp']
    if side == 'LONG':
        tp = round(price * (1 + TP_PCT), dp)
        sl = round(price * (1 - SL_PCT), dp)
    else:
        tp = round(price * (1 - TP_PCT), dp)
        sl = round(price * (1 + SL_PCT), dp)

    log.info(f"Platziere {symbol} {side} | qty={qty} | Entry≈{price} | TP={tp} | SL={sl}")

    # ── 1. Market Entry Order ──
    entry_body = {
        'symbol':      symbol,
        'productType': 'USDT-FUTURES',
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
        return False, 0, 0, 0, 0

    order_id = er.get('data', {}).get('orderId', '')
    log.info(f"✅ Entry OK: {order_id}")
    time.sleep(1.5)  # kurz warten bis gefüllt

    # ── 2. TP Order (Plan Order) ──
    tp_body = {
        'symbol':        symbol,
        'productType':   'USDT-FUTURES',
        'marginCoin':    'USDT',
        'planType':      'profit_plan',
        'triggerPrice':  str(tp),
        'triggerType':   'mark_price',
        'executePrice':  '0',          # market execution
        'holdSide':      'long' if side == 'LONG' else 'short',
        'size':          str(qty),
        'tradeSide':     'close',
    }
    tpr = bg_post('/api/v2/mix/order/place-tpsl-order', tp_body)
    if tpr.get('code') == '00000':
        log.info(f"✅ TP Order gesetzt: {tp}")
    else:
        log.warning(f"⚠️ TP Fehler: {tpr.get('msg')} ({tpr.get('code')})")

    # ── 3. SL Order (Plan Order) ──
    sl_body = {
        'symbol':        symbol,
        'productType':   'USDT-FUTURES',
        'marginCoin':    'USDT',
        'planType':      'loss_plan',
        'triggerPrice':  str(sl),
        'triggerType':   'mark_price',
        'executePrice':  '0',          # market execution
        'holdSide':      'long' if side == 'LONG' else 'short',
        'size':          str(qty),
        'tradeSide':     'close',
    }
    slr = bg_post('/api/v2/mix/order/place-tpsl-order', sl_body)
    if slr.get('code') == '00000':
        log.info(f"✅ SL Order gesetzt: {sl}")
    else:
        log.warning(f"⚠️ SL Fehler: {slr.get('msg')} ({slr.get('code')})")

    return True, qty, price, tp, sl

def close_position_market(symbol, side, qty):
    """Notfall-Close bei Timeout."""
    bg_post('/api/v2/mix/order/place-order', {
        'symbol':      symbol,
        'productType': 'USDT-FUTURES',
        'marginMode':  'isolated',
        'marginCoin':  'USDT',
        'size':        str(qty),
        'side':        'sell' if side == 'LONG' else 'buy',
        'tradeSide':   'close',
        'orderType':   'market',
        'force':       'gtc',
    })

# ── POSITION MONITOR ──────────────────────────────────────────────────────────
def monitor_positions():
    """Nur Timeout-Check — TP/SL macht Bitget selbst."""
    positions = get_open_positions()
    open_syms = {p['symbol'] for p in positions}

    # Geschlossene aus _active entfernen
    for sym in list(_active.keys()):
        if sym not in open_syms:
            p = _active.pop(sym)
            age_min = (time.time() - p['time']) / 60
            log.info(f"📤 {sym} geschlossen nach {age_min:.0f}min (TP/SL/Bitget)")
            _cooldown[sym] = time.time()

    # Timeout Check
    for pos in positions:
        sym  = pos['symbol']
        side = pos.get('holdSide', '').upper()
        qty  = float(pos.get('total', 0))
        upnl = float(pos.get('unrealizedPL', 0))

        if sym not in _active:
            _active[sym] = {'side': side, 'qty': qty, 'time': time.time()}
            continue

        age_h = (time.time() - _active[sym]['time']) / 3600
        if age_h >= MAX_HOLD_H:
            log.warning(f"⏱️ Timeout {sym} nach {age_h:.1f}h — schließe")
            close_position_market(sym, side, qty)
            tg(f"⏱️ <b>Timeout</b> — {sym}\n{side} | PnL: ${upnl:.2f} | {age_h:.1f}h")
            _active.pop(sym, None)
            _cooldown[sym] = time.time()

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("JARVIS V6 gestartet")
    log.info(f"Leverage: {LEVERAGE}x | Risk: {RISK_PCT*100:.0f}% | TP: {TP_PCT*100:.1f}% | SL: {SL_PCT*100:.1f}%")
    log.info("=" * 50)
    tg(f"🚀 <b>JARVIS V6 gestartet</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"🎯 Ziel: $10 pro Trade\n"
       f"📊 TP: +{TP_PCT*100:.1f}% | SL: -{SL_PCT*100:.1f}%\n"
       f"⚡ Hebel: {LEVERAGE}x | Kapital/Trade: {RISK_PCT*100:.0f}%\n"
       f"🔒 TP/SL direkt auf Bitget gesetzt")

    scan = 0
    while True:
        scan += 1
        try:
            log.info(f"── Scan #{scan} [{datetime.now().strftime('%H:%M:%S')}] ──")

            # Positionen überwachen
            monitor_positions()
            open_count = len(get_open_positions())

            if open_count >= 1:
                log.info(f"⏸️ Position offen — warte auf TP/SL")
                time.sleep(SCAN_SEC)
                continue

            # Balance checken
            balance = get_balance()
            if balance < 5:
                log.warning(f"⚠️ Balance zu niedrig: ${balance:.2f}")
                time.sleep(SCAN_SEC)
                continue

            margin_size = round(balance * RISK_PCT, 2)
            expected_tp = round(margin_size * LEVERAGE * TP_PCT, 2)
            log.info(f"💰 Balance: ${balance:.2f} | Margin: ${margin_size:.2f} | Erwarteter Gewinn: ~${expected_tp:.2f}")

            # Coins scannen
            best = None
            best_score = 0

            for coin in COINS:
                # Cooldown check
                if coin in _cooldown:
                    elapsed = (time.time() - _cooldown[coin]) / 60
                    if elapsed < COOLDOWN_MIN:
                        continue

                signal, score, reason = analyse(coin)
                if signal and score >= 65:
                    log.info(f"  🎯 {coin}: {signal} Score={score} | {reason}")
                    if score > best_score:
                        best_score = score
                        best = {'symbol': coin, 'signal': signal, 'score': score, 'reason': reason}
                else:
                    log.info(f"  ⬜ {coin}: kein Signal (score={score})")

            # Besten Trade ausführen
            if best:
                sym    = best['symbol']
                side   = best['signal']
                score  = best['score']
                reason = best['reason']

                log.info(f"🟢 Trade: {sym} {side} Score={score}")
                ok, qty, entry, tp, sl = place_trade(sym, side, balance)

                if ok:
                    profit_est = round(qty * entry * TP_PCT, 2) if side == 'LONG' else round(qty * entry * TP_PCT, 2)
                    tg(
                        f"🟢 <b>TRADE ERÖFFNET</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📌 {sym} <b>{side}</b>\n"
                        f"💵 Entry: {entry:.4f}\n"
                        f"✅ TP: {tp:.4f} (+{TP_PCT*100:.1f}%)\n"
                        f"🛑 SL: {sl:.4f} (-{SL_PCT*100:.1f}%)\n"
                        f"💰 Margin: ${margin_size:.2f} × {LEVERAGE}x\n"
                        f"🎯 Ziel: ~+${profit_est:.2f}\n"
                        f"📊 Score: {score} | {reason}"
                    )
                    _active[sym] = {'side': side, 'qty': qty, 'time': time.time()}
                    _cooldown[sym] = time.time()
                    b44('BotTrade', {
                        'symbol': sym, 'side': side, 'entry_price': entry,
                        'size': qty, 'pnl': 0, 'status': 'filled',
                        'trade_time': datetime.utcnow().isoformat() + 'Z'
                    })
                else:
                    log.warning(f"Order fehlgeschlagen für {sym}")
            else:
                log.info("💤 Kein Setup mit Score ≥ 65 gefunden")

        except KeyboardInterrupt:
            log.info("Bot gestoppt")
            tg("⛔ JARVIS V6 gestoppt")
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
