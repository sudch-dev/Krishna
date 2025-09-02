import os, json, math, threading, time, traceback
from datetime import datetime, timedelta
from functools import wraps

import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, render_template, redirect, url_for
from pytz import timezone
from kiteconnect import KiteConnect

# ───────────────────────── Config ─────────────────────────
IST = timezone("Asia/Kolkata")

KITE_API_KEY       = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET    = os.environ.get("KITE_API_SECRET", "")
INVEST_AMOUNT      = float(os.environ.get("INVEST_AMOUNT", "10000"))   # ₹ default
AUTO_CONFIRM_TOKEN = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")
REDIRECT_URL       = os.environ.get("REDIRECT_URL", "http://localhost:5000/login/callback")

# NEW (optional): if set, /keepalive will 302 to this URL
KEEPALIVE_REDIRECT = os.environ.get("KEEPALIVE_REDIRECT", "").strip()

# NIFTY 50 symbols (tradingsymbols on NSE; edit if needed)
NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE",
    "BAJAJFINSV","BHARTIARTL","BPCL","BRITANNIA","CIPLA","COALINDIA","DIVISLAB","DRREDDY",
    "EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO","HINDUNILVR",
    "ICICIBANK","INDUSINDBK","INFY","ITC","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND",
    "NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SBIN","SHREECEM","SUNPHARMA","TATACONSUM",
    "TATAMOTORS","TATASTEEL","TCS","TECHM","TITAN","ULTRACEMCO","UPL","WIPRO"
]

# ───────────────────────── Flask ─────────────────────────
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# in-memory state (also mirrored to files for persistence)
state = {
    "access_token": None,
    "instrument_map": {},   # {"RELIANCE": 738561, ...}
    "positions": {},        # key: symbol -> dict(position)
    "closed_trades": [],    # list of dicts
    "pending_confirms": [], # pre-trade confirms (UI or auto-confirm script)
    "trade_log": [],
    "error_log": [],
    "engine_running": False
}

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

def now_ist():
    return datetime.now(IST)

def now_ist_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")

def log_trade(msg, payload=None):
    entry = {"ts": now_ist_str(), "msg": msg, "payload": payload or {}}
    state["trade_log"].append(entry)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "trade_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def log_error(msg, payload=None):
    entry = {"ts": now_ist_str(), "msg": msg, "payload": payload or {}}
    state["error_log"].append(entry)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "error_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def market_live_now():
    dt = now_ist()
    if dt.weekday() >= 5:   # Sat/Sun
        return False
    # Regular market 09:15–15:30 IST (ignore pre/auction for simplicity)
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

# ───────────────────────── Auth flow ─────────────────────────
@app.route("/login/start")
def login_start():
    """Redirect user to Kite login page."""
    _ = kite().generate_session  # ensure api_key loaded
    login_url = kite().login_url()
    return redirect(login_url)

@app.route("/login/callback")
def login_callback():
    """Kite redirects here with request_token."""
    req_token = request.args.get("request_token")
    if not req_token:
        return "Missing request_token", 400
    try:
        data = kite().generate_session(req_token, api_secret=KITE_API_SECRET)
        state["access_token"] = data["access_token"]
        kite().set_access_token(state["access_token"])
        log_trade("Kite login success", {"user_id": data.get("user_id")})
        return redirect(url_for("index"))
    except Exception as e:
        log_error("Login error", {"error": str(e), "trace": traceback.format_exc()})
        return "Login failed. Check logs.", 500

# ───────────────────────── Utilities ─────────────────────────
def ensure_instruments_loaded():
    """Build symbol -> instrument_token map for NSE once per process."""
    if state["instrument_map"]:
        return
    try:
        ins = kite().instruments("NSE")
        df = pd.DataFrame(ins)
        # tradingsymbol is exact key
        wanted = set(NIFTY50)
        m = df[df["tradingsymbol"].isin(wanted)][["tradingsymbol","instrument_token"]]
        state["instrument_map"] = {r.tradingsymbol: int(r.instrument_token) for _, r in m.iterrows()}
        missing = [s for s in NIFTY50 if s not in state["instrument_map"]]
        if missing:
            log_error("Missing tokens (check symbol spelling or NSE instruments)", {"missing": missing})
        else:
            log_trade("Instrument map loaded", {"count": len(state["instrument_map"])})
    except Exception as e:
        log_error("Failed loading instruments", {"error": str(e)})

