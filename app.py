"""Entry point for WSGI-style process managers that hardcode `app:app`
(this is exactly what Render's Python auto-detect assumed when this
service was first created: `gunicorn app:app`).

The real application is the T3 Elliott Wave dashboard - a FastAPI (ASGI)
app at t3_engine/dashboard/server.py. This file just re-exports it under
the name `app` so `gunicorn app:app` resolves to something real instead of
crashing on import. gunicorn.conf.py (repo root) tells gunicorn to run it
through an ASGI-capable worker (Uvicorn's), since a bare ASGI app cannot
be served by gunicorn's default sync WSGI workers.

This file used to be a separate Flask prototype (single-timeframe REST
analysis + matplotlib chart) that imported binance_connector.py,
elliott_wave_analyzer.py, fibonacci_calculator.py, visualizer.py - none of
which exist in this repository, so it never actually ran. That version is
still recoverable from git history (see commits before the T3 engine work)
if anyone wants to resurrect it; it wasn't functional code being removed.
"""

from t3_engine.dashboard.server import app  # noqa: F401
