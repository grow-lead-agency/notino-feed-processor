"""
Job: Legal Entity Scraper
Cíl: scrape imprint / impressum / kontakt / "o nás" stránky ze shopů bez legal_checked_at.
Extrahuje legal_name, VAT ID, company registration number, address přes LLM AI extraction.

Engine priority:
  1. Crawl4AI (self-hosted, $0) — default
  2. Firecrawl (cloud API, paid) — fallback pokud Crawl4AI selže nebo není dostupný

Workflow:
  1. SELECT shops WHERE legal_checked_at IS NULL AND deleted_at IS NULL
  2. Discover imprint page URL — zkusit známé patterns (/impressum, /kontakt, /o-nas...)
  3. Scrape via Crawl4AI (→ fallback Firecrawl) s JSON schema (legal_name, vat, reg number, address)
  4. Match existing legal_entity by vat_id OR (reg_number + country) OR legal_name
  5. UPSERT legal_entity + link shops.legal_entity_id
  6. UPDATE shops SET legal_checked_at = NOW()

Cron: hodinově — batch ~50 shopů per run (raised from 20 thanks to Crawl4AI).
"""
import os
import re
import sys
import time
import unicodedata

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.crawl4ai_client import crawl4ai_scrape, crawl4ai_health


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

MAX_SHOPS_PER_RUN = 50          # raised from 20 — Crawl4AI is free
FIRECRAWL_TIMEOUT_SECONDS = 60
FIRECRAWL_MAX_RETRIES = 2

# Engine selection — Crawl4AI first, Firecrawl fallback
_USE_CRAWL4AI = None  # lazy init

# Ordered by specificity — legal pages first, fall back to contact/about.
IMPRINT_PATH_CANDIDATES = [
    # CZ/SK
    "o-nas", "o-spolecnosti", "kontakt", "kontakty",
    "obchodni-podminky", "provozovatel",
    # DE/AT/CH
    "impressum", "impressum-datenschutz", "rechtliches",
    # EN
    "imprint", "legal", "legal-notice", "about", "about-us",
    "contact", "contact-us", "company-info", "company",
    "terms-and-conditions", "terms",
    # FR
    "mentions-legales", "qui-sommes-nous", "contactez-nous",
    # IT
    "chi-siamo", "contatti", "note-legali",
    # ES
    "aviso-legal", "quienes-somos", "sobre-nosotros", "contacto",
    # NL
    "over-ons", "contact-ons", "algemene-voorwaarden",
    # PL
    "o-nas", "kontakt", "regulamin",
    # Nordic
    "om-oss", "kontakta-oss",
    # Generic
    "info/about", "help/contact", "customer-service/about",
]

LEGAL_SCHEMA = {
    "type": "object",
    "properties": {
        "legal_name": {
            "type": "string",
            "description": "Full official company name including legal form (e.g. 'Notino, s.r.o.', 'Douglas GmbH', 'Acme Ltd.'). Not brand name.",
        },
        "trade_name": {
            "type": "string",
            "description": "Brand/trade name if different from legal name.",
        },
        "registration_country_iso2": {
            "type": "string",
            "description": "ISO 3166-1 alpha-2 country code where the company is legally registered (e.g. CZ, DE, PL).",
        },
        "registration_number": {
            "type": "string",
            "description": "National business ID (Czech IČO, German Handelsregister HRB number, UK Companies House number, French SIRET, etc.) — digits/alphanumeric only, no prefix.",
        },
        "vat_id": {
            "type": "string",
            "description": "EU VAT ID with 2-letter country prefix (e.g. CZ27753174, DE123456789, PL5252344078). Null if not VAT registered.",
        },
        "registered_address": {
            "type": "string",
            "description": "Full registered address (street + number).",
        },
        "city": {"type": "string"},
        "postal_code": {"type": "string"},
        "contact_email": {"type": "string"},
        "contact_phone": {"type": "string"},
    },
    "required": ["legal_name"],
}

# Detect registration number type heuristically from the country ISO
REG_TYPE_BY_COUNTRY = {
    "CZ": "ico",
    "SK": "ico",
    "DE": "handelsregister",
    "AT": "handelsregister",
    "CH": "handelsregister",
    "GB": "companies_house",
    "IE": "companies_house",
    "FR": "siret",
    "PL": "nip",
    "ES": "nif",
    "PT": "nif",
    "NL": "kvk",
    "RO": "cui",
    "SE": "orgnr",
    "DK": "cvr",
    "FI": "y_tunnus",
}

