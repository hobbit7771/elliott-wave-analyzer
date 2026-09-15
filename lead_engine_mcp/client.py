"""The HTTP half: a thin read-only client for /api/v1/lead-engine.

Deliberately dumb. It adds the token, it parses JSON, it turns a failure
into a structured answer instead of an exception, and it does nothing
else. Every decision about what the numbers mean stays in the engine.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

BASE_URL_ENV = "LEAD_ENGINE_API_URL"
API_KEY_ENV = "LEAD_ENGINE_API_KEY"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 20.0


class LeadEngineClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 client: Optional[httpx.Client] = None) -> None:
        self.base_url = (base_url or os.getenv(BASE_URL_ENV, "").strip()
                         or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv(API_KEY_ENV, "").strip()
        self.timeout = timeout
        self._client = client

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            # Both spellings, so the adapter works against a deployment
            # configured either way without the operator having to know
            # which one this client picked.
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["x-api-key"] = self.api_key
        return headers

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One GET. Never raises - an agent asking for market data should
        be told what went wrong, not handed a traceback."""
        url = f"{self.base_url}/api/v1/lead-engine/{path.lstrip('/')}"
        http = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = http.get(url, params=params or {}, headers=self._headers())
            try:
                payload = response.json()
            except ValueError:
                return {"error": "the engine returned a non-JSON response",
                        "status": response.status_code, "url": url}
            if response.status_code >= 400:
                return {"error": payload.get("error") or f"HTTP {response.status_code}",
                        "status": response.status_code, "url": url,
                        "hint": _hint_for(response.status_code)}
            return payload
        except httpx.HTTPError as exc:
            return {"error": f"could not reach the Lead Engine: {exc}",
                    "url": url,
                    "hint": f"Is it running, and is {BASE_URL_ENV} pointing at it?"}
        finally:
            if self._client is None:
                http.close()


def _hint_for(status: int) -> str:
    if status == 401:
        return f"Set {API_KEY_ENV} to the deployment's read-only token."
    if status == 404:
        return "External access is disabled: set EXTERNAL_AI_ACCESS_ENABLED=true."
    if status == 429:
        return "Rate limited. The engine allows about ten requests a second per token."
    if status == 503:
        return f"The deployment has no {API_KEY_ENV} set, so it refuses every request."
    return ""
