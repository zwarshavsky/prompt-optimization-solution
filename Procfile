web: python -u -m streamlit run scripts/python/app.py --server.port=$PORT --server.address=0.0.0.0
worker: python -u scripts/python/worker.py
playwright-harness: python -u scripts/python/harness_supervisor.py --stall-seconds 420 --restart-delay-seconds 8

