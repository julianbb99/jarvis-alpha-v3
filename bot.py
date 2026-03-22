#!/usr/bin/env python3
"""
JARVIS ALPHA BOT V5 — Clean & Fast
────────────────────────────────────
Strategie: EMA Cross + RSI + Volumen
Ziel: $3-5 Profit pro Trade, schnell rein und raus
"""
import os, time, hmac, hashlib, base64, json, logging, requests
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('jarvis')

# ── KONFIGURATION ─────────────────────────────────────────────────────────────
API_KEY    = os.getenv('BITGET_API_KEY', '')
API_SECRET = os.getenv('BITGET_API_SECRET', '')
API_PASS   = os.getenv('BITGET_PASSPHRASE', '')
TG_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TG_CHAT    = os.getenv('TELEGRAM_CHAT_ID', '')
BASE44_KEY = os.getenv('BASE44_SERVICE_KEY', '')
BASE44_APP = os.getenv('BASE44_APP_ID', '69a75a817485663824cde2d6')

BASE_URL   = 'https://api.bitget.com'
BASE44_URL = f'https://api.base44.com/api/apps/{BASE44_APP}/entities'

LEVERAGE     = 25
RISK_PCT     = 0.30       # 30% Balance pro Trade — $5-10 Ziel
MAX_OPEN     = 1          # 1 Trade — voll konzentriert
SCAN_SEC     = 90         # alle 90s scannen
COOLDOWN_SEC = 900        # 15min Cooldown
TP_PCT       = 0.025      # 2.5% TP → bei 25x = ~62% auf Margin
SL_PCT       = 0.012      # 1.2% SL — kein frühzeitiger Stop
MAX_HOLD_H   = 1.0        # max 1h

# Coin-Liste — volatile Coins mit gutem Volumen
COINS = [
    'BTCUSDT','SOLUSDT','ETHUSDT','BNBUSDT',
    'DOGEUSDT','ADAUSDT','AVAXUSDT','DOTUSDT',
    'LINKUSDT','XRPUSDT','LTCUSDT','NEARUSDT',
    'TAOUSDT','SUIUSDT','APTUSDT','INJUSDT',
]

_cooldown = {}   # symbol → timestamp letzter Trade
_open_pos = {}   # symbol → {'side','entry','time','tp','sl'}

# ── UTILS ─────────────────────────────────────────────────────────────────────
def now_ms(): return int(time.time() * 1000)

def sign(ts, method, path, body=''):
    msg = f"{ts}{method}{path}{body}"
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()

def headers(method, path, body=''):
    ts = str(now_ms())
    return {
        'ACCESS-KEY':        API_KEY,
        'ACCESS-SIGN':       sign(ts, method, path, body),
        'ACCESS-TIMESTAMP':  ts,
        'ACCESS-PASSPHRASE': API_PASS,
        'Content-Type':      'application/json',
        'locale':            'en-US',
    }

def get(path, timeout=8):
    try:
        r = requests.get(BASE_URL + path, headers=headers('GET', path), timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"GET {path[:50]} Fehler: {e}")
        return {}

def post(path, body, timeout=8):
    bs = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=headers('POST', path, bs), data=bs, timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"POST {path[:50]} Fehler: {e}")
        return {}

def tg(msg, silent=False):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML',
                  'disable_notification': silent},
            timeout=5
        )
    except: pass

def b44_post(entity, data):
    if not BASE44_KEY: return
    try:
        requests.post(f'{BASE44_URL}/{entity}',
            headers={'x-api-key': BASE44_KEY, 'Content-Type': 'application/json'},
            json=data, timeout=5)
    except: pass

# ── MARKT DATEN ───────────────────────────────────────────────────────────────
def get_candles(symbol, tf='1H', limit=60):
    path = f'/api/v2/mix/market/candles?symbol={symbol}&granularity={tf}&limit={limit}&productType=USDT-FUTURES'
    d = get(path)
    if not d or d.get('code') != '00000': return []
    raw = d.get('data', [])
    # [ts, open, high, low, close, vol, volUsdt]
    candles = []
    for c in raw:
        try:
            candles.append({
                'o': float(c[1]), 'h': float(c[2]),
                'l': float(c[3]), 'c': float(c[4]),
                'v': float(c[5])
            })
        except: pass
    return candles

