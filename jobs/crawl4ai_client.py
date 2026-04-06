"""
Crawl4AI client — drop-in replacement for Firecrawl API calls.

Runs against self-hosted Crawl4AI container (Docker on node-03).
Provides the same interface as Firecrawl: URL + JSON schema + prompt → structured JSON.

Usage:
    from jobs.crawl4ai_client import crawl4ai_scrape

    data = crawl4ai_scrape(
        url="https://example.com/shipping",
        schema=SHIPPING_SCHEMA,
        prompt="Extract shipping information...",
    )
    # Returns dict (extracted JSON) or None on failure — same as Firecrawl.

Env vars:
    CRAWL4AI_URL    — base URL (default: http://crawl4ai:11235)
    OPENAI_API_KEY  — for LLM extraction (required for schema-based extraction)
    CRAWL4AI_LLM_PROVIDER — LLM provider (default: openai/gpt-4o-mini)
"""
import json
import os
import sys
import time

import requests

sys.path.insert(0, "/app")
from jobs.langfuse_wrapper import traced_generation

CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://crawl4ai:11235").rstrip("/")
LLM_PROVIDER = os.environ.get("CRAWL4AI_LLM_PROVIDER", "openai/gpt-4o-mini")

MAX_RETRIES = 2
TIMEOUT_SECONDS = 120


def crawl4ai_scrape(
    url: str,
    schema: dict,
    prompt: str,
    *,
    timeout: int = TIMEOUT_SECONDS,
    wait_for: str | None = None,
) -> dict | None:
    """
    Scrape a URL with LLM-based structured extraction via Crawl4AI.

    Args:
        url: Target URL to scrape.
        schema: JSON Schema dict for extraction (same format as Firecrawl jsonOptions.schema).
        prompt: Natural language instruction for extraction.
        timeout: Request timeout in seconds.
        wait_for: Optional CSS selector to wait for before extraction.

    Returns:
        Extracted dict matching the schema, or None on failure.
    """
    crawler_params = {
        "cache_mode": "bypass",
        "word_count_threshold": 10,
        "excluded_tags": ["nav", "footer", "script", "style"],
        "extraction_strategy": {
            "type": "LLMExtractionStrategy",
            "params": {
                "llm_config": {
                    "type": "LLMConfig",
                    "params": {
                        "provider": LLM_PROVIDER,
                        "api_token": "env:OPENROUTER_API_KEY",
                    },
                },
                "schema": {
                    "type": "dict",
                    "value": schema,
                },
                "instruction": prompt,
            },
        },
    }

    if wait_for:
        crawler_params["wait_for"] = f"css:{wait_for}"

    payload = {
        "urls": [url],
        "browser_config": {
            "type": "BrowserConfig",
            "params": {"headless": True},
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": crawler_params,
        },
    }

    with traced_generation(
        name="crawl4ai-extract",
        model=LLM_PROVIDER,
        input_data={"url": url, "prompt": prompt, "schema_keys": list(schema.get("properties", {}).keys())},
        metadata={"crawl4ai_url": CRAWL4AI_URL},
        tags=["feed-processor", "crawl4ai"],
    ) as gen:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"{CRAWL4AI_URL}/crawl",
                    json=payload,
                    timeout=timeout + 10,
                )
            except requests.RequestException as e:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[crawl4ai] Network error for {url}: {e}", flush=True)
                gen.end(level="ERROR", status_message=f"Network error: {e}")
                return None

            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** (attempt + 2))
                    continue
                print(f"[crawl4ai] Rate limited (429) for {url}", flush=True)
                gen.end(level="ERROR", status_message="429 rate limit exhausted")
                return None

            if resp.status_code >= 500:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[crawl4ai] Server error ({resp.status_code}) for {url}", flush=True)
                gen.end(level="ERROR", status_message=f"Server error {resp.status_code}")
                return None

            if resp.status_code != 200:
                print(f"[crawl4ai] HTTP {resp.status_code} for {url}", flush=True)
                gen.end(level="ERROR", status_message=f"HTTP {resp.status_code}")
                return None

            try:
                body = resp.json()
            except ValueError:
                print(f"[crawl4ai] Invalid JSON response for {url}", flush=True)
                gen.end(level="ERROR", status_message="Invalid JSON response")
                return None

            results = body.get("results", [])
            if not results:
                gen.end(level="WARNING", status_message="No results")
                return None

            result = results[0]
            if not result.get("success"):
                gen.end(level="WARNING", status_message="Extraction not successful")
                return None

            extracted = result.get("extracted_content")
            if not extracted:
                gen.end(level="WARNING", status_message="No extracted content")
                return None

            # Crawl4AI returns extracted_content as JSON string
            if isinstance(extracted, str):
                try:
                    extracted = json.loads(extracted)
                except (json.JSONDecodeError, ValueError):
                    gen.end(level="ERROR", status_message="extracted_content JSON parse failed")
                    return None

            # LLMExtractionStrategy may return a list — unwrap first item
            if isinstance(extracted, list):
                if not extracted:
                    gen.end(level="WARNING", status_message="Empty extraction list")
                    return None
                extracted = extracted[0]

            gen.end(output=extracted)
            return extracted

        gen.end(level="ERROR", status_message="All retries exhausted")
        return None


def crawl4ai_scrape_html(
    url: str,
    *,
    timeout: int = 60,
) -> str | None:
    """
    Simple HTML scrape without LLM extraction.
    Returns cleaned HTML string or None.
    """
    payload = {
        "urls": [url],
        "browser_config": {
            "type": "BrowserConfig",
            "params": {"headless": True},
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "cache_mode": "bypass",
                "word_count_threshold": 10,
            },
        },
    }

    try:
        resp = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json=payload,
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results", [])
        if results and results[0].get("success"):
            return results[0].get("cleaned_html") or results[0].get("html")
    except Exception as e:
        print(f"[crawl4ai] HTML scrape error for {url}: {e}", flush=True)

    return None


def crawl4ai_health() -> bool:
    """Check if Crawl4AI container is healthy."""
    try:
        resp = requests.get(f"{CRAWL4AI_URL}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False
