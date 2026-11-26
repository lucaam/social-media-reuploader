"""Tests for src/ratelimit.py: pure slot computation and the DB-backed limiter."""

import math
import random
import time

import pytest

from src import config, db, ratelimit
from src.ratelimit import Decision, Outcome, RateLimiter, evaluate, next_slot

NOW = 1_000.0


# ---------------------------------------------------------------------------
# next_slot (pure)
# ---------------------------------------------------------------------------
def test_empty_history_is_free_now():
    assert next_slot([], [(1, 10), (3, 60)], NOW) == NOW


def test_window_not_full_is_free_now():
    assert next_slot([990.0, 995.0], [(3, 60)], NOW) == NOW


def test_full_window_waits_for_the_oldest_entry_to_leave():
    assert next_slot([997.0], [(1, 10)], NOW) == 1007.0


def test_entry_exactly_one_window_old_is_outside():
    # window is (t - W, t]: an entry at t - W no longer counts
    assert next_slot([990.0], [(1, 10)], NOW) == NOW
    assert next_slot([990.5], [(1, 10)], NOW) == 1000.5


def test_count_greater_than_one():
    assert next_slot([950.0, 960.0, 970.0], [(3, 60)], NOW) == 1010.0
    # the entry at 940 already left the window: still 3 inside
    assert next_slot([940.0, 950.0, 960.0, 970.0], [(3, 60)], NOW) == 1010.0
    # 5 inside, max 3: two must leave before a slot frees up
    assert next_slot([945.0, 950.0, 960.0, 970.0, 980.0], [(3, 60)], NOW) == 1020.0


def test_several_windows_the_most_restrictive_wins():
    times = [955.0, 970.0, 995.0]
    assert next_slot(times, [(1, 10)], NOW) == 1005.0
    assert next_slot(times, [(3, 60)], NOW) == 1015.0
    assert next_slot(times, [(1, 10), (3, 60)], NOW) == 1015.0
    assert next_slot(times, [(3, 60), (1, 10)], NOW) == 1015.0  # order irrelevant
    assert next_slot(times, [(1, 10), (3, 60), (4, 3600)], NOW) == 1015.0
    assert next_slot(times, [(1, 10), (3, 60), (3, 3600)], NOW) == 955.0 + 3600


def test_deferred_future_entries_keep_fifo_order():
    # a request already deferred to 1050 is served first
    assert next_slot([1050.0], [(5, 60)], NOW) == 1050.0
    assert next_slot([995.0, 1005.0], [(1, 10)], NOW) == 1015.0
    assert next_slot([1000.0, 1010.0, 1020.0], [(3, 60)], NOW) == 1060.0


def test_simultaneous_entries():
    assert next_slot([995.0, 995.0], [(2, 10)], NOW) == 1005.0
    assert next_slot([995.0, 995.0, 995.0], [(2, 10)], NOW) == 1005.0


def test_input_order_does_not_matter():
    times = [980.0, 950.0, 970.0, 945.0, 960.0]
    assert next_slot(times, [(3, 60)], NOW) == next_slot(sorted(times), [(3, 60)], NOW)


def test_float_times_terminate_and_are_exact():
    slot = next_slot([0.1], [(1, 10)], 0.2)
    assert slot == 0.1 + 10  # (10.1 - 10) != 0.1 in floats: must still converge
    assert next_slot([0.1, 0.2, 0.30000000000000004], [(2, 7)], 0.4) == 0.2 + 7
    assert next_slot([0.1], [(1, 10.0)], 0.2) == 0.1 + 10  # integral floats are fine


def test_no_limits_only_keeps_fifo_order():
    assert next_slot([990.0, 999.0], [], NOW) == NOW
    assert next_slot([1030.0], [], NOW) == 1030.0


@pytest.mark.parametrize(
    "limits",
    [[(0, 10)], [(1, 0)], [(1, -5)], [(1.5, 10)], [(1, 10.5)], [(1, 2, 3)], [(1,)]],
)
def test_invalid_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        next_slot([], limits, NOW)


def test_non_finite_values_are_rejected():
    with pytest.raises(ValueError):
        next_slot([], [(1, 10)], math.nan)
    with pytest.raises(ValueError):
        next_slot([math.inf], [(1, 10)], NOW)


def _is_free(times, limits, t):
    return all(sum(1 for x in times if t - w < x <= t) < c for c, w in limits)


def test_next_slot_matches_the_definition_on_random_histories():
    rng = random.Random(20261126)
    for _ in range(400):
        limits = [
            (rng.randint(1, 4), rng.randint(1, 120)) for _ in range(rng.randint(1, 3))
        ]
        now = 1_000
        times = [
            float(rng.randint(now - 250, now + 40)) for _ in range(rng.randint(0, 12))
        ]
        slot = next_slot(times, limits, float(now))
        start = max([now, *times])
        assert slot >= start
        assert _is_free(times, limits, slot), (times, limits, slot)
        # integer inputs: windows only change at integer points, so checking
        # every integer below the slot proves it is the earliest one
        for t in range(int(start), int(slot)):
            assert not _is_free(times, limits, t), (times, limits, slot, t)


