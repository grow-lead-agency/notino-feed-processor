"""
Job: Voucher & Banner Scraper
Cíl: scrape aktivní slevové kódy a promo bannery, plnit shop_voucher_codes + banner_snapshots.

Workflow:
  1. SELECT shops WITH url WHERE voucher_checked_at IS NULL (or stale 24h+)
  2. Homepage promo bannery — Firecrawl AI extraction promo codes z hlavní stránky
  3. Voucher agregátor sites — kupon.cz, slevin.cz, vouchercodes.co.uk, gutscheinsammler.de...
  4. UPSERT shop_voucher_codes (dedup by shop_id + code)
  5. INSERT banner_snapshots
  6. Mark expired vouchers (valid_to < now())

Sources:
  1. Homepage promo banners → Firecrawl AI extraction
  2. Voucher aggregator sites → Firecrawl AI extraction
  3. Affiliate feed voucher data → placeholder (future)

Cron: denně — batch 30 shopů per run.
Standalone: python jobs/voucher_scraper.py
"""
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

MAX_SHOPS_PER_RUN = 30
FIRECRAWL_TIMEOUT_SECONDS = 60
FIRECRAWL_MAX_RETRIES = 2
REQUEST_DELAY_S = 1.0  # max 1 req/s rate limit

# Voucher aggregator sites per language/region
# {domain} placeholder = shop domain without www/TLD (e.g. "notino" from "www.notino.cz")
VOUCHER_SITES = {
    "cs": [
        "https://www.kupon.cz/obchod/{domain}",
        "https://www.slevin.cz/{domain}",
        "https://www.promokody.cz/{domain}",
    ],
    "sk": [
        "https://www.kupony.sk/obchod/{domain}",
    ],
    "de": [
        "https://www.gutscheinsammler.de/gutscheine/{domain}",
    ],
    "en": [
        "https://www.vouchercodes.co.uk/{domain}",
    ],
    "it": [
        "https://www.codici-sconto.it/{domain}",
    ],
    "fr": [
        "https://www.ma-reduc.com/{domain}",
    ],
    "pl": [
        "https://www.kuponguru.pl/sklep/{domain}",
    ],
}

# Map country ISO2 → language key for voucher site selection
COUNTRY_TO_LANG = {
    "CZ": "cs", "SK": "sk", "DE": "de", "AT": "de", "CH": "de",
    "GB": "en", "IE": "en", "US": "en",
    "IT": "it", "FR": "fr", "BE": "fr",
    "PL": "pl",
}


# ----------------------------------------------------------------------------
# Firecrawl extraction schemas
# ----------------------------------------------------------------------------
HOMEPAGE_VOUCHER_SCHEMA = {
    "type": "object",
    "properties": {
        "promo_codes": {
            "type": "array",
            "description": "Promotional or discount codes found on the page.",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The voucher/promo code text (e.g. 'SLEVA20', 'WELCOME10', 'FREE2025').",
                    },
                    "discount_type": {
                        "type": "string",
                        "description": "One of: percentage, fixed_amount, free_shipping, gift, other",
                    },
                    "discount_value": {
                        "type": "number",
                        "description": "Numeric discount value. For percentage: 20 means 20%. For fixed: amount in local currency.",
                    },
                    "discount_currency": {
                        "type": "string",
                        "description": "Currency code if fixed amount (EUR, CZK, PLN, GBP...). Omit for percentage.",
                    },
                    "min_order_value": {
                        "type": "number",
                        "description": "Minimum order value to apply the code. Omit if none.",
                    },
                    "conditions": {
                        "type": "string",
                        "description": "Any conditions, restrictions, or product category limitations.",
                    },
                    "valid_to": {
                        "type": "string",
                        "description": "Expiry date if mentioned (ISO 8601 format YYYY-MM-DD).",
                    },
                },
                "required": ["code"],
            },
        },
        "banners": {
            "type": "array",
            "description": "Promotional banners visible on the page.",
            "items": {
                "type": "object",
                "properties": {
                    "banner_copy": {
                        "type": "string",
                        "description": "Main text/headline of the banner.",
                    },
                    "banner_cta": {
                        "type": "string",
                        "description": "Call-to-action button text (e.g. 'Nakoupit', 'Shop Now').",
                    },
                    "banner_link": {
                        "type": "string",
                        "description": "URL the banner links to.",
                    },
                    "banner_position": {
                        "type": "string",
                        "description": "Position: hero, top_bar, popup, sidebar, footer, inline.",
                    },
                    "promo_code": {
                        "type": "string",
                        "description": "Promo code shown on/in the banner, if any.",
                    },
                },
            },
        },
    },
    "required": ["promo_codes", "banners"],
}

