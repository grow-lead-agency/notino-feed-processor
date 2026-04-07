"""
Langfuse → Neon AI cost exporter.

Fetches generation data from Langfuse API, aggregates by day/job/model,
and upserts into ai_cost_log table for Grafana dashboards.

Runs every 15 minutes via supercronic cron.

Usage:
    python jobs/langfuse_exporter.py                 # export last 24h
    python jobs/langfuse_exporter.py --hours 72      # export last 72h
"""
import os
import sys
import time
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "https://langfuse.growlead.cz").rstrip("/")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")

# Cost per 1M tokens (fallback when Langfuse doesn't have cost data)
MODEL_COSTS = {
    # OpenRouter models (current — cheap open-source)
    "google/gemma-3-27b-it:free": {"input": 0.0, "output": 0.0},
    "google/gemma-3-27b-it": {"input": 0.08, "output": 0.16},
    "google/gemma-3-12b-it:free": {"input": 0.0, "output": 0.0},
    "mistralai/mistral-small-3.1-24b-instruct": {"input": 0.03, "output": 0.11},
    "mistralai/mistral-nemo": {"input": 0.02, "output": 0.04},
    "meta-llama/llama-4-scout": {"input": 0.08, "output": 0.30},
    "meta-llama/llama-3.3-70b-instruct:free": {"input": 0.0, "output": 0.0},
    "openai/gpt-oss-120b:free": {"input": 0.0, "output": 0.0},
    # Legacy models (still referenced in historical data)
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "openrouter/google/gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    # Firecrawl (fixed cost per credit)
    "firecrawl/extract": {"fixed": 0.01},
    "firecrawl/map": {"fixed": 0.01},
}

# Map trace names to job names
TRACE_TO_JOB = {
    "category-mapping": "category_mapper",
    "product-categorization": "product_categorizer",
    "product-categorize": "product_categorizer",
    "crawl4ai-extract": "crawl4ai_client",
    "firecrawl-price-check": "smart_price_checker",
    "firecrawl-product-scrape": "firecrawl_scraper",
    "firecrawl-shipping": "shipping_scraper",
    "firecrawl-legal": "legal_scraper",
    "firecrawl-category-map": "category_tree_scraper",
    "firecrawl-ingredient": "ingredient_extractor",
    "firecrawl-reviews": "review_harvester",
    "firecrawl-shop-profile": "shop_profile_scraper",
    "firecrawl-voucher": "voucher_scraper",
    "crawl4ai-shipping": "shipping_scraper",
    "crawl4ai-legal": "legal_scraper",
}

API_PAGE_SIZE = 100
MAX_PAGES = 50  # safety limit: 5000 observations max


