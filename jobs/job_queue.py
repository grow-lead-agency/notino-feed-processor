"""
Redis Queue Engine — BullMQ-like features in Python.

Features:
  - Priority queue (sorted set, lower score = higher priority)
  - Job states: waiting → active → completed/failed/stalled
  - Retry with exponential backoff (delayed queue, no blocking sleep)
  - Stalled job detection (lock-based, configurable TTL)
  - Job deduplication (atomic Lua script — no race conditions)
  - Per-shop result history
  - Queue stats (counts per state)
  - Dead letter queue (exceeded max retries)

Redis key layout:
  {prefix}:waiting          — sorted set (score=priority)
  {prefix}:delayed          — sorted set (score=retry_after timestamp)
  {prefix}:active           — hash (job_id → lock_expires_at)
  {prefix}:completed        — list (last N completed job summaries)
  {prefix}:failed           — list (last N failed job summaries)
  {prefix}:dead             — list (exceeded max retries)
  {prefix}:job:{id}         — hash (full job data)
  {prefix}:dedup            — set (domain keys for dedup)
  {prefix}:stats            — hash (counters)
  {prefix}:results:{domain} — hash (last result per shop)
"""
import os
import json
import time
import uuid
from datetime import datetime, timezone, timedelta

import redis


REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PREFIX = os.environ.get("QUEUE_PREFIX", "notino:scrape")

# Defaults
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_MS = 5000
DEFAULT_LOCK_TTL_S = 3600  # 60 min — safe for large shop scrapes (50K products)
DEFAULT_STALE_CHECK_S = 60
MAX_COMPLETED_KEEP = 500
MAX_FAILED_KEEP = 200
MAX_DEAD_KEEP = 1000  # increased from 100 — need visibility during batch failures

# Lua script for atomic dedup + enqueue
LUA_DEDUP_ENQUEUE = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then
    return 0