HOMEPAGE_VOUCHER_PROMPT = (
    "Extract any promotional offers, discount codes, or coupon codes visible on this e-shop page. "
    "Look for: popup codes, top-bar promotions, hero banner codes, newsletter signup discounts, "
    "cart-level codes, seasonal/holiday offers. "
    "For each code extract: the code text, discount type (percentage, fixed_amount, free_shipping), "
    "discount value, minimum order if any, expiry date if mentioned, and conditions. "
    "Also capture all promotional banners with their text content, CTA button text, and position on page."
)

VOUCHER_SITE_SCHEMA = {
    "type": "object",
    "properties": {
        "vouchers": {
            "type": "array",
            "description": "All voucher/coupon codes listed for the shop on this aggregator page.",
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The voucher/coupon code text. Omit if the offer is a deal without a code.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Description of what the voucher offers.",
                    },
                    "discount_type": {
                        "type": "string",
                        "description": "One of: percentage, fixed_amount, free_shipping, gift, other",
                    },
                    "discount_value": {
                        "type": "number",
                        "description": "Numeric discount value.",
                    },
                    "discount_currency": {
                        "type": "string",
                        "description": "Currency code if fixed amount.",
                    },
                    "min_order_value": {
                        "type": "number",
                        "description": "Minimum order value.",
                    },
                    "valid_to": {
                        "type": "string",
                        "description": "Expiry date (YYYY-MM-DD) if shown.",
                    },
                    "conditions": {
                        "type": "string",
                        "description": "Any conditions or restrictions.",
                    },
                    "is_verified": {
                        "type": "boolean",
                        "description": "Whether the site marks this code as verified/tested.",
                    },
                },
            },
        },
    },
    "required": ["vouchers"],
}

VOUCHER_SITE_PROMPT = (
    "Extract all voucher/coupon codes for the specified shop from this page. "
    "For each code extract: code text, description of the offer, discount type "
    "(percentage, fixed_amount, free_shipping, gift), discount value, currency if fixed, "
    "minimum order, expiry date, conditions, and whether it is marked as verified/tested. "
    "Skip generic deals that don't have a specific code — only extract entries WITH a code."
)


# ----------------------------------------------------------------------------
# Firecrawl helpers
# ----------------------------------------------------------------------------
def _firecrawl_extract(url: str, schema: dict, prompt: str) -> dict | None:
    """Call Firecrawl /v1/scrape with AI extraction. Returns parsed JSON or None."""
    if not FIRECRAWL_API_KEY:
        print("[voucher] FATAL: FIRECRAWL_API_KEY missing", flush=True)
        return None

    payload = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": schema,
            "prompt": prompt,
        },
        "proxy": "stealth",
        "onlyMainContent": True,
        "waitFor": 3000,
        "timeout": FIRECRAWL_TIMEOUT_SECONDS * 1000,
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

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
            print(f"[voucher] Network error for {url}: {e}", flush=True)
            return None

        if resp.status_code == 429:
            if attempt < FIRECRAWL_MAX_RETRIES:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                time.sleep(min(wait, 30))
                continue
            print(f"[voucher] Rate limited on {url}", flush=True)
            return None

        if resp.status_code in (403, 404):
            # Blocked or page not found — not an error, just no data
            return None

        if resp.status_code >= 500:
            if attempt < FIRECRAWL_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None

        if resp.status_code != 200:
            print(f"[voucher] HTTP {resp.status_code} for {url}", flush=True)
            return None

        try:
            body = resp.json()
        except ValueError:
            return None

        if not body.get("success"):
            return None

        data = body.get("data") or {}
        return data.get("json")

    return None


def _rate_limit():
    """Enforce rate limiting between requests."""
    time.sleep(REQUEST_DELAY_S)


# ----------------------------------------------------------------------------
# Domain extraction
# ----------------------------------------------------------------------------
def _extract_brand_domain(shop_url: str) -> str:
    """Extract brand domain slug from URL (e.g. 'https://www.notino.cz' → 'notino')."""
    parsed = urlparse(shop_url)
    hostname = parsed.hostname or ""
    # Remove www. prefix
    hostname = re.sub(r"^www\.", "", hostname)
    # Take first part (before TLD)
    parts = hostname.split(".")
    if len(parts) >= 2:
        return parts[0]
    return hostname


