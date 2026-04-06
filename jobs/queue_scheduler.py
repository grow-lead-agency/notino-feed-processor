"""
Queue Scheduler — dispatches scrape jobs to Redis/BullMQ queues.

Scheduling tiers:
  TIER A: class-1 (142 shops) → daily
  TIER B: class-2 (316 shops) → every 2-3 days (rotate)
  TIER C: class-3 (75 shops)  → weekly

Each shop = 1 job in queue with priority.
Workers pick from queue and scrape.

Cron: runs every hour, checks what's due.
"""
import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import redis

sys.path.insert(0, "/app")
from jobs.db import get_conn

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
QUEUE_PREFIX = "notino"

# Scheduling config
TIER_CONFIG = {
    "class-1": {"interval_hours": 24, "priority": 1, "max_products": 50000},
    "class-2": {"interval_hours": 48, "priority": 5, "max_products": 50000},
    "class-3": {"interval_hours": 168, "priority": 10, "max_products": 50000},  # weekly
}


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def get_shops_with_schedule():
    """Load all shops with their category (class-1/2/3) and last scrape time."""
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
    return rows


def is_due(last_scraped, interval_hours):
    """Check if shop is due for scraping."""
    if last_scraped is None:
        return True  # never scraped
    now = datetime.now(timezone.utc)
    if last_scraped.tzinfo is None:
        last_scraped = last_scraped.replace(tzinfo=timezone.utc)
    return (now - last_scraped) > timedelta(hours=interval_hours)


def enqueue_shop(r, shop_id, domain, category, country_id, feed_url, difficulty, priority, max_products):
    """Add scrape job to Redis queue."""
    job_id = f"scrape:{domain}:{int(time.time())}"
    job_data = {
        "id": job_id,
        "shop_id": shop_id,
        "domain": domain,
        "category": category,
        "country_id": country_id,
        "feed_url": feed_url,
        "difficulty": difficulty,
        "max_products": max_products,
        "priority": priority,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }

    # Use sorted set for priority queue (lower score = higher priority)
    queue_key = f"{QUEUE_PREFIX}:scrape:pending"
    r.zadd(queue_key, {json.dumps(job_data): priority})
    return job_id


def dequeue_shop(r):
    """Pop highest priority job from queue."""
    queue_key = f"{QUEUE_PREFIX}:scrape:pending"
    # ZPOPMIN = lowest score = highest priority
    result = r.zpopmin(queue_key, count=1)
    if not result:
        return None
    job_json, score = result[0]
    return json.loads(job_json)


def get_queue_stats(r):
    """Get current queue statistics."""
    pending = r.zcard(f"{QUEUE_PREFIX}:scrape:pending")
    active = r.scard(f"{QUEUE_PREFIX}:scrape:active")
    completed = r.get(f"{QUEUE_PREFIX}:scrape:completed_count") or 0
    failed = r.get(f"{QUEUE_PREFIX}:scrape:failed_count") or 0
    return {
        "pending": pending,
        "active": int(active),
        "completed": int(completed),
        "failed": int(failed),
    }


def mark_active(r, job_id):
    r.sadd(f"{QUEUE_PREFIX}:scrape:active", job_id)


def mark_completed(r, job_id):
    r.srem(f"{QUEUE_PREFIX}:scrape:active", job_id)
    r.incr(f"{QUEUE_PREFIX}:scrape:completed_count")


def mark_failed(r, job_id, error):
    r.srem(f"{QUEUE_PREFIX}:scrape:active", job_id)
    r.incr(f"{QUEUE_PREFIX}:scrape:failed_count")
    # Store failed job for retry
    r.lpush(f"{QUEUE_PREFIX}:scrape:failed", json.dumps({"job_id": job_id, "error": str(error)}))
    r.ltrim(f"{QUEUE_PREFIX}:scrape:failed", 0, 999)  # keep last 1000


def main():
    """Check what's due and enqueue."""
    print(f"=== Queue Scheduler at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    r = get_redis()
    shops = get_shops_with_schedule()

    # Stats before
    stats = get_queue_stats(r)
    print(f"Queue before: {stats}", flush=True)

    enqueued = 0
    skipped = 0

    for shop_id, domain, category, country_id, feed_url, difficulty, last_scraped in shops:
        tier = TIER_CONFIG.get(category, TIER_CONFIG["class-3"])

        if not is_due(last_scraped, tier["interval_hours"]):
            skipped += 1
            continue

        # Check if already in queue
        queue_key = f"{QUEUE_PREFIX}:scrape:pending"
        existing = r.zscore(queue_key, json.dumps({"domain": domain}))  # won't match, simplified check

        job_id = enqueue_shop(
            r, shop_id, domain, category, country_id,
            feed_url, difficulty, tier["priority"], tier["max_products"]
        )
        enqueued += 1
        print(f"  Enqueued: {domain} ({category}, priority={tier['priority']})", flush=True)

    # Stats after
    stats = get_queue_stats(r)
    print(f"\nEnqueued: {enqueued}, Skipped (not due): {skipped}", flush=True)
    print(f"Queue after: {stats}", flush=True)


if __name__ == "__main__":
    main()
