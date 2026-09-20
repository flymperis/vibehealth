"""Login throttling, in memory.

Every guess (a login, a password confirmation, a setup code) goes through `reserve`
BEFORE it is checked: the attempt is counted as a failure at once, and `success`
takes it back. So a burst of concurrent requests cannot exceed the budget by all
being checked before any of them has failed.

Two independent limits apply:

- Per client. After `per_ip` failures the client is locked for 30 s; each further
  failure doubles the lock, up to 15 minutes. A success forgets the client, and so
  does an hour without failures. IPv4 clients are told apart by address, IPv6 ones
  by their /64 (a single machine has a whole /64 to rotate through).
- For the whole app (a distributed guesser rotating addresses). This is a back-off,
  never a lock: once more than `threshold` failures were counted within the last
  hour, an attempt from a client the app has not seen succeed is admitted only every
  5 s, then every 10 s, 20 s, up to every 60 s as the failures pile up. So somebody
  guessing from many addresses is slowed to at most about one guess a minute, and the
  owner who is typing the right password from an unknown device waits at most 60 s
  (the `Retry-After` says how long). A client that has logged in successfully before
  (remembered in memory, up to 1000 of them) skips this back-off, so the owner's own
  browser is not affected by an attack at all; it is still held to the per-client limit.

All state lives in the process (a restart forgives everyone) and every map is bounded.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

BASE_LOCK = 30.0
MAX_LOCK = 900.0
FORGET_AFTER = 3600.0
MAX_KEYS = 10_000  # per-client entries kept

GLOBAL_WINDOW = 3600.0  # failures older than this no longer count for the whole app
GLOBAL_BASE_DELAY = 5.0
GLOBAL_MAX_DELAY = 60.0
GLOBAL_STEP = 5  # every this many failures over the threshold, the delay doubles
MAX_RECENT = 5000
MAX_KNOWN = 1000  # clients remembered as having logged in


def client_key(host: str) -> str:
    """What identifies a client for throttling: an IPv4 address, an IPv6 /64, else the text."""
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return host[:64] or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network((int(ip) >> 64 << 64, 64)))
    return str(ip)


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0


class Throttle:
    """A failure budget per key, with a lock that doubles."""

    def __init__(self, threshold: int = 5, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> int:
        """Whole seconds left on the lock, 0 when the key may try (does not count anything)."""
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return 0
            left = entry.locked_until - self.clock()
            return math.ceil(left) if left > 0 else 0

    def reserve(self, key: str) -> int:
        """Admit an attempt and count it as a failure: 0. Or, when the key is locked, the
        whole seconds to wait (and nothing is counted). Check and count are one step."""
        now = self.clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry:
                left = entry.locked_until - now
                if left > 0:
                    return math.ceil(left)
            elif len(self._entries) >= MAX_KEYS:
                self._prune(now)
            entry = self._entries.setdefault(key, _Entry())
            if entry.failures and now - entry.last_failure > FORGET_AFTER:
                entry.failures = 0
            entry.failures += 1
            entry.last_failure = now
            over = entry.failures - self.threshold
            if over >= 0:
                entry.locked_until = now + min(BASE_LOCK * (2 ** min(over, 10)), MAX_LOCK)
            return 0

    def success(self, key: str) -> None:
        """The attempt was right: forget the key."""
        with self._lock:
            self._entries.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def _prune(self, now: float) -> None:
        stale = [k for k, e in self._entries.items() if now - e.last_failure > FORGET_AFTER]
        for k in stale:
            del self._entries[k]
        if len(self._entries) >= MAX_KEYS:  # still full: drop the oldest half
            oldest = sorted(self._entries, key=lambda k: self._entries[k].last_failure)
            for k in oldest[: MAX_KEYS // 2]:
                del self._entries[k]


class Backoff:
    """The whole-app limit: a growing delay between attempts, never a lock (see the top)."""

    def __init__(self, threshold: int = 20, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = threshold
        self.clock = clock
        self._recent: deque[float] = deque(maxlen=MAX_RECENT)  # when each counted attempt was made
        self._next_allowed = 0.0
        self._known: OrderedDict[str, float] = OrderedDict()  # clients that logged in before
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._recent and now - self._recent[0] > GLOBAL_WINDOW:
            self._recent.popleft()

    def _delay(self) -> float:
        over = len(self._recent) - self.threshold
        return min(GLOBAL_BASE_DELAY * 2 ** min(over // GLOBAL_STEP, 10), GLOBAL_MAX_DELAY)

    def locked_for(self, key: str = "") -> int:
        """Seconds until an attempt from `key` would be admitted (0: now); counts nothing."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            if key in self._known or len(self._recent) < self.threshold:
                return 0
            left = self._next_allowed - now
            return math.ceil(left) if left > 0 else 0

    def reserve(self, key: str) -> int:
        """Admit and count an attempt (0), or say how long to wait (nothing counted)."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            known = key in self._known
            if not known and len(self._recent) >= self.threshold and now < self._next_allowed:
                return math.ceil(self._next_allowed - now)
            self._recent.append(now)
            if not known and len(self._recent) >= self.threshold:
                self._next_allowed = now + self._delay()
            return 0

    def success(self, key: str) -> None:
        """The attempt was right: take its count back and remember the client as the owner's."""
        now = self.clock()
        with self._lock:
            if self._recent:
                self._recent.pop()
            self._known.pop(key, None)
            self._known[key] = now
            while len(self._known) > MAX_KNOWN:
                self._known.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._recent.clear()
            self._known.clear()
            self._next_allowed = 0.0


class LoginThrottle:
    """Per-client limit and whole-app back-off, applied together."""

    def __init__(self, per_ip: int = 5, global_threshold: int = 20,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.per_ip = Throttle(per_ip, clock)
        self.everyone = Backoff(global_threshold, clock)
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> int:
        """How long `key` would have to wait now; counts nothing."""
        return max(self.per_ip.locked_for(key), self.everyone.locked_for(key))

    def reserve(self, key: str) -> int:
        """0: go ahead (the attempt is counted as a failure until `success`); else seconds to wait."""
        with self._lock:  # one step for both limits: peek at both, then count in both
            wait = max(self.per_ip.locked_for(key), self.everyone.locked_for(key))
            if wait:
                return wait
            self.per_ip.reserve(key)
            self.everyone.reserve(key)
            return 0

    def success(self, key: str) -> None:
        self.per_ip.success(key)
        self.everyone.success(key)

    def reset(self) -> None:
        self.per_ip.reset()
        self.everyone.reset()


login_throttle = LoginThrottle()


class RateLimit:
    """At most `limit` calls per `window` seconds, for the whole app (a light brake)."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: list[float] = []
        self._lock = threading.Lock()

    def hit(self) -> int:
        """Count a call. 0 when it may go ahead, else the whole seconds to wait."""
        now = self.clock()
        with self._lock:
            self._hits = [t for t in self._hits if now - t < self.window]
            if len(self._hits) >= self.limit:
                return max(1, math.ceil(self.window - (now - self._hits[0])))
            self._hits.append(now)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# The Paperless test / discover / preview endpoints make the server open connections
# for whoever is logged in: 30 a minute is plenty for a person and stops a loop.
paperless_limiter = RateLimit(30, 60.0)


# "Detect Ollama" probes a few fixed local addresses: a person clicks it now and then.
detect_limiter = RateLimit(6, 60.0)
test_limiter = RateLimit(30, 60.0)  # Ollama "test connection": as light as the Paperless helpers