def _get_country_lang(country_id: int) -> str | None:
    """Resolve country_id → ISO2 → language key for voucher site selection."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT iso2 FROM countries WHERE id = %s", (country_id,))
            row = cur.fetchone()
            if row:
                iso2 = row[0].strip().upper()
                return COUNTRY_TO_LANG.get(iso2)
    finally:
        put_conn(conn)
    return None


# ----------------------------------------------------------------------------
# Source 1: Homepage promo banners
# ----------------------------------------------------------------------------
def scrape_homepage_promos(_shop_id: int, shop_url: str) -> tuple[list[dict], list[dict]]:
    """
    Firecrawl homepage → extract promo codes + banner data.
    Returns (vouchers_list, banners_list).
    """
    print(f"  [homepage] Scraping {shop_url}", flush=True)
    data = _firecrawl_extract(shop_url, HOMEPAGE_VOUCHER_SCHEMA, HOMEPAGE_VOUCHER_PROMPT)
    if not data:
        print(f"  [homepage] No data extracted", flush=True)
        return [], []

    vouchers = []
    promo_codes = data.get("promo_codes") or []
    if not isinstance(promo_codes, list):
        promo_codes = []

    for pc in promo_codes:
        if not isinstance(pc, dict):
            continue
        code = (pc.get("code") or "").strip().upper()
        if not code or len(code) < 2 or len(code) > 50:
            continue
        # Skip common false positives
        if code in ("N/A", "NONE", "NULL", "NA", "-"):
            continue

        vouchers.append({
            "code": code,
            "discount_type": _normalize_discount_type(pc.get("discount_type")),
            "discount_value": _safe_decimal(pc.get("discount_value")),
            "discount_currency": _normalize_currency(pc.get("discount_currency")),
            "min_order_value": _safe_decimal(pc.get("min_order_value")),
            "conditions": (pc.get("conditions") or "").strip() or None,
            "valid_to": _parse_date(pc.get("valid_to")),
            "source": "homepage",
            "source_url": shop_url,
            "verification_status": "unverified",
        })

    banners = []
    banner_data = data.get("banners") or []
    if not isinstance(banner_data, list):
        banner_data = []

    for b in banner_data:
        if not isinstance(b, dict):
            continue
        copy_text = (b.get("banner_copy") or "").strip()
        if not copy_text:
            continue
        banners.append({
            "page_url": shop_url,
            "page_type": "homepage",
            "banner_copy": copy_text[:2000],
            "banner_cta": (b.get("banner_cta") or "").strip()[:500] or None,
            "banner_link": (b.get("banner_link") or "").strip()[:2000] or None,
            "banner_position": _normalize_banner_position(b.get("banner_position")),
        })

        # If banner contains a promo code, add it to vouchers too
        promo_in_banner = (b.get("promo_code") or "").strip().upper()
        if promo_in_banner and len(promo_in_banner) >= 2 and promo_in_banner not in ("N/A", "NONE"):
            # Check not already captured
            existing_codes = {v["code"] for v in vouchers}
            if promo_in_banner not in existing_codes:
                vouchers.append({
                    "code": promo_in_banner,
                    "discount_type": None,
                    "discount_value": None,
                    "discount_currency": None,
                    "min_order_value": None,
                    "conditions": f"From banner: {copy_text[:200]}",
                    "valid_to": None,
                    "source": "homepage_banner",
                    "source_url": shop_url,
                    "verification_status": "unverified",
                })

    print(f"  [homepage] Found {len(vouchers)} codes, {len(banners)} banners", flush=True)
    return vouchers, banners


# ----------------------------------------------------------------------------
# Source 2: Voucher aggregator sites
# ----------------------------------------------------------------------------
def scrape_voucher_sites(_shop_id: int, domain: str, country_id: int | None) -> list[dict]:
    """
    Search voucher aggregator sites for shop's codes.
    Returns list of voucher dicts.
    """
    brand_slug = _extract_brand_domain(domain)
    if not brand_slug:
        return []

    # Determine which language sites to check
    langs_to_check = set()
    if country_id:
        lang = _get_country_lang(country_id)
        if lang:
            langs_to_check.add(lang)
    # Always check CZ (our primary market)
    langs_to_check.add("cs")

    all_vouchers = []

    for lang in langs_to_check:
        site_urls = VOUCHER_SITES.get(lang, [])
        for url_template in site_urls:
            url = url_template.format(domain=brand_slug)
            print(f"  [aggregator] Trying {url}", flush=True)

            # Quick HEAD check to avoid wasting Firecrawl credits on 404s
            try:
                head = requests.head(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
                    timeout=10,
                    allow_redirects=True,
                )
                if head.status_code >= 400:
                    print(f"  [aggregator] {url} → {head.status_code}, skipping", flush=True)
                    continue
            except requests.RequestException:
                continue

            _rate_limit()

            data = _firecrawl_extract(url, VOUCHER_SITE_SCHEMA, VOUCHER_SITE_PROMPT)
            if not data:
                continue

            vouchers_raw = data.get("vouchers") or []
            if not isinstance(vouchers_raw, list):
                continue

            for v in vouchers_raw:
                if not isinstance(v, dict):
                    continue
                code = (v.get("code") or "").strip().upper()
                if not code or len(code) < 2 or len(code) > 50:
                    continue
                if code in ("N/A", "NONE", "NULL", "NA", "-"):
                    continue

                is_verified = v.get("is_verified")
                verification = "verified" if is_verified else "unverified"

                all_vouchers.append({
                    "code": code,
                    "discount_type": _normalize_discount_type(v.get("discount_type")),
                    "discount_value": _safe_decimal(v.get("discount_value")),
                    "discount_currency": _normalize_currency(v.get("discount_currency")),
                    "min_order_value": _safe_decimal(v.get("min_order_value")),
                    "conditions": _build_conditions(v.get("description"), v.get("conditions")),
                    "valid_to": _parse_date(v.get("valid_to")),
                    "source": f"aggregator_{lang}",
                    "source_url": url,
                    "verification_status": verification,
                })

    # Dedup: keep first seen per code (homepage takes priority over aggregator)
    seen_codes = set()
    deduped = []
    for v in all_vouchers:
        if v["code"] not in seen_codes:
            seen_codes.add(v["code"])
            deduped.append(v)

    print(f"  [aggregator] Found {len(deduped)} unique codes from {len(langs_to_check)} language(s)", flush=True)
    return deduped


# ----------------------------------------------------------------------------
# DB operations
# ----------------------------------------------------------------------------
def upsert_vouchers(shop_id: int, country_id: int | None, vouchers: list[dict]) -> int:
    """
    Batch upsert into shop_voucher_codes.
    ON CONFLICT by (shop_id, code) → update.
    Returns number of rows affected.
    """
    if not vouchers:
        return 0

    conn = get_conn()
    count = 0
    try:
        with conn.cursor() as cur:
            for v in vouchers:
                try:
                    cur.execute(
                        """
                        INSERT INTO shop_voucher_codes (
                            shop_id, country_id, code,
                            discount_type, discount_value, discount_currency,
                            min_order_value, conditions,
                            valid_to, is_active,
                            source, source_url,
                            verification_status, last_verified_at, detected_at
                        ) VALUES (
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s, NOW()
                        )
                        ON CONFLICT (shop_id, code) DO UPDATE SET
                            discount_type = COALESCE(EXCLUDED.discount_type, shop_voucher_codes.discount_type),
                            discount_value = COALESCE(EXCLUDED.discount_value, shop_voucher_codes.discount_value),
                            discount_currency = COALESCE(EXCLUDED.discount_currency, shop_voucher_codes.discount_currency),
                            min_order_value = COALESCE(EXCLUDED.min_order_value, shop_voucher_codes.min_order_value),
                            conditions = COALESCE(EXCLUDED.conditions, shop_voucher_codes.conditions),
                            valid_to = COALESCE(EXCLUDED.valid_to, shop_voucher_codes.valid_to),
                            is_active = EXCLUDED.is_active,
                            source = EXCLUDED.source,
                            source_url = EXCLUDED.source_url,
                            verification_status = CASE
                                WHEN EXCLUDED.verification_status = 'verified' THEN 'verified'
                                ELSE shop_voucher_codes.verification_status
                            END,
                            last_verified_at = CASE
                                WHEN EXCLUDED.verification_status = 'verified' THEN NOW()
                                ELSE shop_voucher_codes.last_verified_at
                            END,
                            updated_at = NOW()
                        """,
                        (
                            shop_id, country_id, v["code"],
                            v.get("discount_type"), v.get("discount_value"), v.get("discount_currency"),
                            v.get("min_order_value"), v.get("conditions"),
                            v.get("valid_to"),
                            _is_voucher_active(v.get("valid_to")),
                            v.get("source"), v.get("source_url"),
                            v.get("verification_status", "unverified"),
                            datetime.now(timezone.utc) if v.get("verification_status") == "verified" else None,
                        ),
                    )
                    count += 1
                except Exception as e:
                    print(f"  [upsert] Error for code '{v.get('code')}': {e}", flush=True)

        conn.commit()
    finally:
        put_conn(conn)

    return count


def save_banner_snapshots(shop_id: int, country_id: int | None, banners: list[dict]) -> int:
    """
    Insert into banner_snapshots.
    Banners are point-in-time snapshots — always INSERT, no upsert.
    Returns number of rows inserted.
    """
    if not banners:
        return 0

    conn = get_conn()
    count = 0
    try:
        with conn.cursor() as cur:
            for b in banners:
                try:
                    cur.execute(
                        """
                        INSERT INTO banner_snapshots (
                            shop_id, country_id,
                            page_url, page_type,
                            banner_copy, banner_cta, banner_link,
                            banner_position, captured_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            shop_id, country_id,
                            b.get("page_url"), b.get("page_type"),
                            b.get("banner_copy"), b.get("banner_cta"),
                            b.get("banner_link"), b.get("banner_position"),
                        ),
                    )
                    count += 1
                except Exception as e:
                    print(f"  [banner] Insert error: {e}", flush=True)

        conn.commit()
    finally:
        put_conn(conn)

    return count


