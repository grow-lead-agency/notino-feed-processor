"""
Job: Ingredient Extractor
Extracts INCI (International Nomenclature of Cosmetic Ingredients) composition
from beauty product pages.

Pipeline:
  1. SELECT beauty products without ingredient data (LEFT JOIN product_ingredients)
  2. Fetch product page HTML (requests)
  3. Regex-first: search for INCI list patterns (Ingredients:, INCI:, Slozeni:, ...)
  4. Firecrawl AI fallback: extract via LLM for JS-heavy / obfuscated pages
  5. Parse individual ingredients (comma-separated INCI format)
  6. Upsert into ingredients master table (dedup by inci_name)
  7. Link product_ingredients N:M with position tracking

Standalone: python jobs/ingredient_extractor.py [--batch N]
Queue:      scrape_type = 'ingredient_extract'

Notes:
  - Regex-first, Firecrawl-second to save credits
  - Rate limiting: max 2 req/s for product pages, max 1 req/s for Firecrawl
  - INCI names normalized to UPPERCASE
  - Position tracks concentration order (>1% descending, <1% arbitrary)
"""
import os
import re
import sys
import time

import requests as http_requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.langfuse_wrapper import traced_generation


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

DEFAULT_BATCH_SIZE = 50
PAGE_FETCH_TIMEOUT = 15
PAGE_REQUEST_DELAY = 0.5  # 2 req/s max
FIRECRAWL_REQUEST_DELAY = 1.0  # 1 req/s max
FIRECRAWL_TIMEOUT = 60
FIRECRAWL_MAX_RETRIES = 2

# Beauty category slugs eligible for ingredient extraction
BEAUTY_CATEGORY_SLUGS = (
    "skincare", "makeup", "hair-care", "bath-shower",
    "fragrances", "nail-care", "oral-care", "mens-grooming",
)

# INCI header patterns (multilingual)
INCI_HEADER_PATTERNS = [
    # English
    r"(?:Ingredients|INCI)\s*[:：]\s*",
    # Czech / Slovak
    r"(?:Slo[zž]en[ií]|Ingredience|Zlo[zž]enie)\s*[:：]\s*",
    # German
    r"(?:Inhaltsstoffe|Zutaten|INCI)\s*[:：]\s*",
    # French
    r"(?:Ingr[eé]dients|Composition)\s*[:：]\s*",
    # Spanish
    r"(?:Ingredientes|Composici[oó]n)\s*[:：]\s*",
    # Italian
    r"(?:Ingredienti|Composizione)\s*[:：]\s*",
    # Polish
    r"(?:Sk[lł]ad|Sk[lł]adniki)\s*[:：]\s*",
    # Dutch
    r"(?:Ingredi[eë]nten|Samenstelling)\s*[:：]\s*",
    # Hungarian
    r"(?:[Öö]sszet[eé]tel|Hozz[aá]val[oó]k)\s*[:：]\s*",
    # Romanian
    r"(?:Ingrediente|Compozi[tț]ie)\s*[:：]\s*",
]

# Combined regex: header followed by INCI text (greedy until double newline or end of block)
_INCI_HEADER_RE = "|".join(f"(?:{p})" for p in INCI_HEADER_PATTERNS)
INCI_REGEX = re.compile(
    rf"(?:{_INCI_HEADER_RE})\s*(.{{20,8000}})",
    re.IGNORECASE | re.DOTALL,
)

# Stop patterns — INCI list usually ends before these
INCI_STOP_PATTERNS = re.compile(
    r"(?:\n\s*\n|</(?:div|p|section|li|td|article)>|"
    r"\b(?:May contain|Peut contenir|Ggf\. enthalten|Puede contener|"
    r"Mohlo by obsahovat|Moze zawierac)\b)",
    re.IGNORECASE,
)

# Firecrawl extraction schema
INCI_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "ingredients": {
            "type": "array",
            "description": "List of individual INCI ingredient names, uppercase. E.g. ['AQUA', 'GLYCERIN', 'CETEARYL ALCOHOL']",
            "items": {"type": "string"},
        },
        "ingredients_text": {
            "type": "string",
            "description": "Full raw ingredients text as found on the page. Null if not found.",
        },
    },
    "required": ["ingredients", "ingredients_text"],
}