def ema(values, period):
    if len(values) < period: return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100
    rs = ag / al
    return 100 - (100 / (1 + rs))

# ── SIGNAL LOGIK ──────────────────────────────────────────────────────────────
def get_signal(symbol):
    """Gibt 'LONG', 'SHORT' oder None zurück."""
    candles = get_candles(symbol, '15m', 80)
    if len(candles) < 60: return None, 0

    closes = [c['c'] for c in candles]
    vols   = [c['v'] for c in candles]
    price  = closes[-1]

    # EMA 9 und 21
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    if len(e9) < 3 or len(e21) < 3: return None, 0

    # EMA Cross — aktuell und vorherige Kerze
    cross_up   = e9[-2] <= e21[-2] and e9[-1] > e21[-1]
    cross_down = e9[-2] >= e21[-2] and e9[-1] < e21[-1]

    # RSI
    r = rsi(closes[-20:])

    # Volumen — letzte 3 Kerzen vs Durchschnitt der letzten 20
    avg_vol = sum(vols[-20:-3]) / 17 if len(vols) >= 20 else 0
    vol_ok  = vols[-1] > avg_vol * 1.3

    # Trend (EMA 9 vs EMA 21 Richtung)
    trend_up   = e9[-1] > e21[-1]
    trend_down = e9[-1] < e21[-1]

    score = 0
    signal = None

    if cross_up and r < 65 and trend_up:
        score += 60
        if vol_ok:   score += 25
        if r < 50:   score += 15
        signal = 'LONG'
    elif cross_down and r > 35 and trend_down:
        score += 60
        if vol_ok:   score += 25
        if r > 50:   score += 15
        signal = 'SHORT'
    # Kein Cross aber starker Trend mit Volumen
    elif trend_up and vol_ok and r < 45 and e9[-1] > e9[-3]:
        score = 65
        signal = 'LONG'
    elif trend_down and vol_ok and r > 55 and e9[-1] < e9[-3]:
        score = 65
        signal = 'SHORT'

    return signal, score

# ── ACCOUNT ───────────────────────────────────────────────────────────────────
def get_balance():
    d = get('/api/v2/mix/account/accounts?productType=USDT-FUTURES')
    if not d or d.get('code') != '00000': return 0
    for acc in d.get('data', []):
        if acc.get('marginCoin') == 'USDT':
            return float(acc.get('available', 0))
    return 0

def get_positions():
    d = get('/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT')
    if not d or d.get('code') != '00000': return []
    return [p for p in d.get('data', []) if float(p.get('total', 0)) > 0]

def set_leverage(symbol):
    post('/api/v2/mix/account/set-leverage', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': 'long'
    })
    post('/api/v2/mix/account/set-leverage', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT', 'leverage': str(LEVERAGE), 'holdSide': 'short'
    })

def get_min_qty(symbol):
    d = get(f'/api/v2/mix/market/contracts?symbol={symbol}&productType=USDT-FUTURES')
    if not d or d.get('code') != '00000': return None, None
    for c in d.get('data', []):
        if c.get('symbol') == symbol:
            return float(c.get('minTradeNum', 1)), float(c.get('pricePlace', 2))
    return None, None

def round_price(price, dp):
    """Preis auf korrekte Dezimalstellen runden."""
    try:
        return round(price, int(dp))
    except:
        return price

