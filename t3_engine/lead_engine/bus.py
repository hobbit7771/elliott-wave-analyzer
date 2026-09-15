"""The Lead Engine's own event bus, namespaced `lead_engine.*`.

Deliberately its own, and deliberately tiny. The specification asks for a
separate namespace so that a subscriber to this engine's events cannot
accidentally receive `elliott.*` or `strategy.*` traffic and vice versa;
the cheapest way to guarantee that is not to share a bus at all. A topic
that does not begin with the prefix is refused rather than silently
renamed, because a mis-namespaced publish is a bug worth hearing about.

Handlers are called synchronously, in registration order, and an
exception in one is caught and logged rather than allowed to stop the
others or unwind into the socket loop. A subscriber that throws is a
broken subscriber, not a reason to lose the tick.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict, List

logger = logging.getLogger(__name__)

PREFIX = "lead_engine."

# The topics this engine publishes. Listed so a typo in a publish call is
# a refused topic rather than an event nobody ever receives.
TOPIC_ORDERBOOK = PREFIX + "orderbook"
TOPIC_TRADE = PREFIX + "trade"
TOPIC_TICKER = PREFIX + "ticker"
TOPIC_KLINE = PREFIX + "kline"
TOPIC_LIQUIDATION = PREFIX + "liquidation"
TOPIC_OPEN_INTEREST = PREFIX + "open_interest"
TOPIC_FEATURES = PREFIX + "features"
TOPIC_SIGNAL = PREFIX + "signal"
TOPIC_HEALTH = PREFIX + "health"

KNOWN_TOPICS = frozenset({
    TOPIC_ORDERBOOK, TOPIC_TRADE, TOPIC_TICKER, TOPIC_KLINE, TOPIC_LIQUIDATION,
    TOPIC_OPEN_INTEREST, TOPIC_FEATURES, TOPIC_SIGNAL, TOPIC_HEALTH,
})

Handler = Callable[[str, Dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._lock = threading.RLock()
        self.published = 0

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Returns the unsubscribe callable, so a caller that registered a
        handler can always take it off again without knowing the internals."""
        self._check(topic)
        with self._lock:
            self._handlers[topic].append(handler)

        def cancel() -> None:
            with self._lock:
                if handler in self._handlers[topic]:
                    self._handlers[topic].remove(handler)

        return cancel

    def publish(self, topic: str, payload: Dict[str, Any]) -> int:
        """Deliver to every handler; returns how many were called."""
        self._check(topic)
        with self._lock:
            handlers = list(self._handlers.get(topic, ()))
            self.published += 1
        for handler in handlers:
            try:
                handler(topic, payload)
            except Exception:                   # noqa: BLE001 - see module docstring
                logger.exception("lead_engine bus handler failed on %s", topic)
        return len(handlers)

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._handlers.get(topic, ()))

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()

    @staticmethod
    def _check(topic: str) -> None:
        if not topic.startswith(PREFIX):
            raise ValueError(
                f"{topic!r} is not a Lead Engine topic; this bus carries {PREFIX}* only, "
                "so that its traffic can never be confused with the rest of the project's"
            )
        if topic not in KNOWN_TOPICS:
            raise ValueError(f"unknown Lead Engine topic {topic!r}; "
                             f"known topics are {', '.join(sorted(KNOWN_TOPICS))}")
