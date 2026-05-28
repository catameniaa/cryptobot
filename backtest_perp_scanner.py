#!/usr/bin/env python3
import json, time, requests, sys, argparse
from datetime import datetime, timezone
from pathlib import Path
try:
    import ccxt, numpy as np, pandas as pd
except ImportError:
    sys.exit("Eksik kutuphane")

OUT = Path(__file__).parent
STRONG = 2
DAMPEN = 0.5

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + g/l.replace(0, float('nan')))

def bollinger(s, n=20, m=2):
    mid = s.rolling(n).mean()
    std = s.rolling(n).std()
    return mid - m*std, mid + m*std

def atr(df, n=10):
    h = df['high']
    l = df['low']
    c = df['close'].shift(1)
    tr = pd.concat([h-l, (h-c).abs(), (l-c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def supertrend(df, n=10, m=3):
    hl2 = (df['high'] + df['low']) / 2
    a = atr(df, n)
    ub = hl2 + m*a
    lb = hl2 - m*a
    st = [float('nan')] * len(df)
    d = [1] * len(df)
    for i in range(1, len(df)):
        p = st[i-1] if st[i-1] == st[i-1] else lb.iloc[i]
        c = df['close'].iloc[i]
        if c > p:
            d[i] = 1
            st[i] = max(lb.iloc[i], p) if d[i-1] == 1 else lb.iloc[i]
        else:
            d[i] = -1
            st[i] = min(ub.iloc[i], p) if d[i-1] == -1 else ub.iloc[i]
    return pd.Series(d, index=df.index)

def get_ex():
    return ccxt.binance({
        'options': {'defaultType': 'future'},
        'enableRateLimit': True
    })

def fetch_ohlcv(ex, sym, tf='4h', lim=300):
    try:
        raw = ex.fetch_ohlcv(sym, tf, limit=lim)
        if not raw or len(raw) < 60:
            return None
        df = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        return df.set_index('ts').astype(float)
    except:
        return None

def fetch_funding(sym):
    bn = sym.replace('/', '').replace(':USDT', '')
    try:
        r = requests.get(
            'https://fapi.binance.com/fapi/v1/fundingRate',
            params={'symbol': bn, 'limit': 1},
            timeout=8
        )
        d = r.json()
        return float(d[-1]['fundingRate']) * 100 * 3 * 365 if d else None
    except:
        return None

def fetch_cvd(sym, tf='4h', lim=100):
    bn = sym.replace('/', '').replace(':USDT', '')
    try:
        r = requests.get(
            'https://fapi.binance.com/fapi/v1/klines',
            params={'symbol': bn, 'interval': tf, 'limit': lim},
            timeout=10
        )
        data = r.json()
        cvd = pd.Series([
            float(k[9]) - (float(k[5]) - float(k[9]))
            for k in data
        ]).cumsum()
        return cvd
    except:
        return None

def fetch_oi(sym):
    bn = sym.replace('/', '').replace(':USDT', '')
    try:
        r = requests.get(
            'https://fapi.binance.com/futures/data/openInterestHist',
            params={'symbol': bn, 'period': '1h', 'limit': 25},
            timeout=10
        )
        d = r.json()
        if isinstance(d, list) and len(d) >= 2:
            o = float(d[0]['sumOpenInterest'])
            n = float(d[-1]['sumOpenInterest'])
            return (n - o) / o * 100 if o else None
    except:
        return None

def divergence(close, rsi_s, lb=14):
    if len(close) < lb + 1:
        return 0
    c = close.iloc[-lb:]
    r = rsi_s.iloc[-lb:]
    pu = c.iloc[-1] > c.iloc[0]
    ru = r.iloc[-1] > r.iloc[0]
    if not pu and ru:
        return 1
    if pu and not ru:
        return -1
    return 0

def btc_regime():
    ex = get_ex()
    df = fetch_ohlcv(ex, 'BTC/USDT:USDT', '1d', 220)
    if df is None or len(df) < 200:
        return {'bull': True, 'price': None, 'ema200': None}
    e = ema(df['close'], 200)
    p = df['close'].iloc[-1]
    return {'bull': p > e.iloc[-1], 'price': p, 'ema200': e.iloc[-1]}

def scan_coin(ex, sym, bull):
    df = fetch_ohlcv(ex, sym)
    if df is None:
        return None
    close = df['close']
    r14 = rsi(close)
    bb_lo, bb_hi = bollinger(close)
    st = supertrend(df)
    cvd = fetch_cvd(sym)
    fund = fetch_funding(sym)
    oi = fetch_oi(sym)
    lc = close.iloc[-1]
    lr = r14.iloc[-1]
    sc = {}
    sc['trend'] = 1 if st.iloc[-1] == 1 else -1
    sc['rsi'] = 1 if lr < 30 else (-1 if lr > 70 else 0)
    sc['div'] = divergence(close, r14)
    if cvd is not None and len(cvd) >= 14:
        pu = lc > close.iloc[-14]
        cu = cvd.iloc[-1] > cvd.iloc[-14]
        sc['cvd'] = 1 if (not pu and cu) else (-1 if (pu and not cu) else 0)
    else:
        sc['cvd'] = 0
    if fund is not None:
        sc['fund'] = 1 if fund < -10 else (-1 if fund > 50 else 0)
    else:
        sc['fund'] = 0
    if oi is not None:
        pc = (lc / close.iloc[-2] - 1) * 100
        sc['oi'] = 1 if (oi > 2 and pc > 0) else (-1 if (oi > 2 and pc < 0) else 0)
    else:
        sc['oi'] = 0
    sc['bb'] = 1 if lc < bb_lo.iloc[-1] else (-1 if lc > bb_hi.iloc[-1] else 0)
    raw = sum(sc.values())
    net = raw * DAMPEN if (not bull and raw > 0) else float(raw)
    return {
        'symbol': sym,
        'close': round(lc, 6),
        'rsi': round(lr, 1),
        'raw_net': raw,
        'net': net,
        'scores': sc,
        'funding': round(fund, 2) if fund is not None else None,
        'oi': round(oi, 2) if oi is not None else None
    }

def harvest_stats(sym, days=30):
    bn = sym.replace('/', '').replace(':USDT', '')
    try:
        r = requests.get(
            'https://fapi.binance.com/fapi/v1/fundingRate',
            params={'symbol': bn, 'limit': days * 3},
            timeout=10
        )
        arr = [float(d['fundingRate']) * 100 * 3 * 365 for d in r.json()]
        if not arr:
            return None
        a = np.array(arr)
        pp = float((a > 0).mean() * 100)
        return {
            'symbol': sym,
            'mean': round(float(a.mean()), 1),
            'std': round(float(a.std()), 1),
            'pos_pct': round(pp, 0),
            'score': round(pp - a.std() * 0.5, 1)
        }
    except:
        return None

def pills(sc):
    names = {
        'trend': 'Trend', 'rsi': 'RSI', 'div': 'Div',
        'cvd': 'CVD', 'fund': 'Fund', 'oi': 'OI', 'bb': 'BB'
    }
    out = ''
    for k, v in sc.items():
        lbl = names.get(k, k)
        cls = 'bull' if v > 0 else ('bear' if v < 0 else 'neu')
        arrow = '▲' if v > 0 else ('▼' if v < 0 else '–')
        out += f'<span class="p {cls}">{arrow}{lbl}</span>'
    return out

def trows(items):
    if not items:
        return "<tr><td colspan='8' class='empty'>Sinyal yok</td></tr>"
    rows = ''
    for r in items:
        nc = 'bull' if r['net'] > 0 else 'bear'
        fn = f"{r['funding']:+.1f}%" if r['funding'] is not None else '-'
        oi = f"{r['oi']:+.1f}%" if r['oi'] is not None else '-'
        rows += (
            f"<tr><td><b>{r['symbol'].replace('/USDT:USDT','')}</b></td>"
            f"<td>{r['close']}</td><td>{r['rsi']}</td>"
            f"<td class='{nc}'><b>{r['raw_net']:+d}</b></td>"
            f"<td class='{nc}'>{r['net']:+.1f}</td>"
            f"<td>{fn}</td><td>{oi}</td>"
            f"<td>{pills(r['scores'])}</td></tr>"
        )
    return rows

def hrows(items):
    if not items:
        return "<tr><td colspan='5' class='empty'>Veri yok</td></tr>"
    rows = ''
    for r in items:
        rows += (
            f"<tr><td><b>{r['symbol'].replace('/USDT:USDT','')}</b></td>"
            f"<td class='bull'>{r['mean']:+.1f}%</td>"
            f"<td>{r['std']}%</td><td>{r['pos_pct']:.0f}%</td>"
            f"<td class='bull'><b>{r['score']}</b></td></tr>"
        )
    return rows

def write_html(sl, ss, rl, rs, hv, regime, ts):
    bull = regime['bull']
    bp = f"${regime['price']:,.0f}" if regime['price'] else 'N/A'
    ep = f"${regime['ema200']:,.0f}" if regime['ema200'] else 'N/A'
    rlbl = 'BULL' if bull else 'BEAR'
    rcls = 'bull' if bull else 'bear'
    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta http-equiv="refresh" content="900">
<title>Crypto Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--green:#3fb950;--red:#f85149;--text:#c9d1d9;--muted:#8b949e}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,sans-serif;font-size:14px;padding:12px}}
h1{{font-size:1.1em;margin-bottom:12px}}
h2{{font-size:.78em;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:14px 0 6px}}
.kpis{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
.kpi{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;flex:1;min-width:90px}}
.kpi .l{{font-size:.7em;color:var(--muted);margin-bottom:3px}}
.kpi .v{{font-size:1em;font-weight:600}}
.bull{{color:var(--green)!important}}
.bear{{color:var(--red)!important}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden;margin-bottom:10px}}
th{{background:#21262d;padding:7px 8px;text-align:left;font-size:.72em;color:var(--muted);white-space:nowrap}}
td{{padding:6px 8px;border-top:1px solid var(--border);font-size:.8em;vertical-align:middle}}
tr:hover td{{background:#1c2128}}
.p{{display:inline-block;font-size:.65em;padding:2px 4px;border-radius:3px;margin:1px}}
.p.bull{{background:#1e3a2a;color:var(--green)}}
.p.bear{{background:#3a1e1e;color:var(--red)}}
.p.neu{{background:#21262d;color:var(--muted)}}
.btn{{background:#238636;color:#fff;border:none;border-radius:6px;padding:11px;font-size:.9em;cursor:pointer;width:100%;margin-bottom:12px}}
.warn{{background:#272115;border:1px solid #b08000;border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:.78em;color:#d29922;line-height:1.5}}
.empty{{text-align:center;color:var(--muted);padding:14px}}
@media(max-width:500px){{th:nth-child(n+7),td:nth-child(n+7){{display:none}}}}
</style>
</head>
<body>
<h1>Crypto Perp Dashboard</h1>
<div class="kpis">
<div class="kpi"><div class="l">BTC</div><div class="v">{bp}</div></div>
<div class="kpi"><div class="l">EMA200</div><div class="v">{ep}</div></div>
<div class="kpi"><div class="l">Rejim</div><div class="v {rcls}">{rlbl}</div></div>
<div class="kpi"><div class="l">Guncelleme</div><div class="v" style="font-size:.72em">{ts}</div></div>
</div>
<button class="btn" onclick="location.reload()">Sayfayi Yenile</button>
<div class="warn">UYARI: Karar destek aracidir, trade botu degildir. Backtest Sharpe -0.7 negatif. Paper trade tutun, 30+ ornek sonrasi hit-rate olcun.</div>
<h2>Guclu Long (net &gt;= {STRONG})</h2>
<table><tr><th>Sembol</th><th>Fiyat</th><th>RSI</th><th>Ham</th><th>Net</th><th>Fund</th><th>OI</th><th>Sinyaller</th></tr>
{trows(sl)}</table>
<h2>Guclu Short (net &lt;= -{STRONG})</h2>
<table><tr><th>Sembol</th><th>Fiyat</th><th>RSI</th><th>Ham</th><th>Net</th><th>Fund</th><th>OI</th><th>Sinyaller</th></tr>
{trows(ss)}</table>
<h2>Ham Long</h2>
<table><tr><th>Sembol</th><th>Fiyat</th><th>RSI</th><th>Ham</th><th>Net</th><th>Fund</th><th>OI</th><th>Sinyaller</th></tr>
{trows(rl)}</table>
<h2>Ham Short</h2>
<table><tr><th>Sembol</th><th>Fiyat</th><th>RSI</th><th>Ham</th><th>Net</th><th>Fund</th><th>OI</th><th>Sinyaller</th></tr>
{trows(rs)}</table>
<h2>Funding Harvest</h2>
<table><tr><th>Sembol</th><th>Yillik Ort.</th><th>Std</th><th>Pozitif%</th><th>Skor</th></tr>
{hrows(hv)}</table>
<p style="color:var(--muted);font-size:.7em;text-align:center;margin-top:12px">15 dk otomatik guncellenir | {ts}</p>
</body></html>"""
    (OUT / "index.html").write_text(html, encoding='utf-8')
    print("[OK] index.html yazildi")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', default='dashboard')
    ap.add_argument('--limit', type=int, default=100)
    args = ap.parse_args()
    print(f"=== Scanner {datetime.now(timezone.utc).strftime('%H:%M UTC')} ===")
    regime = btc_regime()
    print(f"BTC: {'BULL' if regime['bull'] else 'BEAR'} | {regime['price']}")
    ex = get_ex()
    try:
        tickers = ex.fetch_tickers()
        syms = sorted(
            [k for k, v in tickers.items()
             if k.endswith(':USDT') and v.get('quoteVolume', 0)],
            key=lambda s: tickers[s]['quoteVolume'],
            reverse=True
        )[:args.limit]
    except Exception as e:
        print(f"Universe hatasi: {e}")
        return
    print(f"{len(syms)} coin taranacak...")
    results = []
    for i, sym in enumerate(syms):
        print(f"[{i+1}/{len(syms)}] {sym}", end=' ', flush=True)
        try:
            r = scan_coin(ex, sym, regime['bull'])
            if r:
                results.append(r)
                print(f"net={r['net']:+.1f}")
            else:
                print("skip")
        except Exception as e:
            print(f"ERR:{e}")
        time.sleep(0.12)
    sl = sorted([r for r in results if r['net'] >= STRONG], key=lambda x: -x['net'])
    ss = sorted([r for r in results if r['net'] <= -STRONG], key=lambda x: x['net'])
    rl = sorted([r for r in results if r['raw_net'] >= STRONG], key=lambda x: -x['raw_net'])
    rs = sorted([r for r in results if r['raw_net'] <= -STRONG], key=lambda x: x['raw_net'])
    print("Harvest taranıyor...")
    hv = []
    for sym in syms[:60]:
        s = harvest_stats(sym)
        if s and s['mean'] > 20 and s['pos_pct'] > 60:
            hv.append(s)
        time.sleep(0.05)
    hv.sort(key=lambda x: -x['score'])
    hv = hv[:15]
    ts = datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    write_html(sl, ss, rl, rs, hv, regime, ts)
    print(f"=== TAMAM | Long:{len(sl)} Short:{len(ss)} Harvest:{len(hv)} ===")

if __name__ == '__main__':
    main()