def place_order(symbol, side, usdt_size, price):
    set_leverage(symbol)
    min_qty, price_dp = get_min_qty(symbol)
    if not min_qty: return None

    qty = round((usdt_size * LEVERAGE) / price, 4)
    qty = max(qty, min_qty)

    # TP und SL Preise berechnen
    if side == 'LONG':
        tp_price = round_price(price * (1 + TP_PCT), price_dp or 4)
        sl_price = round_price(price * (1 - SL_PCT), price_dp or 4)
    else:
        tp_price = round_price(price * (1 - TP_PCT), price_dp or 4)
        sl_price = round_price(price * (1 + SL_PCT), price_dp or 4)

    log.info(f"Order: {symbol} {side} qty={qty} TP={tp_price} SL={sl_price}")

    d = post('/api/v2/mix/order/place-order', {
        'symbol':                  symbol,
        'productType':             'USDT-FUTURES',
        'marginMode':              'isolated',
        'marginCoin':              'USDT',
        'size':                    str(qty),
        'side':                    'buy' if side == 'LONG' else 'sell',
        'tradeSide':               'open',
        'orderType':               'market',
        'force':                   'gtc',
        'presetTakeProfitPrice':   str(tp_price),
        'presetStopLossPrice':     str(sl_price),
    })
    if d.get('code') == '00000':
        oid = d.get('data', {}).get('orderId')
        log.info(f"✅ Order platziert: {oid} | TP={tp_price} SL={sl_price}")
        return oid
    else:
        log.warning(f"Order Fehler {symbol}: {d.get('msg')} | Code: {d.get('code')}")
        return None

