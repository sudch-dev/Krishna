#!/usr/bin/env python3
"""
Auto-confirm sidecar for the Kite day-trader app (Render-friendly).

Env:
  APP_URL="https://<your-app>.onrender.com"
  AUTO_CONFIRM_TOKEN="your-secret-token"
  KEEPALIVE_URL="https://<your-app>.onrender.com/keepalive"   # optional
"""
import os, time, sys, requests

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
TOKEN   = os.environ.get("AUTO_CONFIRM_TOKEN", "")
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL", "").strip()

if not APP_URL or not TOKEN:
    print("[auto_confirm] ERROR: Set APP_URL and AUTO_CONFIRM_TOKEN environment variables.")
    sys.exit(1)

session = requests.Session()
session.headers.update({"User-Agent": "auto-confirm/1.1"})

def confirm_all_pending():
    # Always pop index 0 (contract with the server)
    did_any = False
    while True:
        r = session.get(f"{APP_URL}/api/pending", timeout=10)
        r.raise_for_status()
        data = r.json()
        pending = data.get("pending", [])
        if not pending:
            return did_any

        payload = {"index": 0, "token": TOKEN}
        cr = session.post(f"{APP_URL}/api/confirm", json=payload, timeout=15)
        try:
            res = cr.json()
        except Exception:
            res = {"ok": False, "error": f"HTTP {cr.status_code}: {cr.text[:200]}"}
        print("[auto_confirm] confirm ->", res)
        did_any = True
        time.sleep(1)  # give server a moment to update
    # unreachable

def maybe_ping_keepalive():
    if not KEEPALIVE_URL:
        return
    try:
        pr = session.get(KEEPALIVE_URL, timeout=8, allow_redirects=True)
        print("[auto_confirm] keepalive:", pr.status_code)
    except Exception as e:
        print("[auto_confirm] keepalive error:", str(e))

def main():
    print(f"[auto_confirm] watching {APP_URL} ...")
    backoff = 2
    while True:
        try:
            did = confirm_all_pending()
            if not did:
                # nothing to do: idle a bit and optionally ping keepalive
                maybe_ping_keepalive()
                time.sleep(3)
            backoff = 2
        except Exception as e:
            print("[auto_confirm] error:", str(e))
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

if __name__ == "__main__":
    main()
