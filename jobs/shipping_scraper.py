"""
Job: Shipping Scraper
Cíl: scrape shipping info pages ze shopů bez shipping_checked_at timestamp.
Extrahuje shipping zones + methods + cost + delivery days přes LLM AI extraction.

Engine priority:
  1. Crawl4AI (self-hosted, $0) — default
  2. Firecrawl (cloud API, paid) — fallback pokud Crawl4AI selže nebo není dostupný

Workflow:
  1. SELECT shops WHERE shipping_checked_at IS NULL AND deleted_at IS NULL
  2. Discover shipping page URL — zkusit známé patterns (/doprava, /shipping, /delivery...)
  3. Scrape via Crawl4AI (→ fallback Firecrawl) s JSON schema (zones + methods)
  4. Insert shipping_zones + shipping_methods (upsert by (shop_id, destination_country_id))
  5. UPDATE shops SET shipping_checked_at = NOW()

Cron: hodinově — zpracovává batch ~20 shopů per run.
Batch size zvýšen z 20 na 50 díky Crawl4AI ($0 cost).
"""
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.crawl4ai_client import crawl4ai_scrape, crawl4ai_health
from jobs.langfuse_wrapper import traced_generation


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

MAX_SHOPS_PER_RUN = 50          # raised from 20 — Crawl4AI is free
MAX_CONCURRENT_WORKERS = 30     # node-03 has 16 cores, 25GB RAM free — Crawl4AI handles concurrency
FIRECRAWL_TIMEOUT_SECONDS = 60
FIRECRAWL_MAX_RETRIES = 2

# Engine selection — Crawl4AI first, Firecrawl fallback
_USE_CRAWL4AI = None  # lazy init

# Keywords that indicate a shipping/delivery page — used for link discovery in HTML.
# Multilingual: matches href text OR URL path substring.
SHIPPING_KEYWORDS = [
    # CZ/SK
    "doprava", "doruceni", "doručení", "zásilk", "přeprava",
    # EN
    "shipping", "delivery", "postage", "dispatch",
    # DE
    "versand", "lieferung", "versandkosten",
    # FR
    "livraison", "expédition", "expedition",
    # IT
    "spedizione", "consegna",
    # ES
    "envío", "envio", "entrega",
    # NL
    "verzending", "bezorging",
    # PL
    "dostawa", "wysyłka", "wysylka",
    # Nordic
    "frakt", "leverans",
    # HU
    "szállítás", "szallitas",
    # PT
    "envio", "entrega",
    # RO
    "livrare",
    # BG
    "доставка",
    # HR/SI
    "dostava", "pošiljanje",
    # Generic
    "shipping", "delivery",
]

# Extraction schema for Firecrawl AI
SHIPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "zones": {
            "type": "array",
            "description": "List of destination countries this shop ships to, with shipping options.",
            "items": {
                "type": "object",
                "properties": {
                    "country_iso2": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code (CZ, DE, AT, SK, PL, HU, IT, FR, ES, NL, BE, GB, IE, SE, DK, FI, NO, PT, GR, CH, RO, BG, HR, SI, EE, LV, LT, LU, MT, CY). Uppercase.",
                    },
                    "country_name": {
                        "type": "string",
                        "description": "Full country name as written on the page.",
                    },
                    "free_shipping_threshold": {
                        "type": "number",
                        "description": "Minimum order value for free shipping. Omit if no free shipping.",
                    },
                    "free_shipping_currency": {
                        "type": "string",
                        "description": "Currency code for the threshold (EUR, CZK, PLN, etc.). 3-letter ISO.",
                    },
                    "delivery_days_min": {
                        "type": "integer",
                        "description": "Minimum delivery days (business days).",
                    },
                    "delivery_days_max": {
                        "type": "integer",
                        "description": "Maximum delivery days (business days).",
                    },
                    "methods": {
                        "type": "array",
                        "description": "Available shipping methods for this country.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "method_type": {
                                    "type": "string",
                                    "description": "One of: standard, express, economy, overnight, pickup_point, parcel_locker, store_pickup, same_day, scheduled, other",
                                },
                                "service_name": {
                                    "type": "string",
                                    "description": "Carrier or service name, e.g. 'DPD', 'Zásilkovna', 'PPL', 'DHL', 'GLS', 'Pickup Point', 'Home delivery'.",
                                },
                                "base_price": {
                                    "type": "number",
                                    "description": "Price for this method as number. 0 if free.",
                                },
                                "currency": {
                                    "type": "string",
                                    "description": "Currency code (EUR, CZK, PLN...). 3-letter ISO.",
                                },
                                "delivery_days_min": {"type": "integer"},
                                "delivery_days_max": {"type": "integer"},
                                "is_pickup_point": {"type": "boolean"},
                                "is_express": {"type": "boolean"},
                                "cod_available": {
                                    "type": "boolean",
                                    "description": "Cash on delivery available for this method.",
                                },
                            },
                        },
                    },
                },
                "required": ["country_iso2"],
            },
        },
    },
    "required": ["zones"],
}


