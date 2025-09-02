#!/usr/bin/env python3
"""
Confirm-only sidecar for the Kite day-trader app.

It DOES NOT scan or queue.
It ONLY confirms already-queued jobs.

Env:
  APP_URL              e.g. https://<your-app>.onrender.com   (required)
  AUTO_CONFIRM_TOKEN   must match server's env                 (required)
  POLL_SEC             seconds between polls (default 3)
  KEEPALIVE_URL        optional; GET this every POLL_SEC*5
"""
import os, time, sys, requests

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
TOKEN   = os.environ.get("AUTO_CONFIRM_TOKEN", "")
POLL    = float(os.environ.get("POLL_SEC", "3"))
KA_URL  = os.environ.get("KEEPALIVE_URL", "").strip()

if not APP_URL or not TOKEN:
    print("[auto_confirm] ERROR: Set APP_URL and AUTO_CONFIRM_TOKEN.")
    sys.exit(1)

S = requests.Session()
S.headers.update({"User-Agent": "auto-confirm/1.1"})

def get_pending():
    r = S.get(f"{APP_URL}/api/pending", timeout=10)
    r.raise_for_status()
    return (r.json() or {}).get("pending", [])

def confirm_index0():
    payload = {"index": 0, "token": TOKEN}
    r = S.post(f"{APP_URL}/api/confirm", json=payload, timeout=15)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": r.text[:200]}

def maybe_keepalive(tick):
    if KA_URL and (tick % 5 == 0):  # every ~5 polls
        try:
            S.get(KA_URL, timeout=5)
        except Exception:
            pass

def main():
    print(f"[auto_confirm] watching {APP_URL} ...")
    backoff = 2.0
    tick = 0
    while True:
        try:
            tick += 1
            maybe_keepalive(tick)

            # drain the queue by repeatedly confirming index 0
            while True:
                pending = get_pending()
                if not pending:
                    break
                res = confirm_index0()
                print("[auto_confirm] confirm ->", res)
                # small pause to let server update state
                time.sleep(1.0)

            # nothing pending → sleep regular poll
            time.sleep(POLL)
            backoff = 2.0

        except KeyboardInterrupt:
            print("\n[auto_confirm] stopped by user.")
            return
        except Exception as e:
            print("[auto_confirm] error:", str(e))
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

if __name__ == "__main__":
    main()
