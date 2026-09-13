"""Storage over Supabase's REST API instead of a Postgres connection.

Why this exists rather than "just use T3_DATABASE_URL": a Postgres URL
needs the database password, which Supabase shows exactly once at project
creation and never again - and a new free project's DIRECT host is
IPv6-only, which the hosting this app runs on cannot reach at all. The
service key, by contrast, is retrievable from the dashboard at any time
and PostgREST answers over ordinary IPv4 HTTPS. So the credential that is
actually obtainable is the one this talks to.

It is deliberately small. The two things that must survive a redeploy -
the saved wave counts and the trade journal - need exactly four
operations between them: insert rows, select rows with equality filters,
order and limit them, and delete rows matching a filter. PostgREST does
all four over URL parameters, so this is a thin translation layer and not
an ORM.

SECURITY: the key used here is the SECRET (service-role) key. It bypasses
Row Level Security, which is the point - it means RLS can be switched ON
for every table, closing the hole where the publishable key, which is
public by design, could read and write the same rows. The key lives in an
environment variable, never in this repository.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

# Read at call time rather than import time, so a test (or a deploy that
# sets them later) is not defeated by module import order.
URL_ENV = "T3_SUPABASE_URL"
KEY_ENV = "T3_SUPABASE_SECRET_KEY"

DEFAULT_TIMEOUT = 20.0


def configured() -> bool:
    return bool(os.getenv(URL_ENV, "").strip() and os.getenv(KEY_ENV, "").strip())


def _base_url() -> str:
    url = os.getenv(URL_ENV, "").strip().rstrip("/")
    # Accept both the project URL and one that already points at the API.
    if url.endswith("/rest/v1"):
        return url
    return url + "/rest/v1"


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    key = os.getenv(KEY_ENV, "").strip()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


class SupabaseError(RuntimeError):
    """A REST call that did not do what was asked. Raised rather than
    swallowed here; the callers decide what a storage failure costs
    them - losing a cached analysis is not the same as losing a trade."""


def _request(method: str, path: str, params: Optional[Sequence[Tuple[str, str]]] = None,
             json_body: Any = None, extra_headers: Optional[Dict[str, str]] = None,
             client: Optional[httpx.Client] = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.request(method, f"{_base_url()}/{path}", params=list(params or []),
                            headers=_headers(extra_headers), json=json_body)
        if resp.status_code >= 400:
            raise SupabaseError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return []
        try:
            return resp.json()
        except ValueError:
            return []
    except httpx.HTTPError as exc:
        raise SupabaseError(f"{method} {path} failed: {exc}") from exc
    finally:
        if client is None:
            http.close()


def select(table: str, filters: Optional[Dict[str, Any]] = None,
           order: Optional[str] = None, limit: Optional[int] = None,
           client: Optional[httpx.Client] = None) -> List[Dict[str, Any]]:
    """Rows matching every filter, as equality comparisons.

    `order` is PostgREST's own syntax ("created_at.desc"), passed through
    rather than re-invented - the one place where this layer is not
    database-agnostic, and it is one string."""
    params: List[Tuple[str, str]] = [("select", "*")]
    for column, value in (filters or {}).items():
        params.append((column, f"eq.{value}"))
    if order:
        params.append(("order", order))
    if limit:
        params.append(("limit", str(limit)))
    rows = _request("GET", table, params=params, client=client)
    return rows if isinstance(rows, list) else []


def insert(table: str, rows: List[Dict[str, Any]],
           client: Optional[httpx.Client] = None) -> List[Dict[str, Any]]:
    if not rows:
        return []
    out = _request("POST", table, json_body=rows,
                   extra_headers={"Prefer": "return=representation"}, client=client)
    return out if isinstance(out, list) else []


def delete(table: str, filters: Dict[str, Any],
           client: Optional[httpx.Client] = None) -> int:
    """Delete every row matching the filters, returning how many went.

    PostgREST refuses an unfiltered DELETE, which is a guard worth keeping
    rather than working around: this is asked for one series at a time,
    never for a whole table."""
    if not filters:
        raise SupabaseError("refusing to delete without a filter")
    params = [(column, f"eq.{value}") for column, value in filters.items()]
    removed = _request("DELETE", table, params=params,
                       extra_headers={"Prefer": "return=representation"}, client=client)
    return len(removed) if isinstance(removed, list) else 0
