#!/usr/bin/env python3
"""
JARVIS ALPHA BOT V3 — Self-Learning Multi-Coin Trading Bot
──────────────────────────────────────────────────────────
• Scannt 30 Coins alle 5 Min (1H Candles)
• RSI + BB + EMA50 + Volumen Strategie
• Trade Memory: Speichert jeden Trade mit Marktbedingungen
• Self-Learning: Passt Parameter nach Win/Loss-Rate automatisch an
• Marktregime-Erkennung: trending/ranging/volatile/dead
• Telegram Notifications
"""

import os, requests, json, time, hmac, hashlib, base64
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
BITGET_API_KEY    = os.getenv('BITGET_API_KEY', '')
BITGET_SECRET     = os.getenv('BITGET_API_SECRET', '')
BITGET_PASSPHRASE = os.getenv('BITGET_PASSPHRASE', '')
TELEGRAM_TOKEN    = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID  = os.getenv('NOTIFY_CHAT_ID', '')

BASE_URL       = 'https://api.bitget.com'
LEVERAGE       = 20
RISK_PCT       = 0.10        # 10% des Kontos pro Trade
MAX_OPEN       = 3
SCAN_INTERVAL  = 120         # 2 Min
TIMEFRAME      = '1H'
MEMORY_FILE    = '/tmp/trade_memory.json'
PARAMS_FILE    = '/tmp/learned_params.json'

BLACKLIST = ['PAXGUSDT', 'XAUTUSDT', 'LYNUSDT', 'XAUUSDT']

# ── BASE44 DASHBOARD ──────────────────────────────────────────────────────────
DASHBOARD_URL    = 'https://jarvis-24cde2d6.base44.app/functions/saveScanResults'
DASHBOARD_SECRET = os.getenv('BOT_WEBHOOK_SECRET', 'jarvis2026')

def push_scan_to_dashboard(results, scan_time):
    """Schreibt Scan-Ergebnisse ans Base44 Dashboard"""
    if not results:
        return
    try:
        payload = {
            'scan_time': scan_time,
            'results': [{
                'coin':      r.get('name','').replace('USDT',''),
                'score':     r.get('score', 0),
                'signal':    r.get('signal', 'NONE'),
                'rsi':       round(float(r.get('rsi') or 0), 1),
                'regime':    r.get('regime', 'unknown'),
                'price':     r.get('price', 0),
                'bb_dist':   round(float(r.get('bb_dist') or 0), 4),
                'volume_ok': bool(r.get('vol_ok', False)),
                'traded':    False,
                'scan_time': scan_time,
            } for r in results[:30]]
        }
        resp = requests.post(
            DASHBOARD_URL,
            headers={'Content-Type': 'application/json', 'x-bot-secret': DASHBOARD_SECRET},
            json=payload, timeout=8
        )
        if resp.status_code == 200:
            print(f"  📡 {len(results)} Coins → Dashboard ✅")
        else:
            print(f"  [Dashboard] Fehler: {resp.status_code} {resp.text[:80]}")
    except Exception as e:
        print(f"  [Dashboard] Fehler: {e}")

# ── MEMORY SYSTEM ─────────────────────────────────────────────────────────────
def load_memory():
    try:
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    except:
        return {'trades': []}

def save_memory(mem):
    try:
        with open(MEMORY_FILE, 'w') as f:
            json.dump(mem, f, indent=2)
    except Exception as e:
        print(f"[Memory] Save Error: {e}")

