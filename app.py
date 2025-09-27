import os
import time
import math
import pandas as pd
import numpy as np
from datetime import datetime, date
from threading import Thread
from flask import Flask, jsonify, Response
from colorama import Fore, Style, init

# Data sources
from nsepython import nse_eq
import yfinance as yf

# DB
import psycopg2
from psycopg2.pool import SimpleConnectionPool

# ================== CONFIG ==================
init(autoreset=True)

SYMBOL = "ITC"         # NSE symbol
YF_TICKER = "ITC.NS"   # Yahoo Finance ticker
CAPITAL = 5000         # fake per-trade capital
STOP_PCT = 0.002       # -0.2%
TARGET_PCT = 0.006     # +0.6%
TICK_INTERVAL = 3      # seconds between polls
BROKERAGE = 40         # Zerodha approx fixed (buy+sell)
TAXES = 9              # approx others combined
SCORE_ENTRY = 0.30     # min score to enter long
MIN_BARS_FOR_INDICATORS = 60

# Neon Postgres (set DB_URL env var on Railway)
DB_URL = os.getenv(
    "DB_URL",
    "postgresql://neondb_owner:npg_jgROvpDtrm03@ep-hidden-truth-aev5l7a7-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
)

# ================== GLOBAL STATE ==================
STATE = {
    "live_price": None,
    "position": None,         # dict or None
    "daily_profit": 0.0,
    "total_profit": 0.0,
    "last_error": "",
    "last_signal": "HOLD",
    "last_score": 0.0,
    "bars_collected": 0
}
MEM_TRADES = []  # in-memory ledger if DB not available

# ================== DB SETUP ==================
DB_ENABLED = False
POOL = None

def db_init():
    """Create connection pool and ensure table exists; fall back to memory on failure."""
    global DB_ENABLED, POOL
    try:
        POOL = SimpleConnectionPool(1, 5, DB_URL)
        conn = POOL.getconn()
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
        POOL.putconn(conn)
        DB_ENABLED = True
        print(Fore.GREEN + "✅ Neon DB connected.")
    except Exception as e:
        DB_ENABLED = False
        print(Fore.YELLOW + f"⚠️  Neon DB unavailable, using in-memory ledger. Reason: {e}")

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
            print(Fore.YELLOW + f"⚠️  DB insert failed, falling back to memory. Reason: {e}")

    # Fallback: memory
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
    # memory fallback
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
    """Try NSE live via nsepython."""
    try:
        data = nse_eq(SYMBOL)
        # nsepython can sometimes return strings like '452.35'
        val = data.get("lastPrice")
        if val is None:
            return None
        return float(val)
    except Exception:
        return None

def get_price_yf():
    """Fallback: Yahoo Finance (near-real-time, often 1-2 min delay)."""
    global _last_yf_ts
    try:
        # throttle to 10s to avoid hammering
        now = time.time()
        if now - _last_yf_ts < 10 and _last_cached_price is not None:
            return _last_cached_price
        _last_yf_ts = now

        df = yf.download(YF_TICKER, period="1d", interval="1m", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        price = float(df["Close"].dropna().iloc[-1])
        return price
    except Exception:
        return None

def get_live_price():
    """Robust fetcher: NSE -> Yahoo -> last cached."""
    global _last_cached_price
    p = get_price_nse()
    if p is None:
        p = get_price_yf()
    if p is not None:
        _last_cached_price = p
    return _last_cached_price

# ================== STRATEGY ==================
def zerodha_fees(buy_value, sell_value):
    # Simple model you used earlier
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

    # Fill missing for early bars
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
    if score > 0.25:
        return "BUY", score
    elif score < -0.25:
        return "SELL", score
    else:
        return "HOLD", score

# ================== TRADING BOT LOOP ==================
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
                closes = closes[-1000:]  # keep memory small

            if len(closes) < MIN_BARS_FOR_INDICATORS:
                STATE["bars_collected"] = len(closes)
                time.sleep(TICK_INTERVAL)
                continue

            row = calc_indicators(closes[-MIN_BARS_FOR_INDICATORS:])
            signal, score = decide_signal(row)
            STATE["last_signal"] = signal
            STATE["last_score"] = score
            STATE["bars_collected"] = len(closes)

            # ENTRY (long-only)
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
                else:
                    print(Fore.YELLOW + "ℹ️  Not enough capital to buy 1 share at current price.")

            # EXIT (SL/TP)
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

                    # Update state profits
                    STATE["daily_profit"] = float(STATE["daily_profit"] + net)
                    total = db_total_profit()
                    STATE["total_profit"] = float(total + net)

                    # Save trade
                    db_save_trade(SYMBOL, position["qty"], position["entry"], price, gross, net, exit_reason)

                    print((Fore.RED if net < 0 else Fore.GREEN) + f"🔚 EXIT: {exit_reason} @ ₹{price:.2f} | Net ₹{net:.2f} (gross {gross:.2f}, fees {fees:.2f})")
                    position = None
                    STATE["position"] = None

            # Status line
            total_in_db = db_total_profit()
            STATE["total_profit"] = float(total_in_db)
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

# ================== FLASK APP ==================
app = Flask(__name__)

@app.route("/api/status")
def api_status():
    # compose a safe status payload
    safe_pos = STATE["position"] if STATE["position"] else None
    return jsonify({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "price": STATE["live_price"],
        "signal": STATE["last_signal"],
        "score": round(float(STATE["last_score"]), 3),
        "daily_profit": round(float(STATE["daily_profit"]), 2),
        "total_profit": round(float(STATE["total_profit"]), 2),
        "bars_collected": STATE["bars_collected"],
        "position": safe_pos,
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
    # Minimal HTML with JS polling (no templates required)
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
    document.getElementById('total').innerHTML = (s.total_profit >= 0 ? '<span class="gain">₹'+s.total_profit.toFixed(2)+'</span>' : '<span class="loss">₹'+s.total_profit.toFixed(2)+'</span>');
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
    # start trading bot in background
    Thread(target=trading_bot, daemon=True).start()
    # run flask (Railway uses PORT env)
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