# ---------------------------------------------------------------------------
# INCI parsing
# ---------------------------------------------------------------------------
def parse_inci(text: str) -> list[str]:
    """Parse INCI ingredients list into individual ingredient names.

    Args:
        text: Raw INCI text, possibly with header prefix.

    Returns:
        List of uppercase INCI names (deduplicated, order preserved).
    """
    if not text or not text.strip():
        return []

    # Remove common prefixes
    text = re.sub(
        rf"^(?:{_INCI_HEADER_RE})",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )

    # Truncate at stop patterns (double newline, HTML close tag, "May contain" etc.)
    m = INCI_STOP_PATTERNS.search(text)
    if m:
        text = text[:m.start()]

    # Strip trailing period, asterisks, footnote markers
    text = re.sub(r"[\.\*†‡¹²³⁴⁵⁶⁷⁸⁹⁰]+\s*$", "", text.strip())

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Split by comma, semicolon, or pipe
    parts = re.split(r"[,;|]", text)

    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        name = p.strip().upper()
        # Remove parenthesized common names but keep CI numbers
        # e.g. "ROSA CANINA FRUIT OIL (Sipkovy olej)" → "ROSA CANINA FRUIT OIL"
        # but "CI 77891" stays as-is
        if not name.startswith("CI "):
            name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
        # Remove trailing dots/numbers from footnotes
        name = re.sub(r"[\.\*†‡]+$", "", name).strip()
        # Filter: at least 2 chars, not just numbers
        if len(name) < 2 or name.isdigit():
            continue
        if name not in seen:
            seen.add(name)
            result.append(name)

    return result


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def fetch_page_html(url: str) -> str | None:
    """Fetch product page HTML. Returns body text or None."""
    try:
        resp = http_requests.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en,cs,de;q=0.5"},
            timeout=PAGE_FETCH_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        return resp.text
    except http_requests.RequestException as e:
        print(f"  [fetch] Error for {url}: {e}", flush=True)
        return None


def extract_inci_from_html(html: str) -> list[str] | None:
    """Try to extract INCI list from HTML via regex.

    Returns:
        List of parsed ingredient names, or None if not found.
    """
    match = INCI_REGEX.search(html)
    if not match:
        return None
    raw_text = match.group(1)
    ingredients = parse_inci(raw_text)
    if len(ingredients) < 3:
        # Too few ingredients — probably a false positive
        return None
    return ingredients


# ---------------------------------------------------------------------------
# Firecrawl AI fallback
# ---------------------------------------------------------------------------
def firecrawl_extract_inci(url: str) -> list[str] | None:
    """Use Firecrawl AI extraction to get INCI list from a product page.

    Returns:
        List of ingredient names, or None on failure / no data.
    """
    if not FIRECRAWL_API_KEY:
        return None

    inci_prompt = (
        "Extract the INCI (cosmetic ingredients) list from this product page. "
        "Return individual ingredient names in uppercase INCI format. "
        "If no ingredients list is found, return empty ingredients array and null ingredients_text."
    )

    payload = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": INCI_EXTRACTION_SCHEMA,
            "prompt": inci_prompt,
        },
        "onlyMainContent": True,
        "waitFor": 3000,
        "timeout": FIRECRAWL_TIMEOUT * 1000,
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    with traced_generation(
        name="firecrawl-ingredient",
        model="firecrawl/extract",
        input_data={"url": url, "prompt": inci_prompt},
        metadata={"job": "ingredient_extractor"},
        tags=["feed-processor", "firecrawl", "ingredient"],
    ) as gen:
        for attempt in range(FIRECRAWL_MAX_RETRIES + 1):
            try:
                resp = http_requests.post(
                    FIRECRAWL_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=FIRECRAWL_TIMEOUT + 10,
                )
            except http_requests.RequestException as e:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                print(f"  [firecrawl] Network error for {url}: {e}", flush=True)
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

            data = (body.get("data") or {}).get("json") or {}
            raw_ingredients = data.get("ingredients") or []
            raw_text = data.get("ingredients_text")

            # If AI returned raw text but empty list, try parsing ourselves
            if not raw_ingredients and raw_text:
                result = parse_inci(raw_text) or None
                gen.end(output={"source": "parsed_text", "count": len(result) if result else 0}, usage={"total": 1})
                return result

            # Normalize AI-returned names
            if raw_ingredients:
                cleaned = [i.strip().upper() for i in raw_ingredients if isinstance(i, str) and len(i.strip()) >= 2]
                result = cleaned if len(cleaned) >= 3 else None
                gen.end(output={"source": "ai_extracted", "count": len(result) if result else 0}, usage={"total": 1})
                return result

            gen.end(output={"source": "none", "count": 0}, usage={"total": 1})
            return None

        gen.end(level="ERROR", status_message="All retries exhausted")
        return None


# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------
def extract_inci_from_page(url: str) -> list[str] | None:
    """Extract INCI ingredients from a product page URL.

    Strategy: regex-first (free), Firecrawl AI fallback (costs credits).

    Returns:
        List of ingredient names, or None if extraction failed.
    """
    # 1. Try regex extraction from raw HTML
    html = fetch_page_html(url)
    if html:
        ingredients = extract_inci_from_html(html)
        if ingredients:
            return ingredients

    # 2. Firecrawl AI fallback for JS-heavy / obfuscated pages
    time.sleep(FIRECRAWL_REQUEST_DELAY)
    ingredients = firecrawl_extract_inci(url)
    return ingredients


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------
def get_products_without_ingredients(limit: int = DEFAULT_BATCH_SIZE) -> list[dict]:
    """Select beauty products that have no ingredient data yet.

    Returns list of dicts with keys: id, name, category_id, product_url, shop_id.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.title, p.category_id, po.product_url, po.shop_id
                FROM products p
                JOIN product_categories pc ON pc.id = p.category_id
                LEFT JOIN product_ingredients pi ON pi.product_id = p.id
                JOIN LATERAL (
                    SELECT product_url, shop_id
                    FROM product_offers
                    WHERE product_id = p.id AND deleted_at IS NULL
                    LIMIT 1
                ) po ON true
                WHERE pi.id IS NULL
                  AND pc.slug IN %s
                  AND p.deleted_at IS NULL
                LIMIT %s
                """,
                (BEAUTY_CATEGORY_SLUGS, limit),
            )
            cols = ("id", "name", "category_id", "product_url", "shop_id")
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def upsert_ingredients(ingredient_names: list[str]) -> dict[str, int]:
    """Upsert ingredient names into the ingredients master table.

    Args:
        ingredient_names: List of uppercase INCI names.

    Returns:
        Dict mapping inci_name -> ingredient_id.
    """
    if not ingredient_names:
        return {}

    conn = get_conn()
    try:
        result: dict[str, int] = {}
        with conn.cursor() as cur:
            for name in ingredient_names:
                name = name.strip().upper()
                if not name or len(name) < 2:
                    continue
                cur.execute(
                    """
                    INSERT INTO ingredients (inci_name)
                    VALUES (%s)
                    ON CONFLICT (inci_name) DO UPDATE SET updated_at = NOW()
                    RETURNING id
                    """,
                    (name,),
                )
                row = cur.fetchone()
                if row:
                    result[name] = row[0]
            conn.commit()
        return result
    finally:
        put_conn(conn)


