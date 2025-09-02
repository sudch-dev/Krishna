#!/usr/bin/env python3
import os, time, requests

APP_URL = os.environ.get("APP_URL", "http://localhost:5000")
TOKEN   = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")

while True:
    try:
        r = requests.get(f"{APP_URL}/api/pending", timeout=10).json()
        pending = r.get("pending", [])
        if pending:
            print("[Sidecar] Found pending:", pending[0])
            res = requests.post(f"{APP_URL}/api/confirm",
                                json={"index":0,"token":TOKEN},
                                timeout=10).json()
            print("[Sidecar] Confirmed:", res)
        else:
            print("[Sidecar] No pending orders")
    except Exception as e:
        print("[Sidecar] Error:", e)
    time.sleep(5)
