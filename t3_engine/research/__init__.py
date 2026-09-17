"""Research package: data capture, cost modelling, execution simulation
and strategy evaluation for the Lead Engine.

It is SEPARATE from `t3_engine.lead_engine` on purpose and the dependency
runs one way. The live engine does not import anything from here, so a
half-finished experiment cannot change what the live engine does. This
package reads the engine's message stream through an explicit tap and
otherwise keeps to itself.
"""

from typing import Any, Callable, Dict, List


class TapFan:
    """Fan one frame out to several observers.

    The engine holds ONE tap. Both the recorder and the paper traders
    want it, and an observer that raises must not stop the others or the
    ingest thread, so each call is guarded individually - a paper trader
    with a bug cannot be allowed to silently stop the recording."""

    def __init__(self) -> None:
        self._observers: List[Callable[[str, Dict[str, Any], int], None]] = []
        self.errors = 0

    def add(self, observer: Callable[[str, Dict[str, Any], int], None]) -> None:
        if observer not in self._observers:
            self._observers.append(observer)

    def remove(self, observer) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    def __len__(self) -> int:
        return len(self._observers)

    def __call__(self, topic: str, message: Dict[str, Any],
                 received_at_ms: int) -> None:
        for observer in list(self._observers):
            try:
                observer(topic, message, received_at_ms)
            except Exception:                   # pragma: no cover - defensive
                self.errors += 1
