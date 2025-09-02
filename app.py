import os, math, threading, time, traceback
from datetime import datetime, timedelta
from functools import wraps

import numpy as np
from flask import Flask, jsonify, request, redirect, url_for
from pytz import timezone
from kiteconnect import KiteConnect

# ───────── Config ─────────
IST = timezone("Asia/Kolkata")

KITE_API_KEY       = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET    = os.environ.get("KITE_API_SECRET", "")
INVEST_AMOUNT      = float(os.environ.get("INVEST_AMOUNT", "10000"))
AUTO_CONFIRM_TOKEN = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")
REDIRECT_URL       = os.environ.get("REDIRECT_URL", "http://localhost:5000/login/callback")
KEEPALIVE_REDIRECT = os.environ.get("KEEPALIVE_REDIRECT", "").strip()

NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE",
    "BAJAJFINSV","BHARTIARTL","BPCL","BRITANNIA","CIPLA","COALINDIA","DIVISLAB","DRREDDY",
    "EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO","HINDUNILVR",
    "ICICIBANK","INDUSINDBK","INFY","ITC","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND",
    "NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SBIN","SHREECEM","SUNPHARMA","TATACONSUM",
    "TATAMOTORS","TATASTEEL","TCS","TECHM","TITAN","ULTRACEMCO","UPL","WIPRO"
]

app = Flask(__name__)

state = {
    "access_token": None,
    "instrument_map": {},   # {"RELIANCE": 738561, ...}
    "positions": {},
    "closed_trades": [],
    "pending_confirms": [],
    "engine_running": False,
}

# ───────── Helpers ─────────
def now_ist(): return datetime.now(IST)
def now_s():   return now_ist().strftime("%Y-%m-%d %H:%M:%S")

def market_live_now():
    dt = now_ist()
    if dt.weekday() >= 5:   # Sat/Sun
        return False
    open_t  = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = dt.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= dt <= close_t

def kite():
    if not getattr(kite, "_client", None):
        if not KITE_API_KEY:
            raise RuntimeError("KITE_API_KEY not set")
        kite._client = KiteConnect(api_key=KITE_API_KEY)
        if state["access_token"]:
            kite._client.set_access_token(state["access_token"])
    return kite._client