# ----------------------------------------------------------------------------
# Engine selection
# ----------------------------------------------------------------------------
SHIPPING_PROMPT = (
    "Extract shipping information from this e-commerce page. "
    "For each destination country listed, capture: ISO2 code, free shipping threshold (if any), "
    "delivery days range, and list of shipping methods with prices. "
    "If a country is mentioned but no price given, still include it with ships_to=true. "
    "Use ISO 3166-1 alpha-2 codes (e.g. CZ, DE, AT). Skip countries that are explicitly excluded."
)


def _check_crawl4ai() -> bool:
    """Lazy check if Crawl4AI is available. Cached for the run."""
    global _USE_CRAWL4AI
    if _USE_CRAWL4AI is None:
        _USE_CRAWL4AI = crawl4ai_health()
        engine = "Crawl4AI" if _USE_CRAWL4AI else "Firecrawl (fallback)"
        print(f"[shipping] Engine: {engine}", flush=True)
    return _USE_CRAWL4AI


def _scrape_via_crawl4ai(url: str) -> dict | None:
    """Scrape shipping page via self-hosted Crawl4AI."""
    return crawl4ai_scrape(url=url, schema=SHIPPING_SCHEMA, prompt=SHIPPING_PROMPT)


def _scrape_via_firecrawl(url: str) -> dict | None:
    """Scrape shipping page via Firecrawl cloud API (paid fallback). Traced via Langfuse."""
    if not FIRECRAWL_API_KEY:
        print("[shipping] FATAL: FIRECRAWL_API_KEY missing", flush=True)
        return None

    payload = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": SHIPPING_SCHEMA,
            "prompt": SHIPPING_PROMPT,
        },
        "proxy": "stealth",
        "onlyMainContent": True,
        "waitFor": 2000,
        "timeout": FIRECRAWL_TIMEOUT_SECONDS * 1000,
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    with traced_generation(
        name="firecrawl-shipping",
        model="firecrawl/extract",
        input_data={"url": url, "prompt": SHIPPING_PROMPT},
        metadata={"job": "shipping_scraper", "engine": "firecrawl-fallback"},
        tags=["feed-processor", "firecrawl", "shipping"],
    ) as gen:
        for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    FIRECRAWL_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=FIRECRAWL_TIMEOUT_SECONDS + 10,
                )
            except requests.RequestException as e:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[shipping] Network error for {url}: {e}", flush=True)
                gen.end(level="ERROR", status_message=f"Network error: {e}")
                return None

            if resp.status_code == 429:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                    time.sleep(min(wait, 30))
                    continue
                gen.end(level="ERROR", status_message="429 rate limited, retries exhausted")
                return None

            if resp.status_code in (403, 404):
                gen.end(level="WARNING", status_message=f"HTTP {resp.status_code}")
                return None

            if resp.status_code >= 500:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                gen.end(level="ERROR", status_message=f"HTTP {resp.status_code} after retries")
                return None

            if resp.status_code != 200:
                gen.end(level="WARNING", status_message=f"HTTP {resp.status_code}")
                return None

            try:
                body = resp.json()
            except ValueError:
                gen.end(level="ERROR", status_message="Invalid JSON response")
                return None

            if not body.get("success"):
                gen.end(level="WARNING", status_message="Firecrawl success=false")
                return None

            data = body.get("data") or {}
            result = data.get("json")
            gen.end(output=result, usage={"total": 1})
            return result

        gen.end(level="ERROR", status_message="All retries exhausted")
        return None