def load_params():
    defaults = {
        'min_score':     55,
        'rsi_long':      30,
        'rsi_short':     70,
        'rsi_extreme_l': 28,
        'rsi_extreme_s': 72,
        'tp_atr_mult':   1.5,
        'sl_atr_mult':   2.0,
        'min_atr_pct':   0.3,
        'bb_tight':      0.005,
        'bb_near':       0.015,
        'regime_scores': {
            'trending_up':   {'score_bonus': 5,   'tp_mult': 1.8, 'sl_mult': 1.8},
            'trending_down': {'score_bonus': 5,   'tp_mult': 1.8, 'sl_mult': 1.8},
            'ranging':       {'score_bonus': 10,  'tp_mult': 1.3, 'sl_mult': 1.5},
            'volatile':      {'score_bonus': -5,  'tp_mult': 2.0, 'sl_mult': 2.5},
            'dead':          {'score_bonus': -15, 'tp_mult': 1.2, 'sl_mult': 1.2},
        },
        'version': 1,
        'total_trades': 0,
        'win_rate': 0.0,
        'last_update': None
    }
    try:
        with open(PARAMS_FILE, 'r') as f:
            saved = json.load(f)
            defaults.update(saved)
    except:
        pass
    return defaults

def save_params(p):
    try:
        with open(PARAMS_FILE, 'w') as f:
            json.dump(p, f, indent=2)
    except:
        pass

# ── SELF-LEARNING ─────────────────────────────────────────────────────────────
def detect_market_regime(closes, hi, lo):
    """Erkennt aktuelles Marktregime"""
    if len(closes) < 50:
        return 'ranging'

    def ema(data, p):
        e = data[0]; k = 2/(p+1); result = [e]
        for v in data[1:]:
            e = v*k + e*(1-k); result.append(e)
        return result

    ema20 = ema(closes[-50:], 20)
    ema50 = ema(closes[-50:], 50)
    e20 = ema20[-1]; e50 = ema50[-1]; price = closes[-1]

    trs = [max(hi[i]-lo[i], abs(hi[i]-closes[i-1]), abs(lo[i]-closes[i-1]))
           for i in range(max(1, len(closes)-20), len(closes))]
    atr = sum(trs)/len(trs) if trs else 0
    atr_pct = atr/price*100

    trend_str = abs(e20-e50)/e50*100

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

def learn_from_trades(mem, params):
    """
    Analysiert Trade-History und passt Parameter automatisch an.
    Läuft nach jedem abgeschlossenen Trade.
    """
    trades  = mem.get('trades', [])
    closed  = [t for t in trades if t.get('status') in ['win', 'loss']]

    if len(closed) < 10:
        return params  # Zu wenig Daten zum Lernen

    recent = closed[-20:]  # Letzte 20 Trades
    wins   = [t for t in recent if t['status'] == 'win']
    wr     = len(wins) / len(recent)

    print(f"\n🧠 [LEARNING] {len(recent)} Trades analysiert | WR: {wr*100:.1f}%")

    # Win-Rate nach Regime analysieren
    regime_stats = {}
    for t in recent:
        reg = t.get('regime', 'unknown')
        if reg not in regime_stats:
            regime_stats[reg] = {'wins': 0, 'total': 0}
        regime_stats[reg]['total'] += 1
        if t['status'] == 'win':
            regime_stats[reg]['wins'] += 1

    for reg, s in regime_stats.items():
        r_wr = s['wins']/s['total'] if s['total'] > 0 else 0
        print(f"   {reg:15}: WR {r_wr*100:.0f}% ({s['total']} Trades)")
        if reg in params['regime_scores'] and s['total'] >= 3:
            if r_wr > 0.65:
                # Regime funktioniert gut → mehr Bonus
                params['regime_scores'][reg]['score_bonus'] = min(20, params['regime_scores'][reg]['score_bonus'] + 2)
                print(f"   ✅ {reg} Bonus erhöht → {params['regime_scores'][reg]['score_bonus']}")
            elif r_wr < 0.40:
                # Regime funktioniert schlecht → mehr Malus / weniger Trades
                params['regime_scores'][reg]['score_bonus'] = max(-25, params['regime_scores'][reg]['score_bonus'] - 3)
                print(f"   ⚠️ {reg} Bonus gesenkt → {params['regime_scores'][reg]['score_bonus']}")

    # Gesamt-Score-Schwelle anpassen
    if wr > 0.65:
        params['min_score'] = max(45, params['min_score'] - 1)
        print(f"   📉 Min-Score gesenkt: {params['min_score']} (WR gut)")
    elif wr < 0.40:
        params['min_score'] = min(75, params['min_score'] + 2)
        print(f"   📈 Min-Score erhöht: {params['min_score']} (WR schlecht)")

    params['win_rate']     = round(wr * 100, 1)
    params['total_trades'] = len(closed)
    params['last_update']  = datetime.now().strftime('%Y-%m-%d %H:%M')
    params['version']     += 1

    save_params(params)

    tg(f"🧠 <b>Bot hat gelernt!</b> (Brain v{params['version']})\n"
       f"📊 WR letzte 20 Trades: {wr*100:.1f}%\n"
       f"🎯 Neuer Min-Score: {params['min_score']}\n"
       f"📋 Regime-Anpassungen: {', '.join(f'{k}:{v[\"score_bonus\"]:+d}' for k,v in params['regime_scores'].items())}")

    return params

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] {msg[:100]}")
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'},
            timeout=5
        )
    except:
        pass

