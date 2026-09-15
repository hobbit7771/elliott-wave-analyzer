"""Market Lead Engine - a self-contained realtime microstructure engine.

This package is NOT part of the Elliott wave analyser it lives beside. It
shares the project's shell - the web process, the layout, the Supabase
account, the logging setup - and nothing else. It has its own WebSocket
client, its own order book, its own feature calculations, its own state,
its own storage tables, its own API namespace and its own UI tab.

The boundary is deliberate and one-directional: the Lead Engine never
imports from t3_engine.elliott_engine, t3_engine.signal_engine,
t3_engine.pipeline, t3_engine.execution or t3_engine.risk_engine, and the
older system never reaches inside this package. Everything the rest of the
application may ask of it goes through `LeadEngine` (engine.py) - a facade
with five methods. If a future requirement seems to need more than that,
the answer is another method on the facade, not an import across the line.

Nothing here starts on import. The engine runs only when
T3_LEAD_ENGINE_ENABLED is true, and when it is false this package is inert
even though it is installed: no socket, no thread, no polling, no routes
doing work. See config.py.
"""

from t3_engine.lead_engine.engine import LeadEngine, get_engine

__all__ = ["LeadEngine", "get_engine"]