# ---------------------------------------------------------------------------
# Langfuse API
# ---------------------------------------------------------------------------
def fetch_observations(since: datetime) -> list[dict]:
    """Fetch all GENERATION observations from Langfuse since given timestamp."""
    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        print("[langfuse-export] WARNING: Langfuse keys not set — skipping", flush=True)
        return []

    all_obs = []
    page = 1

    while page <= MAX_PAGES:
        # Langfuse API expects ISO 8601 format with Z suffix
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        url = (
            f"{LANGFUSE_HOST}/api/public/observations"
            f"?type=GENERATION"
            f"&limit={API_PAGE_SIZE}"
            f"&page={page}"
            f"&fromStartTime={since_str}"
        )

        try:
            resp = requests.get(
                url,
                auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"[langfuse-export] API error page {page}: {e}", flush=True)
            break

        if resp.status_code != 200:
            print(f"[langfuse-export] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
            break

        data = resp.json()
        observations = data.get("data", [])
        all_obs.extend(observations)

        meta = data.get("meta", {})
        total_pages = meta.get("totalPages", 1)

        if page >= total_pages:
            break
        page += 1

    print(f"[langfuse-export] Fetched {len(all_obs)} observations ({page} pages)", flush=True)
    return all_obs


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _detect_provider(model: str) -> str:
    """Detect provider from model name."""
    if not model:
        return "unknown"
    model_lower = model.lower()
    if "firecrawl" in model_lower:
        return "firecrawl"
    # OpenRouter models use org/model format (google/gemma-3, mistralai/mistral, meta-llama/llama)
    if "/" in model_lower and any(prefix in model_lower for prefix in [
        "google/", "mistralai/", "meta-llama/", "qwen/", "nvidia/",
        "microsoft/", "ibm-granite/", "deepseek", "nousresearch/",
    ]):
        return "openrouter"
    if "openrouter" in model_lower:
        return "openrouter"
    if "gemini" in model_lower:
        return "gemini"
    if "gpt" in model_lower or "openai" in model_lower:
        return "openai"
    return "unknown"


def _detect_job(name: str, metadata: dict) -> str:
    """Detect job name from observation name or metadata."""
    if name in TRACE_TO_JOB:
        return TRACE_TO_JOB[name]
    # Check metadata
    if metadata and metadata.get("job"):
        return metadata["job"]
    # Fallback: use observation name
    return name or "unknown"


def _estimate_cost(model: str, usage: dict) -> float:
    """Estimate cost from model and token usage."""
    if not model:
        return 0.0

    costs = MODEL_COSTS.get(model) or MODEL_COSTS.get(model.split("/")[-1], {})

    # Fixed cost models (firecrawl)
    if "fixed" in costs:
        total = (usage or {}).get("total", 0) or (usage or {}).get("totalTokens", 0) or 1
        return costs["fixed"] * total

    # Token-based cost
    input_tokens = (usage or {}).get("input", 0) or (usage or {}).get("inputTokens", 0) or 0
    output_tokens = (usage or {}).get("output", 0) or (usage or {}).get("outputTokens", 0) or 0

    input_cost = (input_tokens / 1_000_000) * costs.get("input", 0)
    output_cost = (output_tokens / 1_000_000) * costs.get("output", 0)

    return input_cost + output_cost


def aggregate_observations(observations: list[dict]) -> list[dict]:
    """Aggregate observations by date + job + model."""
    # Key: (date, job_name, model)
    buckets = defaultdict(lambda: {
        "total_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "latencies": [],
        "provider": "",
    })

    for obs in observations:
        start_time = obs.get("startTime")
        if not start_time:
            continue

        # Parse date
        try:
            dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            date = dt.date().isoformat()
        except (ValueError, TypeError):
            continue

        model = obs.get("model") or "unknown"
        name = obs.get("name") or "unknown"
        metadata = obs.get("metadata") or {}
        usage = obs.get("usage") or {}
        level = obs.get("level") or "DEFAULT"

        job_name = _detect_job(name, metadata)
        provider = _detect_provider(model)

        key = (date, job_name, model)
        bucket = buckets[key]
        bucket["provider"] = provider
        bucket["total_calls"] += 1

        if level in ("ERROR",):
            bucket["failed_calls"] += 1
        else:
            bucket["successful_calls"] += 1

        input_tokens = usage.get("input", 0) or usage.get("inputTokens", 0) or 0
        output_tokens = usage.get("output", 0) or usage.get("outputTokens", 0) or 0
        total_tokens = usage.get("total", 0) or usage.get("totalTokens", 0) or (input_tokens + output_tokens)

        bucket["total_input_tokens"] += input_tokens
        bucket["total_output_tokens"] += output_tokens
        bucket["total_tokens"] += total_tokens
        bucket["total_cost"] += _estimate_cost(model, usage)

        # Latency from metadata
        latency = (metadata or {}).get("latency_ms")
        if latency and isinstance(latency, (int, float)):
            bucket["latencies"].append(int(latency))

    # Convert to list
    results = []
    for (date, job_name, model), bucket in buckets.items():
        latencies = sorted(bucket["latencies"])
        avg_latency = int(sum(latencies) / len(latencies)) if latencies else None
        p95_latency = latencies[int(len(latencies) * 0.95)] if len(latencies) >= 2 else avg_latency

        results.append({
            "date": date,
            "job_name": job_name,
            "model": model,
            "provider": bucket["provider"],
            "total_calls": bucket["total_calls"],
            "successful_calls": bucket["successful_calls"],
            "failed_calls": bucket["failed_calls"],
            "total_input_tokens": bucket["total_input_tokens"],
            "total_output_tokens": bucket["total_output_tokens"],
            "total_tokens": bucket["total_tokens"],
            "estimated_cost_usd": round(bucket["total_cost"], 4),
            "avg_latency_ms": avg_latency,
            "p95_latency_ms": p95_latency,
        })

    return results


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------
def upsert_cost_data(rows: list[dict]) -> int:
    """Upsert aggregated cost data into ai_cost_log. Returns count."""
    if not rows:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute("""
                    INSERT INTO ai_cost_log
                    (date, job_name, model, provider, total_calls, successful_calls,
                     failed_calls, total_input_tokens, total_output_tokens, total_tokens,
                     estimated_cost_usd, avg_latency_ms, p95_latency_ms)
                    VALUES (%(date)s, %(job_name)s, %(model)s, %(provider)s,
                            %(total_calls)s, %(successful_calls)s, %(failed_calls)s,
                            %(total_input_tokens)s, %(total_output_tokens)s, %(total_tokens)s,
                            %(estimated_cost_usd)s, %(avg_latency_ms)s, %(p95_latency_ms)s)
                    ON CONFLICT (date, job_name, model)
                    DO UPDATE SET
                        total_calls = EXCLUDED.total_calls,
                        successful_calls = EXCLUDED.successful_calls,
                        failed_calls = EXCLUDED.failed_calls,
                        total_input_tokens = EXCLUDED.total_input_tokens,
                        total_output_tokens = EXCLUDED.total_output_tokens,
                        total_tokens = EXCLUDED.total_tokens,
                        estimated_cost_usd = EXCLUDED.estimated_cost_usd,
                        avg_latency_ms = EXCLUDED.avg_latency_ms,
                        p95_latency_ms = EXCLUDED.p95_latency_ms,
                        created_at = NOW()
                """, row)
            conn.commit()
        return len(rows)
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(hours: int = 24) -> dict:
    start_ts = time.time()
    print(f"=== Langfuse Cost Exporter starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    print(f"Config: hours={hours}, host={LANGFUSE_HOST}", flush=True)

    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Fetch
    observations = fetch_observations(since)
    if not observations:
        print("No observations found — nothing to export.", flush=True)
        return {"observations": 0, "rows": 0}

    # Aggregate
    rows = aggregate_observations(observations)
    print(f"Aggregated into {len(rows)} date/job/model buckets", flush=True)

    # Upsert
    count = upsert_cost_data(rows)
    elapsed = time.time() - start_ts

    # Summary
    total_cost = sum(r["estimated_cost_usd"] for r in rows)
    total_calls = sum(r["total_calls"] for r in rows)
    print(f"=== Done in {elapsed:.1f}s: {count} rows upserted, "
          f"{total_calls} calls, ${total_cost:.4f} estimated cost ===", flush=True)

    return {"observations": len(observations), "rows": count, "cost": total_cost}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Langfuse → Neon AI cost exporter")
    parser.add_argument("--hours", type=int, default=24, help="Hours of data to export (default: 24)")
    args = parser.parse_args()
    main(hours=args.hours)
