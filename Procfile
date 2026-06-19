web: streamlit run examples/daily-market-dashboard/app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
worker: python3 scripts/alpaca_auto_connect.py && python3 auto_trader.py