end
redis.call('SADD', KEYS[1], ARGV[1])
return 1
"""

# Redis singleton
_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


# ============================================================
# JOB CREATION
# ============================================================

def create_job(
    *,
    shop_id: int,
    domain: str,
    category: str,
    country_id: int,
    feed_url: str,
    difficulty: str = "easy",
    max_products: int = 50000,
    priority: int = 5,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """Create a job dict (not yet enqueued)."""
    return {
        "id": f"scrape:{domain}:{uuid.uuid4().hex[:8]}",
        "shop_id": shop_id,
        "domain": domain,
        "category": category,
        "country_id": country_id,
        "feed_url": feed_url,
        "difficulty": difficulty,
        "max_products": max_products,
        "priority": priority,
        "max_retries": max_retries,
        "attempts": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "state": "waiting",
    }


# ============================================================
# ENQUEUE
# ============================================================

def enqueue(r, job: dict) -> bool:
    """
    Add job to waiting queue.
    Returns False if duplicate (same domain already in waiting/active).
    Uses Lua script for atomic dedup check.
    """
    domain = job["domain"]
    dedup_key = f"{PREFIX}:dedup"

    # Atomic dedup check + add
    added = r.eval(LUA_DEDUP_ENQUEUE, 1, dedup_key, domain)
    if not added:
        return False

    job_id = job["id"]
    job_key = f"{PREFIX}:job:{job_id}"

    pipe = r.pipeline()
    pipe.hset(job_key, mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in job.items()})
    pipe.expire(job_key, 86400)  # TTL 24h
    pipe.zadd(f"{PREFIX}:waiting", {job_id: job["priority"]})
    pipe.hincrby(f"{PREFIX}:stats", "total_enqueued", 1)
    pipe.execute()

    return True


def enqueue_bulk(r, jobs: list[dict]) -> int:
    """Enqueue multiple jobs. Returns count enqueued (skips duplicates)."""
    enqueued = 0
    for job in jobs:
        if enqueue(r, job):
            enqueued += 1
    return enqueued


def enqueue_delayed(r, job: dict, retry_after_ts: float):
    """Add job to delayed queue (sorted by retry_after timestamp)."""
    job_id = job["id"]
    job_key = f"{PREFIX}:job:{job_id}"

    pipe = r.pipeline()
    pipe.hset(job_key, mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in job.items()})
    pipe.expire(job_key, 86400)
    pipe.zadd(f"{PREFIX}:delayed", {job_id: retry_after_ts})
    pipe.execute()


def promote_delayed(r) -> int:
    """Move delayed jobs that are ready to the waiting queue. Returns count promoted."""
    now = time.time()
    # Get all delayed jobs with score <= now (ready to run)
    ready = r.zrangebyscore(f"{PREFIX}:delayed", "-inf", str(now))
    if not ready:
        return 0

    promoted = 0
    for job_id in ready:
        job_key = f"{PREFIX}:job:{job_id}"
        raw = r.hgetall(job_key)
        if not raw:
            r.zrem(f"{PREFIX}:delayed", job_id)
            continue

        priority = int(raw.get("priority", "5"))

        pipe = r.pipeline()
        pipe.zrem(f"{PREFIX}:delayed", job_id)
        pipe.zadd(f"{PREFIX}:waiting", {job_id: priority})
        pipe.hset(job_key, "state", "waiting")
        pipe.execute()
        promoted += 1

    return promoted


# ============================================================
# DEQUEUE (Worker picks job)
# ============================================================

def dequeue(r, worker_id: str = "worker") -> dict | None:
    """
    Pop highest-priority job from waiting queue.
    Moves to active with lock.
    Returns job dict or None.
    """
    # ZPOPMIN = lowest score = highest priority
    result = r.zpopmin(f"{PREFIX}:waiting", count=1)
    if not result:
        return None

    job_id, score = result[0]
    job_key = f"{PREFIX}:job:{job_id}"

    # Load job data
    raw = r.hgetall(job_key)
    if not raw:
        return None

    # Parse job
    job = {}
    for k, v in raw.items():
        try:
            job[k] = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            job[k] = v

    # Ensure numeric types
    for int_field in ("shop_id", "country_id", "max_products", "priority", "max_retries", "attempts"):
        if int_field in job:
            try:
                job[int_field] = int(job[int_field])
            except (ValueError, TypeError):
                pass

    # Set active with lock
    lock_expires = time.time() + DEFAULT_LOCK_TTL_S
    pipe = r.pipeline()
    pipe.hset(f"{PREFIX}:active", job_id, str(lock_expires))
    pipe.hset(job_key, "state", "active")
    pipe.hset(job_key, "started_at", datetime.now(timezone.utc).isoformat())
    pipe.hset(job_key, "worker_id", worker_id)
    pipe.hset(job_key, "attempts", str(job.get("attempts", 0) + 1))
    pipe.execute()

    job["state"] = "active"
    job["attempts"] = job.get("attempts", 0) + 1

    return job


# ============================================================
# LOCK RENEWAL
# ============================================================

def renew_lock(r, job_id: str, ttl: int = DEFAULT_LOCK_TTL_S):
    """Extend the lock on an active job. Call periodically for long-running scrapes."""
    r.hset(f"{PREFIX}:active", job_id, str(time.time() + ttl))


# ============================================================
# COMPLETE / FAIL
# ============================================================

def complete(r, job: dict, result: dict = None):
    """Mark job as completed."""
    job_id = job["id"]
    domain = job.get("domain", "?")

    pipe = r.pipeline()
    pipe.hdel(f"{PREFIX}:active", job_id)
    pipe.srem(f"{PREFIX}:dedup", domain)
    pipe.hset(f"{PREFIX}:job:{job_id}", "state", "completed")
    pipe.hset(f"{PREFIX}:job:{job_id}", "completed_at", datetime.now(timezone.utc).isoformat())
    if result:
        pipe.hset(f"{PREFIX}:job:{job_id}", "result", json.dumps(result))

    # Stats
    pipe.hincrby(f"{PREFIX}:stats", "total_completed", 1)

    # Completed log (keep last N)
    summary = json.dumps({"id": job_id, "domain": domain, "result": result, "at": datetime.now(timezone.utc).isoformat()})
    pipe.lpush(f"{PREFIX}:completed", summary)
    pipe.ltrim(f"{PREFIX}:completed", 0, MAX_COMPLETED_KEEP - 1)

    # Per-shop result
    pipe.hset(f"{PREFIX}:results", domain, json.dumps({"saved": result.get("saved", 0) if result else 0, "at": datetime.now(timezone.utc).isoformat()}))

    pipe.execute()


def fail(r, job: dict, error: str, retry: bool = True):
    """
    Mark job as failed.
    If retry=True and attempts < max_retries, schedule delayed re-enqueue.
    Otherwise, move to dead letter queue.
    No blocking sleep — uses delayed queue for backoff.
    """
    job_id = job["id"]
    domain = job.get("domain", "?")
    attempts = job.get("attempts", 1)
    max_retries = job.get("max_retries", DEFAULT_MAX_RETRIES)

    if retry and attempts < max_retries:
        # Exponential backoff with jitter (non-blocking)
        import random
        delay_ms = DEFAULT_BACKOFF_BASE_MS * (2 ** (attempts - 1))
        jitter = random.uniform(0.5, 1.5)
        delay_s = (delay_ms * jitter) / 1000
        retry_after_ts = time.time() + delay_s

        # Atomic: remove from active + dedup, update stats
        pipe = r.pipeline()
        pipe.hdel(f"{PREFIX}:active", job_id)
        pipe.srem(f"{PREFIX}:dedup", domain)
        pipe.hincrby(f"{PREFIX}:stats", "total_failed", 1)
        pipe.execute()

        # Create retry job and schedule it
        retry_job = job.copy()
        retry_job["id"] = f"scrape:{domain}:{uuid.uuid4().hex[:8]}"
        retry_job["state"] = "delayed"
        retry_job["last_error"] = error
        retry_job["retry_after"] = datetime.fromtimestamp(retry_after_ts, tz=timezone.utc).isoformat()

        enqueue_delayed(r, retry_job, retry_after_ts)

        print(f"  [RETRY] {domain} attempt {attempts}/{max_retries}, delayed {delay_s:.1f}s: {error}", flush=True)
    else:
        # Dead letter queue — atomic pipeline
        pipe = r.pipeline()
        pipe.hdel(f"{PREFIX}:active", job_id)
        pipe.srem(f"{PREFIX}:dedup", domain)
        pipe.hset(f"{PREFIX}:job:{job_id}", "state", "dead")
        pipe.hset(f"{PREFIX}:job:{job_id}", "error", error)
        pipe.hincrby(f"{PREFIX}:stats", "total_failed", 1)
        pipe.hincrby(f"{PREFIX}:stats", "total_dead", 1)

        summary = json.dumps({"id": job_id, "domain": domain, "error": error, "attempts": attempts, "at": datetime.now(timezone.utc).isoformat()})
        pipe.lpush(f"{PREFIX}:dead", summary)
        pipe.ltrim(f"{PREFIX}:dead", 0, MAX_DEAD_KEEP - 1)
        pipe.execute()

        print(f"  [DEAD] {domain} after {attempts} attempts: {error}", flush=True)


# ============================================================
# STALLED JOB DETECTION
# ============================================================

def recover_stalled(r) -> int:
    """
    Check active jobs for expired locks.
    Re-enqueue stalled jobs. Atomic pipeline per job.
    Returns count of recovered jobs.
    """
    active = r.hgetall(f"{PREFIX}:active")
    now = time.time()
    recovered = 0

    for job_id, lock_expires_str in active.items():
        try:
            lock_expires = float(lock_expires_str)
        except ValueError:
            continue

        if now > lock_expires:
            # Job is stalled — worker probably crashed
            job_key = f"{PREFIX}:job:{job_id}"
            raw = r.hgetall(job_key)
            if not raw:
                r.hdel(f"{PREFIX}:active", job_id)
                continue

            domain = raw.get("domain", "?")
            attempts = int(raw.get("attempts", "1"))
            max_retries = int(raw.get("max_retries", str(DEFAULT_MAX_RETRIES)))

            print(f"  [STALLED] {domain} (job {job_id}) — lock expired, recovering", flush=True)

            # Atomic: remove from active + dedup + update stats
            pipe = r.pipeline()
            pipe.hdel(f"{PREFIX}:active", job_id)
            pipe.srem(f"{PREFIX}:dedup", domain)
            pipe.hincrby(f"{PREFIX}:stats", "total_stalled", 1)
            pipe.execute()

            if attempts < max_retries:
                # Re-enqueue
                job = {}
                for k, v in raw.items():
                    try:
                        job[k] = json.loads(v)
                    except Exception:
                        job[k] = v
                job["id"] = f"scrape:{domain}:{uuid.uuid4().hex[:8]}"
                job["state"] = "waiting"
                job["last_error"] = "stalled"
                for int_field in ("shop_id", "country_id", "max_products", "priority", "max_retries", "attempts"):
                    if int_field in job:
                        try:
                            job[int_field] = int(job[int_field])
                        except Exception:
                            pass
                enqueue(r, job)
                recovered += 1

    return recovered


# ============================================================
# QUEUE STATS
# ============================================================

def get_stats(r) -> dict:
    """Get queue statistics."""
    stats = r.hgetall(f"{PREFIX}:stats") or {}
    return {
        "waiting": r.zcard(f"{PREFIX}:waiting"),
        "delayed": r.zcard(f"{PREFIX}:delayed"),
        "active": r.hlen(f"{PREFIX}:active"),
        "completed_total": int(stats.get("total_completed", 0)),
        "failed_total": int(stats.get("total_failed", 0)),
        "dead_total": int(stats.get("total_dead", 0)),
        "stalled_total": int(stats.get("total_stalled", 0)),
        "enqueued_total": int(stats.get("total_enqueued", 0)),
    }


def get_waiting_jobs(r, count: int = 10) -> list[str]:
    """Get top N waiting job IDs (highest priority first)."""
    return r.zrange(f"{PREFIX}:waiting", 0, count - 1)


def get_dead_letter(r, count: int = 10) -> list[dict]:
    """Get last N dead letter entries."""
    raw = r.lrange(f"{PREFIX}:dead", 0, count - 1)
    return [json.loads(x) for x in raw]


def flush_queue(r):
    """Clear all queue data. DANGEROUS."""
    keys = r.keys(f"{PREFIX}:*")
    if keys:
        r.delete(*keys)


# ============================================================
# SCHEDULER (enqueue due shops)
# ============================================================

TIER_CONFIG = {
    "class-1": {"interval_hours": 24, "priority": 1, "max_products": 50000},
    "class-2": {"interval_hours": 48, "priority": 5, "max_products": 50000},
    "class-3": {"interval_hours": 168, "priority": 10, "max_products": 50000},
}


def schedule_due_shops(r) -> int:
    """Check what's due and enqueue. Returns count enqueued."""
    from jobs.db import get_conn

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name, c.category, c.country_id,
               wc.sitemap_product_feed, wc.difficulty,
               (SELECT max(last_seen_at) FROM product_offers WHERE shop_id = c.id) as last_scraped
        FROM website_checks wc
        JOIN competitors c ON c.id = wc.competitor_id
        WHERE wc.sitemap_product_feed IS NOT NULL
          AND wc.difficulty NOT IN ('dead', 'unreachable')
          AND c.country_id IS NOT NULL
        ORDER BY c.category, c.name
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    now = datetime.now(timezone.utc)
    enqueued = 0

    for shop_id, domain, category, country_id, feed_url, difficulty, last_scraped in rows:
        tier = TIER_CONFIG.get(category, TIER_CONFIG["class-3"])

        # Check if due
        if last_scraped is not None:
            if last_scraped.tzinfo is None:
                last_scraped = last_scraped.replace(tzinfo=timezone.utc)
            if (now - last_scraped) < timedelta(hours=tier["interval_hours"]):
                continue

        job = create_job(
            shop_id=shop_id,
            domain=domain,
            category=category,
            country_id=country_id,
            feed_url=feed_url.replace("&amp;", "&"),
            difficulty=difficulty,
            max_products=tier["max_products"],
            priority=tier["priority"],
        )

        if enqueue(r, job):
            enqueued += 1

    return enqueued