# ---------------------------------------------------------------------------
# evaluate (pure)
# ---------------------------------------------------------------------------
def decide(times, active=0, **overrides):
    params = {
        "now": NOW,
        "limits": [(1, 10), (3, 60)],
        "max_defer": 120,
        "max_pending": 3,
    }
    params.update(overrides)
    return evaluate(times, active, **params)


def test_accept_when_there_is_room():
    decision = decide([])
    assert decision == Decision(Outcome.ACCEPT)
    assert decision.delay_seconds == 0 and decision.window is None
    assert decision.accepted
    assert decide([900.0, 950.0]).outcome is Outcome.ACCEPT


def test_defer_within_max_defer():
    decision = decide([995.0])
    assert decision == Decision(Outcome.DEFER, 5.0, (1, 10))
    assert decision.accepted


def test_defer_edge_equal_to_max_defer():
    assert decide([995.0], max_defer=5).outcome is Outcome.DEFER
    decision = decide([995.0], max_defer=4.999)
    assert decision.outcome is Outcome.RATE_LIMITED
    assert decision.delay_seconds == 5.0 and decision.window == (1, 10)
    assert not decision.accepted


def test_rate_limited_reports_the_triggering_window():
    times = [960.0, 980.0, 990.0]
    decision = decide(times, max_defer=10)
    assert decision.outcome is Outcome.RATE_LIMITED
    assert decision.delay_seconds == 20.0 and decision.window == (3, 60)
    big = decide([NOW - 60 * i for i in range(5)], limits=[(5, 3600)], max_defer=120)
    assert big.outcome is Outcome.RATE_LIMITED
    assert big.delay_seconds == 3600 - 240 and big.window == (5, 3600)


def test_max_defer_zero_or_none_never_defers():
    assert decide([995.0], max_defer=0).outcome is Outcome.RATE_LIMITED
    assert decide([995.0], max_defer=None).outcome is Outcome.RATE_LIMITED
    assert decide([], max_defer=0).outcome is Outcome.ACCEPT


def test_pending_cap_takes_precedence():
    assert decide([], active=3) == Decision(Outcome.PENDING_CAP)
    assert decide([995.0], active=5).outcome is Outcome.PENDING_CAP
    assert decide([NOW - i for i in range(50)], active=3).outcome is Outcome.PENDING_CAP
    assert decide([], active=2).outcome is Outcome.ACCEPT
    assert decide([], active=99, max_pending=None).outcome is Outcome.ACCEPT
    assert decide([], active=0, max_pending=0).outcome is Outcome.PENDING_CAP
    assert not Decision(Outcome.PENDING_CAP).accepted


def test_deferred_future_entries_defer_new_requests():
    decision = decide([1030.0], limits=[(1, 10)])
    assert decision == Decision(Outcome.DEFER, 40.0, (1, 10))
    fifo_only = decide([1030.0], limits=[(5, 60)])
    assert fifo_only == Decision(Outcome.DEFER, 30.0, None)


def test_no_limits_means_rate_limiting_disabled():
    assert decide([1030.0, 999.0], limits=[]) == Decision(Outcome.ACCEPT)
    assert decide([], active=3, limits=[]).outcome is Outcome.PENDING_CAP


def test_outcome_values():
    assert [o.value for o in Outcome] == [
        "accept",
        "defer",
        "rate_limited",
        "pending_cap",
    ]
    assert Outcome.RATE_LIMITED == "rate_limited"


# ---------------------------------------------------------------------------
# RateLimiter (DB-backed)
# ---------------------------------------------------------------------------
@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    monkeypatch.setenv("REQUESTS_DB", str(path))
    db.init_db()
    return path


def add(chat_id, status="queued", *, age=0.0, not_before=None, url=None):
    request_id = db.create_request(
        chat_id,
        url or f"https://example.com/{chat_id}/{time.monotonic_ns()}",
        not_before=not_before,
    )
    with db.write() as conn:
        conn.execute(
            "UPDATE requests SET status = ?, created_at = ? WHERE id = ?",
            (status, db.iso_from_ts(time.time() - age), request_id),
        )
    return request_id


def limiter(**overrides):
    params = {"limits": [(1, 10)], "max_defer": 120, "max_pending": 10}
    params.update(overrides)
    return RateLimiter(**params)


def test_defaults_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMITS", ((2, 30), (1, 5)), raising=False)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_DEFER_SECONDS", 45, raising=False)
    monkeypatch.setattr(config, "MAX_PENDING_PER_CHAT", 4, raising=False)
    rl = RateLimiter()
    assert rl.limits == ((1, 5), (2, 30))
    assert rl.max_defer == 45 and rl.max_pending == 4 and rl.max_window == 30
    explicit = RateLimiter(limits=[], max_defer=0, max_pending=1)
    assert explicit.limits == () and explicit.max_window == 0


