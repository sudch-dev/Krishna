import os, time, threading, json
from datetime import datetime
from flask import Flask, request, jsonify
from pytz import timezone

IST = timezone("Asia/Kolkata")
AUTO_CONFIRM_TOKEN = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")

app = Flask(__name__)

# in-memory + persisted state
STATE_FILE = "state.json"
state = {"pending_confirms": []}

def now_s(): return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_state():
    global state
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

load_state()

# ───────── Routes ─────────
@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "pending": state["pending_confirms"]
    })

@app.post("/api/queue_order")
def api_queue_order():
    p = request.get_json(force=True)
    required = ["symbol","side","qty","entry_order_type","tp_pct","sl_pct","exit_order_pref"]
    if not all(k in p for k in required):
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    p["queued_at"] = now_s()
    p["confirmed"] = False
    state["pending_confirms"].append(p)
    save_state()
    return jsonify({"ok": True, "queued": p})

@app.get("/api/pending")
def api_pending():
    return jsonify({"ok": True, "pending": state["pending_confirms"]})

@app.post("/api/confirm")
def api_confirm():
    p = request.get_json(force=True)
    token = p.get("token", "")
    idx = int(p.get("index", -1))
    if token != AUTO_CONFIRM_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    if not (0 <= idx < len(state["pending_confirms"])):
        return jsonify({"ok": False, "error": "Index out of range"}), 400
    state["pending_confirms"][idx]["confirmed"] = True
    state["pending_confirms"][idx]["confirmed_at"] = now_s()
    save_state()
    return jsonify({"ok": True, "confirmed": state["pending_confirms"][idx]})

@app.get("/ping")
def ping():
    return jsonify({"pong": True, "time": now_s()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
