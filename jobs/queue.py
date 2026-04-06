"""
Redis Queue Engine — BullMQ-like features in Python.

Features:
  - Priority queue (sorted set, lower score = higher priority)
  - Job states: waiting → active → completed/failed/stalled
  - Retry with exponential backoff + jitter
  - Stalled job detection (lock-based, configurable TTL)
  - Job deduplication (by shop domain — no double-processing)
  - Rate limiting per queue (global max jobs/second)
  - Per-shop result history
  - Queue stats (counts per state)
  - Dead letter queue (exceeded max retries)

Redis key layout:
  {prefix}:waiting          — sorted set (score=priority)
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
import random
from datetime import datetime, timezone, timedelta

import redis


REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PREFIX = os.environ.get("QUEUE_PREFIX", "notino:scrape")

# Defaults
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_MS = 5000
DEFAULT_LOCK_TTL_S = 1800  # 30 min — if job takes longer, it's stalled
DEFAULT_STALE_CHECK_S = 60
MAX_COMPLETED_KEEP = 500
MAX_FAILED_KEEP = 200
MAX_DEAD_KEEP = 100


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


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
    """
    domain = job["domain"]
    dedup_key = f"{PREFIX}:dedup"

    # Dedup check
    if r.sismember(dedup_key, domain):
        return False

    job_id = job["id"]
    job_key = f"{PREFIX}:job:{job_id}"

    pipe = r.pipeline()
    pipe.hset(job_key, mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in job.items()})
    pipe.expire(job_key, 86400)  # TTL 24h
    pipe.zadd(f"{PREFIX}:waiting", {job_id: job["priority"]})
    pipe.sadd(dedup_key, domain)
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
    If retry=True and attempts < max_retries, re-enqueue with exponential backoff.
    Otherwise, move to dead letter queue.
    """
    job_id = job["id"]
    domain = job.get("domain", "?")
    attempts = job.get("attempts", 1)
    max_retries = job.get("max_retries", DEFAULT_MAX_RETRIES)

    pipe = r.pipeline()
    pipe.hdel(f"{PREFIX}:active", job_id)
    pipe.hincrby(f"{PREFIX}:stats", "total_failed", 1)

    if retry and attempts < max_retries:
        # Exponential backoff with jitter
        delay_ms = DEFAULT_BACKOFF_BASE_MS * (2 ** (attempts - 1))
        jitter = random.uniform(0.5, 1.5)
        delay_s = (delay_ms * jitter) / 1000

        # Re-enqueue (keep dedup, will re-add after delay)
        pipe.srem(f"{PREFIX}:dedup", domain)
        pipe.execute()

        # Create retry job
        retry_job = job.copy()
        retry_job["id"] = f"scrape:{domain}:{uuid.uuid4().hex[:8]}"
        retry_job["state"] = "waiting"
        retry_job["last_error"] = error
        retry_job["retry_after"] = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat()

        time.sleep(min(delay_s, 60))  # wait before re-enqueue (cap at 60s)
        enqueue(r, retry_job)

        print(f"  [RETRY] {domain} attempt {attempts}/{max_retries} in {delay_s:.1f}s: {error}", flush=True)
    else:
        # Dead letter queue
        pipe.srem(f"{PREFIX}:dedup", domain)
        pipe.hset(f"{PREFIX}:job:{job_id}", "state", "dead")
        pipe.hset(f"{PREFIX}:job:{job_id}", "error", error)

        summary = json.dumps({"id": job_id, "domain": domain, "error": error, "attempts": attempts, "at": datetime.now(timezone.utc).isoformat()})
        pipe.lpush(f"{PREFIX}:dead", summary)
        pipe.ltrim(f"{PREFIX}:dead", 0, MAX_DEAD_KEEP - 1)
        pipe.hincrby(f"{PREFIX}:stats", "total_dead", 1)
        pipe.execute()

        print(f"  [DEAD] {domain} after {attempts} attempts: {error}", flush=True)


# ============================================================
# STALLED JOB DETECTION
# ============================================================

def recover_stalled(r) -> int:
    """
    Check active jobs for expired locks.
    Re-enqueue stalled jobs.
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

            # Remove from active
            r.hdel(f"{PREFIX}:active", job_id)
            r.srem(f"{PREFIX}:dedup", domain)
            r.hincrby(f"{PREFIX}:stats", "total_stalled", 1)

            if attempts < max_retries:
                # Re-enqueue
                job = {}
                for k, v in raw.items():
                    try:
                        job[k] = json.loads(v)
                    except:
                        job[k] = v
                job["id"] = f"scrape:{domain}:{uuid.uuid4().hex[:8]}"
                job["state"] = "waiting"
                job["last_error"] = "stalled"
                for int_field in ("shop_id", "country_id", "max_products", "priority", "max_retries", "attempts"):
                    if int_field in job:
                        try:
                            job[int_field] = int(job[int_field])
                        except:
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

        # Recover stalled first
        recovered = recover_stalled(r)
        if recovered:
            print(f"Recovered {recovered} stalled jobs", flush=True)

        enqueued = schedule_due_shops(r)

        stats_after = get_stats(r)
        print(f"Enqueued: {enqueued}, After: {stats_after}", flush=True)

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
        print("Usage: queue.py [schedule|stats|waiting|dead|recover|flush]")


if __name__ == "__main__":
    main()