# Detect entity type from legal_name suffix
ENTITY_TYPE_PATTERNS = [
    (r"\bs\.?\s*r\.?\s*o\.?\b", "sro"),
    (r"\ba\.?\s*s\.?\b", "as"),
    (r"\bGmbH\b", "gmbh"),
    (r"\bAG\b", "ag"),
    (r"\bLtd\.?\b", "ltd"),
    (r"\bLimited\b", "ltd"),
    (r"\bPLC\b", "plc"),
    (r"\bB\.?V\.?\b", "bv"),
    (r"\bSAS\b", "sas"),
    (r"\bSARL\b", "sarl"),
    (r"\bSp\.?\s*z\s*o\.?o\.?\b", "sp_zoo"),
    (r"\bKft\b", "kft"),
    (r"\bSrl\b", "srl"),
    (r"\bAB\b", "ab"),
    (r"\bApS\b", "aps"),
    (r"\bOy\b", "oy"),
]


# ----------------------------------------------------------------------------
# Engine selection
# ----------------------------------------------------------------------------
LEGAL_PROMPT = (
    "Extract the legal entity / company information from this page. "
    "Focus on the official company name (including legal form like s.r.o., GmbH, Ltd), "
    "VAT number, national business registration number, and registered address. "
    "Only extract if you find explicit legal/imprint information on the page. "
    "Ignore contact forms and privacy policies."
)


def _check_crawl4ai() -> bool:
    """Lazy check if Crawl4AI is available. Cached for the run."""
    global _USE_CRAWL4AI
    if _USE_CRAWL4AI is None:
        _USE_CRAWL4AI = crawl4ai_health()
        engine = "Crawl4AI" if _USE_CRAWL4AI else "Firecrawl (fallback)"
        print(f"[legal] Engine: {engine}", flush=True)
    return _USE_CRAWL4AI


def _scrape_via_crawl4ai(url: str) -> dict | None:
    """Scrape legal page via self-hosted Crawl4AI."""
    return crawl4ai_scrape(url=url, schema=LEGAL_SCHEMA, prompt=LEGAL_PROMPT)


def _scrape_via_firecrawl(url: str) -> dict | None:
    """Scrape legal page via Firecrawl cloud API (paid fallback)."""
    if not FIRECRAWL_API_KEY:
        print("[legal] FATAL: FIRECRAWL_API_KEY missing", flush=True)
        return None

    payload = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": LEGAL_SCHEMA,
            "prompt": LEGAL_PROMPT,
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

    for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                FIRECRAWL_API_URL, headers=headers, json=payload,
                timeout=FIRECRAWL_TIMEOUT_SECONDS + 10,
            )
        except requests.RequestException as e:
            if attempt < FIRECRAWL_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            print(f"[legal] Network error for {url}: {e}", flush=True)
            return None

        if resp.status_code == 429:
            if attempt < FIRECRAWL_MAX_RETRIES:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                time.sleep(min(wait, 30))
                continue
            return None

        if resp.status_code in (403, 404):
            return None

        if resp.status_code >= 500:
            if attempt < FIRECRAWL_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None

        if resp.status_code != 200:
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


def scrape_legal(url: str) -> dict | None:
    """Scrape legal page. Crawl4AI first, Firecrawl fallback."""
    if _check_crawl4ai():
        result = _scrape_via_crawl4ai(url)
        if result is not None:
            return result
        print(f"[legal] Crawl4AI failed for {url}, trying Firecrawl...", flush=True)

    return _scrape_via_firecrawl(url)


# ----------------------------------------------------------------------------
# URL discovery
# ----------------------------------------------------------------------------
def candidate_urls(shop_url: str) -> list[str]:
    base = shop_url.rstrip("/")
    return [f"{base}/{path}" for path in IMPRINT_PATH_CANDIDATES]