def test_defaults_when_config_lacks_the_settings(monkeypatch):
    for name in ("RATE_LIMITS", "RATE_LIMIT_MAX_DEFER_SECONDS", "MAX_PENDING_PER_CHAT"):
        monkeypatch.delattr(config, name, raising=False)
    rl = RateLimiter()
    assert rl.limits == ratelimit.DEFAULT_RATE_LIMITS
    assert rl.max_defer == ratelimit.DEFAULT_MAX_DEFER_SECONDS
    assert rl.max_pending == ratelimit.DEFAULT_MAX_PENDING


def test_check_accepts_a_fresh_chat(db_file):
    assert limiter().check(1) == Decision(Outcome.ACCEPT)


def test_check_defers_or_refuses_after_an_accepted_request(db_file):
    add(1, "done", age=3)
    now = time.time()
    deferred = limiter().check(1, now=now)
    assert deferred.outcome is Outcome.DEFER and deferred.window == (1, 10)
    assert deferred.delay_seconds == pytest.approx(7, abs=0.5)
    refused = limiter(max_defer=2).check(1, now=now)
    assert refused.outcome is Outcome.RATE_LIMITED
    assert refused.delay_seconds == pytest.approx(7, abs=0.5)
    assert limiter().check(1, now=now + 8) == Decision(Outcome.ACCEPT)


@pytest.mark.parametrize(
    "status", ["queued", "processing", "sending", "done", "failed"]
)
def test_accepted_statuses_consume_quota(db_file, status):
    add(1, status, age=1)
    assert limiter().check(1).outcome is Outcome.DEFER


@pytest.mark.parametrize(
    "status",
    ["rate_limited", "duplicate", "cancelled", "aborted", "pending", "unlimited"],
)
def test_rejected_rows_never_consume_quota(db_file, status):
    for index in range(5):
        add(1, status, age=index)
    assert limiter(limits=[(1, 10), (2, 3600)]).check(1) == Decision(Outcome.ACCEPT)
    assert limiter(max_pending=1).check(1) == Decision(Outcome.ACCEPT)


def test_pending_cap_counts_only_active_requests(db_file):
    add(1, "queued", age=500)
    add(1, "processing", age=400)
    add(1, "done", age=300)
    add(1, "failed", age=200)
    assert limiter(max_pending=2).check(1) == Decision(Outcome.PENDING_CAP)
    assert limiter(max_pending=3).check(1).outcome is Outcome.ACCEPT


def test_reset_at_is_honoured(db_file):
    add(1, "done", age=2)
    assert limiter().check(1).outcome is Outcome.DEFER
    db.reset_chat_limit(1, actor="admin")
    assert limiter().check(1) == Decision(Outcome.ACCEPT)
    add(1, "done")  # created after the reset: counts again
    assert limiter().check(1).outcome is Outcome.DEFER


def test_other_chats_and_old_rows_do_not_count(db_file):
    add(2, "done", age=1)
    add(1, "done", age=7200)
    rl = limiter(limits=[(1, 10), (1, 3600)])
    assert rl.check(1) == Decision(Outcome.ACCEPT)
    refused = rl.check(2)
    assert refused.outcome is Outcome.RATE_LIMITED and refused.window == (1, 3600)
    assert refused.delay_seconds == pytest.approx(3599, abs=1)


def test_deferred_request_in_the_future_is_counted_at_its_slot(db_file):
    now = time.time()
    add(1, "queued", not_before=db.iso_from_ts(now + 30))
    decision = limiter().check(1, now=now)
    assert decision.outcome is Outcome.DEFER
    assert decision.delay_seconds == pytest.approx(40, abs=0.01)


def test_blocked_until_is_display_only(db_file):
    db.set_chat_blocked_until(1, db.iso_from_ts(time.time() + 3600))
    assert limiter().check(1) == Decision(Outcome.ACCEPT)


def test_disabled_limits_still_apply_the_pending_cap(db_file):
    for index in range(3):
        add(1, "done", age=index)
    assert limiter(limits=[]).check(1) == Decision(Outcome.ACCEPT)
    add(1, "queued")
    assert limiter(limits=[], max_pending=1).check(1).outcome is Outcome.PENDING_CAP


def test_realistic_burst_with_default_limits(db_file):
    rl = RateLimiter(limits=ratelimit.DEFAULT_RATE_LIMITS, max_defer=120, max_pending=3)
    start = time.time()
    outcomes = []
    for offset in range(5):
        now = start + offset
        decision = rl.check(9, now=now)
        outcomes.append(decision.outcome)
        if decision.accepted:
            not_before = (
                db.iso_from_ts(now + decision.delay_seconds)
                if decision.outcome is Outcome.DEFER
                else None
            )
            add(9, "queued", not_before=not_before)
    # 1st now, 2nd/3rd deferred (1 per 10 s), then the pending cap (3) kicks in
    assert outcomes == [
        Outcome.ACCEPT,
        Outcome.DEFER,
        Outcome.DEFER,
        Outcome.PENDING_CAP,
        Outcome.PENDING_CAP,
    ]
    slots = db.accepted_times(9, db.iso_from_ts(start - 1))
    assert [round(slot - start) for slot in slots] == [0, 10, 20]