def mark_expired_vouchers() -> int:
    """Mark vouchers with valid_to < now() as inactive. Returns count updated."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shop_voucher_codes
                SET is_active = FALSE, updated_at = NOW()
                WHERE is_active = TRUE
                  AND valid_to IS NOT NULL
                  AND valid_to < NOW()
                """
            )
            count = cur.rowcount
            conn.commit()
            return count
    finally:
        put_conn(conn)


def mark_shop_voucher_checked(shop_id: int) -> None:
    """
    Record that voucher scan was done for this shop.
    Uses profile_checked_at as interim column (covers vouchers among other profile data).
    TODO: Add dedicated voucher_checked_at column to shops table (migration needed).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shops SET profile_checked_at = NOW() WHERE id = %s AND deleted_at IS NULL",
                (shop_id,),
            )
            conn.commit()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Shop selection
# ----------------------------------------------------------------------------
def get_shops_to_scrape(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """
    Load shops needing voucher scan.
    Priority: shops never checked first, then stale (>24h).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url, country_id
                FROM shops
                WHERE deleted_at IS NULL
                  AND url IS NOT NULL
                  AND (
                    profile_checked_at IS NULL
                    OR profile_checked_at < NOW() - INTERVAL '24 hours'
                  )
                ORDER BY
                    (profile_checked_at IS NULL) DESC,
                    profile_checked_at ASC NULLS FIRST,
                    (offers_checked_at IS NOT NULL) DESC,
                    id
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------------------
def _normalize_discount_type(raw: str | None) -> str | None:
    """Map LLM output to canonical discount types."""
    if not raw:
        return None
    r = raw.strip().lower().replace(" ", "_")
    mapping = {
        "percentage": "percentage",
        "percent": "percentage",
        "%": "percentage",
        "fixed_amount": "fixed_amount",
        "fixed": "fixed_amount",
        "amount": "fixed_amount",
        "free_shipping": "free_shipping",
        "freeshipping": "free_shipping",
        "shipping": "free_shipping",
        "gift": "gift",
        "freebie": "gift",
        "bogo": "gift",
        "buy_one_get_one": "gift",
    }
    return mapping.get(r, "other")


