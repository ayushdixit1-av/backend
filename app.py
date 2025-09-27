import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
from threading import Thread
from flask import Flask, jsonify, Response
from colorama import Fore, Style, init

# Data sources
from nsepython import nse_eq
import yfinance as yf

# DB
import psycopg2
from psycopg2.pool import SimpleConnectionPool
import certifi  # <- important for Railway/SSL

# ================== CONFIG ==================
init(autoreset=True)

SYMBOL = "ITC"
YF_TICKER = "ITC.NS"
CAPITAL = 5000
STOP_PCT = 0.002      # 0.2%
TARGET_PCT = 0.006    # 0.6%
TICK_INTERVAL = 3
BROKERAGE = 40
TAXES = 9
SCORE_ENTRY = 0.30
MIN_BARS_FOR_INDICATORS = 60

# ---- DB URL: MUST come from Railway env var (no hardcoded fallback) ----
DB_URL = os.getenv("DB_URL")  # set this in Railway → Variables

# ================== GLOBAL STATE ==================
STATE = {
    "live_price": None,
    "position": None,
    "daily_profit": 0.0,
    "total_profit": 0.0,
    "last_error": "",
    "last_signal": "HOLD",
    "last_score": 0.0,
    "bars_collected": 0,
}
MEM_TRADES = []

DB_ENABLED = False
POOL = None

# ================== DB SETUP ==================
def _mask_url(url: str) -> str:
    if not url: return "None"
    try:
        head, tail = url.split("://", 1)
        if "@" in tail and ":" in tail.split("@", 1)[0]:
            creds, rest = tail.split("@", 1)
            user, _pwd = creds.split(":", 1)
            return f"{head}://{user}:****@{rest}"
        return f"{head}://****"
    except Exception:
        return "****"

def _pool_try(dsn: str, note: str):
    """Try to make a pool with strong SSL hints; return (pool|None, error|None, note)."""
    try:
        pool = SimpleConnectionPool(
            1, 5,
            dsn=dsn,
            connect_timeout=10,
            sslmode="require",
            sslrootcert=certifi.where(),
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
            options="-c client_encoding=UTF8",
        )
        # Sanity check
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.fetchone()
        cur.close()
        pool.putconn(conn)
        return pool, None, note
    except Exception as e:
        return None, f"[{note}] {e}", note

def db_init():
    """Connect to Neon with retries across DSN variants; else fall back to memory."""
    global DB_ENABLED, POOL

    if not DB_URL:
        msg = "DB_URL env var is missing. Set it in Railway → Variables."
        STATE["last_error"] = msg
        print(Fore.YELLOW + "⚠️  " + msg)
        DB_ENABLED = False
        return

    print(Fore.CYAN + f"DB_URL (masked): {_mask_url(DB_URL)}")

    # A) as-is
    dsn_a = DB_URL.strip()

    # B) remove channel_binding param if present
    if "channel_binding=" in dsn_a.lower():
        base, q = dsn_a.split("?", 1)
        kv = [p for p in q.split("&") if not p.lower().startswith("channel_binding=")]
        dsn_b = base + ("?" + "&".join(kv) if kv else "")
    else:
        dsn_b = dsn_a

    # C) also force gssencmode=disable (fixes some libpq builds)
    if "gssencmode=" not in dsn_b.lower():
        sep = "&" if "?" in dsn_b else "?"
        dsn_c = f"{dsn_b}{sep}gssencmode=disable"
    else:
        dsn_c = dsn_b

    attempts = [
        (dsn_a, "A: as-is"),
        (dsn_b, "B: no channel_binding"),
        (dsn_c, "C: no channel_binding + gssencmode=disable"),
    ]

    errors = []
    for dsn, note in attempts:
        print(Fore.CYAN + f"🔌 DB attempt {note} → {_mask_url(dsn)}")
        pool, err, which = _pool_try(dsn, note)
        if pool:
            print(Fore.GREEN + f"✅ Connected using attempt {which}")
            # ensure table
            try:
                conn = pool.getconn()
                cur = conn.cursor()
                cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    time TIMESTAMP DEFAULT NOW(),
                    symbol TEXT,
                    qty INTEGER,
                    entry FLOAT,
                    exit FLOAT,
                    gross FLOAT,
                    net FLOAT,
                    reason TEXT
                );
                """)
                conn.commit()
                cur.close()
                pool.putconn(conn)
            except Exception as e:
                STATE["last_error"] = f"Table init failed: {e}"
                print(Fore.YELLOW + "⚠️  " + STATE["last_error"])
            # success
            globals()["POOL"] = pool
            globals()["DB_ENABLED"] = True
            STATE["last_error"] = ""
            return
        else:
            errors.append(err)
            print(Fore.YELLOW + f"⚠️  {err}")

    DB_ENABLED = False
    STATE["last_error"] = "All DB attempts failed:\n" + "\n".join(errors)
    print(Fore.RED + "❌ " + STATE["last_error"])

def db_save_trade(symbol, qty, entry, exitp, gross, net, reason):
    if DB_ENABLED and POOL:
        try:
            conn = POOL.getconn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades(symbol, qty, entry, exit, gross, net, reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s);
            """, (symbol, qty, entry, exitp, gross, net, reason))
            conn.commit()
            cur.close()
            POOL.putconn(conn)
            return
        except Exception as e:
            print(Fore.YELLOW + f"⚠️  DB insert failed, using memory. Reason: {e}")

    MEM_TRADES.append({
        "time": datetime.now(),
        "symbol": symbol,
        "qty": qty,
        "entry": float(entry),
        "exit": float(exitp),
        "gross": float(gross),
        "net": float(net),
        "reason": reason
    })

