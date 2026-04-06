"""Tests for jobs/job_queue.py — uses real Redis (localhost or Docker)."""
import time
import pytest

from jobs.job_queue import (
    create_job,
    enqueue,
    enqueue_bulk,
    dequeue,
    complete,
    fail,
    recover_stalled,
    get_stats,
    get_waiting_jobs,
    get_dead_letter,
    flush_queue,
    promote_delayed,
    enqueue_delayed,
    renew_lock,
    TIER_CONFIG,
    PREFIX,
)


@pytest.fixture
def r():
    """Get a Redis connection and flush test keys before/after each test."""
    import redis as _redis
    import os

    url = os.environ.get("REDIS_URL", "redis://localhost:6379/1")  # DB 1 for tests
    client = _redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except _redis.ConnectionError:
        pytest.skip("Redis not available")

    # Flush test keys
    keys = client.keys(f"{PREFIX}:*")
    if keys:
        client.delete(*keys)

    yield client

    # Cleanup
    keys = client.keys(f"{PREFIX}:*")
    if keys:
        client.delete(*keys)


def _make_job(**overrides):
    defaults = dict(
        shop_id=1, domain="testshop.com", category="class-1",
        country_id=1, feed_url="https://testshop.com/sitemap.xml",
        difficulty="easy", priority=5,
    )
    defaults.update(overrides)
    return create_job(**defaults)


# ── create_job ───────────────────────────────────────────────────


class TestCreateJob:
    def test_structure(self):
        job = _make_job()
        assert job["shop_id"] == 1
        assert job["domain"] == "testshop.com"
        assert job["state"] == "waiting"
        assert job["attempts"] == 0
        assert "id" in job
        assert job["id"].startswith("scrape:testshop.com:")

    def test_defaults(self):
        job = _make_job()
        assert job["max_retries"] == 3
        assert job["max_products"] == 50000

    def test_custom_priority(self):
        job = _make_job(priority=1)
        assert job["priority"] == 1


# ── enqueue / dequeue ────────────────────────────────────────────


class TestEnqueueDequeue:
    def test_enqueue_returns_true(self, r):
        job = _make_job()
        assert enqueue(r, job) is True

    def test_duplicate_blocked(self, r):
        job1 = _make_job()
        job2 = _make_job()  # same domain
        enqueue(r, job1)
        assert enqueue(r, job2) is False

    def test_different_domains_ok(self, r):
        job1 = _make_job(domain="shop-a.com")
        job2 = _make_job(domain="shop-b.com")
        assert enqueue(r, job1) is True
        assert enqueue(r, job2) is True

    def test_dequeue_returns_job(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "test-worker")
        assert dequeued is not None
        assert dequeued["domain"] == "testshop.com"
        assert dequeued["state"] == "active"
        assert dequeued["attempts"] == 1

    def test_dequeue_empty_returns_none(self, r):
        assert dequeue(r, "test-worker") is None

    def test_priority_ordering(self, r):
        """Lower priority score = dequeued first."""
        enqueue(r, _make_job(domain="low-prio.com", priority=10))
        enqueue(r, _make_job(domain="high-prio.com", priority=1))
        enqueue(r, _make_job(domain="mid-prio.com", priority=5))

        first = dequeue(r, "w")
        assert first["domain"] == "high-prio.com"
        second = dequeue(r, "w")
        assert second["domain"] == "mid-prio.com"
        third = dequeue(r, "w")
        assert third["domain"] == "low-prio.com"


class TestEnqueueBulk:
    def test_bulk_enqueue(self, r):
        jobs = [_make_job(domain=f"shop-{i}.com") for i in range(5)]
        count = enqueue_bulk(r, jobs)
        assert count == 5

    def test_bulk_skips_dupes(self, r):
        jobs = [_make_job(domain="same.com") for _ in range(3)]
        count = enqueue_bulk(r, jobs)
        assert count == 1


# ── complete / fail ──────────────────────────────────────────────


