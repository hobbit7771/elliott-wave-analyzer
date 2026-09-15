"""MCP adapter for the Market Lead Engine.

A SEPARATE package on purpose, and outside `t3_engine/` on purpose. The
correct ordering is:

    Bybit → Lead Engine → internal state → REST/WebSocket API → MCP → AI

and nothing downstream is allowed to become a dependency of anything
upstream. This package is the last hop: it speaks MCP to an agent and
HTTP to the engine's own read-only API, and it holds no market state, no
calculation and no opinion of its own. If it is switched off, misconfigured
or crashes, the Lead Engine keeps ingesting Bybit and keeps scoring - a
test asserts the import direction that guarantees it.

Every tool here is READ ONLY. There is no write path to expose: the API it
calls has none, and the engine behind that API cannot place an order,
close one, change leverage or touch an exchange credential.
"""

__version__ = "1.0.0"
SCHEMA_VERSION = "1.0.0"