def link_product_ingredients(product_id: int, ingredient_map: dict[str, int], ordered_names: list[str]) -> int:
    """Insert product_ingredients links with position tracking.

    Args:
        product_id: Product ID.
        ingredient_map: Dict mapping inci_name -> ingredient_id.
        ordered_names: Ordered list of ingredient names (position = index + 1).

    Returns:
        Number of links created.
    """
    if not ingredient_map or not ordered_names:
        return 0

    conn = get_conn()
    try:
        count = 0
        with conn.cursor() as cur:
            # Delete existing links for this product (re-extraction replaces old data)
            cur.execute("DELETE FROM product_ingredients WHERE product_id = %s", (product_id,))

            for position, name in enumerate(ordered_names, start=1):
                name_upper = name.strip().upper()
                ingredient_id = ingredient_map.get(name_upper)
                if not ingredient_id:
                    continue
                try:
                    cur.execute(
                        """
                        INSERT INTO product_ingredients (product_id, ingredient_id, "position")
                        VALUES (%s, %s, %s)
                        ON CONFLICT (product_id, ingredient_id) DO UPDATE
                            SET "position" = EXCLUDED."position"
                        """,
                        (product_id, ingredient_id, position),
                    )
                    count += 1
                except Exception as e:
                    print(f"  [link] Error product={product_id} ingredient={ingredient_id}: {e}", flush=True)

            conn.commit()
        return count
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main batch runner
# ---------------------------------------------------------------------------
def run_ingredient_extraction(batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    """Main entry point. Process a batch of products without ingredient data.

    Returns:
        Stats dict: products_processed, products_with_ingredients,
                     ingredients_found, ingredients_new, links_created,
                     regex_hits, firecrawl_hits.
    """
    start_ts = time.time()
    print(f"=== Ingredient Extractor starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    products = get_products_without_ingredients(batch_size)
    print(f"Found {len(products)} products without ingredients (batch={batch_size})", flush=True)

    if not products:
        print("No products to process. Done.", flush=True)
        return {
            "products_processed": 0,
            "products_with_ingredients": 0,
            "ingredients_found": 0,
            "ingredients_new": 0,
            "links_created": 0,
            "regex_hits": 0,
            "firecrawl_hits": 0,
        }

    stats = {
        "products_processed": 0,
        "products_with_ingredients": 0,
        "ingredients_found": 0,
        "ingredients_new": 0,
        "links_created": 0,
        "regex_hits": 0,
        "firecrawl_hits": 0,
    }

    # Pre-count existing ingredients for "new" tracking
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingredients")
            ingredients_before = cur.fetchone()[0]
    finally:
        put_conn(conn)

    for i, product in enumerate(products, start=1):
        product_id = product["id"]
        product_name = product["name"] or "?"
        product_url = product["product_url"]

        print(f"[{i}/{len(products)}] {product_name[:60]} — {product_url}", flush=True)

        # Rate limiting between product page requests
        if i > 1:
            time.sleep(PAGE_REQUEST_DELAY)

        # Try extraction: regex first, Firecrawl fallback
        html = fetch_page_html(product_url)
        ingredients = None
        source = None

        if html:
            ingredients = extract_inci_from_html(html)
            if ingredients:
                source = "regex"
                stats["regex_hits"] += 1

        if not ingredients:
            # Firecrawl fallback
            time.sleep(FIRECRAWL_REQUEST_DELAY)
            ingredients = firecrawl_extract_inci(product_url)
            if ingredients:
                source = "firecrawl"
                stats["firecrawl_hits"] += 1

        stats["products_processed"] += 1

        if not ingredients:
            print(f"  No ingredients found", flush=True)
            continue

        print(f"  Found {len(ingredients)} ingredients via {source}", flush=True)
        stats["products_with_ingredients"] += 1
        stats["ingredients_found"] += len(ingredients)

        # Upsert into ingredients master table
        ingredient_map = upsert_ingredients(ingredients)

        # Link product <-> ingredients with positions
        links = link_product_ingredients(product_id, ingredient_map, ingredients)
        stats["links_created"] += links
        print(f"  Linked {links} ingredients to product {product_id}", flush=True)

    # Count new ingredients
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ingredients")
            ingredients_after = cur.fetchone()[0]
    finally:
        put_conn(conn)

    stats["ingredients_new"] = ingredients_after - ingredients_before

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {stats['products_processed']} processed, "
        f"{stats['products_with_ingredients']} with ingredients, "
        f"{stats['ingredients_new']} new ingredients, "
        f"{stats['links_created']} links | "
        f"regex={stats['regex_hits']}, firecrawl={stats['firecrawl_hits']} | "
        f"{elapsed:.1f}s ===",
        flush=True,
    )
    return stats


# ---------------------------------------------------------------------------
# Queue integration helper
# ---------------------------------------------------------------------------
def run_from_queue(job: dict) -> dict:
    """Entry point when called from queue_worker.py.

    Accepts a job dict, returns result dict compatible with worker protocol.
    """
    batch_size = job.get("batch_size", DEFAULT_BATCH_SIZE)
    stats = run_ingredient_extraction(batch_size=batch_size)
    return {
        "saved": stats["links_created"],
        "domain": job.get("domain", "ingredient-extract"),
        "scrape_type": "ingredient_extract",
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract INCI ingredients from beauty product pages")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH_SIZE, help="Batch size (default: 50)")
    args = parser.parse_args()

    run_ingredient_extraction(batch_size=args.batch)
