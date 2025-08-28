#!/usr/bin/env python3
"""
Auto-confirm sidecar for the Kite day-trader app.

Usage:
  export APP_URL="https://<your-app>.onrender.com"
  export AUTO_CONFIRM_TOKEN="your-secret-token"
  python auto_confirm.py
"""
import os, time, sys, requests

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
TOKEN   = os.environ.get("AUTO_CONFIRM_TOKEN", "")

if not APP_URL or not TOKEN:
    print("[auto_confirm] ERROR: Set APP_URL and AUTO_CONFIRM_TOKEN environment variables.")
    sys.exit(1)

session = requests.Session()
session.headers.update({"User-Agent": "auto-confirm/1.0"})

def confirm_all_pending():
    # Always confirm index 0 repeatedly to avoid index shifting after pop
    while True:
        r = session.get(f"{APP_URL}/api/pending", timeout=10)
        r.raise_for_status()
        data = r.json()
        pending = data.get("pending", [])
        if not pending:
            return 0

        payload = {"index": 0, "token": TOKEN}
        cr = session.post(f"{APP_URL}/api/confirm", json=payload, timeout=15)
        try:
            res = cr.json()
        except Exception:
            res = {"ok": False, "error": cr.text[:200]}
        print("[auto_confirm] confirm ->", res)
        # Small pause to let server update state
        time.sleep(1)

def main():
    print(f"[auto_confirm] watching {APP_URL} ...")
    backoff = 2
    while True:
        try:
            n = confirm_all_pending()
            if n == 0:
                time.sleep(3)
            backoff = 2
        except Exception as e:
            print("[auto_confirm] error:", str(e))
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

if __name__ == "__main__":
    main()