def find_imprint_page(shop_url: str) -> tuple[str | None, dict | None]:
    """Try candidate URLs. First one with usable legal data wins."""
    for url in candidate_urls(shop_url):
        try:
            head = requests.head(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
                timeout=10,
                allow_redirects=True,
            )
            if head.status_code >= 400:
                continue
        except requests.RequestException:
            continue

        data = scrape_legal(url)
        if data and isinstance(data, dict) and data.get("legal_name"):
            # Require at least one identifier besides name (avoid noise)
            if data.get("vat_id") or data.get("registration_number") or data.get("registered_address"):
                return url, data

    return None, None


# ----------------------------------------------------------------------------
# Normalization / matching
# ----------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", no_accents.lower()).strip()


def normalize_vat_id(vat: str | None) -> str | None:
    """Uppercase, strip spaces and separators. Keep 2-letter country prefix."""
    if not vat:
        return None
    v = re.sub(r"[\s\-\.]", "", vat.upper())
    if len(v) < 4:
        return None
    return v


def normalize_reg_number(rn: str | None) -> str | None:
    if not rn:
        return None
    # Keep only alphanumeric
    v = re.sub(r"[^A-Za-z0-9]", "", rn)
    return v or None


def detect_entity_type(legal_name: str) -> str:
    for pattern, etype in ENTITY_TYPE_PATTERNS:
        if re.search(pattern, legal_name, re.I):
            return etype
    return "unknown"


def load_country_map() -> dict[str, int]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT iso2, id FROM countries")
            return {row[0].strip().upper(): row[1] for row in cur.fetchall()}
    finally:
        put_conn(conn)


def find_existing_entity(
    *,
    cur,
    vat_id: str | None,
    reg_number: str | None,
    reg_country_id: int | None,
    legal_name: str,
) -> int | None:
    """Try multiple lookups to find existing legal_entity."""
    # 1. By VAT ID (most reliable)
    if vat_id:
        cur.execute("SELECT id FROM legal_entities WHERE vat_id = %s LIMIT 1", (vat_id,))
        row = cur.fetchone()
        if row:
            return row[0]

    # 2. By registration number + country
    if reg_number and reg_country_id:
        cur.execute(
            """
            SELECT id FROM legal_entities
            WHERE registration_number = %s AND registration_country_id = %s
            LIMIT 1
            """,
            (reg_number, reg_country_id),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    # 3. By legal name (fuzzy, country-scoped if we have country)
    name_norm = normalize_name(legal_name)
    if name_norm and len(name_norm) >= 5:
        if reg_country_id:
            cur.execute(
                """
                SELECT id, legal_name FROM legal_entities
                WHERE registration_country_id = %s
                """,
                (reg_country_id,),
            )
        else:
            cur.execute("SELECT id, legal_name FROM legal_entities")
        for row in cur.fetchall():
            if normalize_name(row[1]) == name_norm:
                return row[0]

    return None


def upsert_legal_entity(
    *,
    source_url: str,
    data: dict,
    country_map: dict[str, int],
    fallback_country_id: int | None,
) -> int | None:
    """
    UPSERT legal_entity. Returns entity_id or None if data too weak.
    """
    legal_name = (data.get("legal_name") or "").strip()
    if not legal_name or len(legal_name) < 3:
        return None

    trade_name = (data.get("trade_name") or "").strip() or None
    vat_id = normalize_vat_id(data.get("vat_id"))
    reg_number = normalize_reg_number(data.get("registration_number"))

    # Determine registration country
    reg_iso = (data.get("registration_country_iso2") or "").strip().upper()
    reg_country_id = country_map.get(reg_iso) if reg_iso else None

    # Fallback: infer from VAT prefix
    if not reg_country_id and vat_id and len(vat_id) >= 2:
        reg_country_id = country_map.get(vat_id[:2])

    # Final fallback: use shop's country
    if not reg_country_id:
        reg_country_id = fallback_country_id

    if not reg_country_id:
        print(f"  [legal] no registration country for '{legal_name}' — skipping", flush=True)
        return None

    reg_type = None
    if reg_number:
        # Lookup iso by reg_country_id
        for iso, cid in country_map.items():
            if cid == reg_country_id:
                reg_type = REG_TYPE_BY_COUNTRY.get(iso)
                break

    entity_type = detect_entity_type(legal_name)

    address = (data.get("registered_address") or "").strip() or None
    city = (data.get("city") or "").strip() or None
    postal = (data.get("postal_code") or "").strip() or None
    email = (data.get("contact_email") or "").strip() or None
    phone = (data.get("contact_phone") or "").strip() or None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            existing_id = find_existing_entity(
                cur=cur,
                vat_id=vat_id,
                reg_number=reg_number,
                reg_country_id=reg_country_id,
                legal_name=legal_name,
            )

            if existing_id:
                # Update — fill in missing fields (never overwrite non-null with null)
                cur.execute(
                    """
                    UPDATE legal_entities SET
                        vat_id = COALESCE(vat_id, %s),
                        registration_number = COALESCE(registration_number, %s),
                        registration_type = COALESCE(registration_type, %s),
                        trade_name = COALESCE(trade_name, %s),
                        registered_address = COALESCE(registered_address, %s),
                        city = COALESCE(city, %s),
                        postal_code = COALESCE(postal_code, %s),
                        contact_email = COALESCE(contact_email, %s),
                        contact_phone = COALESCE(contact_phone, %s),
                        last_checked_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        vat_id, reg_number, reg_type, trade_name,
                        address, city, postal, email, phone, existing_id,
                    ),
                )
                conn.commit()
                return existing_id

            # Insert new
            cur.execute(
                """
                INSERT INTO legal_entities (
                    legal_name, trade_name, registration_country_id,
                    registration_number, registration_type,
                    vat_id,
                    registered_address, city, postal_code,
                    contact_email, contact_phone,
                    entity_type, data_source, data_confidence,
                    source_url, last_checked_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'scraped', 0.80, %s, NOW())
                RETURNING id
                """,
                (
                    legal_name, trade_name, reg_country_id,
                    reg_number, reg_type,
                    vat_id,
                    address, city, postal,
                    email, phone,
                    entity_type, source_url,
                ),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
    finally:
        put_conn(conn)


def link_shop_to_entity(shop_id: int, entity_id: int) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shops
                SET legal_entity_id = %s
                WHERE id = %s AND deleted_at IS NULL
                """,
                (entity_id, shop_id),
            )
            conn.commit()
    finally:
        put_conn(conn)


def mark_shop_checked(shop_id: int) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shops SET legal_checked_at = NOW() WHERE id = %s AND deleted_at IS NULL",
                (shop_id,),
            )
            conn.commit()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Shop loader