def get_candles(symbol, interval="15minute", lookback=200):
    """Fetch recent historical candles for a symbol as DataFrame with ['date','open','high','low','close','volume']."""
    ensure_instruments_loaded()
    token = state["instrument_map"].get(symbol)
    if not token:
        raise RuntimeError(f"No instrument token for {symbol}")
    end = now_ist()
    # pick a wide window to cover lookback bars
    days = 14 if "minute" in interval else 365
    start = end - timedelta(days=days)
    data = kite().historical_data(token, start, end, interval, continuous=False, oi=False)
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    # harmonized column names already fine with Kite
    return df.tail(lookback).reset_index(drop=True)

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.ewm(com=period-1, adjust=False).mean()
    ma_down = down.ewm(com=period-1, adjust=False).mean()
    rs = ma_up / (ma_down.replace(0, np.nan))
    return 100 - (100 / (1 + rs))

def classic_pivots(daily_df):
    """Expect a daily timeframe DF. Returns P,S1,R1 based on previous day's H/L/C."""
    if len(daily_df) < 2:
        return None
    prev = daily_df.iloc[-2]
    P = (prev["high"] + prev["low"] + prev["close"]) / 3.0
    R1 = 2*P - prev["low"]
    S1 = 2*P - prev["high"]
    return {"P": float(P), "S1": float(S1), "R1": float(R1)}

def qty_for_invest(symbol, ltp, invest_amount):
    if ltp <= 0:
        return 0
    q = int(max(1, math.floor(invest_amount / ltp)))
    return q