def db_get_trades():
    if DB_ENABLED and POOL:
        try:
            conn = POOL.getconn()
            df = pd.read_sql("SELECT * FROM trades ORDER BY time DESC", conn)
            POOL.putconn(conn)
            return df
        except Exception as e:
            print(Fore.YELLOW + f"⚠️  DB read failed, using memory. Reason: {e}")
    if not MEM_TRADES:
        return pd.DataFrame(columns=["time","symbol","qty","entry","exit","gross","net","reason"])
    return pd.DataFrame(MEM_TRADES)

def db_total_profit():
    if DB_ENABLED and POOL:
        try:
            conn = POOL.getconn()
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(net),0) FROM trades;")
            total = cur.fetchone()[0]
            POOL.putconn(conn)
            return float(total or 0)
        except Exception as e:
            print(Fore.YELLOW + f"⚠️  DB sum failed, using memory. Reason: {e}")
    return float(sum(t["net"] for t in MEM_TRADES))

# ================== PRICE FETCHER ==================
_last_cached_price = None
_last_yf_ts = 0.0

def get_price_nse():
    try:
        data = nse_eq(SYMBOL)
        val = data.get("lastPrice")
        return float(val) if val is not None else None
    except Exception:
        return None

def get_price_yf():
    global _last_yf_ts, _last_cached_price
    try:
        now = time.time()
        if now - _last_yf_ts < 10 and _last_cached_price is not None:
            return _last_cached_price
        _last_yf_ts = now
        df = yf.download(YF_TICKER, period="1d", interval="1m", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        return float(df["Close"].dropna().iloc[-1])
    except Exception:
        return None

def get_live_price():
    global _last_cached_price
    p = get_price_nse()
    if p is None:
        p = get_price_yf()
    if p is not None:
        _last_cached_price = p
    return _last_cached_price

# ================== STRATEGY ==================
def zerodha_fees(buy_value, sell_value):
    return float(BROKERAGE + TAXES)

def calc_indicators(closes):
    df = pd.DataFrame(closes, columns=["Close"])
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    delta = df["Close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    rs = (up.rolling(14).mean() / down.rolling(14).mean()).replace([np.inf, -np.inf], np.nan)
    df["RSI14"] = 100 - (100 / (1 + rs))
    ma = df["Close"].rolling(20).mean()
    sd = df["Close"].rolling(20).std()
    df["BB_Mid"], df["BB_Up"], df["BB_Lo"] = ma, ma + 2*sd, ma - 2*sd
    df.fillna(method="bfill", inplace=True)
    df.fillna(method="ffill", inplace=True)
    return df.iloc[-1]

def decide_signal(row):
    votes = [
        1 if row["EMA20"] > row["EMA50"] else -1,
        1 if row["RSI14"] < 30 else -1 if row["RSI14"] > 70 else 0,
        1 if row["Close"] > row["BB_Mid"] else -1 if row["Close"] < row["BB_Mid"] else 0,
    ]
    score = float(np.mean(votes))
    if score > 0.25: return "BUY", score
    if score < -0.25: return "SELL", score
    return "HOLD", score

# ================== BOT LOOP ==================
def trading_bot():
    print(Fore.CYAN + "\n🚀 Starting ITC Live Algo (Neon + Flask)")
    print(Fore.YELLOW + "=" * 80)

    db_init()

    position = None
    closes = []
    STATE["bars_collected"] = 0

    while True:
        try:
            price = get_live_price()
            if price is None:
                STATE["last_error"] = "Price unavailable (both sources)."
                print(Fore.RED + "⚠️  Price unavailable. Retrying...")
                time.sleep(TICK_INTERVAL)
                continue

            STATE["live_price"] = float(price)
            closes.append(float(price))
            if len(closes) > 2000:
                closes = closes[-1000:]

            if len(closes) < MIN_BARS_FOR_INDICATORS:
                STATE["bars_collected"] = len(closes)
                time.sleep(TICK_INTERVAL)
                continue

            row = calc_indicators(closes[-MIN_BARS_FOR_INDICATORS:])
            signal, score = decide_signal(row)
            STATE["last_signal"] = signal
            STATE["last_score"] = score
            STATE["bars_collected"] = len(closes)

            if not position and signal == "BUY" and score >= SCORE_ENTRY:
                qty = int(CAPITAL // price)
                if qty >= 1:
                    position = {
                        "entry": price,
                        "qty": qty,
                        "sl": price * (1 - STOP_PCT),
                        "tp": price * (1 + TARGET_PCT),
                        "time": datetime.now()
                    }
                    STATE["position"] = {
                        "qty": qty, "entry": float(price),
                        "sl": float(position["sl"]), "tp": float(position["tp"]),
                        "since": position["time"].strftime("%H:%M:%S")
                    }
                    print(Fore.GREEN + f"🟢 ENTRY: BUY {qty} @ ₹{price:.2f} | SL ₹{position['sl']:.2f} | TP ₹{position['tp']:.2f}")

            elif position:
                exit_reason = None
                if price <= position["sl"]:
                    exit_reason = "SL HIT"
                elif price >= position["tp"]:
                    exit_reason = "TP HIT"

                if exit_reason:
                    buy_val = position["entry"] * position["qty"]
                    sell_val = price * position["qty"]
                    gross = sell_val - buy_val
                    fees = zerodha_fees(buy_val, sell_val)
                    net = gross - fees

                    STATE["daily_profit"] += net
                    total = db_total_profit()
                    STATE["total_profit"] = total + net

                    db_save_trade(SYMBOL, position["qty"], position["entry"], price, gross, net, exit_reason)

                    print((Fore.RED if net < 0 else Fore.GREEN) + f"🔚 EXIT: {exit_reason} @ ₹{price:.2f} | Net ₹{net:.2f} (gross {gross:.2f}, fees {fees:.2f})")
                    position = None
                    STATE["position"] = None

            STATE["total_profit"] = db_total_profit()
            print(
                Fore.WHITE + f"⏰ {datetime.now().strftime('%H:%M:%S')} | "
                f"💰 Price ₹{price:.2f} | "
                f"Signal {STATE['last_signal']} ({STATE['last_score']:.2f}) | "
                f"Bars {STATE['bars_collected']} | "
                + (Fore.GREEN + f"Holding {STATE['position']['qty']}" if STATE['position'] else Fore.YELLOW + "No position")
            )
            print(Fore.MAGENTA + f"🏦 Daily: ₹{STATE['daily_profit']:.2f} | 📊 Total(Neon): ₹{STATE['total_profit']:.2f}")
            print(Fore.CYAN + "-" * 80)

            time.sleep(TICK_INTERVAL)

        except Exception as e:
            STATE["last_error"] = f"Loop error: {e}"
            print(Fore.RED + f"❌ Loop error: {e}")
            time.sleep(TICK_INTERVAL)

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/api/status")
def api_status():
    return jsonify({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "price": STATE["live_price"],
        "signal": STATE["last_signal"],
        "score": round(float(STATE["last_score"]), 3),
        "daily_profit": round(float(STATE["daily_profit"]), 2),
        "total_profit": round(float(STATE["total_profit"]), 2),
        "bars_collected": STATE["bars_collected"],
        "position": STATE["position"] if STATE["position"] else None,
        "error": STATE["last_error"],
        "db_enabled": DB_ENABLED,
    })

@app.route("/api/trades")
def api_trades():
    df = db_get_trades()
    if not df.empty and not isinstance(df.iloc[0]["time"], str):
        df["time"] = df["time"].astype(str)
    return jsonify(df.to_dict(orient="records"))

@app.route("/")
def dashboard():
    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>🚀 ITC Algo Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body {{ background:#0b0b0b; color:#eee; font-family:Arial, sans-serif; margin:0; padding:20px; }}
    h1 {{ color:#00ff9f; }}
    .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }}
    .card {{ background:#161616; border-radius:12px; padding:14px; box-shadow:0 0 0 1px #222; }}
    .label {{ color:#aaa; font-size:12px; }}
    .value {{ font-size:22px; margin-top:4px; }}
    .gain {{ color:#00ff9f; }}
    .loss {{ color:#ff5a5a; }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
    th,td {{ border-bottom:1px solid #222; padding:8px; text-align:left; }}
    th {{ color:#00ff9f; }}
    .small {{ font-size:12px; color:#aaa; }}
    @media (max-width:900px) {{ .cards {{ grid-template-columns:1fr 1fr; }} }}
    @media (max-width:600px) {{ .cards {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <h1>🚀 ITC Live Algo — Neon + Flask</h1>
  <div id="info" class="small">Loading...</div>
  <div class="cards">
    <div class="card"><div class="label">Live Price</div><div id="price" class="value">—</div></div>
    <div class="card"><div class="label">Signal</div><div id="signal" class="value">—</div></div>
    <div class="card"><div class="label">Score</div><div id="score" class="value">—</div></div>
    <div class="card"><div class="label">Bars</div><div id="bars" class="value">—</div></div>
    <div class="card"><div class="label">Daily Profit</div><div id="daily" class="value">—</div></div>
    <div class="card"><div class="label">Total Profit (Neon)</div><div id="total" class="value">—</div></div>
    <div class="card"><div class="label">Position</div><div id="pos" class="value small">—</div></div>
    <div class="card"><div class="label">DB Status</div><div id="db" class="value small">—</div></div>
  </div>

  <div class="card">
    <div class="label">Trades</div>
    <table id="table">
      <thead><tr><th>Time</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Net</th><th>Reason</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
async function refresh() {{
  try {{
    const s = await fetch('/api/status').then(r => r.json());
    const t = await fetch('/api/trades').then(r => r.json());

    document.getElementById('info').textContent = 'Last update: ' + s.time + (s.error ? ' | ⚠️ ' + s.error : '');
    document.getElementById('price').textContent = s.price ? '₹' + s.price.toFixed(2) : 'Fetching...';
    document.getElementById('signal').textContent = s.signal;
    document.getElementById('score').textContent = s.score.toFixed(2);
    document.getElementById('bars').textContent = s.bars_collected;
    document.getElementById('daily').innerHTML = (s.daily_profit >= 0 ? '<span class="gain">₹'+s.daily_profit.toFixed(2)+'</span>' : '<span class="loss">₹'+s.daily_profit.toFixed(2)+'</span>');
    document.getElementById('total').innerHTML = (s.db_enabled ? (s.total_profit >= 0 ? '<span class="gain">₹'+s.total_profit.toFixed(2)+'</span>' : '<span class="loss">₹'+s.total_profit.toFixed(2)+'</span>') : '<span class="loss">—</span>');
    document.getElementById('db').textContent = s.db_enabled ? '✅ Connected' : '⚠️ Memory mode';

    const pos = s.position;
    document.getElementById('pos').textContent = pos ? ('LONG '+pos.qty+' @ ₹'+pos.entry.toFixed(2)+' | SL ₹'+pos.sl.toFixed(2)+' | TP ₹'+pos.tp.toFixed(2)+' | since '+pos.since) : 'No position';

    const tbody = document.querySelector('#table tbody');
    tbody.innerHTML = '';
    for (const row of t) {{
      const tr = document.createElement('tr');
      const net = parseFloat(row.net);
      tr.innerHTML = `
        <td>${{row.time}}</td>
        <td>${{row.qty}}</td>
        <td>₹${{parseFloat(row.entry).toFixed(2)}}</td>
        <td>₹${{parseFloat(row.exit).toFixed(2)}}</td>
        <td class="${{net>=0?'gain':'loss'}}">₹${{net.toFixed(2)}}</td>
        <td>${{row.reason}}</td>`;
      tbody.appendChild(tr);
    }}
  }} catch (e) {{
    document.getElementById('info').textContent = 'Error updating: ' + e;
  }}
}}
setInterval(refresh, 5000);
refresh();
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")

# ================== RUN BOTH ==================
if __name__ == "__main__":
    Thread(target=trading_bot, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))  # Railway usually binds 8080
    app.run(host="0.0.0.0", port=port)
