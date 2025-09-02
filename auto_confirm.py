#!/usr/bin/env python3
"""
Sidecar executor for Kite Day Trader.

Usage:
  export APP_URL="https://<your-app>.onrender.com"
  export AUTO_CONFIRM_TOKEN="same-as-app"
  export KITE_API_KEY="..."
  export KITE_API_SECRET="..."
  python sidecar.py
"""
import os, time, requests, traceback
from kiteconnect import KiteConnect
from datetime import datetime
from pytz import timezone

IST = timezone("Asia/Kolkata")
APP_URL = os.environ["APP_URL"].rstrip("/")
TOKEN   = os.environ["AUTO_CONFIRM_TOKEN"]

KITE_API_KEY    = os.environ["KITE_API_KEY"]
KITE_API_SECRET = os.environ["KITE_API_SECRET"]

kite = KiteConnect(api_key=KITE_API_KEY)

def now_s(): return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

def ensure_login():
    if not getattr(kite, "_logged_in", False):
        print("[sidecar] Please login via browser once and set access_token manually!")
        # for demo, assume you already have access token saved
        kite.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
        kite._logged_in = True

def run_loop():
    ensure_login()
    while True:
        try:
            r = requests.get(f"{APP_URL}/api/pending", timeout=10)
            pending = r.json().get("pending", [])
            for idx, job in enumerate(pending):
                if job.get("confirmed") and not job.get("executed"):
                    print(f"[sidecar] Executing -> {job}")
                    try:
                        oid = kite.place_order(
                            variety="regular",
                            exchange="NSE",
                            tradingsymbol=job["symbol"],
                            transaction_type=("BUY" if job["side"]=="LONG" else "SELL"),
                            quantity=int(job["qty"]),
                            product=KiteConnect.PRODUCT_MIS,
                            order_type=job["entry_order_type"],
                            validity=KiteConnect.VALIDITY_DAY,
                            price=None
                        )
                        print(f"[sidecar] Order placed -> {oid}")
                        job["executed"] = True
                    except Exception as e:
                        print("[sidecar] ERROR placing order:", str(e))
            time.sleep(5)
        except Exception as e:
            print("[sidecar] loop error:", e)
            time.sleep(10)

if __name__ == "__main__":
    run_loop()
