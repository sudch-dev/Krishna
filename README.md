# Kite Day Trader (Starter)

A simple Flask + Kite Connect starter for day trading on NIFTY50 with:
- Scanner (EMA 5/10, RSI, daily pivots)
- Queue + Confirm flow (manual or auto-confirm helper)
- MIS orders (LONG/SHORT)
- TP/SL engine (LIMIT off-market, MARKET when live if `exit_pref=AUTO`)
- Trade/Error logs + Realized P&L

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export KITE_API_KEY=xxxx
export KITE_API_SECRET=yyyy
export INVEST_AMOUNT=12000
export AUTO_CONFIRM_TOKEN=some-long-random
export REDIRECT_URL=http://localhost:5000/login/callback
python app.py
```

Open http://localhost:5000 and click **Login with Kite** (configure same redirect URL in your Kite app).

## Auto-confirm helper (optional)

```bash
export APP_URL=http://localhost:5000
export AUTO_CONFIRM_TOKEN=some-long-random
python auto_confirm.py
```

## Notes
- Educational template. Harden for production: order state checks, SL-M stops, retries, rate limits, etc.
- Short selling is subject to broker/exchange rules for MIS.
