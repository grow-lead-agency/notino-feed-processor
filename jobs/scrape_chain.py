"""
Scrape Chain — shared fallback engine for URL-type scrapers.

3-layer waterfall with circuit breaker:
  1. Crawl4AI (self-hosted, free, LLM extraction)
  2. Plain HTTP + BeautifulSoup (free, meta/JSON-LD extraction)
  3. Firecrawl (paid, anti-bot, LLM extraction) — only for high-priority URLs

Circuit breaker: if Crawl4AI fails N times consecutively, skip it for
COOLDOWN_SECONDS and go straight to layer 2/3. Resets on first success.

Usage:
    from jobs.scrape_chain import scrape_with_chain

    data = scrape_with_chain(
        url="https://example.com/brand/chanel",
        schema=BRAND_SCHEMA,
        prompt="Extract brand info...",
        priority=5,          # 1-3 = high (allow Firecrawl), 4+ = low (skip Firecrawl)
        html_extractor=my_meta_fallback,  # custom Layer 2 function
    )
    # Returns: {"data": {...}, "engine": "crawl4ai"|"http"|"firecrawl"|None}
"""
import os
import sys
import time
import threading

import requests
import structlog

sys.path.insert(0, "/app")

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Circuit breaker for Crawl4AI
# ---------------------------------------------------------------------------
CRAWL4AI_FAIL_THRESHOLD = 5      # consecutive failures before tripping
CRAWL4AI_COOLDOWN_SECONDS = 300  # 5 min cooldown after trip

_crawl4ai_lock = threading.Lock()
_crawl4ai_consecutive_fails = 0
_crawl4ai_tripped_at: float | None = None


def _crawl4ai_is_available() -> bool:
    """Check if Crawl4AI circuit is closed (available)."""
    global _crawl4ai_tripped_at
    with _crawl4ai_lock:
        if _crawl4ai_tripped_at is None:
            return True
        elapsed = time.time() - _crawl4ai_tripped_at
        if elapsed >= CRAWL4AI_COOLDOWN_SECONDS:
            # Cooldown expired — half-open, allow one attempt
            _crawl4ai_tripped_at = None
            return True
        return False


def _crawl4ai_record_success():
    """Reset circuit breaker on success."""
    global _crawl4ai_consecutive_fails, _crawl4ai_tripped_at
    with _crawl4ai_lock:
        _crawl4ai_consecutive_fails = 0
        _crawl4ai_tripped_at = None


def _crawl4ai_record_failure():
    """Record failure and trip breaker if threshold reached."""
    global _crawl4ai_consecutive_fails, _crawl4ai_tripped_at
    with _crawl4ai_lock:
        _crawl4ai_consecutive_fails += 1
        if _crawl4ai_consecutive_fails >= CRAWL4AI_FAIL_THRESHOLD:
            _crawl4ai_tripped_at = time.time()
            logger.warning("crawl4ai_circuit_tripped",
                          fails=_crawl4ai_consecutive_fails,
                          cooldown_s=CRAWL4AI_COOLDOWN_SECONDS)


# ---------------------------------------------------------------------------
# Firecrawl budget gate
# ---------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_MAX_DAILY = int(os.environ.get("FIRECRAWL_DAILY_BUDGET_CREDITS", "200"))

_firecrawl_lock = threading.Lock()
_firecrawl_daily_used = 0
_firecrawl_day: str | None = None


def _firecrawl_budget_ok() -> bool:
    """Check if we still have Firecrawl budget today."""
    global _firecrawl_daily_used, _firecrawl_day
    today = time.strftime("%Y-%m-%d")
    with _firecrawl_lock:
        if _firecrawl_day != today:
            _firecrawl_daily_used = 0
            _firecrawl_day = today
        return _firecrawl_daily_used < FIRECRAWL_MAX_DAILY


def _firecrawl_record_usage(credits: int = 1):
    global _firecrawl_daily_used
    with _firecrawl_lock:
        _firecrawl_daily_used += credits


# ---------------------------------------------------------------------------
# Layer 1: Crawl4AI
# ---------------------------------------------------------------------------
def _try_crawl4ai(url: str, schema: dict, prompt: str, timeout: int = 60) -> dict | None:
    """Try Crawl4AI extraction. Returns extracted dict or None."""
    if not _crawl4ai_is_available():
        return None

    try:
        from jobs.crawl4ai_client import crawl4ai_scrape, crawl4ai_health
        if not crawl4ai_health():
            _crawl4ai_record_failure()
            return None

        result = crawl4ai_scrape(url, schema=schema, prompt=prompt, timeout=timeout)
        if result:
            _crawl4ai_record_success()
            return result
        else:
            _crawl4ai_record_failure()
            return None
    except Exception as e:
        _crawl4ai_record_failure()
        logger.debug("crawl4ai_error", url=url[:80], error=str(e)[:100])
        return None