def scrape_shipping(url: str) -> dict | None:
    """Scrape shipping page. Crawl4AI first, Firecrawl fallback."""
    if _check_crawl4ai():
        result = _scrape_via_crawl4ai(url)
        if result is not None:
            return result
        # Crawl4AI failed on this URL — try Firecrawl as fallback
        print(f"[shipping] Crawl4AI failed for {url}, trying Firecrawl...", flush=True)

    return _scrape_via_firecrawl(url)


# ----------------------------------------------------------------------------
# URL discovery — smart link search (replaces brute-force path guessing)
# ----------------------------------------------------------------------------
def _find_shipping_links_in_html(html: str, base_url: str) -> list[str]:
    """
    Parse HTML and find links that look like shipping/delivery pages.
    Matches both link text and href path against SHIPPING_KEYWORDS.
    Returns deduplicated list of absolute URLs, best matches first.
    """
    from urllib.parse import urljoin, urlparse

    base_domain = urlparse(base_url).netloc

    # Find all <a> tags with href
    link_pattern = re.compile(
        r'<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    scored: list[tuple[int, str]] = []
    seen = set()

    for match in link_pattern.finditer(html):
        href_raw = match.group(1).strip()
        link_text = re.sub(r"<[^>]+>", "", match.group(2)).strip().lower()

        # Build absolute URL
        abs_url = urljoin(base_url, href_raw)
        parsed = urlparse(abs_url)

        # Skip external links, anchors, mailto, tel
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc and parsed.netloc != base_domain:
            continue

        # Skip media, product, category pages
        path_lower = parsed.path.lower()
        if any(ext in path_lower for ext in (".jpg", ".png", ".pdf", ".css", ".js")):
            continue

        # Dedup
        if abs_url in seen:
            continue
        seen.add(abs_url)

        # Score: how many shipping keywords match in link text + path
        score = 0
        for kw in SHIPPING_KEYWORDS:
            kw_lower = kw.lower()
            if kw_lower in link_text:
                score += 3  # text match is strongest signal
            if kw_lower in path_lower:
                score += 2  # path match

        # Negative signals — avoid these pages
        for neg in ("privacy", "datenschutz", "cookie", "login", "account", "cart", "checkout", "produkt", "product", "kategori", "category"):
            if neg in path_lower or neg in link_text:
                score -= 5

        if score > 0:
            scored.append((score, abs_url))

    # Sort by score descending, take top 5
    scored.sort(key=lambda x: -x[0])
    return [url for _, url in scored[:5]]


def find_shipping_page(shop_url: str) -> tuple[str | None, dict | None]:
    """
    Smart shipping page discovery:
      1. Fetch homepage HTML via Crawl4AI (no LLM, $0)
      2. Find shipping links by keyword matching in <a> tags
      3. Try top candidates with LLM extraction (max 3 attempts)

    Returns (url_used, extracted_json) or (None, None).
    """
    from jobs.crawl4ai_client import crawl4ai_scrape_html

    base = shop_url.rstrip("/")

    # Step 1: Fetch homepage HTML
    html = crawl4ai_scrape_html(base, timeout=30)

    # Fallback: plain HTTP if Crawl4AI HTML fails
    if not html:
        try:
            resp = requests.get(
                base,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
                timeout=15,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                html = resp.text
        except requests.RequestException:
            pass

    if not html:
        print(f"  [discovery] Could not fetch homepage for {base}", flush=True)
        return None, None

    # Step 2: Find shipping links in HTML
    candidates = _find_shipping_links_in_html(html, base)

    if not candidates:
        print(f"  [discovery] No shipping links found on {base}", flush=True)
        return None, None

    print(f"  [discovery] Found {len(candidates)} candidates: {[c.split('/')[-1] for c in candidates]}", flush=True)

    # Step 3: Try top candidates with LLM extraction (max 3)
    for url in candidates[:3]:
        data = scrape_shipping(url)
        if data and isinstance(data.get("zones"), list) and len(data["zones"]) > 0:
            return url, data

    return None, None


# ----------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------
def load_country_map() -> dict[str, int]:
    """Return {iso2: country_id} for all countries."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT iso2, id FROM countries")
            return {row[0].strip().upper(): row[1] for row in cur.fetchall()}
    finally:
        put_conn(conn)


def _strip_accents(text: str) -> str:
    """NFKD decompose + drop combining marks. Lowercase output."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def load_carrier_map() -> dict[str, int]:
    """Return {accent-stripped lowercase carrier name/code: carrier_id} for fuzzy matching."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, name FROM shipping_carriers")
            out: dict[str, int] = {}
            for cid, code, name in cur.fetchall():
                out[_strip_accents(code)] = cid
                out[_strip_accents(name)] = cid
            return out
    finally:
        put_conn(conn)


def resolve_carrier_id(service_name: str | None, carrier_map: dict[str, int]) -> int | None:
    """Fuzzy match service_name to a known carrier_id. Returns None if no match."""
    if not service_name:
        return None
    n = _strip_accents(service_name.strip())
    if not n:
        return None
    # Exact match first
    if n in carrier_map:
        return carrier_map[n]
    # Substring match: "DPD Express" → dpd, "Zasilkovna CZ" → zasilkovna
    for key, cid in carrier_map.items():
        if key and len(key) >= 3 and key in n:
            return cid
    return None


def get_shops_to_scrape(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """
    Load shops needing shipping data.
    Priority: never-checked first, then stale (>90 days).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url, country_id, discovered_shipping_url
                FROM shops
                WHERE deleted_at IS NULL
                  AND url IS NOT NULL
                  AND (
                      shipping_checked_at IS NULL
                      OR shipping_checked_at < NOW() - INTERVAL '90 days'
                  )
                ORDER BY
                    -- prioritize shops with discovered URL (instant hit, no homepage scan)
                    discovered_shipping_url IS NOT NULL DESC,
                    shipping_checked_at IS NULL DESC,
                    (offers_checked_at IS NOT NULL) DESC,
                    shipping_checked_at ASC NULLS FIRST
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


def normalize_method_type(raw: str | None) -> str:
    """Map LLM output to our enum values."""
    if not raw:
        return "standard"
    r = raw.strip().lower()
    mapping = {
        "standard": "standard",
        "express": "express",
        "economy": "economy",
        "overnight": "overnight",
        "pickup_point": "pickup_point",
        "pickup point": "pickup_point",
        "pickup": "pickup_point",
        "parcel_locker": "parcel_locker",
        "parcel locker": "parcel_locker",
        "locker": "parcel_locker",
        "store_pickup": "store_pickup",
        "store pickup": "store_pickup",
        "same_day": "same_day",
        "same day": "same_day",
        "scheduled": "scheduled",
        "freight": "freight",
    }
    return mapping.get(r, "other")


def normalize_currency(raw: str | None) -> str:
    """Normalize currency to 3-letter uppercase ISO. Fallback EUR."""
    if not raw:
        return "EUR"
    r = raw.strip().upper()
    # Handle symbols
    if r in ("€", "EURO", "EURS"):
        return "EUR"
    if r in ("KČ", "KC"):
        return "CZK"
    if r == "£":
        return "GBP"
    if len(r) == 3 and r.isalpha():
        return r
    return "EUR"


def upsert_zone_with_methods(
    *,
    shop_id: int,
    country_id: int,
    source_url: str,
    zone_data: dict,
    carrier_map: dict[str, int],
) -> int:
    """
    UPSERT one shipping_zone and all its shipping_methods.
    Returns number of methods inserted.
    """
    threshold = zone_data.get("free_shipping_threshold")
    try:
        threshold = float(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        threshold = None

    free_currency = (
        normalize_currency(zone_data.get("free_shipping_currency"))
        if threshold is not None
        else None
    )

    days_min = zone_data.get("delivery_days_min")
    days_max = zone_data.get("delivery_days_max")
    try:
        days_min = int(days_min) if days_min is not None else None
    except (TypeError, ValueError):
        days_min = None
    try:
        days_max = int(days_max) if days_max is not None else None
    except (TypeError, ValueError):
        days_max = None
    if days_min and days_max and days_min > days_max:
        days_min, days_max = days_max, days_min

    conn = get_conn()
    methods_inserted = 0
    try:
        with conn.cursor() as cur:
            # UPSERT shipping_zone
            cur.execute(
                """
                INSERT INTO shipping_zones (
                    shop_id, destination_country_id, ships_to,
                    free_shipping_threshold, free_shipping_currency,
                    delivery_days_min, delivery_days_max,
                    data_source, data_confidence, source_url, last_checked_at
                ) VALUES (%s, %s, TRUE, %s, %s, %s, %s, 'scraped', 0.80, %s, NOW())
                ON CONFLICT (shop_id, destination_country_id) DO UPDATE SET
                    ships_to = TRUE,
                    free_shipping_threshold = EXCLUDED.free_shipping_threshold,
                    free_shipping_currency = EXCLUDED.free_shipping_currency,
                    delivery_days_min = EXCLUDED.delivery_days_min,
                    delivery_days_max = EXCLUDED.delivery_days_max,
                    data_source = 'scraped',
                    data_confidence = 0.80,
                    source_url = EXCLUDED.source_url,
                    last_checked_at = NOW(),
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    shop_id, country_id,
                    threshold, free_currency,
                    days_min, days_max, source_url,
                ),
            )
            zone_id = cur.fetchone()[0]

            # Insert methods
            methods = zone_data.get("methods") or []
            if not isinstance(methods, list):
                methods = []

            for m in methods:
                if not isinstance(m, dict):
                    continue

                method_type = normalize_method_type(m.get("method_type"))
                service_name = (m.get("service_name") or "").strip() or None
                carrier_id = resolve_carrier_id(service_name, carrier_map)

                base_price = m.get("base_price")
                try:
                    base_price = float(base_price) if base_price is not None else None
                except (TypeError, ValueError):
                    base_price = None

                currency = normalize_currency(m.get("currency"))
                is_free = base_price == 0 if base_price is not None else False

                m_days_min = m.get("delivery_days_min")
                m_days_max = m.get("delivery_days_max")
                try:
                    m_days_min = int(m_days_min) if m_days_min is not None else None
                except (TypeError, ValueError):
                    m_days_min = None
                try:
                    m_days_max = int(m_days_max) if m_days_max is not None else None
                except (TypeError, ValueError):
                    m_days_max = None

                try:
                    cur.execute(
                        """
                        INSERT INTO shipping_methods (
                            shipping_zone_id, carrier_id, method_type, service_name,
                            base_price, currency, is_free,
                            delivery_days_min, delivery_days_max,
                            is_pickup_point, is_express, cod_available,
                            data_source, data_confidence, source_url, last_checked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'scraped', 0.80, %s, NOW())
                        ON CONFLICT (shipping_zone_id, method_type, service_name) DO UPDATE SET
                            carrier_id = COALESCE(EXCLUDED.carrier_id, shipping_methods.carrier_id),
                            base_price = EXCLUDED.base_price,
                            currency = EXCLUDED.currency,
                            is_free = EXCLUDED.is_free,
                            delivery_days_min = EXCLUDED.delivery_days_min,
                            delivery_days_max = EXCLUDED.delivery_days_max,
                            last_checked_at = NOW(),
                            updated_at = NOW()
                        """,
                        (
                            zone_id, carrier_id, method_type, service_name,
                            base_price, currency, is_free,
                            m_days_min, m_days_max,
                            bool(m.get("is_pickup_point", method_type in ("pickup_point", "parcel_locker", "store_pickup"))),
                            bool(m.get("is_express", method_type in ("express", "overnight", "same_day"))),
                            m.get("cod_available"),
                            source_url,
                        ),
                    )
                    methods_inserted += 1
                except Exception as e:
                    print(f"  [method insert error] {e}", flush=True)

            conn.commit()
    finally:
        put_conn(conn)

    return methods_inserted


def mark_shop_checked(shop_id: int) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shops SET shipping_checked_at = NOW() WHERE id = %s AND deleted_at IS NULL",
                (shop_id,),
            )
            conn.commit()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Per-shop worker
# ----------------------------------------------------------------------------
def process_shop(shop_row: tuple, country_map: dict[str, int], carrier_map: dict[str, int]) -> tuple[int, int]:
    """
    Process a single shop. Returns (zones_created, methods_created).
    Always marks shop as checked (even on failure) to avoid infinite retries.
    """
    shop_id, name, url, _country_id, discovered_url = shop_row

    # Fast path: use pre-discovered URL from shop_discovery.py
    source_url, data = None, None
    if discovered_url:
        print(f"[{name}] Using discovered URL: {discovered_url}", flush=True)
        data = scrape_shipping(discovered_url)
        if data and isinstance(data.get("zones"), list) and len(data["zones"]) > 0:
            source_url = discovered_url
        else:
            data = None

    # Slow path: scan homepage for shipping links
    if not data:
        print(f"[{name}] Discovering shipping page...", flush=True)
        source_url, data = find_shipping_page(url)
    if not data:
        print(f"[{name}] No shipping data found — marking as checked", flush=True)
        mark_shop_checked(shop_id)
        return 0, 0

    zones_created = 0
    methods_created = 0
    zones = data.get("zones") or []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        iso2 = (zone.get("country_iso2") or "").strip().upper()
        if not iso2 or len(iso2) != 2:
            continue
        country_id = country_map.get(iso2)
        if not country_id:
            continue

        try:
            m_count = upsert_zone_with_methods(
                shop_id=shop_id,
                country_id=country_id,
                source_url=source_url or url,
                zone_data=zone,
                carrier_map=carrier_map,
            )
            zones_created += 1
            methods_created += m_count
        except Exception as e:
            print(f"[{name}] Zone insert error for {iso2}: {e}", flush=True)

    mark_shop_checked(shop_id)
    print(f"[{name}] Saved {zones_created} zones, {methods_created} methods (source: {source_url})", flush=True)
    return zones_created, methods_created


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(f"=== Shipping Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    if not FIRECRAWL_API_KEY and not crawl4ai_health():
        print("[shipping] ERROR: Neither Crawl4AI nor FIRECRAWL_API_KEY available — aborting", flush=True)
        sys.exit(1)

    country_map = load_country_map()
    carrier_map = load_carrier_map()
    shops = get_shops_to_scrape()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run, {MAX_CONCURRENT_WORKERS} workers)", flush=True)

    if not shops:
        print("No shops to scrape. Done.", flush=True)
        return

    total_zones = 0
    total_methods = 0

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        futures = {executor.submit(process_shop, s, country_map, carrier_map): s for s in shops}
        for future in as_completed(futures):
            shop = futures[future]
            try:
                z, m = future.result()
                total_zones += z
                total_methods += m
            except Exception as e:
                print(f"[{shop[1]}] Worker error: {e}", flush=True)

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {total_zones} zones, {total_methods} methods saved | "
        f"{len(shops)} shops processed | {elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