# ── BITGET AUTH ────────────────────────────────────────────────────────────────
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
    try:
        r = requests.get(BASE_URL + path, headers=hdrs('GET', path), timeout=10)
        return r.json()
    except Exception as e:
        print(f"GET Error: {e}"); return {}

def api_post(path, body):
    bs = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=hdrs('POST', path, bs), data=bs, timeout=10)
        return r.json()
    except Exception as e:
        print(f"POST Error: {e}"); return {}

# ── MARKTDATEN ────────────────────────────────────────────────────────────────
def get_candles(symbol, gran=TIMEFRAME, limit=100):
    url = f'/api/v2/mix/market/candles?symbol={symbol}&productType=USDT-FUTURES&granularity={gran}&limit={limit}'
    try:
        r = requests.get(BASE_URL + url, timeout=8)
        data = sorted(r.json().get('data', []), key=lambda x: int(x[0]))
        if len(data) < 30: return None
        return {
            'hi': [float(c[2]) for c in data],
            'lo': [float(c[3]) for c in data],
            'cl': [float(c[4]) for c in data],
            'vo': [float(c[5]) for c in data],
        }
    except:
        return None

def get_liquid_coins():
    try:
        r = requests.get(f'{BASE_URL}/api/v2/mix/market/tickers?productType=USDT-FUTURES', timeout=8)
        coins = []
        for c in r.json().get('data', []):
            vol = float(c.get('usdtVolume', 0))
            sym = c.get('symbol', '')
            if vol >= 5e6 and sym not in BLACKLIST and sym.endswith('USDT'):
                coins.append((sym, vol))
        coins.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in coins[:30]]
    except:
        return []

# ── INDIKATOREN ───────────────────────────────────────────────────────────────
def calc_rsi(cl, p=14):
    if len(cl) < p+2: return []
    diffs = [cl[i]-cl[i-1] for i in range(1, len(cl))]
    ag = sum(max(d,0) for d in diffs[:p])/p
    al = sum(max(-d,0) for d in diffs[:p])/p
    vals = [None]*p
    for i in range(p, len(diffs)):
        g=max(diffs[i],0); l=max(-diffs[i],0)
        ag=(ag*(p-1)+g)/p; al=(al*(p-1)+l)/p
        rs=ag/al if al else 100
        vals.append(100-(100/(1+rs)))
    vals.append(None)
    return vals

