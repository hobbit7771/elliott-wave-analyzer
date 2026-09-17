"""An optional self-ping, and the cost of using it.

THE PROBLEM IT SOLVES. Render's free plan stops a web service about
fifteen minutes after the last INBOUND HTTP request. Outbound websockets
do not count, so an engine that is happily consuming Bybit around the
clock is stopped anyway. Every stop ends the recording, ends the paper
session and ends whatever window was being measured.

THE PROBLEM IT CREATES, which is why it is OFF BY DEFAULT. Free instance
hours are a shared monthly allowance across the whole workspace. A
service that pings itself never sleeps, so it spends that allowance
continuously - and if it runs out, it takes every other free service in
the workspace down with it. That is a decision for whoever owns the
account, not for this module, so it is switched on explicitly or not at
all and the log says plainly what it is spending.

WHAT IT IS NOT. It is not a way to pretend the free plan is a paid one.
A self-ping keeps ONE process alive; it does not survive a redeploy, a
platform restart or an out-of-memory kill, and every result in this
project still has to be correct across those. The recovery path is the
real answer to downtime, and this only reduces how often it is needed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

ENABLED_ENV = "RENDER_KEEPALIVE_ENABLED"
URL_ENV = "RENDER_EXTERNAL_URL"          # Render sets this itself
INTERVAL_ENV = "RENDER_KEEPALIVE_SECONDS"

# Comfortably inside the roughly fifteen-minute idle window, and not so
# frequent that it is itself a load.
DEFAULT_INTERVAL_SECONDS = 600.0
MIN_INTERVAL_SECONDS = 120.0

# The cheapest endpoint that proves the process is answering.
PING_PATH = "/api/live/status"

_TRUE = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "").strip().lower() in _TRUE


def target_url() -> str:
    base = os.getenv(URL_ENV, "").strip().rstrip("/")
    return f"{base}{PING_PATH}" if base else ""


def interval_seconds() -> float:
    raw = os.getenv(INTERVAL_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        value = DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, value)


class KeepAlive:
    def __init__(self, url: str, interval: float,
                 fetch=None, clock=time.time) -> None:
        self.url = url
        self.interval = interval
        self._fetch = fetch or _http_get
        self._clock = clock
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.pings = 0
        self.failures = 0
        self.last_ping = 0.0
        self.last_error = ""

    def start(self) -> bool:
        if not self.url:
            logger.warning("keepalive: no %s set, not starting", URL_ENV)
            return False
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="render-keepalive",
                                        daemon=True)
        self._thread.start()
        logger.info("keepalive: pinging %s every %.0fs. This spends free "
                    "instance hours continuously - they are a shared monthly "
                    "allowance across the workspace.", self.url, self.interval)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                return
            try:
                self._fetch(self.url)
                self.pings += 1
                self.last_ping = self._clock()
            except Exception as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("keepalive ping failed: %s", exc)

    def stats(self) -> dict:
        return {"enabled": True, "url": self.url, "interval_seconds": self.interval,
                "pings": self.pings, "failures": self.failures,
                "last_ping": self.last_ping, "last_error": self.last_error,
                "note": "spends shared free instance hours continuously"}


def _http_get(url: str) -> None:
    import httpx

    with httpx.Client(timeout=15.0) as client:
        client.get(url)


_keepalive: Optional[KeepAlive] = None


def get_keepalive() -> Optional[KeepAlive]:
    return _keepalive


def start_keepalive() -> Optional[KeepAlive]:
    global _keepalive
    if not enabled():
        return None
    if _keepalive is not None and _keepalive._thread \
            and _keepalive._thread.is_alive():
        return _keepalive
    _keepalive = KeepAlive(target_url(), interval_seconds())
    return _keepalive if _keepalive.start() else None


def reset_keepalive() -> None:
    global _keepalive
    if _keepalive is not None:
        _keepalive.stop()
    _keepalive = None
