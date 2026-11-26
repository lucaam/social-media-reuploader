"""Per-chat sliding-window rate limiting.

The pure functions :func:`next_slot` and :func:`evaluate` decide from the
timestamps ("slots") of a chat's accepted requests; :class:`RateLimiter` reads
those from the database (:func:`src.db.accepted_times`,
:func:`src.db.count_active_for_chat`).

Only accepted requests (queued, processing, sending, done, failed) consume
quota: refused links (rate limited, pending cap, duplicates) are never
counted, so a refusal never blocks the chat for other links.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from . import config, db

# Used when config does not define the corresponding setting.
DEFAULT_RATE_LIMITS: tuple[tuple[int, int], ...] = (
    (1, 10),
    (3, 60),
    (5, 3600),
    (20, 30 * 86400),
)
DEFAULT_MAX_DEFER_SECONDS = 120
DEFAULT_MAX_PENDING = 3

Limit = tuple[int, int]  # (max requests, window seconds)


class Outcome(StrEnum):
    ACCEPT = "accept"
    DEFER = "defer"
    RATE_LIMITED = "rate_limited"
    PENDING_CAP = "pending_cap"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    # DEFER: process at now + delay_seconds; RATE_LIMITED: retry hint.
    delay_seconds: float = 0.0
    # The (count, seconds) window that triggered DEFER / RATE_LIMITED (None
    # when the delay only comes from requests already deferred to the future).
    window: Limit | None = None

    @property
    def accepted(self) -> bool:
        """True when the request must be stored as ``queued`` (now or deferred)."""
        return self.outcome in (Outcome.ACCEPT, Outcome.DEFER)


def normalize_limits(limits: Iterable[Sequence[float]]) -> tuple[Limit, ...]:
    """Validate ``[(count, seconds), ...]``; sorted by window. ValueError if bad."""
    result: list[Limit] = []
    for item in limits:
        count, window = item
        if int(count) != count or int(window) != window:
            raise ValueError(f"rate limit {item!r} must use integers")
        if count < 1 or window < 1:
            raise ValueError(f"rate limit {item!r} needs count >= 1 and seconds >= 1")
        result.append((int(count), int(window)))
    return tuple(sorted(result, key=lambda limit: (limit[1], limit[0])))


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return number


def _find_slot(
    times: Iterable[float], limits: Sequence[Limit], now: float
) -> tuple[float, Limit | None]:
    """Return ``(slot, binding window)``; see :func:`next_slot`.

    A time ``x`` is inside the window ``(t - W, t]`` when ``x <= t`` and
    ``x + W > t``. Comparing ``x + W`` (instead of ``t - W``) keeps the test
    exact for the candidates ``x + W`` this function produces, so it always
    makes progress with floats.
    """
    ordered = sorted(_finite(x, "times") for x in times)
    current = max(now, ordered[-1]) if ordered else now
    binding: Limit | None = None
    while True:
        best: tuple[float, Limit] | None = None
        for count, window in limits:
            inside = [x for x in ordered if x <= current and x + window > current]
            if len(inside) >= count:
                candidate = inside[len(inside) - count] + window
                if best is None or candidate > best[0]:
                    best = (candidate, (count, window))
        if best is None or best[0] <= current:
            return current, binding
        current, binding = best


def next_slot(
    times: Iterable[float], limits: Iterable[Sequence[float]], now: float
) -> float:
    """Earliest ``t >= max(now, max(times))`` with room in every window.

    "Room" means: for every ``(count, W)``, fewer than ``count`` times fall in
    ``(t - W, t]``. While a window is full the candidate is
    ``sorted(in_window)[len - count] + W`` (when enough old entries have left
    it); ``t`` moves to the largest candidate and the check repeats. Pure.
    """
    return _find_slot(times, normalize_limits(limits), _finite(now, "now"))[0]


def evaluate(
    times: Iterable[float],
    active_count: int,
    *,
    now: float,
    limits: Iterable[Sequence[float]],
    max_defer: float | None,
    max_pending: int | None,
) -> Decision:
    """Decide what to do with one new request of a chat. Pure.

    * ``active_count >= max_pending`` -> PENDING_CAP (checked first; None = no cap)
    * no limits configured -> ACCEPT (rate limiting disabled)
    * ``delay = next_slot(...) - now``: ``<= 0`` -> ACCEPT,
      ``<= max_defer`` -> DEFER(delay), else RATE_LIMITED(delay, window).
    """
    if max_pending is not None and active_count >= max_pending:
        return Decision(Outcome.PENDING_CAP)
    normalized = normalize_limits(limits)
    if not normalized:
        return Decision(Outcome.ACCEPT)
    current = _finite(now, "now")
    slot, window = _find_slot(times, normalized, current)
    delay = slot - current
    if delay <= 0:
        return Decision(Outcome.ACCEPT)
    if delay <= (max_defer or 0.0):
        return Decision(Outcome.DEFER, delay, window)
    return Decision(Outcome.RATE_LIMITED, delay, window)


class RateLimiter:
    """Rate limiter backed by the requests table.

    Defaults come from ``config.RATE_LIMITS``, ``config.RATE_LIMIT_MAX_DEFER_SECONDS``
    and ``config.MAX_PENDING_PER_CHAT`` (read once, at construction).
    :meth:`check` is synchronous (two short DB reads): call it through
    ``await db.aio(limiter.check, chat_id)`` from async code, and serialize
    check + ``db.create_request`` per chat so two concurrent links of the same
    chat cannot both take the last free slot.
    """

    def __init__(
        self,
        *,
        limits: Iterable[Sequence[float]] | None = None,
        max_defer: float | None = None,
        max_pending: int | None = None,
    ) -> None:
        if limits is None:
            limits = getattr(config, "RATE_LIMITS", DEFAULT_RATE_LIMITS)
        if max_defer is None:
            max_defer = getattr(
                config, "RATE_LIMIT_MAX_DEFER_SECONDS", DEFAULT_MAX_DEFER_SECONDS
            )
        if max_pending is None:
            max_pending = getattr(config, "MAX_PENDING_PER_CHAT", DEFAULT_MAX_PENDING)
        self.limits: tuple[Limit, ...] = normalize_limits(limits)
        self.max_defer: float = max(0.0, float(max_defer))
        self.max_pending: int = int(max_pending)

    @property
    def max_window(self) -> int:
        """Largest window in seconds (0 when rate limiting is disabled)."""
        return max((window for _count, window in self.limits), default=0)

    def check(self, chat_id: int, now: float | None = None) -> Decision:
        """Decision for a new request of ``chat_id`` at ``now`` (epoch seconds).

        Reads only accepted requests created after the chat's ``reset_at``;
        never consults ``chat_limits.blocked_until`` (display only).
        """
        current = time.time() if now is None else _finite(now, "now")
        active = db.count_active_for_chat(chat_id)
        if active >= self.max_pending:
            return Decision(Outcome.PENDING_CAP)
        if not self.limits:
            return Decision(Outcome.ACCEPT)
        since = db.iso_from_ts(current - self.max_window)
        times = db.accepted_times(chat_id, since)
        return evaluate(
            times,
            active,
            now=current,
            limits=self.limits,
            max_defer=self.max_defer,
            max_pending=self.max_pending,
        )