# ---------------------------------------------------------------------------
# Layer 2: Plain HTTP (caller provides extractor function)
# ---------------------------------------------------------------------------
def _try_http(url: str, html_extractor) -> dict | None:
    """Try plain HTTP + custom extractor function. Returns dict or None."""
    if html_extractor is None:
        return None
    try:
        return html_extractor(url)
    except Exception as e:
        logger.debug("http_extractor_error", url=url[:80], error=str(e)[:100])
        return None


# ---------------------------------------------------------------------------
# Layer 3: Firecrawl (paid, anti-bot)
# ---------------------------------------------------------------------------
def _try_firecrawl(url: str, schema: dict, prompt: str) -> dict | None:
    """Try Firecrawl API extraction. Returns dict or None."""
    if not FIRECRAWL_API_KEY:
        return None
    if not _firecrawl_budget_ok():
        logger.debug("firecrawl_budget_exhausted")
        return None

    try:
        payload = {
            "url": url,
            "formats": ["extract"],
            "extract": {
                "prompt": prompt,
                "schema": schema,
            },
        }
        resp = requests.post(
            FIRECRAWL_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.debug("firecrawl_error", url=url[:80], status=resp.status_code)
            return None

        body = resp.json()
        credits_used = body.get("metadata", {}).get("credits_used", 1)
        _firecrawl_record_usage(credits_used)

        extracted = body.get("data", {}).get("extract")
        if isinstance(extracted, dict) and extracted:
            return extracted
        return None
    except Exception as e:
        logger.debug("firecrawl_error", url=url[:80], error=str(e)[:100])
        return None


# ---------------------------------------------------------------------------
# Main chain
# ---------------------------------------------------------------------------
def scrape_with_chain(
    url: str,
    schema: dict,
    prompt: str,
    *,
    priority: int = 5,
    html_extractor=None,
    crawl4ai_timeout: int = 60,
) -> dict:
    """
    Try 3 layers in waterfall. Returns {"data": dict|None, "engine": str|None}.

    Args:
        url: Target URL.
        schema: JSON Schema for LLM extraction (layers 1 & 3).
        prompt: Natural language extraction instruction.
        priority: 1-3 = high (Firecrawl allowed), 4+ = low (skip Firecrawl).
        html_extractor: Callable(url) → dict|None for Layer 2.
        crawl4ai_timeout: Timeout for Crawl4AI.
    """
    # Layer 1: Crawl4AI
    data = _try_crawl4ai(url, schema, prompt, timeout=crawl4ai_timeout)
    if data:
        return {"data": data, "engine": "crawl4ai"}

    # Layer 2: Plain HTTP
    data = _try_http(url, html_extractor)
    if data:
        return {"data": data, "engine": "http"}

    # Layer 3: Firecrawl — DISABLED (no budget)
    # TODO: re-enable when Firecrawl budget is allocated for URL-type scrapers
    # if priority <= 3:
    #     data = _try_firecrawl(url, schema, prompt)
    #     if data:
    #         return {"data": data, "engine": "firecrawl"}

    return {"data": None, "engine": None}


def get_circuit_status() -> dict:
    """Return current circuit breaker + budget status (for monitoring)."""
    with _crawl4ai_lock:
        crawl4ai_status = "open" if _crawl4ai_tripped_at else "closed"
        crawl4ai_fails = _crawl4ai_consecutive_fails
        crawl4ai_cooldown_remaining = 0
        if _crawl4ai_tripped_at:
            crawl4ai_cooldown_remaining = max(0, CRAWL4AI_COOLDOWN_SECONDS - (time.time() - _crawl4ai_tripped_at))

    with _firecrawl_lock:
        fc_used = _firecrawl_daily_used

    return {
        "crawl4ai_circuit": crawl4ai_status,
        "crawl4ai_consecutive_fails": crawl4ai_fails,
        "crawl4ai_cooldown_remaining_s": round(crawl4ai_cooldown_remaining),
        "firecrawl_daily_used": fc_used,
        "firecrawl_daily_budget": FIRECRAWL_MAX_DAILY,
    }