# ============================================================
# CLI
# ============================================================

def main():
    """CLI: schedule, stats, recover, flush."""
    import sys
    r = get_redis()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "schedule"

    if cmd == "schedule":
        print(f"=== Queue Scheduler at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
        stats_before = get_stats(r)
        print(f"Before: {stats_before}", flush=True)

        # Promote delayed jobs first
        promoted = promote_delayed(r)
        if promoted:
            print(f"Promoted {promoted} delayed jobs to waiting", flush=True)

        # Recover stalled
        recovered = recover_stalled(r)
        if recovered:
            print(f"Recovered {recovered} stalled jobs", flush=True)

        enqueued = schedule_due_shops(r)

        stats_after = get_stats(r)
        print(f"Enqueued: {enqueued}, After: {stats_after}", flush=True)

        # DLQ alerting
        dead_count = stats_after["dead_total"]
        dead_before = stats_before["dead_total"]
        if dead_count > dead_before:
            print(f"  [WARN] DLQ grew by {dead_count - dead_before} entries! Check dead letter queue.", flush=True)

    elif cmd == "stats":
        stats = get_stats(r)
        print(json.dumps(stats, indent=2))

    elif cmd == "waiting":
        jobs = get_waiting_jobs(r, 20)
        for j in jobs:
            print(f"  {j}")

    elif cmd == "dead":
        dead = get_dead_letter(r, 20)
        for d in dead:
            print(f"  {d['domain']}: {d.get('error','?')} ({d.get('attempts','?')} attempts)")

    elif cmd == "recover":
        recovered = recover_stalled(r)
        print(f"Recovered: {recovered}")

    elif cmd == "flush":
        flush_queue(r)
        print("Queue flushed")

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: job_queue.py [schedule|stats|waiting|dead|recover|flush]")


if __name__ == "__main__":
    main()