def _normalize_currency(raw: str | None) -> str | None:
    """Normalize currency to 3-letter uppercase ISO. None if not applicable."""
    if not raw:
        return None
    r = raw.strip().upper()
    if r in ("€", "EURO", "EURS"):
        return "EUR"
    if r in ("KČ", "KC", "CZK"):
        return "CZK"
    if r == "£" or r == "GBP":
        return "GBP"
    if r in ("ZŁ", "ZL", "PLN"):
        return "PLN"
    if len(r) == 3 and r.isalpha():
        return r
    return None


def _normalize_banner_position(raw: str | None) -> str | None:
    """Normalize banner position."""
    if not raw:
        return None
    r = raw.strip().lower().replace(" ", "_")
    valid = {"hero", "top_bar", "popup", "sidebar", "footer", "inline", "header", "modal", "sticky"}
    return r if r in valid else "other"


def _safe_decimal(val) -> float | None:
    """Safely convert to float. None on failure."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str | None) -> datetime | None:
    """Parse date string to datetime. Returns None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    # Try multiple formats
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_voucher_active(valid_to: datetime | None) -> bool:
    """Determine if voucher is currently active based on expiry date."""
    if valid_to is None:
        return True  # No expiry = active
    now = datetime.now(timezone.utc)
    return valid_to > now