def require_authed(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not state["access_token"]:
            return jsonify({"ok": False, "error": "Login required"}), 401
        return fn(*a, **k)
    return wrap

# ───────── Auth ─────────
@app.get("/login/start")
def login_start():
    _ = kite().generate_session
    return redirect(kite().login_url())

@app.get("/login/callback")
def login_callback():
    req_token = request.args.get("request_token")
    if not req_token:
        return "Missing request_token", 400
    try:
        data = kite().generate_session(req_token, api_secret=KITE_API_SECRET)
        state["access_token"] = data["access_token"]
        kite().set_access_token(state["access_token"])
        return redirect(url_for("index"))
    except Exception as e:
        return f"Login failed: {e}", 500

# ───────── Instruments & candles (no pandas) ─────────
def ensure_instruments_loaded():
    if state["instrument_map"]:
        return
    instruments = kite().instruments("NSE")
    wanted = set(NIFTY50)
    mapping = {}
    for row in instruments:
        ts = row.get("tradingsymbol")
        if ts in wanted:
            mapping[ts] = int(row.get("instrument_token"))
    state["instrument_map"] = mapping

def get_candles(symbol, interval="15minute", lookback=200):
    ensure_instruments_loaded()
    token = state["instrument_map"].get(symbol)
    if not token:
        raise RuntimeError(f"No instrument token for {symbol}")
    end = now_ist()
    days = 14 if "minute" in interval else 365
    start = end - timedelta(days=days)
    data = kite().historical_data(token, start, end, interval, continuous=False, oi=False) or []
    closes = np.array([d["close"] for d in data], dtype=float)
    highs  = np.array([d["high"]  for d in data], dtype=float)
    lows   = np.array([d["low"]   for d in data], dtype=float)
    return {
        "close": closes[-lookback:],
        "high":  highs[-lookback:],
        "low":   lows[-lookback:]
    }

# ───────── Indicators (numpy) ─────────
def ema(arr, span):
    arr = np.asarray(arr, dtype=float)
    if arr.size == 0: return arr
    alpha = 2.0/(span+1.0)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha*arr[i] + (1-alpha)*out[i-1]
    return out

def rsi(arr, period=14):
    arr = np.asarray(arr, dtype=float)
    if arr.size <= period: 
        return np.zeros_like(arr)
    delta = np.diff(arr, prepend=arr[0])
    up = np.clip(delta, 0, None)
    down = -np.clip(delta, None, 0)
    # Wilder's smoothing
    ru = np.empty_like(arr)
    rd = np.empty_like(arr)
    ru[:]=0; rd[:]=0
    ru[period] = up[1:period+1].mean()
    rd[period] = down[1:period+1].mean()
    for i in range(period+1, arr.size):
        ru[i] = (ru[i-1]*(period-1)+up[i])/period
        rd[i] = (rd[i-1]*(period-1)+down[i])/period
    rs = np.divide(ru, rd, out=np.zeros_like(ru), where=rd!=0)
    rsi = 100 - (100/(1+rs))
    return rsi

def classic_pivot_from_prev(highs, lows, closes):
    if highs.size < 2 or lows.size < 2 or closes.size < 2:
        return None
    P = (highs[-2] + lows[-2] + closes[-2]) / 3.0
    return {"P": float(P)}

def qty_for_invest(ltp, rupees):
    return int(max(1, math.floor(rupees / max(ltp, 0.01))))

# ───────── Decision: scan → picks ─────────
@app.post("/api/scan")
@require_authed
def api_scan():
    p = request.get_json(silent=True) or {}
    interval   = p.get("interval", "15minute")
    tp_pct     = float(p.get("tp_pct", 0.8))
    sl_pct     = float(p.get("sl_pct", 0.4))
    entry_type = p.get("entry_order_type", "LIMIT").upper()
    exit_pref  = p.get("exit_order_pref", "AUTO").upper()
    invest_amt = float(p.get("investment_target") or INVEST_AMOUNT)

    long_picks, short_picks, errors = [], [], []
    piv_cache = {}
    for sym in NIFTY50:
        try:
            bars = get_candles(sym, interval=interval, lookback=250)
            closes = bars["close"]
            if closes.size < 30:
                continue
            ema5  = ema(closes, 5)
            ema10 = ema(closes, 10)
            rsi14 = rsi(closes, 14)
            ltp   = float(closes[-1])

            if sym not in piv_cache:
                d = get_candles(sym, interval="day", lookback=20)
                piv_cache[sym] = classic_pivot_from_prev(d["high"], d["low"], d["close"])
            piv = piv_cache[sym]

            rsi_rising = rsi14[-1] > rsi14[-2]
            rsi_falling= rsi14[-1] < rsi14[-2]
            bull = (ema5[-1] > ema10[-1]) and (rsi14[-1] > 50) and rsi_rising and (piv is None or ltp > piv["P"])
            bear = (ema5[-1] < ema10[-1]) and (rsi14[-1] < 50) and rsi_falling and (piv is None or ltp < piv["P"])

            if bull:
                long_picks.append({
                    "symbol": sym, "ltp": ltp, "qty": qty_for_invest(ltp, invest_amt),
                    "tp_pct": tp_pct, "sl_pct": sl_pct,
                    "entry_order_type": entry_type, "exit_order_pref": exit_pref,
                    "interval": interval
                })
            elif bear:
                short_picks.append({
                    "symbol": sym, "ltp": ltp, "qty": qty_for_invest(ltp, invest_amt),
                    "tp_pct": tp_pct, "sl_pct": sl_pct,
                    "entry_order_type": entry_type, "exit_order_pref": exit_pref,
                    "interval": interval
                })
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})

    return jsonify({"ok": True, "long": long_picks, "short": short_picks, "errors": errors})

# ───────── Queue → Confirm → Place ─────────
@app.post("/api/queue_order")
@require_authed
def api_queue_order():
    p = request.get_json(force=True)
    need = ["symbol","side","qty","entry_order_type","tp_pct","sl_pct","exit_order_pref"]
    if not all(k in p for k in need):
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    p["queued_at"] = now_s()
    state["pending_confirms"].append(p)
    return jsonify({"ok": True, "queued": p})

