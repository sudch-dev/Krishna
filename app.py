import os, json, math, threading, time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, url_for
from kiteconnect import KiteConnect
from pytz import timezone
import numpy as np

# ───────── CONFIG ─────────
IST = timezone("Asia/Kolkata")
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")
AUTO_CONFIRM_TOKEN = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")

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
    "instrument_map": {},
    "pending_confirms": [],
    "closed_trades": [],
    "positions": {}
}

# ───────── HELPERS ─────────
def now_s(): return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

def kite():
    if not getattr(kite, "_client", None):
        kite._client = KiteConnect(api_key=KITE_API_KEY)
        if state["access_token"]:
            kite._client.set_access_token(state["access_token"])
    return kite._client

# ───────── LOGIN ─────────
@app.get("/login/start")
def login_start():
    return redirect(kite().login_url())

@app.get("/login/callback")
def login_callback():
    req_token = request.args.get("request_token")
    data = kite().generate_session(req_token, api_secret=KITE_API_SECRET)
    state["access_token"] = data["access_token"]
    kite().set_access_token(state["access_token"])
    return redirect(url_for("root"))

# ───────── INDICATORS ─────────
def ema(arr, span):
    alpha = 2/(span+1)
    out = np.zeros_like(arr)
    out[0] = arr[0]
    for i in range(1,len(arr)):
        out[i] = alpha*arr[i] + (1-alpha)*out[i-1]
    return out

def rsi(arr, period=14):
    delta = np.diff(arr, prepend=arr[0])
    up = np.clip(delta,0,None)
    down = -np.clip(delta,None,0)
    avg_up = up[:period].mean()
    avg_dn = down[:period].mean()
    rs = avg_up/avg_dn if avg_dn != 0 else 0
    rsi_vals=[0]*len(arr)
    for i in range(period,len(arr)):
        avg_up = (avg_up*(period-1)+up[i])/period
        avg_dn = (avg_dn*(period-1)+down[i])/period
        rs = avg_up/avg_dn if avg_dn!=0 else 0
        rsi_vals[i]=100-(100/(1+rs))
    return np.array(rsi_vals)

# ───────── SCAN ─────────
@app.post("/api/scan")
def api_scan():
    signals = []
    for sym in NIFTY50:
        try:
            # Fake candles; replace with kite().historical_data
            prices = np.linspace(100,110,50) + np.random.randn(50)
            ema5, ema10 = ema(prices,5), ema(prices,10)
            rsi14 = rsi(prices,14)
            ltp = prices[-1]
            if ema5[-1]>ema10[-1] and rsi14[-1]>50:
                signals.append({"symbol":sym,"side":"LONG","ltp":ltp,"tp_pct":0.8,"sl_pct":0.4})
            elif ema5[-1]<ema10[-1] and rsi14[-1]<50:
                signals.append({"symbol":sym,"side":"SHORT","ltp":ltp,"tp_pct":0.8,"sl_pct":0.4})
        except Exception: pass

    with open("signals.json","w") as f: json.dump(signals,f,indent=2)
    state["pending_confirms"].extend(signals)
    return jsonify({"ok":True,"signals":signals})

# ───────── PENDING & CONFIRM ─────────
@app.get("/api/pending")
def api_pending():
    return jsonify({"ok":True,"pending":state["pending_confirms"]})

@app.post("/api/confirm")
def api_confirm():
    p=request.get_json(force=True)
    if p.get("token")!=AUTO_CONFIRM_TOKEN: return jsonify({"ok":False,"error":"unauth"}),401
    if not state["pending_confirms"]: return jsonify({"ok":True,"empty":True})
    job=state["pending_confirms"].pop(0)

    # Here call kite().place_order in live
    with open("orders.json","a") as f: f.write(json.dumps(job)+"\n")

    return jsonify({"ok":True,"confirmed":job})

# ───────── ROOT ─────────
@app.get("/")
def root():
    return f"""
    <h2>Kite Day Trader</h2>
    Status: {"<span style='color:green'>Logged in</span>" if state['access_token'] else "Not logged in"}<br>
    <a href='/login/start'>Login to Kite</a><br><br>
    <a href='/api/status'>/api/status</a> | <a href='/api/pending'>/api/pending</a> | <a href='/api/scan'>/api/scan</a>
    """

@app.get("/api/status")
def api_status():
    return jsonify({"ok":True,"pending":state["pending_confirms"],"closed_trades":state["closed_trades"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
