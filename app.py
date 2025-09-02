import os, math, threading, time, traceback
from datetime import datetime, timedelta
from functools import wraps

import pandas as pd
import numpy as np
from flask import Flask, jsonify, request, redirect, url_for
from pytz import timezone
from kiteconnect import KiteConnect

# ───────── Config ─────────
IST = timezone("Asia/Kolkata")

KITE_API_KEY       = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET    = os.environ.get("KITE_API_SECRET", "")
INVEST_AMOUNT      = float(os.environ.get("INVEST_AMOUNT", "10000"))   # ₹ default
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

# ───────── Minimal in-memory state ─────────
state = {
    "access_token": None,
    "instrument_map": {},   # {"RELIANCE": 738561, ...}
    "positions": {},        # symbol -> position dict
    "closed_trades": [],    # list of position dicts (closed)
    "pending_confirms": [], # queue for manual/auto confirm
    "engine_running": False
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
    _ = kite().generate_session  # ensure client
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
        return redirect(url_for("root"))
    except Exception as e:
        return f"Login failed: {e}", 500

# ───────── Market data & indicators ─────────
def ensure_instruments_loaded():
    if state["instrument_map"]:
        return
    ins = kite().instruments("NSE")
    df = pd.DataFrame(ins)
    m = df[df["tradingsymbol"].isin(NIFTY50)][["tradingsymbol","instrument_token"]]
    state["instrument_map"] = {r.tradingsymbol: int(r.instrument_token) for _, r in m.iterrows()}

def get_candles(symbol, interval="15minute", lookback=200):
    ensure_instruments_loaded()
    token = state["instrument_map"].get(symbol)
    if not token:
        raise RuntimeError(f"No instrument token for {symbol}")
    end = now_ist()
    days = 14 if "minute" in interval else 365
    start = end - timedelta(days=days)
    data = kite().historical_data(token, start, end, interval, continuous=False, oi=False)
    df = pd.DataFrame(data) if data else pd.DataFrame()
    return df.tail(lookback).reset_index(drop=True)

def ema(series, span): return series.ewm(span=span, adjust=False).mean()

def rsi(series, period=14):
    d = series.diff()
    up = d.clip(lower=0); down = -1*d.clip(upper=0)
    rs = up.ewm(com=period-1, adjust=False).mean() / (down.ewm(com=period-1, adjust=False).mean().replace(0, np.nan))
    return 100 - (100/(1+rs))

def classic_pivots(daily_df):
    if len(daily_df) < 2: return None
    prev = daily_df.iloc[-2]
    P = (prev["high"] + prev["low"] + prev["close"]) / 3.0
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
    daily_cache = {}
    for sym in NIFTY50:
        try:
            df = get_candles(sym, interval=interval, lookback=250)
            if df.empty or len(df) < 30: 
                continue
            df["ema5"]  = ema(df["close"], 5)
            df["ema10"] = ema(df["close"], 10)
            df["rsi14"] = rsi(df["close"], 14)
            last = df.iloc[-1]

            if sym not in daily_cache:
                ddf = get_candles(sym, interval="day", lookback=20)
                daily_cache[sym] = classic_pivots(ddf) if not ddf.empty else None
            piv  = daily_cache[sym]
            ltp  = float(last["close"])
            bull = (last["ema5"] > last["ema10"]) and (last["rsi14"] > 50) and (last["rsi14"] > df["rsi14"].iloc[-2]) and (piv is None or ltp > piv["P"])
            bear = (last["ema5"] < last["ema10"]) and (last["rsi14"] < 50) and (last["rsi14"] < df["rsi14"].iloc[-2]) and (piv is None or ltp < piv["P"])

            if bull:
                long_picks.append({"symbol": sym, "ltp": ltp, "qty": qty_for_invest(ltp, invest_amt),
                                   "tp_pct": tp_pct, "sl_pct": sl_pct,
                                   "entry_order_type": entry_type, "exit_order_pref": exit_pref,
                                   "interval": interval})
            elif bear:
                short_picks.append({"symbol": sym, "ltp": ltp, "qty": qty_for_invest(ltp, invest_amt),
                                    "tp_pct": tp_pct, "sl_pct": sl_pct,
                                    "entry_order_type": entry_type, "exit_order_pref": exit_pref,
                                    "interval": interval})
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
    job = state["pending_confirms"].pop(0)  # always pop head
    try:
        res = place_entry_and_track(job)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────── TP/SL monitor (kept, but tiny) ─────────
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

                # exit order type: AUTO (market if live, else limit)
                otype = "MARKET" if (pos["exit_pref"].upper()=="MARKET" or (pos["exit_pref"].upper()=="AUTO" and market_live_now())) else "LIMIT"
                exit_txn = KiteConnect.TRANSACTION_TYPE_SELL if side=="LONG" else KiteConnect.TRANSACTION_TYPE_BUY
                exit_price = target if otype=="LIMIT" else None

                try:
                    exit_oid = submit_order(sym, exit_txn, qty, otype, price=exit_price, tag=f"exit-{reason}")
                    pnl = (price - ent)*qty if side=="LONG" else (ent - price)*qty
                    pos.update({"open": False, "closed_at": now_s(), "exit_reason": reason,
                                "exit_order_id": exit_oid, "pnl": round(pnl, 2)})
                    state["closed_trades"].append(pos.copy())
                except Exception:
                    pass  # keep it minimal
        except Exception:
            pass
        time.sleep(5)

def start_engine_once():
    if not state["engine_running"]:
        threading.Thread(target=monitor_loop, daemon=True).start()
        state["engine_running"] = True

# ───────── Health / minimal status ─────────
@app.get("/ping")
def ping():
    return jsonify({"pong": True, "time_ist": now_s()})

@app.get("/keepalive")
def keepalive():
    if KEEPALIVE_REDIRECT:
        return redirect(KEEPALIVE_REDIRECT, code=302)
    return jsonify({"ok": True, "msg": "keepalive", "time_ist": now_s()})

@app.get("/")
def root():
    start_engine_once()
    return jsonify({
        "ok": True,
        "live": market_live_now(),
        "logged_in": bool(state["access_token"]),
        "redirect_url": REDIRECT_URL
    })

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

# ───────── Entrypoint (Flask only) ─────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
