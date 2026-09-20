"""Login throttle: no race past the budget, IPv6 /64 keys, validated X-Forwarded-For, a back-off
that never locks the owner out for long, bounded memory, and a cap on concurrent scrypt runs."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import security, throttle
from app.main import app
from app.throttle import Backoff, LoginThrottle, Throttle, client_key, login_throttle

PASSWORD = "correct horse battery"


def new_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def protected():
    c = new_client()
    r = c.post("/api/auth/change-password", json={"new": PASSWORD, "setup_code": security.current_setup_code()})
    assert r.status_code == 200
    return c


def login(client, password="wrong", **kw):
    return client.post("/api/auth/login", json={"password": password}, **kw)


def run_threads(n, target):
    """Start n threads together (a barrier) and wait for them; returns the results in order."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def run(i):
        barrier.wait()
        results[i] = target(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(r is not None for r in results), "a thread did not finish"
    return results


# --- M3: no burst past the budget ---------------------------------------------------------------


def test_a_concurrent_burst_cannot_exceed_the_failure_budget(protected, monkeypatch):
    from app.routers import auth

    checked = []
    real = auth.verify_password

    def counting(password, stored):
        checked.append(1)
        return real(password, stored)

    monkeypatch.setattr(auth, "verify_password", counting)
    clients = [new_client() for _ in range(40)]
    statuses = run_threads(40, lambda i: login(clients[i]).status_code)
    assert statuses.count(401) == 5  # the per-client budget, exactly
    assert statuses.count(429) == 35
    assert len(checked) == 5  # and the 35 others never reached scrypt


def test_a_burst_that_includes_the_right_password_is_answered_cleanly(protected):
    """The right password clears the client's count (as it always did), so the wrong guesses
    that come after it get a fresh budget: what must hold is that nothing errors and one wins at most."""
    clients = [new_client() for _ in range(30)]
    statuses = run_threads(30, lambda i: login(clients[i], PASSWORD if i == 17 else "wrong").status_code)
    assert set(statuses) <= {200, 401, 429}
    assert statuses.count(200) <= 1
    assert statuses.count(401) <= 10  # at most two budgets of five


def test_a_burst_of_change_password_guesses_is_bounded_too(protected):
    def guess(i):
        return protected.post("/api/auth/change-password", json={"current": f"guess-number-{i}", "new": "n" * 12})

    statuses = run_threads(1, lambda i: guess(i).status_code)  # warm up the shared client
    assert statuses == [403]
    clients = []
    for _ in range(20):
        c = new_client()
        c.cookies.set(security.COOKIE_NAME, protected.cookies.get(security.COOKIE_NAME))
        clients.append(c)
    statuses = run_threads(20, lambda i: clients[i].post(
        "/api/auth/change-password", json={"current": f"guess-{i}", "new": "n" * 12}).status_code)
    assert statuses.count(403) <= 4  # one attempt was used above
    assert statuses.count(429) >= 16


def test_a_burst_of_first_password_claims_with_wrong_codes_is_bounded():
    clients = [new_client() for _ in range(25)]
    statuses = run_threads(25, lambda i: clients[i].post(
        "/api/auth/change-password", json={"new": "n" * 12, "setup_code": f"BADCODE{i % 10}"}).status_code)
    assert statuses.count(403) == 5 and statuses.count(429) == 20


def test_the_global_budget_holds_under_concurrency_across_addresses(protected, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    clients = [new_client() for _ in range(60)]
    statuses = run_threads(60, lambda i: login(clients[i], headers={"X-Forwarded-For": f"10.5.{i}.1"}).status_code)
    assert statuses.count(401) == 20  # then the back-off starts
    assert statuses.count(429) == 40


def test_scrypt_runs_are_capped(monkeypatch):
    active = [0]
    peak = [0]
    lock = threading.Lock()

    def slow_scrypt(password, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return b"\0" * 32

    monkeypatch.setattr(security.hashlib, "scrypt", slow_scrypt)
    done = run_threads(10, lambda i: security._scrypt("x", b"salt", 2**10, 8, 1))
    assert len(done) == 10
    assert 1 <= peak[0] <= security.MAX_CONCURRENT_SCRYPT


# --- reserve / success unit behaviour --------------------------------------------------------------


def test_reserve_counts_before_the_check_and_success_takes_it_back():
    now = [0.0]
    t = Throttle(3, clock=lambda: now[0])
    assert [t.reserve("k") for _ in range(3)] == [0, 0, 0]  # the third one sets the lock
    assert t.reserve("k") == 30
    t.success("k")
    assert t.locked_for("k") == 0 and t.reserve("k") == 0


def test_a_locked_key_is_not_counted_further():
    now = [0.0]
    t = Throttle(1, clock=lambda: now[0])
    assert t.reserve("k") == 0
    for _ in range(20):
        assert t.reserve("k") == 30  # hammering while locked does not lengthen the lock
    now[0] += 30
    assert t.reserve("k") == 0
    assert t.locked_for("k") == 60


# --- M4: IPv6 /64, X-Forwarded-For -------------------------------------------------------------------


def test_client_keys():
    assert client_key("192.0.2.5") == "192.0.2.5"
    assert client_key("2001:db8:1:2::1") == client_key("2001:db8:1:2:ffff:eeee:dddd:cccc") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") != client_key("2001:db8:1:2::1")
    assert client_key("::ffff:198.51.100.7") == "198.51.100.7"
    assert client_key("fe80::1%eth0") == "fe80::/64"
    assert client_key("testclient") == "testclient"
    assert client_key("") == "unknown"
    assert len(client_key("x" * 500)) == 64


def request_with(headers, host="203.0.113.9"):
    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "client": (host, 1234),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


def test_forwarded_for_is_used_only_when_trusted_and_only_as_an_ip(monkeypatch):
    header = {"X-Forwarded-For": "1.1.1.1, 198.51.100.8"}
    assert security.client_ip(request_with(header)) == "203.0.113.9"  # not trusted: the connection
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    assert security.client_ip(request_with(header)) == "198.51.100.8"  # the hop our proxy appended
    for junk in ("garbage", "1.1.1.1, garbage", "'; DROP TABLE", "999.1.1.1", "a" * 1000, ", ,"):
        assert security.client_ip(request_with({"X-Forwarded-For": junk})) == "203.0.113.9", junk
    assert security.client_ip(request_with({})) == "203.0.113.9"
    v6 = security.client_ip(request_with({"X-Forwarded-For": "2001:db8:aaaa:bbbb:1:2:3:4"}))
    assert v6 == "2001:db8:aaaa:bbbb::/64"


def test_one_ipv6_64_shares_one_budget(protected, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    c = new_client()
    for n in range(5):  # five different addresses inside one /64
        assert login(c, headers={"X-Forwarded-For": f"2001:db8:1:2::{n + 1:x}"}).status_code == 401
    assert login(c, headers={"X-Forwarded-For": "2001:db8:1:2:9:9:9:9"}).status_code == 429
    assert login(c, headers={"X-Forwarded-For": "2001:db8:1:3::1"}).status_code == 401  # another /64 has its own


def test_a_client_cannot_pick_its_own_key_with_a_junk_header(protected, monkeypatch):
    monkeypatch.setenv("VIBEHEALTH_TRUST_PROXY", "1")
    c = new_client()
    for n in range(5):
        assert login(c, headers={"X-Forwarded-For": f"not-an-ip-{n}"}).status_code == 401
    assert login(c, headers={"X-Forwarded-For": "still not an ip"}).status_code == 429


# --- M4: the whole-app limit is a back-off, not a lock ------------------------------------------------------


@pytest.fixture
def clock():
    now = [1000.0]
    return now


def attack(b, now, failures, prefix="atk"):
    """`failures` guesses from distinct clients, each after the back-off allows it; returns time spent."""
    start = now[0]
    for n in range(failures):
        wait = b.reserve(f"{prefix}{n}")
        while wait:
            now[0] += wait
            wait = b.reserve(f"{prefix}{n}")
    return now[0] - start


def test_backoff_delay_grows_and_is_capped(clock):
    b = Backoff(threshold=20, clock=lambda: clock[0])
    for n in range(20):
        assert b.reserve(f"a{n}") == 0  # under (and at) the threshold: no waiting
    delays = []
    for n in range(80):
        wait = b.reserve(f"b{n}")
        assert wait > 0
        delays.append(wait)
        clock[0] += wait
        assert b.reserve(f"b{n}") == 0  # admitted once the wait is over
    assert delays[0] == 5 and delays[5] == 10 and delays[10] == 20 and delays[15] == 40
    assert max(delays) == 60 and delays[-1] == 60
    assert all(a <= b_ for a, b_ in zip(delays, delays[1:]))


def test_the_owner_gets_through_after_a_bounded_wait_during_an_attack(clock, monkeypatch):
    lt = LoginThrottle(clock=lambda: clock[0])
    for n in range(400):  # a long distributed attack, as fast as the back-off allows
        wait = lt.reserve(f"atk{n}")
        while wait:
            clock[0] += wait
            wait = lt.reserve(f"atk{n}")
    # the owner arrives from a device the app has never seen, right after an attacker attempt
    waited = 0
    wait = lt.reserve("owner")
    while wait:
        assert wait <= 60
        waited += wait
        clock[0] += wait
        wait = lt.reserve("owner")
    assert waited <= 60
    lt.success("owner")  # the right password
    # from now on the owner's device is not held by the back-off at all
    for _ in range(3):
        assert lt.reserve("owner") == 0
        lt.success("owner")
    for n in range(30):  # ...even while the attack continues
        lt.reserve(f"more{n}")
    assert lt.locked_for("owner") == 0


def test_a_known_client_keeps_its_per_client_limit(clock):
    lt = LoginThrottle(per_ip=3, clock=lambda: clock[0])
    assert lt.reserve("owner") == 0
    lt.success("owner")
    assert [lt.reserve("owner") for _ in range(3)] == [0, 0, 0]
    assert lt.reserve("owner") == 30  # still limited on its own


def test_the_backoff_fades_when_the_attack_stops(clock):
    b = Backoff(threshold=20, clock=lambda: clock[0])
    attack(b, clock, 60)
    assert b.reserve("during") > 0  # still backing off right after the attack
    clock[0] += throttle.GLOBAL_WINDOW + 1
    assert b.locked_for("newcomer") == 0 and b.reserve("newcomer") == 0


def test_success_takes_back_the_count(clock):
    b = Backoff(threshold=2, clock=lambda: clock[0])
    for n in range(10):
        assert b.reserve(f"k{n}") == 0
        b.success(f"k{n}")  # every attempt was right
    assert len(b._recent) == 0


def test_a_locked_or_delayed_attempt_is_not_counted(clock):
    lt = LoginThrottle(per_ip=1, global_threshold=100, clock=lambda: clock[0])
    assert lt.reserve("a") == 0
    before = len(lt.everyone._recent)
    for _ in range(10):
        assert lt.reserve("a") > 0
    assert len(lt.everyone._recent) == before


# --- memory stays bounded ------------------------------------------------------------------------------------


def test_per_client_map_is_bounded(monkeypatch):
    monkeypatch.setattr(throttle, "MAX_KEYS", 50)
    now = [0.0]
    t = Throttle(5, clock=lambda: now[0])
    for n in range(500):
        now[0] += 1
        t.reserve(f"10.0.{n // 250}.{n % 250}")
        assert len(t) <= 50
    assert len(t) > 0


def test_recent_failures_and_known_clients_are_bounded(monkeypatch, clock):
    monkeypatch.setattr(throttle, "MAX_KNOWN", 10)
    b = Backoff(threshold=10_000_000, clock=lambda: clock[0])
    assert b._recent.maxlen == throttle.MAX_RECENT
    for n in range(100):
        b.reserve(f"k{n}")
        b.success(f"k{n}")
    assert len(b._known) == 10 and "k99" in b._known and "k0" not in b._known
    for n in range(throttle.MAX_RECENT + 500):
        b.reserve("z")
    assert len(b._recent) == throttle.MAX_RECENT


def test_the_paperless_limiter_stays_small():
    limiter = throttle.RateLimit(30, 60.0)
    for _ in range(1000):
        limiter.hit()
    assert len(limiter._hits) <= 30