def require_authed(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not state["access_token"]:
            return jsonify({"ok": False, "error": "Login required"}), 401
        return fn(*args, **kwargs)
    return wrapper

# ───────────────────────── Scanner ─────────────────────────
@app.post("/api/scan")
@require_authed
def api_scan():
    """
    Body JSON:
    {
      "interval": "15minute",
      "tp_pct": 0.8,
      "sl_pct": 0.4,
      "entry_order_type": "LIMIT" | "MARKET",
      "exit_order_pref": "AUTO",
      "investment_target": 12000
    }
    """
    p = request.get_json(force=True, silent=True) or {}
    interval = p.get("interval", "15minute")
    tp_pct  = float(p.get("tp_pct", 0.8))
    sl_pct  = float(p.get("sl_pct", 0.4))
    entry_type = p.get("entry_order_type", "LIMIT").upper()
    exit_pref  = p.get("exit_order_pref", "AUTO").upper()
    invest_amt = float(p.get("investment_target") or INVEST_AMOUNT)

    long_picks, short_picks = [], []
    errors = []

    try:
        daily_cache = {}  # pivot inputs per symbol
        for sym in NIFTY50:
            try:
                df = get_candles(sym, interval=interval, lookback=250)
                if df.empty or len(df) < 30:
                    continue
                df["ema5"]  = ema(df["close"], 5)
                df["ema10"] = ema(df["close"], 10)
                df["rsi14"] = rsi(df["close"], 14)
                last = df.iloc[-1]

                # daily pivots
                if sym not in daily_cache:
                    ddf = get_candles(sym, interval="day", lookback=20)
                    piv = classic_pivots(ddf) if not ddf.empty else None
                    daily_cache[sym] = piv
                piv = daily_cache[sym]

                ltp_val = float(last["close"])
                # RSI heuristics
                rsi_bull = last["rsi14"] > 50 and (last["rsi14"] > df["rsi14"].iloc[-2])
                rsi_bear = last["rsi14"] < 50 and (last["rsi14"] < df["rsi14"].iloc[-2])
                ema_bull = last["ema5"] > last["ema10"]
                ema_bear = last["ema5"] < last["ema10"]

                pivot_ok_long = True if (piv is None) else (ltp_val > piv["P"])
                pivot_ok_short = True if (piv is None) else (ltp_val < piv["P"])

                if ema_bull and rsi_bull and pivot_ok_long:
                    qty = qty_for_invest(sym, ltp_val, invest_amt)
                    long_picks.append({
                        "symbol": sym, "ltp": ltp_val, "qty": qty,
                        "tp_pct": tp_pct, "sl_pct": sl_pct,
                        "entry_order_type": entry_type,
                        "exit_order_pref": exit_pref,
                        "interval": interval
                    })
                elif ema_bear and rsi_bear and pivot_ok_short:
                    qty = qty_for_invest(sym, ltp_val, invest_amt)
                    short_picks.append({
                        "symbol": sym, "ltp": ltp_val, "qty": qty,
                        "tp_pct": tp_pct, "sl_pct": sl_pct,
                        "entry_order_type": entry_type,
                        "exit_order_pref": exit_pref,
                        "interval": interval
                    })
            except Exception as e:
                errors.append({"symbol": sym, "error": str(e)})

        return jsonify({"ok": True, "long": long_picks, "short": short_picks, "errors": errors})
    except Exception as e:
        log_error("Scan failed", {"error": str(e), "trace": traceback.format_exc()})
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────────────────────── Confirm & Place ─────────────────────────
@app.post("/api/queue_order")
@require_authed
def api_queue_order():
    """
    Body JSON:
    {
      "symbol": "RELIANCE",
      "side": "LONG" | "SHORT",
      "qty": 10,
      "entry_order_type": "LIMIT" | "MARKET",
      "tp_pct": 0.8,
      "sl_pct": 0.4,
      "exit_order_pref": "AUTO" | "LIMIT" | "MARKET"
    }
    Adds to pending queue requiring confirm (UI or auto-confirm script).
    """
    p = request.get_json(force=True)
    required = ["symbol","side","qty","entry_order_type","tp_pct","sl_pct","exit_order_pref"]
    if not all(k in p for k in required):
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    p["queued_at"] = now_ist_str()
    state["pending_confirms"].append(p)
    return jsonify({"ok": True, "queued": p})

@app.get("/api/pending")
def api_pending():
    return jsonify({"ok": True, "pending": state["pending_confirms"]})

@app.post("/api/confirm")
def api_confirm():
    """
    Body JSON: { "index": 0, "token": "AUTO_CONFIRM_TOKEN" }
    Removes from pending queue and places the order immediately.
    Sidecar calls this repeatedly with index=0 to avoid shifting.
    """
    p = request.get_json(force=True)
    token = p.get("token","")
    idx = int(p.get("index", -1))
    if token != AUTO_CONFIRM_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not state["pending_confirms"]:
        return jsonify({"ok": True, "empty": True})
    # Enforce head pop for safety regardless of idx
    job = state["pending_confirms"].pop(0)
    try:
        res = place_entry_and_track(job)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        log_error("Confirm->place failed", {"job": job, "error": str(e)})
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────────────────────── Order Engine ─────────────────────────
def ltp(symbol):
    data = kite().ltp([f"NSE:{symbol}"])
    return float(data[f"NSE:{symbol}"]["last_price"])

def submit_order(symbol, txn_type, qty, order_type, price=None, variety=None, tag="app"):
    """Submit a simple equity order on NSE with MIS product."""
    variety = variety or ("amo" if not market_live_now() else "regular")
    params = dict(
        variety=variety,
        exchange="NSE",
        tradingsymbol=symbol,
        transaction_type=txn_type,
        quantity=int(qty),
        product=KiteConnect.PRODUCT_MIS,
        order_type=order_type,  # "MARKET" or "LIMIT"
        validity=KiteConnect.VALIDITY_DAY,
        tag=tag
    )
    if order_type == "LIMIT":
        if price is None:
            raise RuntimeError("LIMIT order needs price")
        params["price"] = round(float(price), 2)
    oid = kite().place_order(**params)
    log_trade("Order placed", {"symbol": symbol, "txn": txn_type, "qty": qty, "type": order_type, "price": price, "variety": variety, "order_id": oid})
    return oid

def place_entry_and_track(job):
    """
    job = {
      symbol, side("LONG"/"SHORT"), qty, entry_order_type, tp_pct, sl_pct, exit_pref
    }
    """
    sym   = job["symbol"]
    side  = job["side"].upper()
    qty   = int(job["qty"])
    tp_pct= float(job["tp_pct"])
    sl_pct= float(job["sl_pct"])
    entry_type = job["entry_order_type"].upper()     # LIMIT | MARKET
    exit_pref  = job["exit_order_pref"].upper()      # AUTO | LIMIT | MARKET

    last = ltp(sym)
    entry_price = last
    if entry_type == "LIMIT":
        # modest price improvement: for LONG set a tiny below LTP; for SHORT a tiny above
        entry_price = last * (0.999 if side == "LONG" else 1.001)

    txn = KiteConnect.TRANSACTION_TYPE_BUY if side == "LONG" else KiteConnect.TRANSACTION_TYPE_SELL
    oid = submit_order(sym, txn, qty, entry_type, price=entry_price if entry_type=="LIMIT" else None)

    pos_key = sym
    state["positions"][pos_key] = {
        "symbol": sym, "side": side, "qty": qty,
        "entry_price": float(entry_price), "entry_order_id": oid,
        "tp_pct": tp_pct, "sl_pct": sl_pct,
        "exit_pref": exit_pref, "open": True,
        "created_at": now_ist_str()
    }
    log_trade("Position opened (tracking)", state["positions"][pos_key])
    return {"order_id": oid, "position": state["positions"][pos_key]}

def monitor_loop():
    """Simple background monitor to exit at TP/SL (MARKET in live, LIMIT in off-market OR as per exit_pref)."""
    while True:
        try:
            for k, pos in list(state["positions"].items()):
                if not pos.get("open"):
                    continue
                sym, side, qty = pos["symbol"], pos["side"], pos["qty"]
                ent = float(pos["entry_price"])
                tp_pct = float(pos["tp_pct"]) / 100.0
                sl_pct = float(pos["sl_pct"]) / 100.0
                price = ltp(sym)
                # targets
                tp_price = ent * (1 + tp_pct) if side == "LONG" else ent * (1 - tp_pct)
                sl_price = ent * (1 - sl_pct) if side == "LONG" else ent * (1 + sl_pct)

                exit_by = None
                if side == "LONG":
                    if price >= tp_price:
                        exit_by = ("TP", tp_price)
                    elif price <= sl_price:
                        exit_by = ("SL", sl_price)
                else: # SHORT
                    if price <= tp_price:
                        exit_by = ("TP", tp_price)
                    elif price >= sl_price:
                        exit_by = ("SL", sl_price)

                if not exit_by:
                    continue

                reason, target = exit_by
                # exit order type
                if pos["exit_pref"] == "LIMIT":
                    otype = "LIMIT"
                elif pos["exit_pref"] == "MARKET":
                    otype = "MARKET"
                else:
                    # AUTO: LIMIT if off-market, MARKET if live
                    otype = "MARKET" if market_live_now() else "LIMIT"

                exit_txn = KiteConnect.TRANSACTION_TYPE_SELL if side == "LONG" else KiteConnect.TRANSACTION_TYPE_BUY
                price_param = None
                if otype == "LIMIT":
                    price_param = target

                try:
                    exit_oid = submit_order(sym, exit_txn, qty, otype, price=price_param, tag=f"exit-{reason}")
                    pnl = (price - ent) * qty if side == "LONG" else (ent - price) * qty
                    pos.update({"open": False, "closed_at": now_ist_str(), "exit_reason": reason, "exit_order_id": exit_oid, "pnl": round(pnl, 2), "exit_price_hint": price})
                    state["closed_trades"].append(pos.copy())
                    log_trade(f"Position closed [{reason}]", pos)
                except Exception as e:
                    log_error("Exit order failed", {"symbol": sym, "err": str(e), "pos": pos})
        except Exception as e:
            log_error("Monitor loop error", {"err": str(e), "trace": traceback.format_exc()})
        time.sleep(5)

# start monitor thread once
def start_engine():
    if not state["engine_running"]:
        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()
        state["engine_running"] = True
        log_trade("Engine started")

# ───────────────────────── Keep-alive / Health ─────────────────────────
@app.get("/ping")
def ping():
    return jsonify({"pong": True, "time_ist": now_ist_str()})

@app.get("/keepalive")
def keepalive():
    # If env is set, redirect; otherwise return OK JSON
    from flask import redirect
    if KEEPALIVE_REDIRECT:
        return redirect(KEEPALIVE_REDIRECT, code=302)
    return jsonify({"ok": True, "msg": "keepalive", "time_ist": now_ist_str()})

# ───────────────────────── API: status & logs ─────────────────────────
@app.get("/")
def index():
    start_engine()
    return render_template("index.html",
                           invest_amount=INVEST_AMOUNT,
                           is_live=market_live_now(),
                           redirect_url=REDIRECT_URL,
                           logged_in=bool(state["access_token"]))

@app.get("/api/status")
def api_status():
    realized = sum([c.get("pnl", 0.0) for c in state["closed_trades"]])
    return jsonify({
        "ok": True,
        "live": market_live_now(),
        "positions": list(state["positions"].values()),
        "closed_trades": state["closed_trades"][-50:],
        "realized_pnl": round(realized, 2),
        "pending_confirms": state["pending_confirms"],
        "logged_in": bool(state["access_token"])
    })

@app.get("/api/logs")
def api_logs():
    kind = request.args.get("type", "trade")
    if kind == "error":
        return jsonify({"ok": True, "logs": state["error_log"][-200:]})
    return jsonify({"ok": True, "logs": state["trade_log"][-200:]})

# ───────────────────────── Main ─────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[{now_ist_str()}] Starting server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
