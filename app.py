from flask import Flask, request, jsonify, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from kiteconnect import KiteConnect
from datetime import datetime
from pytz import timezone
import os, time, threading, traceback

# ───────────────────────────── Config ─────────────────────────────
IST = timezone("Asia/Kolkata")

KITE_API_KEY       = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET    = os.environ.get("KITE_API_SECRET", "")
AUTO_CONFIRM_TOKEN = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")
INVEST_AMOUNT      = float(os.environ.get("INVEST_AMOUNT", "10000"))
REDIRECT_URL       = os.environ.get("REDIRECT_URL", "http://localhost:5000/login/callback")

# NSE tradingsymbols for NIFTY 50
NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK","BAJAJ-AUTO","BAJFINANCE",
    "BAJAJFINSV","BHARTIARTL","BPCL","BRITANNIA","CIPLA","COALINDIA","DIVISLAB","DRREDDY",
    "EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO","HINDUNILVR",
    "ICICIBANK","INDUSINDBK","INFY","ITC","JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND",
    "NTPC","ONGC","POWERGRID","RELIANCE","SBILIFE","SBIN","SHREECEM","SUNPHARMA","TATACONSUM",
    "TATAMOTORS","TATASTEEL","TCS","TECHM","TITAN","ULTRACEMCO","UPL","WIPRO"
]

app = Flask(__name__)

# Trust Render's proxy so redirects/URLs/HTTPS work correctly
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["PREFERRED_URL_SCHEME"] = "https"

state = {
    "access_token": None,
    "engine_running": False,
    "positions": {},        # symbol -> position dict
    "closed_trades": [],    # list of closed positions
    "pending_confirms": [], # queue requiring confirm
    "tlog": [],             # trade logs
    "elog": [],             # error logs
}

# ───────────────────────────── Helpers ─────────────────────────────
def now_ist():
    return datetime.now(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")

def log_trade(msg, payload=None):
    state["tlog"].append({"ts": now_str(), "msg": msg, "payload": payload or {}})

def log_err(msg, payload=None):
    state["elog"].append({"ts": now_str(), "msg": msg, "payload": payload or {}})

def market_live_now():
    dt = now_ist()
    if dt.weekday() >= 5:  # Sat/Sun
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

def get_login_url():
    kc = kite()
    # Compatibility with both property and method forms
    try:
        return kc.login_url()
    except TypeError:
        return kc.login_url

def ltp(symbol):
    data = kite().ltp([f"NSE:{symbol}"])
    return float(data[f"NSE:{symbol}"]["last_price"])

def submit_order(symbol, txn_type, qty, order_type, price=None, tag="app"):
    """Place simple MIS order (MARKET/LIMIT). Uses AMO off-hours."""
    variety = "regular" if market_live_now() else "amo"
    params = dict(
        variety=variety,
        exchange="NSE",
        tradingsymbol=symbol,
        transaction_type=txn_type,    # KiteConnect.TRANSACTION_TYPE_BUY / _SELL
        quantity=int(qty),
        product=KiteConnect.PRODUCT_MIS,
        order_type=order_type,        # "MARKET" or "LIMIT"
        validity=KiteConnect.VALIDITY_DAY,
        tag=tag
    )
    if order_type == "LIMIT":
        if price is None:
            raise RuntimeError("LIMIT needs price")
        params["price"] = round(float(price), 2)
    oid = kite().place_order(**params)
    log_trade("Order placed", {"symbol": symbol, "txn": txn_type, "qty": qty, "type": order_type, "price": price, "variety": variety, "order_id": oid})
    return oid

# ───────────────────────────── Auth ─────────────────────────────
@app.route("/login/start")
def login_start():
    return redirect(get_login_url())

@app.route("/login/callback")
def login_callback():
    req_token = request.args.get("request_token")
    if not req_token:
        return "Missing request_token", 400
    try:
        data = kite().generate_session(req_token, api_secret=KITE_API_SECRET)
        state["access_token"] = data["access_token"]
        kite().set_access_token(state["access_token"])
        log_trade("Kite login success", {"user_id": data.get("user_id")})
        # Normal redirect to home
        try:
            return redirect(url_for("home"))
        except Exception:
            pass
        # Fallback for strict/mobile browsers
        return (
            "<script>location.replace('/');</script>"
            "<noscript><a href='/'>Continue to home</a></noscript>",
            200,
            {"Content-Type": "text/html"},
        )
    except Exception as e:
        log_err("Login error", {"error": str(e), "trace": traceback.format_exc()})
        return "Login failed. See /api/logs?type=error", 500

# ───────────────────────────── Basic pages ─────────────────────────────
@app.route("/")
def home():
    return (
        "✅ Flask up. "
        f"Logged in: {'yes' if state['access_token'] else 'no'}. "
        "GET /healthz • GET /api/nifty50 • GET /api/ltp?symbol=RELIANCE • "
        "POST /api/queue_order then POST /api/confirm"
    )

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "live": market_live_now()})