@app.get("/api/pending")
def api_pending():
    return jsonify({"ok": True, "pending": state["pending_confirms"]})

def ltp(symbol):
    data = kite().ltp([f"NSE:{symbol}"])
    return float(data[f"NSE:{symbol}"]["last_price"])

def submit_order(symbol, txn_type, qty, order_type, price=None, tag="app"):
    variety = "regular" if market_live_now() else "amo"
    params = dict(
        variety=variety, exchange="NSE", tradingsymbol=symbol,
        transaction_type=txn_type, quantity=int(qty),
        product=KiteConnect.PRODUCT_MIS, order_type=order_type,
        validity=KiteConnect.VALIDITY_DAY, tag=tag
    )
    if order_type == "LIMIT":
        params["price"] = round(float(price), 2)
    return kite().place_order(**params)

def place_entry_and_track(job):
    sym, side = job["symbol"], job["side"].upper()
    qty       = int(job["qty"])
    tp_pct    = float(job["tp_pct"])
    sl_pct    = float(job["sl_pct"])
    entry_t   = job["entry_order_type"].upper()
    exit_pref = job["exit_order_pref"].upper()

    last = ltp(sym)
    entry_price = last if entry_t == "MARKET" else last * (0.999 if side == "LONG" else 1.001)
    txn = KiteConnect.TRANSACTION_TYPE_BUY if side == "LONG" else KiteConnect.TRANSACTION_TYPE_SELL

    oid = submit_order(sym, txn, qty, entry_t, price=(entry_price if entry_t=="LIMIT" else None))
    state["positions"][sym] = {
        "symbol": sym, "side": side, "qty": qty,
        "entry_price": float(entry_price), "entry_order_id": oid,
        "tp_pct": tp_pct, "sl_pct": sl_pct,
        "exit_pref": exit_pref, "open": True,
        "created_at": now_s()
    }
    return {"order_id": oid, "position": state["positions"][sym]}

@app.post("/api/confirm")
def api_confirm():
    p = request.get_json(force=True)
    if p.get("token","") != AUTO_CONFIRM_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not state["pending_confirms"]:
        return jsonify({"ok": True, "empty": True})
    job = state["pending_confirms"].pop(0)  # pop head
    try:
        res = place_entry_and_track(job)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────── TP/SL monitor ─────────
def monitor_loop():
    while True:
        try:
            for sym, pos in list(state["positions"].items()):
                if not pos.get("open"):
                    continue
                side, qty = pos["side"], int(pos["qty"])
                ent       = float(pos["entry_price"])
                tp_p      = ent * (1 + pos["tp_pct"]/100.0) if side=="LONG" else ent * (1 - pos["tp_pct"]/100.0)
                sl_p      = ent * (1 - pos["sl_pct"]/100.0) if side=="LONG" else ent * (1 + pos["sl_pct"]/100.0)

                price = ltp(sym)
                reason, target = None, None
                if side=="LONG" and price >= tp_p:      reason, target = "TP", tp_p
                elif side=="LONG" and price <= sl_p:    reason, target = "SL", sl_p
                elif side=="SHORT" and price <= tp_p:   reason, target = "TP", tp_p
                elif side=="SHORT" and price >= sl_p:   reason, target = "SL", sl_p
                if not reason: 
                    continue

                otype = "MARKET" if (pos["exit_pref"].upper()=="MARKET" or (pos["exit_pref"].upper()=="AUTO" and market_live_now())) else "LIMIT"
                exit_txn = KiteConnect.TRANSACTION_TYPE_SELL if side=="LONG" else KiteConnect.TRANSACTION_TYPE_BUY
                exit_price = target if otype=="LIMIT" else None

                try:
                    submit_order(sym, exit_txn, qty, otype, price=exit_price, tag=f"exit-{reason}")
                    pnl = (price - ent)*qty if side=="LONG" else (ent - price)*qty
                    pos.update({"open": False, "closed_at": now_s(), "exit_reason": reason, "pnl": round(pnl, 2)})
                    state["closed_trades"].append(pos.copy())
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(5)

def start_engine_once():
    if not state["engine_running"]:
        threading.Thread(target=monitor_loop, daemon=True).start()
        state["engine_running"] = True

# ───────── Health ─────────
@app.get("/ping")
def ping():
    return jsonify({"pong": True, "time_ist": now_s()})