def calc_atr(hi, lo, cl, p=14):
    tr = [hi[0]-lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
    v = sum(tr[:p])/p; r = [None]*(p-1); r.append(v)
    for i in range(p, len(tr)):
        v=(v*(p-1)+tr[i])/p; r.append(v)
    return r

def calc_bb(cl, p=20, k=2.0):
    mid=[None]*(p-1); up=[None]*(p-1); dn=[None]*(p-1)
    for i in range(p-1, len(cl)):
        w=cl[i-p+1:i+1]; m=sum(w)/p
        std=(sum((x-m)**2 for x in w)/p)**0.5
        mid.append(m); up.append(m+k*std); dn.append(m-k*std)
    return mid, up, dn

def calc_ema(data, p):
    if not data: return []
    e=data[0]; k=2/(p+1); r=[e]
    for v in data[1:]:
        e=v*k+e*(1-k); r.append(e)
    return r

# ── COIN ANALYSE ──────────────────────────────────────────────────────────────
def analyze_coin(symbol, params):
    d = get_candles(symbol, TIMEFRAME, 100)
    if not d: return None

    cl=d['cl']; hi=d['hi']; lo=d['lo']
    n=len(cl)
    if n < 50: return None

    rsi_v      = calc_rsi(cl)
    atr_v      = calc_atr(hi, lo, cl)
    _, bbu, bbl = calc_bb(cl)
    ema50_v    = calc_ema(cl, 50)

    i=n-1; ip=n-2
    r     = rsi_v[i]   if rsi_v  and i < len(rsi_v)  else None
    rp    = rsi_v[ip]  if rsi_v  and ip < len(rsi_v) else None
    at    = atr_v[i]   if atr_v  and i < len(atr_v)  else None
    bbu_v = bbu[i]     if i < len(bbu) else None
    bbl_v = bbl[i]     if i < len(bbl) else None
    e50   = ema50_v[-1] if ema50_v else None

    if None in [r, rp, at, bbu_v, bbl_v, e50]: return None

    price   = cl[i]
    atr_pct = at/price*100
    regime  = detect_market_regime(cl, hi, lo)

    reg_cfg   = params['regime_scores'].get(regime, {'score_bonus': 0, 'tp_mult': 1.5, 'sl_mult': 2.0})
    tp_mult   = reg_cfg['tp_mult']
    sl_mult   = reg_cfg['sl_mult']
    reg_bonus = reg_cfg['score_bonus']

    score=0; signal=None; reasons=[]

    # Signal 1: RSI Crossover (stärkstes Signal)
    if rp < params['rsi_long'] and r >= params['rsi_long']:
        score+=45; signal='LONG';  reasons.append(f'RSI Bounce {rp:.0f}→{r:.0f}')
    elif rp > params['rsi_short'] and r <= params['rsi_short']:
        score+=45; signal='SHORT'; reasons.append(f'RSI Drop {rp:.0f}→{r:.0f}')
    elif r < params['rsi_extreme_l']:
        score+=25; signal='LONG';  reasons.append(f'RSI oversold {r:.0f}')
    elif r > params['rsi_extreme_s']:
        score+=25; signal='SHORT'; reasons.append(f'RSI overbought {r:.0f}')
    else:
        return None

    # Signal 2: Bollinger Band Touch
    if signal == 'LONG':
        dist = (price - bbl_v) / price
        if dist < params['bb_tight']:  score+=30; reasons.append('BB-Low Touch')
        elif dist < params['bb_near']: score+=15; reasons.append('BB-Low Nähe')
    else:
        dist = (bbu_v - price) / price
        if dist < params['bb_tight']:  score+=30; reasons.append('BB-High Touch')
        elif dist < params['bb_near']: score+=15; reasons.append('BB-High Nähe')

    # Signal 3: EMA50 Filter
    if signal == 'LONG'  and price > e50: score+=10; reasons.append('über EMA50')
    if signal == 'SHORT' and price < e50: score+=10; reasons.append('unter EMA50')

    # Signal 4: Volumen-Bestätigung
    if len(d['vo']) >= 10:
        avg_vol = sum(d['vo'][-10:-1]) / 9
        if d['vo'][-1] > avg_vol * 1.3:
            score+=10; reasons.append('Volumen↑')

    # Signal 5: Volatilität (brauchen Bewegung für Profit)
    if atr_pct > 2.0:    score+=15; reasons.append(f'Vola {atr_pct:.1f}%')
    elif atr_pct > 1.0:  score+=8;  reasons.append(f'Vola {atr_pct:.1f}%')
    elif atr_pct < params['min_atr_pct']:
        return None  # Zu wenig Bewegung

    # Regime-Bonus/Malus (vom selbstlernenden System)
    if reg_bonus != 0:
        score += reg_bonus
        reasons.append(f'Regime:{regime}({reg_bonus:+d})')

    # TP/SL mit Regime-angepassten Multiplikatoren
    if signal == 'LONG':
        tp = price + at * tp_mult
        sl = price - at * sl_mult
    else:
        tp = price - at * tp_mult
        sl = price + at * sl_mult

    rr = abs(tp-price) / abs(sl-price) if abs(sl-price) > 0 else 0

    return {
        'symbol':  symbol,
        'name':    symbol.replace('USDT', ''),
        'signal':  signal,
        'score':   score,
        'price':   price,
        'rsi':     r,
        'rsi_prev':rp,
        'atr':     at,
        'atr_pct': atr_pct,
        'tp':      tp,
        'sl':      sl,
        'rr':      rr,
        'regime':  regime,
        'reasons': ' + '.join(reasons),
    }

# ── KONTO & ORDERS ────────────────────────────────────────────────────────────
def get_balance():
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
            'symbol': symbol, 'productType': 'USDT-FUTURES',
            'marginCoin': 'USDT', 'leverage': str(lev), 'holdSide': side
        })