def close_position(symbol, side, qty):
    post('/api/v2/mix/order/place-order', {
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

# ── POSITION MANAGEMENT ───────────────────────────────────────────────────────
def check_positions():
    """TP/SL/Timeout für offene Positionen."""
    positions = get_positions()
    open_syms = {p['symbol'] for p in positions}

    # Geschlossene aus _open_pos entfernen
    closed = [s for s in list(_open_pos.keys()) if s not in open_syms]
    for sym in closed:
        p = _open_pos.pop(sym)
        elapsed = (time.time() - p['time']) / 60
        log.info(f"📤 {sym} geschlossen nach {elapsed:.0f}min")
        _cooldown[sym] = time.time()

    for pos in positions:
        sym   = pos['symbol']
        side  = pos.get('holdSide', '').upper()
        mark  = float(pos.get('markPrice', 0))
        entry = float(pos.get('openPriceAvg', 0))
        qty   = float(pos.get('total', 0))
        upnl  = float(pos.get('unrealizedPL', 0))

        if sym not in _open_pos:
            # Neu erkannte Position
            tp = entry * (1 + TP_PCT) if side == 'LONG' else entry * (1 - TP_PCT)
            sl = entry * (1 - SL_PCT) if side == 'LONG' else entry * (1 + SL_PCT)
            _open_pos[sym] = {'side': side, 'entry': entry, 'time': time.time(), 'tp': tp, 'sl': sl, 'qty': qty}
            log.info(f"📥 {sym} {side} erkannt: entry={entry:.4f} TP={tp:.4f} SL={sl:.4f}")
            continue

        p = _open_pos[sym]
        age_h = (time.time() - p['time']) / 3600

        # TP Hit
        tp_hit = (side == 'LONG' and mark >= p['tp']) or (side == 'SHORT' and mark <= p['tp'])
        # SL Hit
        sl_hit = (side == 'LONG' and mark <= p['sl']) or (side == 'SHORT' and mark >= p['sl'])
        # Timeout
        timeout = age_h >= MAX_HOLD_H

        if tp_hit:
            close_position(sym, side, qty)
            pnl_est = upnl
            log.info(f"✅ TP: {sym} {side} +${pnl_est:.2f}")
            tg(f"✅ <b>TP HIT</b> — {sym}\n{side} | +${pnl_est:.2f} | {age_h*60:.0f}min")
            b44_post('BotTrade', {'symbol': sym, 'side': side, 'entry_price': entry,
                'size': qty, 'pnl': pnl_est, 'status': 'tp', 'trade_time': datetime.utcnow().isoformat()+'Z'})
        elif sl_hit:
            close_position(sym, side, qty)
            log.info(f"❌ SL: {sym} {side} ${upnl:.2f}")
            tg(f"❌ <b>SL HIT</b> — {sym}\n{side} | ${upnl:.2f}")
            b44_post('BotTrade', {'symbol': sym, 'side': side, 'entry_price': entry,
                'size': qty, 'pnl': upnl, 'status': 'sl', 'trade_time': datetime.utcnow().isoformat()+'Z'})
        elif timeout:
            close_position(sym, side, qty)
            log.info(f"⏱️ Timeout: {sym} ${upnl:.2f}")
            tg(f"⏱️ <b>Timeout</b> — {sym}\n{side} | ${upnl:.2f} | {age_h:.1f}h")
            b44_post('BotTrade', {'symbol': sym, 'side': side, 'entry_price': entry,
                'size': qty, 'pnl': upnl, 'status': 'timeout', 'trade_time': datetime.utcnow().isoformat()+'Z'})

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def run():
    log.info("🚀 JARVIS V5 gestartet")
    tg("🚀 <b>JARVIS V5.1 gestartet</b>\n⚡ 1 Trade gleichzeitig | 30% Kapital\n🎯 TP 2.5% | SL 1.2% | 25x")

    scan = 0
    while True:
        scan += 1
        log.info(f"─── Scan #{scan} [{datetime.now().strftime('%H:%M:%S')}] ───")

        try:
            balance = get_balance()
            if balance <= 0:
                log.warning("⚠️ Kein Guthaben")
                time.sleep(SCAN_SEC)
                continue

            trade_size = round(balance * RISK_PCT, 2)
            log.info(f"💰 Balance: ${balance:.2f} | Trade: ${trade_size:.2f}")

            # Positionen checken (TP/SL/Timeout)
            check_positions()
            open_count = len(_open_pos)
            log.info(f"📂 Offene Positionen: {open_count}/{MAX_OPEN}")

            if open_count >= MAX_OPEN:
                log.info("⏸️ Max Positionen erreicht")
                time.sleep(SCAN_SEC)
                continue

            # Coins scannen
            signals = []
            for coin in COINS:
                symbol = coin if coin.endswith('USDT') else coin + 'USDT'

                # Cooldown check
                if symbol in _cooldown:
                    if time.time() - _cooldown[symbol] < COOLDOWN_SEC:
                        remain = int((COOLDOWN_SEC - (time.time() - _cooldown[symbol])) / 60)
                        log.info(f"  ⏳ {symbol}: Cooldown noch {remain}min")
                        continue

                # Bereits offen
                if symbol in _open_pos:
                    continue

                signal, score = get_signal(symbol)
                if signal and score >= 60:
                    price_d = get(f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES')
                    price   = float(price_d.get('data', [{}])[0].get('lastPr', 0)) if price_d.get('code') == '00000' else 0
                    if price > 0:
                        signals.append({'symbol': symbol, 'signal': signal, 'score': score, 'price': price})
                        log.info(f"  🎯 {symbol}: {signal} Score={score} Price={price:.4f}")

            # Bestes Signal traden
            if signals and open_count < MAX_OPEN:
                signals.sort(key=lambda x: x['score'], reverse=True)
                slots = MAX_OPEN - open_count
                for sig in signals[:slots]:
                    sym    = sig['symbol']
                    side   = sig['signal']
                    price  = sig['price']
                    oid    = place_order(sym, side, trade_size, price)
                    if oid:
                        log.info(f"✅ Trade eröffnet: {sym} {side} ${trade_size:.2f}")
                        tg(f"🟢 <b>TRADE ERÖFFNET</b>\n{sym} <b>{side}</b>\n💰 Margin: ${trade_size:.2f} | Hebel: {LEVERAGE}x\n🎯 TP: +2.5% | SL: -1.2%\n📊 Score: {sig['score']}")
                        _cooldown[sym] = time.time()
                        b44_post('BotTrade', {'symbol': sym, 'side': side, 'entry_price': price,
                            'size': trade_size, 'pnl': 0, 'status': 'filled',
                            'trade_time': datetime.utcnow().isoformat()+'Z'})
            else:
                log.info(f"💤 Keine Signale (Score < 60)")

        except KeyboardInterrupt:
            tg("⛔ JARVIS V5 gestoppt")
            break
        except Exception as e:
            log.error(f"Fehler: {e}", exc_info=True)

        time.sleep(SCAN_SEC)

if __name__ == '__main__':
    while True:
        try:
            run()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Kritischer Fehler: {e}")
            time.sleep(15)
