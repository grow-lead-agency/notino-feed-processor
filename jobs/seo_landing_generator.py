"""
Job: SEO Landing Page Generator
Kombinatoricky generuje landing page definice z dat v DB.
Každá stránka = brand × category × country × price_range.
Filtruje jen kombinace kde existují min. 3 produkty.

Interval: denně (cron: 0 5 * * *)
Výstup: ~15,000-30,000 stránek upserted do seo_landing_pages.
"""
import sys
import time
import re
import unicodedata
from decimal import Decimal

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PRODUCTS = 3        # Skip combos with fewer products
BATCH_SIZE = 500        # Upsert batch size
PAGE_TYPE = "brand_category_price"

# Main 6 markets (from PRD)
MAIN_COUNTRIES = ["CZ", "SK", "DE", "AT", "PL", "HU"]

# Price ranges per currency
PRICE_RANGES = {
    "CZK": [
        (Decimal("0"), Decimal("500")),
        (Decimal("500"), Decimal("1000")),
        (Decimal("1000"), Decimal("2000")),
        (Decimal("2000"), Decimal("5000")),
        (Decimal("5000"), None),
    ],
    "EUR": [
        (Decimal("0"), Decimal("20")),
        (Decimal("20"), Decimal("50")),
        (Decimal("50"), Decimal("100")),
        (Decimal("100"), Decimal("200")),
        (Decimal("200"), None),
    ],
    "PLN": [
        (Decimal("0"), Decimal("100")),
        (Decimal("100"), Decimal("200")),
        (Decimal("200"), Decimal("500")),
        (Decimal("500"), Decimal("1000")),
        (Decimal("1000"), None),
    ],
    "HUF": [
        (Decimal("0"), Decimal("5000")),
        (Decimal("5000"), Decimal("10000")),
        (Decimal("10000"), Decimal("20000")),
        (Decimal("20000"), Decimal("50000")),
        (Decimal("50000"), None),
    ],
}

# Country → language code mapping (primary language)
COUNTRY_LANG = {
    "CZ": "cs",
    "SK": "sk",
    "DE": "de",
    "AT": "de",
    "PL": "pl",
    "HU": "hu",
}

# Currency display symbols
CURRENCY_DISPLAY = {
    "CZK": "Kč",
    "EUR": "€",
    "PLN": "zł",
    "HUF": "Ft",
}