def place_order(symbol, side, size_usdt, tp, sl, price):
    qty = round(size_usdt * LEVERAGE / price, 4)
    if qty <= 0: return None
    set_leverage(symbol, LEVERAGE)
    order_side = 'buy' if side == 'LONG' else 'sell'
    body = {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'marginMode': 'isolated', 'marginCoin': 'USDT',
        'size': str(qty), 'side': order_side,
        'tradeSide': 'open', 'orderType': 'market',
        'presetStopSurplusPrice': str(round(tp, 6)),
        'presetStopLossPrice':    str(round(sl, 6)),
    }
    return api_post('/api/v2/mix/order/place-order', body)

# ── GESCHLOSSENE TRADES ERKENNEN → LERNEN ─────────────────────────────────────
def check_closed_trades(mem, params, open_symbols_prev, open_symbols_now):
    closed_syms = open_symbols_prev - open_symbols_now
    if not closed_syms:
        return params

    trades  = mem.get('trades', [])
    changed = False

    for sym in closed_syms:
        for t in reversed(trades):
            if t.get('symbol') == sym and t.get('status') == 'open':
                try:
                    r = requests.get(
                        f'{BASE_URL}/api/v2/mix/market/ticker?symbol={sym}&productType=USDT-FUTURES',
                        timeout=5
                    ).json()
                    current_price = float(r['data'][0]['lastPr'])
                    entry  = t['entry_price']
                    signal = t['signal']

                    pnl_pct = (current_price-entry)/entry*100 if signal=='LONG' else (entry-current_price)/entry*100

                    t['status']     = 'win' if pnl_pct > 0 else 'loss'
                    t['exit_price'] = current_price
                    t['pnl_pct']    = round(pnl_pct, 2)
                    t['closed_at']  = datetime.now().strftime('%Y-%m-%d %H:%M')

                    icon = '✅ WIN' if t['status'] == 'win' else '❌ LOSS'
                    print(f"\n  {icon}: {sym} | PnL: {pnl_pct:+.2f}% | Regime war: {t.get('regime','?')}")
                    tg(f"{icon} <b>{sym.replace('USDT','')}</b> {signal}\n"
                       f"Entry: ${entry:.4f} → Exit: ${current_price:.4f}\n"
                       f"PnL: {pnl_pct:+.2f}% | Regime: {t.get('regime','?')}")
                    changed = True
                except Exception as e:
                    print(f"  [Close Check Error] {sym}: {e}")
                break

    if changed:
        save_memory(mem)
        params = learn_from_trades(mem, params)  # 🧠 Lernen!

    return params

