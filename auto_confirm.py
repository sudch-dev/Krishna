import time, requests, os

BASE = os.environ.get("APP_URL", "http://localhost:5000")
TOKEN = os.environ.get("AUTO_CONFIRM_TOKEN", "changeme")

print("Auto-confirm watcher running:", BASE)
while True:
    try:
        p = requests.get(f"{BASE}/api/pending", timeout=5).json()
        for idx, item in enumerate(list(p.get("pending", []))):
            r = requests.post(f"{BASE}/api/confirm",
                              json={"index": idx, "token": TOKEN},
                              timeout=5).json()
            print("Confirm:", r)
    except Exception as e:
        print("Err:", e)
    time.sleep(3)
