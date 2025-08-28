# Kite Day Trader (Quality Server)

## Deploy on Render (Quality server)
1. Add env vars from `.env.example` to Render dashboard
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`

## Auto Confirm (sidecar)
```bash
export APP_URL="https://krishna-idxy.onrender.com"
export AUTO_CONFIRM_TOKEN="your-secret-token"
python auto_confirm.py
```