# ── HAUPTSCHLEIFE ─────────────────────────────────────────────────────────────
def run():
    print(f"\n{'='*60}")
    print(f"  🤖 JARVIS ALPHA BOT V3 — Self-Learning")
    print(f"  {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"  Leverage: {LEVERAGE}x | Risk: {RISK_PCT*100:.0f}%/Trade | MaxPos: {MAX_OPEN}")
    print(f"{'='*60}\n")

    mem    = load_memory()
    params = load_params()

    closed_all = [t for t in mem['trades'] if t.get('status') in ['win','loss']]
    wins_all   = len([t for t in closed_all if t['status']=='win'])
    wr_all     = (wins_all/len(closed_all)*100) if closed_all else 0

    tg(f"🤖 <b>JARVIS ALPHA V3 gestartet</b>\n"
       f"🧠 Brain v{params['version']} | Trades gesamt: {len(closed_all)}\n"
       f"📊 Win-Rate: {wr_all:.1f}% | Min-Score: {params['min_score']}\n"
       f"⚙️ {LEVERAGE}x Leverage | {RISK_PCT*100:.0f}% Risiko/Trade\n"
       f"🔍 Scanning alle {SCAN_INTERVAL//60} Min...")

    open_symbols_prev = set()
    scan_count = 0

    while True:
        try:
            scan_count += 1
            now = datetime.now().strftime('%H:%M:%S')
            print(f"\n[{now}] ─── Scan #{scan_count} ───")

            # Konto
            balance = get_balance()
            if balance == 0:
                print("  ⚠️ Konto leer oder nicht erreichbar")
                time.sleep(60); continue

            trade_size = round(balance * RISK_PCT, 2)
            print(f"  💰 Balance: ${balance:.2f} | TradeSize: ${trade_size:.2f} | MinScore: {params['min_score']}")

            # Offene Positionen
            positions    = get_open_positions()
            open_symbols = {p['symbol'] for p in positions}
            open_count   = len(positions)
            print(f"  📂 Positionen: {open_count}/{MAX_OPEN} — {', '.join([s.replace('USDT','') for s in open_symbols]) or 'keine'}")

            # Geschlossene Trades erkennen → Bot lernt!
            params = check_closed_trades(mem, params, open_symbols_prev, open_symbols)
            open_symbols_prev = open_symbols.copy()

            if open_count >= MAX_OPEN:
                print(f"  ⏸️ Max Positionen erreicht — überspringe Scan")
                time.sleep(SCAN_INTERVAL); continue

            # Coins scannen
            coins = get_liquid_coins()
            print(f"  🔍 Scanne {len(coins)} Coins...")

            signals = []
            all_scan_results = []
            scan_time = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')
            for sym in coins:
                if sym in open_symbols: continue
                result = analyze_coin(sym, params)
                if result:
                    all_scan_results.append(result)
                    if result['score'] >= params['min_score']:
                        signals.append(result)
                time.sleep(0.15)

            # Scan-Ergebnisse ans Dashboard schicken
            push_scan_to_dashboard(all_scan_results, scan_time)

            signals.sort(key=lambda x: x['score'], reverse=True)

            if not signals:
                print(f"  ⚪ Keine Signale (Score ≥ {params['min_score']}) — warte...")
                time.sleep(SCAN_INTERVAL); continue

            print(f"\n  🎯 {len(signals)} Signal(e):")
            for s in signals[:5]:
                print(f"     {s['signal']:5} {s['name']:8} Score:{s['score']:3} | RSI:{s['rsi']:.0f} | {s['regime']:12} | {s['reasons'][:55]}")

            # Trades platzieren
            slots = MAX_OPEN - open_count
            for sig in signals[:slots]:
                if trade_size < 3:
                    print(f"  ⚠️ Balance zu niedrig (${balance:.2f})")
                    tg(f"⚠️ Balance zu niedrig: ${balance:.2f} — pausiert")
                    break

                print(f"\n  🚀 {sig['signal']} {sig['name']} @ ${sig['price']:.4f}")
                print(f"     Score:{sig['score']} | Regime:{sig['regime']} | R/R:{sig['rr']:.1f}")
                print(f"     TP:${sig['tp']:.4f} | SL:${sig['sl']:.4f}")

                order_id = 'DEMO'
                success  = True

                if BITGET_API_KEY:
                    res      = place_order(sig['symbol'], sig['signal'], trade_size, sig['tp'], sig['sl'], sig['price'])
                    success  = res.get('code') == '00000' if res else False
                    order_id = res.get('data', {}).get('orderId', '?') if res else '?'
                    if not success:
                        print(f"  ❌ Order Fehler: {res}")

                direction = '📈 LONG' if sig['signal'] == 'LONG' else '📉 SHORT'
                tg(f"{direction} <b>{sig['name']}</b>  {'✅' if success else '❌'}\n"
                   f"💰 ${trade_size:.2f} × {LEVERAGE}x | Score: {sig['score']}\n"
                   f"🎯 Entry: ${sig['price']:.4f}\n"
                   f"✅ TP: ${sig['tp']:.4f}  ❌ SL: ${sig['sl']:.4f}\n"
                   f"📊 R/R: {sig['rr']:.1f} | Regime: {sig['regime']}\n"
                   f"💡 {sig['reasons'][:60]}")

                if success:
                    mem['trades'].append({
                        'symbol':      sig['symbol'],
                        'signal':      sig['signal'],
                        'entry_price': sig['price'],
                        'tp':          sig['tp'],
                        'sl':          sig['sl'],
                        'size_usdt':   trade_size,
                        'score':       sig['score'],
                        'regime':      sig['regime'],
                        'entry_rsi':   sig['rsi'],
                        'atr_pct':     sig['atr_pct'],
                        'rr':          sig['rr'],
                        'reasons':     sig['reasons'],
                        'order_id':    order_id,
                        'status':      'open',
                        'opened_at':   datetime.now().strftime('%Y-%m-%d %H:%M'),
                    })
                    save_memory(mem)
                    open_symbols.add(sig['symbol'])

                time.sleep(1)

            # Alle 20 Scans: Status-Report
            if scan_count % 20 == 0:
                cl = [t for t in mem['trades'] if t.get('status') in ['win','loss']]
                wn = len([t for t in cl if t['status']=='win'])
                wr = (wn/len(cl)*100) if cl else 0
                tg(f"📊 <b>Status Report</b> (Scan #{scan_count})\n"
                   f"🧠 Brain v{params['version']} | WR: {wr:.1f}%\n"
                   f"📈 Trades: {len(cl)} | Min-Score: {params['min_score']}\n"
                   f"💰 Balance: ${balance:.2f}")

        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback; traceback.print_exc()
            time.sleep(30)
            continue

        print(f"\n  ⏳ Nächster Scan in {SCAN_INTERVAL//60} Min...")
        time.sleep(SCAN_INTERVAL)


async def run_forever():
    """Auto-Reconnect bei Crash"""
    import asyncio
    attempt = 0
    while True:
        attempt += 1
        print(f"\n🔁 [Versuch {attempt}] Bot startet...")
        try:
            run()
        except Exception as e:
            print(f"💥 Fehler: {e}")
            print("↩️ Neustart in 15s...")
            time.sleep(15)


if __name__ == '__main__':
    attempt = 0
    while True:
        attempt += 1
        print(f"\n🔁 [Start #{attempt}]")
        try:
            run()
        except Exception as e:
            print(f"💥 Crash: {e}")
            import traceback; traceback.print_exc()
            print("↩️ Neustart in 15s...")
            time.sleep(15)