def _build_conditions(description: str | None, conditions: str | None) -> str | None:
    """Combine description and conditions into a single conditions field."""
    parts = []
    if description and description.strip():
        parts.append(description.strip())
    if conditions and conditions.strip():
        parts.append(conditions.strip())
    if not parts:
        return None
    combined = " | ".join(parts)
    return combined[:2000]  # Reasonable limit


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------
def scrape_vouchers(shop_id: int, domain: str, shop_url: str, country_id: int | None = None,
                    _config_override: dict | None = None) -> int:
    """
    Main entry for queue_worker routing.
    Scrapes homepage promos + voucher aggregators for a single shop.
    Returns total vouchers found.
    """
    print(f"[{domain}] Starting voucher scan...", flush=True)
    total_vouchers = 0
    total_banners = 0

    # Source 1: Homepage promo banners
    try:
        homepage_vouchers, banners = scrape_homepage_promos(shop_id, shop_url)
        _rate_limit()
    except Exception as e:
        print(f"[{domain}] Homepage scrape error: {e}", flush=True)
        homepage_vouchers, banners = [], []

    # Source 2: Voucher aggregator sites
    try:
        aggregator_vouchers = scrape_voucher_sites(shop_id, shop_url, country_id)
    except Exception as e:
        print(f"[{domain}] Aggregator scrape error: {e}", flush=True)
        aggregator_vouchers = []

    # Merge vouchers: homepage first (higher trust), then aggregators
    all_vouchers = homepage_vouchers.copy()
    existing_codes = {v["code"] for v in all_vouchers}
    for av in aggregator_vouchers:
        if av["code"] not in existing_codes:
            all_vouchers.append(av)
            existing_codes.add(av["code"])

    # Persist
    if all_vouchers:
        total_vouchers = upsert_vouchers(shop_id, country_id, all_vouchers)
    if banners:
        total_banners = save_banner_snapshots(shop_id, country_id, banners)

    # Mark shop as checked
    mark_shop_voucher_checked(shop_id)

    print(f"[{domain}] Voucher scan done: {total_vouchers} codes, {total_banners} banners", flush=True)
    return total_vouchers


# ----------------------------------------------------------------------------
# Standalone entrypoint
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(f"=== Voucher Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    if not FIRECRAWL_API_KEY:
        print("[voucher] ERROR: FIRECRAWL_API_KEY missing — aborting", flush=True)
        sys.exit(1)

    # Mark expired vouchers first
    expired = mark_expired_vouchers()
    if expired:
        print(f"[voucher] Marked {expired} expired vouchers as inactive", flush=True)

    # Load shops to process
    shops = get_shops_to_scrape()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run)", flush=True)

    if not shops:
        print("No shops to scrape. Done.", flush=True)
        return

    total_vouchers = 0
    shops_processed = 0

    for shop_row in shops:
        shop_id, name, url, country_id = shop_row

        if not url:
            print(f"[{name}] No URL — skipping", flush=True)
            mark_shop_voucher_checked(shop_id)
            continue

        try:
            v_count = scrape_vouchers(shop_id, name, url, country_id)
            total_vouchers += v_count
            shops_processed += 1
        except Exception as e:
            print(f"[{name}] Error: {e}", flush=True)
            # Still mark as checked to avoid infinite retry
            mark_shop_voucher_checked(shop_id)

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {total_vouchers} vouchers saved | "
        f"{shops_processed} shops processed | {elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