# ───────────────────────────── NIFTY 50 routes ─────────────────────────────
@app.get("/api/nifty50")
def list_nifty50():
    return jsonify({"ok": True, "symbols": NIFTY50})

@app.get("/api/ltp")
def api_ltp():
    sym = request.args.get("symbol")
    if not sym:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    if sym not in NIFTY50:
        return jsonify({"ok": False, "error": "symbol not in NIFTY50"}), 400
    try:
        return jsonify({"ok": True, "symbol": sym, "ltp": ltp(sym)})
    except Exception as e:
        log_err("LTP error", {"symbol": sym, "error": str(e)})
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────────────────────────── Queue & Confirm ─────────────────────────────
@app.post("/api/queue_order")
def api_queue():
    """
    JSON:
    {
      "symbol": "RELIANCE",           # must be in NIFTY50
      "side": "LONG" | "SHORT",
      "qty": 10,
      "entry_type": "MARKET" | "LIMIT",
      "limit_price": 2800,            # required if entry_type=LIMIT
      "tp_pct": 0.8,                  # percent
      "sl_pct": 0.4                   # percent
    }
    """
    if not state["access_token"]:
        return jsonify({"ok": False, "error": "Login required"}), 401
    p = request.get_json(force=True, silent=True) or {}
    for k in ["symbol", "side", "qty", "entry_type", "tp_pct", "sl_pct"]:
        if k not in p:
            return jsonify({"ok": False, "error": f"Missing {k}"}), 400
    if p["symbol"] not in NIFTY50:
        return jsonify({"ok": False, "error": "Symbol not in NIFTY50"}), 400
    if p["entry_type"].upper() == "LIMIT" and "limit_price" not in p:
        return jsonify({"ok": False, "error": "limit_price required for LIMIT"}), 400
    p["queued_at"] = now_str()
    state["pending_confirms"].append(p)
    log_trade("Queued order", p)
    return jsonify({"ok": True, "queued": p})

@app.get("/api/pending")
def api_pending():
    return jsonify({"ok": True, "pending": state["pending_confirms"]})

