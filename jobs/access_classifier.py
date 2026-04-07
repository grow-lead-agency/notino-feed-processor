"""
Job: Access Tier Classifier
Classify shops by the minimum tool required to scrape them.

Waterfall test (first success wins):
  1. http           → plain requests GET
  2. crawl4ai       → Playwright headless (self-hosted)
  3. crawl4ai_captcha → Playwright + Capsolver (future)
  4. crawl4ai_proxy → Playwright + proxy (Proxmox only, future)
  5. firecrawl      → Fire-engine cloud API
  6. brightdata     → Bright Data Web Unlocker (future)
  7. blocked        → nothing works

For each shop: fetch homepage with progressively stronger tools.
A "success" = HTTP 200 + response body > 1KB + contains <html.

Cron: daily at 2 AM, batch 100 unclassified shops.
Re-classification: shops older than 30 days get re-tested.
"""
import os
import sys
import time

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.crawl4ai_client import crawl4ai_scrape_html, crawl4ai_health

FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

MAX_SHOPS_PER_RUN = 100
MIN_BODY_LENGTH = 1000  # bytes — anything less is likely a block page


# ----------------------------------------------------------------------------
# Tier testers
# ----------------------------------------------------------------------------
def _test_http(url: str) -> bool:
    """Tier 1: plain HTTP GET."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return False
        body = resp.text
        return len(body) > MIN_BODY_LENGTH and "<html" in body.lower()[:500]
    except requests.RequestException:
        return False


def _test_crawl4ai(url: str) -> bool:
    """Tier 2: Crawl4AI Playwright headless."""
    html = crawl4ai_scrape_html(url, timeout=30)
    if not html:
        return False
    return len(html) > MIN_BODY_LENGTH and "<" in html[:100]


def _test_firecrawl(url: str) -> bool:
    """Tier 5: Firecrawl cloud API."""
    if not FIRECRAWL_API_KEY:
        return False
    try:
        resp = requests.post(
            FIRECRAWL_API_URL,
            headers={
                "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "url": url,
                "formats": ["html"],
                "proxy": "stealth",
                "onlyMainContent": False,
                "timeout": 30000,
            },
            timeout=40,
        )
        if resp.status_code != 200:
            return False
        body = resp.json()
        if not body.get("success"):
            return False
        html = (body.get("data") or {}).get("html") or ""
        return len(html) > MIN_BODY_LENGTH
    except Exception:
        return False


# Tier waterfall — ordered from cheapest to most expensive
TIERS = [
    ("http", _test_http),
    ("crawl4ai", _test_crawl4ai),
    # ("crawl4ai_captcha", _test_captcha),  # future
    # ("crawl4ai_proxy", _test_proxy),      # future — Proxmox only
    ("firecrawl", _test_firecrawl),
    # ("brightdata", _test_brightdata),     # future
]


# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------
def get_shops_to_classify(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """Load shops needing classification. Unclassified first, then stale (>30d)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url
                FROM shops
                WHERE deleted_at IS NULL
                  AND url IS NOT NULL
                  AND (
                      access_tier IS NULL
                      OR access_tier_checked_at < NOW() - INTERVAL '30 days'
                  )
                ORDER BY
                    access_tier IS NULL DESC,
                    access_tier_checked_at ASC NULLS FIRST
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


def save_classification(shop_id: int, tier: str, error: str | None = None) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shops
                SET access_tier = %s::access_tier,
                    access_tier_checked_at = NOW(),
                    access_tier_error = %s
                WHERE id = %s AND deleted_at IS NULL
                """,
                (tier, error, shop_id),
            )
            conn.commit()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Per-shop classifier
# ----------------------------------------------------------------------------
def classify_shop(shop_id: int, name: str, url: str) -> str:
    """Run waterfall test. Returns the tier name."""
    base = url.rstrip("/")

    for tier_name, tester in TIERS:
        try:
            if tester(base):
                print(f"  [{name}] → {tier_name}", flush=True)
                save_classification(shop_id, tier_name)
                return tier_name
        except Exception as e:
            print(f"  [{name}] {tier_name} error: {e}", flush=True)
            continue

    # Nothing worked
    print(f"  [{name}] → blocked", flush=True)
    save_classification(shop_id, "blocked", error="All tiers failed")
    return "blocked"


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(f"=== Access Classifier starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    crawl4ai_up = crawl4ai_health()
    print(f"Crawl4AI: {'UP' if crawl4ai_up else 'DOWN (skipping tier 2)'}", flush=True)

    shops = get_shops_to_classify()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run)", flush=True)

    if not shops:
        print("No shops to classify. Done.", flush=True)
        return

    tier_counts = {}
    for shop_id, name, url in shops:
        tier = classify_shop(shop_id, name, url)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    elapsed = time.time() - start_ts
    summary = ", ".join(f"{t}={c}" for t, c in sorted(tier_counts.items()))
    print(f"=== Done: {len(shops)} shops classified | {summary} | {elapsed:.1f}s ===", flush=True)


if __name__ == "__main__":
    main()