# ----------------------------------------------------------------------------
def get_shops_to_scrape(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url, country_id
                FROM shops
                WHERE deleted_at IS NULL
                  AND legal_checked_at IS NULL
                  AND url IS NOT NULL
                ORDER BY
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
# Per-shop
# ----------------------------------------------------------------------------
def process_shop(shop_row: tuple, country_map: dict[str, int]) -> int:
    """Returns 1 if legal_entity was created/linked, 0 otherwise."""
    shop_id, name, url, country_id = shop_row
    print(f"[{name}] Discovering imprint page...", flush=True)

    source_url, data = find_imprint_page(url)
    if not data:
        print(f"[{name}] No legal data found — marking as checked", flush=True)
        mark_shop_checked(shop_id)
        return 0

    entity_id = upsert_legal_entity(
        source_url=source_url or url,
        data=data,
        country_map=country_map,
        fallback_country_id=country_id,
    )

    if entity_id:
        link_shop_to_entity(shop_id, entity_id)
        print(
            f"[{name}] Linked to legal_entity #{entity_id} ({data.get('legal_name')}) from {source_url}",
            flush=True,
        )

    mark_shop_checked(shop_id)
    return 1 if entity_id else 0


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(f"=== Legal Scraper starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    if not FIRECRAWL_API_KEY and not crawl4ai_health():
        print("[legal] ERROR: Neither Crawl4AI nor FIRECRAWL_API_KEY available — aborting", flush=True)
        sys.exit(1)

    country_map = load_country_map()
    shops = get_shops_to_scrape()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run — sequential)", flush=True)

    if not shops:
        print("No shops to scrape. Done.", flush=True)
        return

    # Sequential — imprint scraping is mostly HEAD requests + 1 Firecrawl call per shop.
    # Parallelism would risk hitting Firecrawl rate limits for small gain.
    total_linked = 0
    for shop in shops:
        try:
            total_linked += process_shop(shop, country_map)
        except Exception as e:
            print(f"[{shop[1]}] Shop-level error: {e}", flush=True)

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {total_linked}/{len(shops)} shops linked to legal entity | {elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