@app.post("/api/confirm")
def api_confirm():
    """
    JSON: { "index": 0, "token": "AUTO_CONFIRM_TOKEN" }
    """
    if not state["access_token"]:
        return jsonify({"ok": False, "error": "Login required"}), 401
    p = request.get_json(force=True)
    token = p.get("token", "")
    idx = int(p.get("index", -1))
    if token != AUTO_CONFIRM_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not (0 <= idx < len(state["pending_confirms"])):
        return jsonify({"ok": False, "error": "Index out of range"}), 400

    job = state["pending_confirms"].pop(idx)
    try:
        res = place_and_track(job)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        log_err("Confirm->place failed", {"job": job, "error": str(e), "trace": traceback.format_exc()})
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────────────────────────── Place + TP/SL watcher ─────────────────────────────
def place_and_track(job):
    sym    = job["symbol"]
    side   = job["side"].upper()
    qty    = int(job["qty"])
    etype  = job["entry_type"].upper()
    tp_pct = float(job["tp_pct"]) / 100.0
    sl_pct = float(job["sl_pct"]) / 100.0

    entry_price = ltp(sym) if etype == "MARKET" else float(job["limit_price"])
    entry_txn = KiteConnect.TRANSACTION_TYPE_BUY if side == "LONG" else KiteConnect.TRANSACTION_TYPE_SELL

    oid = submit_order(sym, entry_txn, qty, etype,
                       price=(entry_price if etype == "LIMIT" else None),
                       tag="entry")

    pos = {
        "symbol": sym, "side": side, "qty": qty,
        "entry_price": float(entry_price), "tp_pct": tp_pct*100, "sl_pct": sl_pct*100,
        "open": True, "entry_order_id": oid, "created_at": now_str()
    }
    state["positions"][sym] = pos
    log_trade("Position opened", pos)
    return {"order_id": oid, "position": pos}

def watcher():
    while True:
        try:
            for sym, pos in list(state["positions"].items()):
                if not pos.get("open"):
                    continue
                side = pos["side"]; qty = pos["qty"]; ent = float(pos["entry_price"])
                tp_pct = float(pos["tp_pct"]) / 100.0
                sl_pct = float(pos["sl_pct"]) / 100.0
                price = ltp(sym)

                tp_price = ent * (1 + tp_pct) if side == "LONG" else ent * (1 - tp_pct)
                sl_price = ent * (1 - sl_pct) if side == "LONG" else ent * (1 + sl_pct)

                exit_reason, target = None, None
                if side == "LONG" and price >= tp_price: exit_reason, target = "TP", tp_price
                elif side == "LONG" and price <= sl_price: exit_reason, target = "SL", sl_price
                elif side == "SHORT" and price <= tp_price: exit_reason, target = "TP", tp_price
                elif side == "SHORT" and price >= sl_price: exit_reason, target = "SL", sl_price
                if not exit_reason: continue

                otype = "MARKET" if market_live_now() else "LIMIT"
                exit_txn = KiteConnect.TRANSACTION_TYPE_SELL if side == "LONG" else KiteConnect.TRANSACTION_TYPE_BUY
                price_arg = None if otype == "MARKET" else target

                try:
                    exit_oid = submit_order(sym, exit_txn, qty, otype, price=price_arg, tag=f"exit-{exit_reason}")
                    pnl = (price - ent) * qty if side == "LONG" else (ent - price) * qty
                    pos.update({"open": False, "closed_at": now_str(), "exit_order_id": exit_oid,
                                "exit_reason": exit_reason, "pnl": round(pnl, 2), "exit_price_hint": price})
                    state["closed_trades"].append(pos.copy())
                    log_trade("Position closed", pos)
                except Exception as e:
                    log_err("Exit order failed", {"symbol": sym, "error": str(e), "pos": pos})
        except Exception as e:
            log_err("Watcher error", {"error": str(e), "trace": traceback.format_exc()})
        time.sleep(5)

def start_engine():
    if not state["engine_running"]:
        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        state["engine_running"] = True
        log_trade("Engine started")

start_engine()

# ───────────────────────────── Status & Logs ─────────────────────────────
@app.get("/api/status")
def api_status():
    realized = sum([c.get("pnl", 0.0) for c in state["closed_trades"]])
    return jsonify({
        "ok": True,
        "logged_in": bool(state["access_token"]),
        "live": market_live_now(),
        "positions": list(state["positions"].values()),
        "closed_trades": state["closed_trades"][-50:],
        "realized_pnl": round(realized, 2),
        "pending_confirms": state["pending_confirms"]
    })

@app.get("/api/logs")
def api_logs():
    kind = request.args.get("type", "trade")
    if kind == "error":
        return jsonify({"ok": True, "logs": state["elog"][-200:]})
    return jsonify({"ok": True, "logs": state["tlog"][-200:]})

# ───────────────────────────── Local run (Render uses Gunicorn) ─────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