@app.get("/keepalive")
def keepalive():
    if KEEPALIVE_REDIRECT:
        return redirect(KEEPALIVE_REDIRECT, code=302)
    return jsonify({"ok": True, "msg": "keepalive", "time_ist": now_s()})
@app.get("/")
def index():
    start_engine_once()
    is_live = market_live_now()
    logged_in = bool(state["access_token"])
    keepalive_url = KEEPALIVE_REDIRECT or "/keepalive"

    if not logged_in:
        return f"""
        <!doctype html>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Kite Day Trader</title>
        <style>
          body{{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:24px;}}
          .btn{{display:inline-block;padding:10px 16px;background:#1976d2;color:#fff;text-decoration:none;border-radius:6px}}
          .row a{{margin-right:12px}}
          .pill{{padding:2px 8px;border-radius:999px;background:#eef}}
          code{{background:#f6f8fa;padding:2px 6px;border-radius:4px}}
        </style>
        <h2>Kite Day Trader</h2>
        <p>Status: <span class="pill">Not logged in</span> • Market live: <b>{"Yes" if is_live else "No"}</b></p>
        <p><a class="btn" href="/login/start">Login with Kite</a></p>
        <p>Callback configured: <code>{REDIRECT_URL}</code></p>
        <div class="row">
          <a href="/ping" target="_blank">/ping</a>
          <a href="{keepalive_url}" target="_blank">/keepalive</a>
          <a href="/api/status" target="_blank">/api/status</a>
        </div>
        """

    # Logged-in view with form + queue panel
    return f"""
    <!doctype html>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Kite Day Trader</title>
    <style>
      body{{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:20px;}}
      h2{{margin:0 0 6px}}
      .meta{{margin:0 0 16px;color:#444}}
      .pill{{padding:2px 8px;border-radius:999px;background:#e8f5e9}}
      .row a{{margin-right:12px}}
      .card{{border:1px solid #eee;border-radius:8px;padding:14px;margin:14px 0;background:#fff}}
      label{{display:block;margin:8px 0 4px;font-weight:600}}
      input,select{{width:100%;padding:8px 10px;border:1px solid #ddd;border-radius:6px}}
      .grid{{display:grid;grid-template-columns:repeat(2, minmax(0,1fr));gap:12px}}
      .btn{{display:inline-block;padding:10px 16px;background:#2e7d32;color:#fff;text-decoration:none;border-radius:6px;border:0;cursor:pointer}}
      .btn2{{display:inline-block;padding:10px 16px;background:#1976d2;color:#fff;text-decoration:none;border-radius:6px;border:0;cursor:pointer}}
      .btn:disabled, .btn2:disabled{{opacity:.6;cursor:not-allowed}}
      table{{width:100%;border-collapse:collapse}}
      th,td{{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;font-size:13px;}}
      code,pre{{background:#f6f8fa;padding:2px 6px;border-radius:4px}}
      .hint{{color:#666;font-size:12px;margin-top:6px}}
    </style>

    <h2>Kite Day Trader</h2>
    <p class="meta">Status: <span class="pill">Logged in</span> • Market live: <b>{"Yes" if is_live else "No"}</b></p>
    <div class="row">
      <a href="/api/status" target="_blank">/api/status</a>
      <a href="/ping" target="_blank">/ping</a>
      <a href="{keepalive_url}" target="_blank">/keepalive</a>
    </div>

    <div class="card">
      <h3 style="margin:0 0 10px">Queue a test order</h3>
      <div class="grid">
        <div>
          <label>Symbol</label>
          <input id="f_symbol" placeholder="e.g. RELIANCE" />
        </div>
        <div>
          <label>Side</label>
          <select id="f_side">
            <option>LONG</option>
            <option>SHORT</option>
          </select>
        </div>
        <div>
          <label>Qty</label>
          <input id="f_qty" type="number" min="1" value="1" />
        </div>
        <div>
          <label>Entry order type</label>
          <select id="f_eot">
            <option>LIMIT</option>
            <option>MARKET</option>
          </select>
        </div>
        <div>
          <label>TP %</label>
          <input id="f_tp" type="number" step="0.01" value="0.8" />
        </div>
        <div>
          <label>SL %</label>
          <input id="f_sl" type="number" step="0.01" value="0.4" />
        </div>
        <div>
          <label>Exit order pref</label>
          <select id="f_xpref">
            <option>AUTO</option>
            <option>LIMIT</option>
            <option>MARKET</option>
          </select>
        </div>
      </div>
      <div style="margin-top:12px">
        <button class="btn2" id="btnQueue">Queue order</button>
        <span id="queueMsg" class="hint"></span>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 10px">Pending queue</h3>
      <table id="qtable">
        <thead><tr><th>#</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>TP%</th><th>SL%</th><th>Exit</th><th>Queued at</th></tr></thead>
        <tbody></tbody>
      </table>
      <div class="hint">Auto-refreshes every 5s. Sidecar will confirm automatically; or confirm manually below.</div>
    </div>

    <div class="card">
      <h3 style="margin:0 0 10px">Confirm next (manual)</h3>
      <label>Token (AUTO_CONFIRM_TOKEN)</label>
      <input id="f_token" placeholder="enter token…" />
      <div style="margin-top:12px">
        <button class="btn" id="btnConfirm">Confirm index 0</button>
        <span id="confirmMsg" class="hint"></span>
      </div>
    </div>

    <script>
      async function postJSON(url, body) {{
        const r = await fetch(url, {{
          method: 'POST',
          headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify(body)
        }});
        let data; 
        try {{ data = await r.json(); }} catch (e) {{ data = {{ok:false, error:(await r.text()).slice(0,200)}}; }}
        return data;
      }}

      async function refreshQueue() {{
        const r = await fetch('/api/pending');
        let data = {{pending:[]}};
        try {{ data = await r.json(); }} catch(e) {{}}
        const tb = document.querySelector('#qtable tbody');
        tb.innerHTML = '';
        (data.pending||[]).forEach((row, i) => {{
          const tr = document.createElement('tr');
          tr.innerHTML = `<td>${{i}}</td><td>${{row.symbol}}</td><td>${{row.side}}</td>
                          <td>${{row.qty}}</td><td>${{row.entry_order_type}}</td>
                          <td>${{row.tp_pct}}</td><td>${{row.sl_pct}}</td>
                          <td>${{row.exit_order_pref}}</td><td>${{row.queued_at||''}}</td>`;
          tb.appendChild(tr);
        }});
      }}
      setInterval(refreshQueue, 5000);
      refreshQueue();

      document.querySelector('#btnQueue').addEventListener('click', async () => {{
        const payload = {{
          symbol: document.querySelector('#f_symbol').value.trim(),
          side: document.querySelector('#f_side').value,
          qty: Number(document.querySelector('#f_qty').value||1),
          entry_order_type: document.querySelector('#f_eot').value,
          tp_pct: Number(document.querySelector('#f_tp').value||0),
          sl_pct: Number(document.querySelector('#f_sl').value||0),
          exit_order_pref: document.querySelector('#f_xpref').value
        }};
        const msg = document.querySelector('#queueMsg');
        msg.textContent = '…queueing';
        const res = await postJSON('/api/queue_order', payload);
        msg.textContent = res.ok ? 'Queued ✓' : ('Error: ' + (res.error||''));
        if (res.ok) refreshQueue();
      }});

      document.querySelector('#btnConfirm').addEventListener('click', async () => {{
        const token = document.querySelector('#f_token').value.trim();
        const msg = document.querySelector('#confirmMsg');
        if (!token) {{ msg.textContent = 'Token required'; return; }}
        msg.textContent = '…confirming';
        const res = await postJSON('/api/confirm', {{index:0, token}});
        msg.textContent = res.ok ? 'Confirmed ✓' : ('Error: ' + (res.error||''));
        if (res.ok) refreshQueue();
      }});
    </script>
    """

@app.get("/api/status")
def api_status():
    realized = round(sum(c.get("pnl", 0.0) for c in state["closed_trades"]), 2)
    return jsonify({
        "ok": True,
        "live": market_live_now(),
        "positions": list(state["positions"].values()),
        "closed_trades": state["closed_trades"][-50:],
        "realized_pnl": realized,
        "pending_confirms": state["pending_confirms"],
        "logged_in": bool(state["access_token"])
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