class TestComplete:
    def test_complete_removes_from_active(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")
        complete(r, dequeued, {"saved": 42})

        stats = get_stats(r)
        assert stats["active"] == 0
        assert stats["completed_total"] == 1

    def test_complete_removes_dedup(self, r):
        """After completion, same domain can be enqueued again."""
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")
        complete(r, dequeued, {"saved": 10})

        # Should be able to enqueue same domain again
        new_job = _make_job()
        assert enqueue(r, new_job) is True


class TestFail:
    def test_fail_with_retry(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")

        fail(r, dequeued, "timeout", retry=True)

        # Should be in delayed queue (non-blocking retry)
        stats = get_stats(r)
        assert stats["delayed"] == 1
        assert stats["failed_total"] == 1
        assert stats["dead_total"] == 0

    def test_fail_exhausted_retries_goes_to_dead(self, r):
        job = _make_job()
        job["max_retries"] = 1
        job["attempts"] = 1
        enqueue(r, job)
        dequeued = dequeue(r, "w")
        # attempts is now 2 (incremented in dequeue), max_retries=1
        # but dequeue sets attempts = old + 1, so we need to force it
        dequeued["attempts"] = 2
        dequeued["max_retries"] = 1

        fail(r, dequeued, "permanent error", retry=True)

        stats = get_stats(r)
        assert stats["dead_total"] == 1

    def test_fail_no_retry(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")

        fail(r, dequeued, "fatal", retry=False)

        stats = get_stats(r)
        assert stats["dead_total"] == 1

    def test_dead_letter_contains_error(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")
        fail(r, dequeued, "site blocked", retry=False)

        dead = get_dead_letter(r, 10)
        assert len(dead) == 1
        assert dead[0]["error"] == "site blocked"


# ── stalled job recovery ─────────────────────────────────────────


class TestStalledRecovery:
    def test_recover_stalled_job(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")

        # Simulate expired lock
        r.hset(f"{PREFIX}:active", dequeued["id"], str(time.time() - 100))

        recovered = recover_stalled(r)
        assert recovered == 1

        stats = get_stats(r)
        assert stats["waiting"] == 1  # re-enqueued
        assert stats["active"] == 0

    def test_no_stalled_returns_zero(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeue(r, "w")
        # Lock is fresh, should not recover
        assert recover_stalled(r) == 0


# ── stats ────────────────────────────────────────────────────────


class TestStats:
    def test_empty_stats(self, r):
        stats = get_stats(r)
        assert stats["waiting"] == 0
        assert stats["active"] == 0

    def test_stats_after_operations(self, r):
        for i in range(3):
            enqueue(r, _make_job(domain=f"shop-{i}.com"))

        stats = get_stats(r)
        assert stats["waiting"] == 3
        assert stats["enqueued_total"] == 3


# ── get_waiting_jobs ─────────────────────────────────────────────


class TestGetWaitingJobs:
    def test_returns_job_ids(self, r):
        enqueue(r, _make_job(domain="a.com"))
        enqueue(r, _make_job(domain="b.com"))
        jobs = get_waiting_jobs(r, 10)
        assert len(jobs) == 2


# ── flush ────────────────────────────────────────────────────────


class TestFlush:
    def test_flush_clears_everything(self, r):
        enqueue(r, _make_job(domain="x.com"))
        flush_queue(r)
        stats = get_stats(r)
        assert stats["waiting"] == 0
        assert stats["enqueued_total"] == 0


# ── TIER_CONFIG ──────────────────────────────────────────────────


class TestTierConfig:
    def test_class1(self):
        assert TIER_CONFIG["class-1"]["interval_hours"] == 24
        assert TIER_CONFIG["class-1"]["priority"] == 1

    def test_class2(self):
        assert TIER_CONFIG["class-2"]["interval_hours"] == 48

    def test_class3(self):
        assert TIER_CONFIG["class-3"]["interval_hours"] == 168


# ── delayed queue ────────────────────────────────────────────────


class TestDelayedQueue:
    def test_enqueue_delayed_adds_to_delayed_set(self, r):
        job = _make_job()
        future_ts = time.time() + 3600  # 1 hour from now
        enqueue_delayed(r, job, future_ts)

        stats = get_stats(r)
        assert stats["delayed"] == 1
        assert stats["waiting"] == 0

    def test_promote_delayed_moves_ready_jobs(self, r):
        job = _make_job()
        past_ts = time.time() - 10  # already ready
        enqueue_delayed(r, job, past_ts)

        promoted = promote_delayed(r)
        assert promoted == 1

        stats = get_stats(r)
        assert stats["delayed"] == 0
        assert stats["waiting"] == 1

    def test_promote_delayed_skips_future_jobs(self, r):
        job = _make_job()
        future_ts = time.time() + 3600
        enqueue_delayed(r, job, future_ts)

        promoted = promote_delayed(r)
        assert promoted == 0

        stats = get_stats(r)
        assert stats["delayed"] == 1

    def test_promote_delayed_empty_returns_zero(self, r):
        assert promote_delayed(r) == 0


# ── lock renewal ─────────────────────────────────────────────────


class TestLockRenewal:
    def test_renew_extends_lock(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")

        # Get original lock expiry
        original = float(r.hget(f"{PREFIX}:active", dequeued["id"]))

        # Renew lock
        renew_lock(r, dequeued["id"], ttl=7200)

        renewed = float(r.hget(f"{PREFIX}:active", dequeued["id"]))
        assert renewed > original

    def test_renewed_lock_prevents_stalled_recovery(self, r):
        job = _make_job()
        enqueue(r, job)
        dequeued = dequeue(r, "w")

        # Renew with fresh TTL
        renew_lock(r, dequeued["id"], ttl=3600)

        # Should not recover (lock is fresh)
        assert recover_stalled(r) == 0


# ── stats includes delayed ───────────────────────────────────────


class TestStatsDelayed:
    def test_stats_shows_delayed_count(self, r):
        job = _make_job()
        enqueue_delayed(r, job, time.time() + 3600)

        stats = get_stats(r)
        assert stats["delayed"] == 1