# Localized SEO templates
TEMPLATES = {
    "cs": {
        "title": "{brand} {category} | Srovnání cen {price_range}",
        "title_no_range": "{brand} {category} | Srovnání cen",
        "description": "Porovnejte {product_count} produktů {brand} {category} od {min_price} do {max_price} {currency_symbol} v {shop_count} obchodech.",
        "description_open": "Porovnejte {product_count} produktů {brand} {category} od {min_price} {currency_symbol} v {shop_count} obchodech.",
        "h1": "{brand} {category}",
    },
    "sk": {
        "title": "{brand} {category} | Porovnanie cien {price_range}",
        "title_no_range": "{brand} {category} | Porovnanie cien",
        "description": "Porovnajte {product_count} produktov {brand} {category} od {min_price} do {max_price} {currency_symbol} v {shop_count} obchodoch.",
        "description_open": "Porovnajte {product_count} produktov {brand} {category} od {min_price} {currency_symbol} v {shop_count} obchodoch.",
        "h1": "{brand} {category}",
    },
    "de": {
        "title": "{brand} {category} | Preisvergleich {price_range}",
        "title_no_range": "{brand} {category} | Preisvergleich",
        "description": "Vergleichen Sie {product_count} {brand} {category} Produkte von {min_price} bis {max_price} {currency_symbol} in {shop_count} Shops.",
        "description_open": "Vergleichen Sie {product_count} {brand} {category} Produkte ab {min_price} {currency_symbol} in {shop_count} Shops.",
        "h1": "{brand} {category}",
    },
    "en": {
        "title": "{brand} {category} | Price Comparison {price_range}",
        "title_no_range": "{brand} {category} | Price Comparison",
        "description": "Compare {product_count} {brand} {category} products from {min_price} to {max_price} {currency_symbol} across {shop_count} shops.",
        "description_open": "Compare {product_count} {brand} {category} products from {min_price} {currency_symbol} across {shop_count} shops.",
        "h1": "{brand} {category}",
    },
    "pl": {
        "title": "{brand} {category} | Porównanie cen {price_range}",
        "title_no_range": "{brand} {category} | Porównanie cen",
        "description": "Porównaj {product_count} produktów {brand} {category} od {min_price} do {max_price} {currency_symbol} w {shop_count} sklepach.",
        "description_open": "Porównaj {product_count} produktów {brand} {category} od {min_price} {currency_symbol} w {shop_count} sklepach.",
        "h1": "{brand} {category}",
    },
    "hu": {
        "title": "{brand} {category} | Ár-összehasonlítás {price_range}",
        "title_no_range": "{brand} {category} | Ár-összehasonlítás",
        "description": "Hasonlítson össze {product_count} {brand} {category} terméket {min_price} és {max_price} {currency_symbol} között {shop_count} üzletben.",
        "description_open": "Hasonlítson össze {product_count} {brand} {category} terméket {min_price} {currency_symbol} felett {shop_count} üzletben.",
        "h1": "{brand} {category}",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", no_accents.lower()).strip("-")
    return slug


def format_price(price: Decimal) -> str:
    """Format price for display (no trailing zeros)."""
    if price is None:
        return "0"
    p = int(price) if price == int(price) else float(price)
    return f"{p:,}".replace(",", " ")


def price_range_slug(lo: Decimal, hi: Decimal | None, currency: str) -> str:
    """Generate slug part for price range. E.g. '1000-2000-czk' or '5000-plus-czk'."""
    lo_str = str(int(lo))
    if hi is None:
        return f"{lo_str}-plus-{currency.lower()}"
    return f"{lo_str}-{int(hi)}-{currency.lower()}"


def price_range_display(lo: Decimal, hi: Decimal | None, currency_symbol: str) -> str:
    """Human-readable price range. E.g. '1000-2000 Kč' or 'od 5000 Kč'."""
    if hi is None:
        return f"{format_price(lo)}+ {currency_symbol}"
    return f"{format_price(lo)}–{format_price(hi)} {currency_symbol}"


def get_hreflang_code(lang_code: str, country_iso2: str) -> str:
    """Build hreflang code like 'cs-CZ', 'de-AT'."""
    return f"{lang_code}-{country_iso2}"


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def load_target_countries(conn, country_filter: list[str] | None = None):
    """Load target countries with language + currency info.
    Returns list of dicts: {id, iso2, currency_code, lang_code, lang_id, hreflang}.
    """
    countries_to_load = country_filter or MAIN_COUNTRIES
    placeholders = ",".join(["%s"] * len(countries_to_load))

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT c.id, c.iso2, c.currency_code,
                   l.code AS lang_code, l.id AS lang_id
            FROM countries c
            LEFT JOIN languages l ON l.primary_country_id = c.id
            WHERE c.iso2 IN ({placeholders})
            ORDER BY c.iso2
        """, countries_to_load)
        rows = cur.fetchall()

    result = []
    for country_id, iso2, currency_code, lang_code, lang_id in rows:
        # Fallback: if no language matched via primary_country_id
        lc = lang_code or COUNTRY_LANG.get(iso2, "en")
        lid = lang_id
        if lid is None:
            # Try to find by code
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM languages WHERE code = %s LIMIT 1", (lc,))
                r = cur.fetchone()
                lid = r[0] if r else 1  # fallback to id=1
        result.append({
            "id": country_id,
            "iso2": iso2,
            "currency_code": currency_code.strip() if currency_code else "EUR",
            "lang_code": lc,
            "lang_id": lid,
            "hreflang": get_hreflang_code(lc, iso2),
        })
    return result


def get_valid_combinations(conn, country_id: int):
    """Find brand × category combos with >= MIN_PRODUCTS products in a country.
    Returns list of tuples:
        (brand_id, brand_name, brand_slug,
         category_id, category_name, category_slug,
         product_count, shop_count, min_price, max_price, avg_price)
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.id, b.name, b.slug,
                   pc.id, pc.name, pc.slug,
                   COUNT(DISTINCT po.id) AS product_count,
                   COUNT(DISTINCT po.shop_id) AS shop_count,
                   MIN(po.price) AS min_price,
                   MAX(po.price) AS max_price,
                   AVG(po.price) AS avg_price
            FROM product_offers po
            JOIN products p ON p.id = po.product_id
            JOIN brands b ON b.id = p.brand_id
            JOIN product_categories pc ON pc.id = p.category_id
            WHERE po.country_id = %s
              AND po.deleted_at IS NULL
              AND p.deleted_at IS NULL
              AND po.price IS NOT NULL
              AND po.is_active = TRUE
            GROUP BY b.id, b.name, b.slug, pc.id, pc.name, pc.slug
            HAVING COUNT(DISTINCT po.id) >= %s
        """, (country_id, MIN_PRODUCTS))
        return cur.fetchall()


def get_price_range_stats(conn, country_id: int, brand_id: int,
                          category_id: int, price_lo: Decimal,
                          price_hi: Decimal | None):
    """Get product/shop counts + price stats for a specific price range slice."""
    with conn.cursor() as cur:
        if price_hi is not None:
            cur.execute("""
                SELECT COUNT(DISTINCT po.id),
                       COUNT(DISTINCT po.shop_id),
                       MIN(po.price),
                       MAX(po.price),
                       AVG(po.price)
                FROM product_offers po
                JOIN products p ON p.id = po.product_id
                WHERE po.country_id = %s
                  AND p.brand_id = %s
                  AND p.category_id = %s
                  AND po.price >= %s
                  AND po.price < %s
                  AND po.deleted_at IS NULL
                  AND p.deleted_at IS NULL
                  AND po.price IS NOT NULL
                  AND po.is_active = TRUE
            """, (country_id, brand_id, category_id, price_lo, price_hi))
        else:
            cur.execute("""
                SELECT COUNT(DISTINCT po.id),
                       COUNT(DISTINCT po.shop_id),
                       MIN(po.price),
                       MAX(po.price),
                       AVG(po.price)
                FROM product_offers po
                JOIN products p ON p.id = po.product_id
                WHERE po.country_id = %s
                  AND p.brand_id = %s
                  AND p.category_id = %s
                  AND po.price >= %s
                  AND po.deleted_at IS NULL
                  AND p.deleted_at IS NULL
                  AND po.price IS NOT NULL
                  AND po.is_active = TRUE
            """, (country_id, brand_id, category_id, price_lo))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Generation logic
# ---------------------------------------------------------------------------

def generate_seo_content(brand_name: str, category_name: str,
                         lang_code: str, price_lo: Decimal,
                         price_hi: Decimal | None, currency: str,
                         product_count: int, shop_count: int,
                         min_price: Decimal, max_price: Decimal) -> dict:
    """Generate title, meta_description, h1 from localized templates."""
    tpl = TEMPLATES.get(lang_code, TEMPLATES["en"])
    currency_symbol = CURRENCY_DISPLAY.get(currency, currency)

    pr_slug = price_range_slug(price_lo, price_hi, currency)
    pr_display = price_range_display(price_lo, price_hi, currency_symbol)

    fmt = {
        "brand": brand_name,
        "category": category_name,
        "price_range": pr_display,
        "product_count": product_count,
        "shop_count": shop_count,
        "min_price": format_price(min_price),
        "max_price": format_price(max_price),
        "currency_symbol": currency_symbol,
    }

    title = tpl["title"].format(**fmt)
    h1 = tpl["h1"].format(**fmt)

    if price_hi is not None:
        description = tpl["description"].format(**fmt)
    else:
        description = tpl["description_open"].format(**fmt)

    return {
        "title": title[:160],
        "meta_description": description[:320],
        "h1": h1,
        "pr_slug": pr_slug,
    }


def generate_landing_pages(combinations, country: dict, currency: str,
                           conn) -> list[dict]:
    """For each brand × category combo, split by price range and generate pages."""
    pages = []
    ranges = PRICE_RANGES.get(currency, PRICE_RANGES["EUR"])
    lang_code = country["lang_code"]

    for (brand_id, brand_name, brand_slug,
         cat_id, cat_name, cat_slug,
         total_count, total_shops, total_min, total_max, total_avg) in combinations:

        b_slug = brand_slug or slugify(brand_name)
        c_slug = cat_slug or slugify(cat_name)

        for price_lo, price_hi in ranges:
            stats = get_price_range_stats(
                conn, country["id"], brand_id, cat_id, price_lo, price_hi
            )
            product_count, shop_count, min_p, max_p, avg_p = stats

            if product_count < MIN_PRODUCTS:
                continue

            seo = generate_seo_content(
                brand_name, cat_name, lang_code,
                price_lo, price_hi, currency,
                product_count, shop_count,
                min_p, max_p,
            )

            pr_slug_part = seo["pr_slug"]
            slug = f"{b_slug}-{c_slug}-{pr_slug_part}"
            hreflang = country["hreflang"]
            canonical = f"/{lang_code}/{slug}"

            pages.append({
                "page_type": PAGE_TYPE,
                "language_id": country["lang_id"],
                "country_id": country["id"],
                "hreflang_code": hreflang,
                "slug": slug,
                "canonical_url": canonical,
                "brand_id": brand_id,
                "category_id": cat_id,
                "attribute_filter": {
                    "price_min": float(price_lo),
                    "price_max": float(price_hi) if price_hi else None,
                    "currency": currency,
                },
                "title": seo["title"],
                "meta_description": seo["meta_description"],
                "h1": seo["h1"],
                "product_count": product_count,
                "shop_count": shop_count,
                "avg_price": avg_p,
                "min_price": min_p,
                "max_price": max_p,
                "currency": currency,
            })

    return pages


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_landing_pages(conn, pages: list[dict]) -> int:
    """Batch upsert pages into seo_landing_pages.
    ON CONFLICT (hreflang_code, page_type, slug) DO UPDATE.
    Returns count of upserted rows.
    """
    if not pages:
        return 0

    import json
    upserted = 0

    for i in range(0, len(pages), BATCH_SIZE):
        batch = pages[i:i + BATCH_SIZE]
        with conn.cursor() as cur:
            for page in batch:
                cur.execute("""
                    INSERT INTO seo_landing_pages (
                        page_type, language_id, country_id, hreflang_code,
                        slug, canonical_url, brand_id, category_id,
                        attribute_filter, title, meta_description, h1,
                        product_count, avg_price, min_price, max_price, currency,
                        last_generated_at, content_generated_by,
                        published, noindex, priority
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        NOW(), 'seo_landing_generator',
                        FALSE, FALSE, 0.5
                    )
                    ON CONFLICT (hreflang_code, page_type, slug) DO UPDATE SET
                        canonical_url = EXCLUDED.canonical_url,
                        brand_id = EXCLUDED.brand_id,
                        category_id = EXCLUDED.category_id,
                        attribute_filter = EXCLUDED.attribute_filter,
                        title = EXCLUDED.title,
                        meta_description = EXCLUDED.meta_description,
                        h1 = EXCLUDED.h1,
                        product_count = EXCLUDED.product_count,
                        avg_price = EXCLUDED.avg_price,
                        min_price = EXCLUDED.min_price,
                        max_price = EXCLUDED.max_price,
                        currency = EXCLUDED.currency,
                        last_generated_at = NOW(),
                        content_generated_by = 'seo_landing_generator'
                """, (
                    page["page_type"],
                    page["language_id"],
                    page["country_id"],
                    page["hreflang_code"],
                    page["slug"],
                    page["canonical_url"],
                    page["brand_id"],
                    page["category_id"],
                    json.dumps(page["attribute_filter"]),
                    page["title"],
                    page["meta_description"],
                    page["h1"],
                    page["product_count"],
                    page["avg_price"],
                    page["min_price"],
                    page["max_price"],
                    page["currency"],
                ))
                upserted += 1
        conn.commit()
        print(f"  Batch upserted: {min(i + BATCH_SIZE, len(pages))}/{len(pages)}", flush=True)

    return upserted


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_landing_generator(countries: list[str] | None = None) -> dict:
    """Main entry point. Generate SEO landing pages for target countries.

    Args:
        countries: List of ISO2 country codes to process. None = all 6 main markets.

    Returns:
        Stats dict: {total_generated, by_country, skipped_low_count, duration_s}
    """
    start_ts = time.time()
    stats = {
        "total_generated": 0,
        "by_country": {},
        "skipped_low_count": 0,
    }

    conn = get_conn()
    try:
        target_countries = load_target_countries(conn, countries)
        if not target_countries:
            print("No target countries found in DB. Exiting.", flush=True)
            return stats

        print(f"Target countries: {[c['iso2'] for c in target_countries]}", flush=True)

        for country in target_countries:
            iso2 = country["iso2"]
            currency = country["currency_code"]
            print(f"\n--- [{iso2}] Processing (currency: {currency}) ---", flush=True)

            # Step 1: Get valid brand × category combinations
            combinations = get_valid_combinations(conn, country["id"])
            print(f"  [{iso2}] Found {len(combinations)} brand×category combos "
                  f"with >= {MIN_PRODUCTS} products", flush=True)

            if not combinations:
                stats["by_country"][iso2] = 0
                continue

            # Step 2: Generate landing pages (split by price ranges)
            pages = generate_landing_pages(combinations, country, currency, conn)
            print(f"  [{iso2}] Generated {len(pages)} landing pages", flush=True)

            # Step 3: Upsert into DB
            upserted = upsert_landing_pages(conn, pages)
            print(f"  [{iso2}] Upserted {upserted} pages", flush=True)

            stats["by_country"][iso2] = upserted
            stats["total_generated"] += upserted

    finally:
        put_conn(conn)

    elapsed = time.time() - start_ts
    stats["duration_s"] = round(elapsed, 1)
    return stats


def main():
    print(f"=== SEO Landing Generator starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===",
          flush=True)

    stats = run_landing_generator()

    print(f"\n{'=' * 60}", flush=True)
    print(f"DONE: {stats['total_generated']:,} landing pages generated", flush=True)
    for iso2, count in stats.get("by_country", {}).items():
        print(f"  {iso2}: {count:,} pages", flush=True)
    print(f"Duration: {stats.get('duration_s', 0):.1f}s", flush=True)


if __name__ == "__main__":
    main()
