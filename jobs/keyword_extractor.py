"""
Job: Keyword Extraction Pipeline
Extracts keywords from products, brands, categories, taxonomy.
Writes to: seo_keywords (targeting ~50K rows)

Pipeline: extraction -> normalization -> dedup -> upsert

Sources:
  1. Product titles (67K) — full title, brand alone, title w/o brand, n-grams
  2. Brand names (3K) — direct brand keywords
  3. Brand x category combos — "Chanel parfem", "Dior rtenka" (localized)
  4. Portal categories (1.8K) — portal_categories.name
  5. Google Taxonomy names (346) — path segments
  6. Product type from JSON-LD — distinct product_type_path values

Interval: weekly (manual or cron)
"""
import sys
import re
import time
import unicodedata
from collections import defaultdict

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PRODUCT_BATCH_SIZE = 10_000
MAX_KEYWORD_LENGTH = 120   # Skip absurdly long strings
MIN_KEYWORD_LENGTH = 2     # Skip single chars
MAX_NGRAM_WORDS = 4        # Up to 4-grams

# Volume regex — strip from titles to create keyword variants
VOLUME_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:ml|g|oz|kg|l|mg|cl|fl\.?\s*oz)\b", re.IGNORECASE
)

# Beauty category translations for brand x category combos
# Top-level beauty categories with localized names
CATEGORY_TRANSLATIONS = {
    "parfem":     {"cs": "parfem",     "sk": "parfum",     "de": "Parfum",     "pl": "perfumy",     "hu": "parfum",     "en": "perfume"},
    "rtenka":     {"cs": "rtenka",     "sk": "ruz",        "de": "Lippenstift","pl": "szminka",     "hu": "ruzs",       "en": "lipstick"},
    "serum":      {"cs": "serum",      "sk": "serum",      "de": "Serum",      "pl": "serum",       "hu": "szerum",     "en": "serum"},
    "krem":       {"cs": "krem",       "sk": "krem",       "de": "Creme",      "pl": "krem",        "hu": "krem",       "en": "cream"},
    "sampon":     {"cs": "sampon",     "sk": "sampon",     "de": "Shampoo",    "pl": "szampon",     "hu": "sampon",     "en": "shampoo"},
    "sprchovy_gel":{"cs":"sprchovy gel","sk":"sprchovy gel","de":"Duschgel",    "pl":"zel pod prysznic","hu":"tusfurdo", "en": "shower gel"},
    "deodorant":  {"cs": "deodorant",  "sk": "deodorant",  "de": "Deodorant",  "pl": "dezodorant",  "hu": "dezodor",    "en": "deodorant"},
    "maskara":    {"cs": "maskara",    "sk": "maskara",    "de": "Mascara",    "pl": "tusz do rzes","hu": "szempillaspiral","en": "mascara"},
    "pudr":       {"cs": "pudr",       "sk": "puder",      "de": "Puder",      "pl": "puder",       "hu": "puder",      "en": "powder"},
    "make-up":    {"cs": "make-up",    "sk": "make-up",    "de": "Make-up",    "pl": "makijaz",     "hu": "smink",      "en": "makeup"},
    "telove_mleko":{"cs":"telove mleko","sk":"telove mlieko","de":"Bodylotion", "pl":"balsam do ciala","hu":"testapolo", "en": "body lotion"},
    "ocni_stiny": {"cs":"ocni stiny",  "sk":"tiene na oci","de":"Lidschatten", "pl":"cienie do powiek","hu":"szemhejfestek","en":"eyeshadow"},
    "kondicioner":{"cs":"kondicioner", "sk":"kondicioner", "de":"Conditioner", "pl":"odzywka",      "hu":"kondicionalo","en": "conditioner"},
    "toaletni_voda":{"cs":"toaletni voda","sk":"toaletna voda","de":"Eau de Toilette","pl":"woda toaletowa","hu":"eau de toilette","en":"eau de toilette"},
    "parfemovana_voda":{"cs":"parfemovana voda","sk":"parfumovana voda","de":"Eau de Parfum","pl":"woda perfumowana","hu":"eau de parfum","en":"eau de parfum"},
}

# Language detection character sets
LANG_MARKERS = {
    "cs": set("ěščřžůúýáíéďťň"),
    "sk": set("ôľĺŕäďťňščžúáíéý"),
    "de": set("üöäß"),
    "pl": set("ąćęłńóśźż"),
    "hu": set("áéíóöőúüű"),
}


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

def normalize_keyword(text: str) -> str:
    """Lowercase + remove accents + collapse whitespace for matching."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents.lower()).strip()


def detect_language(keyword: str) -> str:
    """
    Simple heuristic language detection based on diacritical characters.
    Returns ISO 639-1 code: cs, sk, de, pl, hu, en (default).
    """
    if not keyword:
        return "en"
    lower = keyword.lower()
    scores = {}
    for lang, chars in LANG_MARKERS.items():
        score = sum(1 for c in lower if c in chars)
        if score > 0:
            scores[lang] = score

    if not scores:
        return "en"

    # Slovak and Czech overlap a lot — ô, ľ, ĺ, ŕ are uniquely Slovak
    best = max(scores, key=scores.get)
    if best == "cs" and any(c in lower for c in "ôľĺŕ"):
        return "sk"
    return best


def strip_volume(title: str) -> str:
    """Remove volume info (100ml, 50g, etc.) from title."""
    return VOLUME_RE.sub("", title).strip()


def extract_ngrams(words: list[str], min_n: int = 2, max_n: int = MAX_NGRAM_WORDS) -> list[str]:
    """Generate n-grams from a list of words."""
    ngrams = []
    for n in range(min_n, min(max_n + 1, len(words) + 1)):
        for i in range(len(words) - n + 1):
            ngram = " ".join(words[i:i + n])
            if len(ngram) >= MIN_KEYWORD_LENGTH:
                ngrams.append(ngram)
    return ngrams


def is_valid_keyword(kw: str) -> bool:
    """Filter out garbage keywords."""
    if not kw or len(kw) < MIN_KEYWORD_LENGTH or len(kw) > MAX_KEYWORD_LENGTH:
        return False
    # Must have at least one letter
    if not re.search(r"[a-zA-ZÀ-ž]", kw):
        return False
    # Skip pure numbers
    if re.match(r"^[\d\s.,]+$", kw):
        return False
    return True


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------

def extract_from_products(conn, limit: int = PRODUCT_BATCH_SIZE) -> list[dict]:
    """
    Extract keywords from product titles + brand names.
    Processes in batches of `limit`.
    Returns list of keyword dicts.
    """
    keywords = []
    offset = 0
    batch_num = 0

    while True:
        batch_num += 1
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.title, b.name AS brand_name
                FROM products p
                LEFT JOIN brands b ON b.id = p.brand_id
                WHERE p.deleted_at IS NULL AND p.title IS NOT NULL
                ORDER BY p.id
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cur.fetchall()

        if not rows:
            break

        print(f"  Products batch {batch_num}: {len(rows)} rows (offset {offset})", flush=True)

        for product_id, title, brand_name in rows:
            title = title.strip()
            lang = detect_language(title)

            # 1. Full title as keyword
            if is_valid_keyword(title):
                keywords.append({
                    "keyword": title,
                    "source": "product_title",
                    "lang": lang,
                    "brand_name": brand_name,
                    "related_product_id": product_id,
                })

            # 2. Title without volume
            title_no_vol = strip_volume(title)
            if title_no_vol != title and is_valid_keyword(title_no_vol):
                keywords.append({
                    "keyword": title_no_vol,
                    "source": "product_title",
                    "lang": lang,
                    "brand_name": brand_name,
                    "related_product_id": product_id,
                })

            # 3. Title without brand
            if brand_name:
                brand_lower = brand_name.lower()
                title_no_brand = re.sub(
                    re.escape(brand_lower), "", title.lower(), count=1
                ).strip()
                title_no_brand = re.sub(r"\s{2,}", " ", title_no_brand).strip(" -–—,")
                if title_no_brand and title_no_brand != title.lower() and is_valid_keyword(title_no_brand):
                    keywords.append({
                        "keyword": title_no_brand,
                        "source": "product_title",
                        "lang": lang,
                        "brand_name": brand_name,
                        "related_product_id": product_id,
                    })

            # 4. N-grams (2-gram, 3-gram) from title words
            words = title.lower().split()
            if len(words) >= 3:
                for ngram in extract_ngrams(words, 2, 3):
                    if is_valid_keyword(ngram):
                        keywords.append({
                            "keyword": ngram,
                            "source": "product_title",
                            "lang": lang,
                            "brand_name": brand_name,
                        })

        offset += limit

    print(f"  Products: extracted {len(keywords)} keyword candidates", flush=True)
    return keywords


def extract_from_brands(conn) -> list[dict]:
    """Extract brand names as keywords."""
    keywords = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name FROM brands
            WHERE name IS NOT NULL AND name != ''
            ORDER BY id
        """)
        rows = cur.fetchall()

    for brand_id, name in rows:
        name = name.strip()
        if is_valid_keyword(name):
            keywords.append({
                "keyword": name,
                "source": "brand",
                "lang": "en",  # brand names are typically language-neutral
                "brand_id": brand_id,
            })

    print(f"  Brands: extracted {len(keywords)} keyword candidates", flush=True)
    return keywords


def extract_from_brand_category_combos(conn) -> list[dict]:
    """
    Cross-join top brands x category translations.
    Generate localized combos: "Chanel parfem", "Dior rtenka", etc.
    """
    keywords = []
    with conn.cursor() as cur:
        # Get top brands (those with actual products)
        cur.execute("""
            SELECT DISTINCT b.id, b.name
            FROM brands b
            INNER JOIN products p ON p.brand_id = b.id AND p.deleted_at IS NULL
            WHERE b.name IS NOT NULL AND b.name != ''
            ORDER BY b.name
        """)
        brands = cur.fetchall()

    print(f"  Brand x category: {len(brands)} brands x {len(CATEGORY_TRANSLATIONS)} categories", flush=True)

    for brand_id, brand_name in brands:
        brand_name = brand_name.strip()
        for _cat_key, translations in CATEGORY_TRANSLATIONS.items():
            for lang_code, cat_name in translations.items():
                # "Chanel parfem" + "parfem Chanel"
                combo1 = f"{brand_name} {cat_name}"
                combo2 = f"{cat_name} {brand_name}"
                for combo in [combo1, combo2]:
                    if is_valid_keyword(combo):
                        keywords.append({
                            "keyword": combo,
                            "source": "brand_category",
                            "lang": lang_code,
                            "brand_id": brand_id,
                        })

    print(f"  Brand x category: extracted {len(keywords)} keyword candidates", flush=True)
    return keywords


def extract_from_portal_categories(conn) -> list[dict]:
    """Extract keywords from portal_categories.name."""
    keywords = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pc.id, pc.name, pc.name_en
            FROM portal_categories pc
            INNER JOIN price_portals pp ON pp.id = pc.portal_id
            WHERE pc.name IS NOT NULL AND pc.name != ''
              AND pc.is_beauty_relevant = TRUE
            ORDER BY pc.id
        """)
        rows = cur.fetchall()

    for _cat_id, name, name_en in rows:
        name = name.strip()
        if is_valid_keyword(name):
            lang = detect_language(name)
            keywords.append({
                "keyword": name,
                "source": "portal_category",
                "lang": lang,
            })
        # Also add English name if different
        if name_en and name_en.strip() and name_en.strip().lower() != name.lower():
            name_en = name_en.strip()
            if is_valid_keyword(name_en):
                keywords.append({
                    "keyword": name_en,
                    "source": "portal_category",
                    "lang": "en",
                })

    print(f"  Portal categories: extracted {len(keywords)} keyword candidates", flush=True)
    return keywords


def extract_from_taxonomy(conn) -> list[dict]:
    """
    Extract keywords from google_taxonomy.
    Split path segments + use localized names.
    """
    keywords = []
    lang_columns = [
        ("name_en", "en"), ("name_cs", "cs"), ("name_sk", "sk"),
        ("name_de", "de"), ("name_pl", "pl"), ("name_hu", "hu"),
    ]
    col_select = ", ".join(col for col, _ in lang_columns)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT google_id, path_en, name_en, {col_select}
            FROM google_taxonomy
            WHERE is_beauty = TRUE
            ORDER BY google_id
        """)
        rows = cur.fetchall()

    # Column indices: google_id=0, path_en=1, name_en=2, then lang columns start at 3
    for row in rows:
        path_en = row[1] or ""
        # Split path: "Health & Beauty > Personal Care > Cosmetics" -> segments
        segments = [s.strip() for s in path_en.split(">") if s.strip()]
        for segment in segments:
            if is_valid_keyword(segment):
                keywords.append({
                    "keyword": segment,
                    "source": "taxonomy",
                    "lang": "en",
                })
        # Full path as keyword too (for long-tail)
        if len(segments) >= 2 and is_valid_keyword(path_en):
            keywords.append({
                "keyword": path_en,
                "source": "taxonomy",
                "lang": "en",
            })

        # Localized names
        for i, (col, lang) in enumerate(lang_columns):
            val = row[3 + i]  # offset by google_id, path_en, name_en
            if val and val.strip() and is_valid_keyword(val.strip()):
                keywords.append({
                    "keyword": val.strip(),
                    "source": "taxonomy",
                    "lang": lang,
                })

    print(f"  Taxonomy: extracted {len(keywords)} keyword candidates", flush=True)
    return keywords


def extract_from_product_types(conn) -> list[dict]:
    """Extract distinct product_type values from products.product_type_path."""
    keywords = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT unnest(product_type_path) AS pt
            FROM products
            WHERE product_type_path IS NOT NULL AND deleted_at IS NULL
            ORDER BY pt
        """)
        rows = cur.fetchall()

    for (pt,) in rows:
        pt = pt.strip()
        if is_valid_keyword(pt):
            lang = detect_language(pt)
            keywords.append({
                "keyword": pt,
                "source": "product_type",
                "lang": lang,
            })

    print(f"  Product types: extracted {len(keywords)} keyword candidates", flush=True)
    return keywords


# ---------------------------------------------------------------------------
# Normalization & Dedup
# ---------------------------------------------------------------------------

def normalize_keywords(keywords: list[dict]) -> list[dict]:
    """
    Normalize, dedup, and merge keyword candidates.
    Dedup on (keyword_normalized, lang) — keep first occurrence per source.
    """
    print(f"  Normalizing {len(keywords)} raw candidates...", flush=True)

    # Group by (normalized_form, lang)
    groups: dict[tuple[str, str], dict] = {}
    source_counts = defaultdict(int)

    for kw in keywords:
        raw = kw["keyword"]
        lang = kw.get("lang", "en")
        normalized = normalize_keyword(raw)

        if not normalized or len(normalized) < MIN_KEYWORD_LENGTH:
            continue

        key = (normalized, lang)
        if key not in groups:
            groups[key] = {
                "keyword": raw,              # Keep original form (first seen)
                "keyword_normalized": normalized,
                "lang": lang,
                "source": kw["source"],
                "brand_id": kw.get("brand_id"),
                "brand_name": kw.get("brand_name"),
                "related_product_id": kw.get("related_product_id"),
            }
            source_counts[kw["source"]] += 1
        else:
            # Merge: prefer shorter original (cleaner), keep brand_id if missing
            existing = groups[key]
            if len(raw) < len(existing["keyword"]):
                existing["keyword"] = raw
            if not existing.get("brand_id") and kw.get("brand_id"):
                existing["brand_id"] = kw["brand_id"]
            if not existing.get("related_product_id") and kw.get("related_product_id"):
                existing["related_product_id"] = kw["related_product_id"]

    deduped = list(groups.values())
    print(f"  Deduped: {len(keywords)} -> {len(deduped)} unique keywords", flush=True)
    for src, cnt in sorted(source_counts.items()):
        print(f"    {src}: {cnt}", flush=True)

    return deduped


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_language_map(conn) -> dict[str, int]:
    """Return {code: id} for languages."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, code FROM languages WHERE is_active = TRUE")
        return {code.strip(): lid for lid, code in cur.fetchall()}


def get_country_language_map(conn) -> dict[str, dict]:
    """
    Return {hreflang_code: {language_id, country_id}} from country_languages.
    Fallback: if no country_languages data, build from languages + countries.
    """
    mapping = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cl.hreflang_code, cl.language_id, cl.country_id
            FROM country_languages cl
            ORDER BY cl.is_primary DESC
        """)
        for hreflang, lang_id, country_id in cur.fetchall():
            if hreflang not in mapping:
                mapping[hreflang] = {"language_id": lang_id, "country_id": country_id}
    return mapping


def resolve_hreflang(lang_code: str, cl_map: dict, lang_map: dict) -> tuple[str, int, int] | None:
    """
    Given a detected language code (cs, sk, de, etc.), resolve to
    (hreflang_code, language_id, country_id).
    Returns None if unresolvable.
    """
    # Primary hreflang patterns to try
    primary_patterns = {
        "cs": "cs-CZ", "sk": "sk-SK", "de": "de-DE", "pl": "pl-PL",
        "hu": "hu-HU", "en": "en-GB",
    }

    # Try exact match
    primary = primary_patterns.get(lang_code)
    if primary and primary in cl_map:
        entry = cl_map[primary]
        return primary, entry["language_id"], entry["country_id"]

    # Try any hreflang starting with this language
    for hreflang, entry in cl_map.items():
        if hreflang.startswith(lang_code):
            return hreflang, entry["language_id"], entry["country_id"]

    # Fallback: use language_map directly, country_id=0 placeholder
    lang_id = lang_map.get(lang_code)
    if lang_id:
        # Build hreflang from lang code + first country
        fallback_hreflang = f"{lang_code}-XX"
        return fallback_hreflang, lang_id, 1  # country_id=1 as fallback

    return None


def resolve_brand_id(conn, brand_name: str | None, cached_brands: dict) -> int | None:
    """Resolve brand_name to brand_id using cache + DB fallback."""
    if not brand_name:
        return None
    brand_name_lower = brand_name.strip().lower()
    if brand_name_lower in cached_brands:
        return cached_brands[brand_name_lower]

    slug = re.sub(r"[^a-z0-9]+", "-", brand_name_lower).strip("-")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM brands WHERE slug = %s", (slug,))
        row = cur.fetchone()
    if row:
        cached_brands[brand_name_lower] = row[0]
        return row[0]

    cached_brands[brand_name_lower] = None
    return None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_keywords(conn, keywords: list[dict], cl_map: dict, lang_map: dict) -> int:
    """
    Batch upsert into seo_keywords.
    ON CONFLICT (keyword, hreflang_code) DO UPDATE.
    Returns count of upserted rows.
    """
    upserted = 0
    skipped = 0
    brand_cache: dict[str, int | None] = {}

    # Pre-load brand cache
    with conn.cursor() as cur:
        cur.execute("SELECT id, lower(name) FROM brands")
        for bid, bname in cur.fetchall():
            brand_cache[bname] = bid

    batch = []
    BATCH_SIZE = 500

    for kw in keywords:
        lang = kw.get("lang", "en")
        resolved = resolve_hreflang(lang, cl_map, lang_map)
        if not resolved:
            skipped += 1
            continue

        hreflang_code, language_id, country_id = resolved

        # Resolve brand_id
        brand_id = kw.get("brand_id")
        if not brand_id and kw.get("brand_name"):
            brand_id = resolve_brand_id(conn, kw["brand_name"], brand_cache)

        batch.append((
            kw["keyword"],
            kw["keyword_normalized"],
            language_id,
            country_id,
            hreflang_code,
            kw["source"],
            brand_id,
            kw.get("related_product_id"),
        ))

        if len(batch) >= BATCH_SIZE:
            upserted += _flush_batch(conn, batch)
            batch = []

    if batch:
        upserted += _flush_batch(conn, batch)

    if skipped:
        print(f"  Skipped {skipped} keywords (unresolved language)", flush=True)
    return upserted


def _flush_batch(conn, batch: list[tuple]) -> int:
    """Execute batch upsert. Returns rows affected."""
    sql = """
        INSERT INTO seo_keywords (
            keyword, keyword_normalized, language_id, country_id,
            hreflang_code, intent, related_brand_id, related_product_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (keyword, hreflang_code) DO UPDATE SET
            keyword_normalized = EXCLUDED.keyword_normalized,
            related_brand_id = COALESCE(EXCLUDED.related_brand_id, seo_keywords.related_brand_id),
            related_product_id = COALESCE(EXCLUDED.related_product_id, seo_keywords.related_product_id),
            updated_at = NOW()
    """
    count = 0
    with conn.cursor() as cur:
        for params in batch:
            try:
                # Map source to intent column temporarily (intent classification is Phase 4)
                # For now store source as intent placeholder
                cur.execute(sql, params)
                count += 1
            except Exception as e:
                print(f"  WARN: upsert failed for '{params[0][:50]}': {e}", flush=True)
                conn.rollback()
                continue
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_keyword_extraction() -> dict:
    """
    Main entry point. Runs full extraction pipeline.
    Returns stats dict.
    """
    start = time.time()
    stats = {
        "total_extracted": 0,
        "total_deduped": 0,
        "total_upserted": 0,
        "by_source": {},
        "elapsed_s": 0,
    }

    conn = get_conn()
    try:
        print("--- Phase 1: Extraction ---", flush=True)

        all_keywords = []

        # 1. Products
        print("[1/6] Extracting from product titles...", flush=True)
        kw_products = extract_from_products(conn)
        all_keywords.extend(kw_products)
        stats["by_source"]["product_title"] = len(kw_products)

        # 2. Brands
        print("[2/6] Extracting from brand names...", flush=True)
        kw_brands = extract_from_brands(conn)
        all_keywords.extend(kw_brands)
        stats["by_source"]["brand"] = len(kw_brands)

        # 3. Brand x category combos
        print("[3/6] Extracting brand x category combos...", flush=True)
        kw_combos = extract_from_brand_category_combos(conn)
        all_keywords.extend(kw_combos)
        stats["by_source"]["brand_category"] = len(kw_combos)

        # 4. Portal categories
        print("[4/6] Extracting from portal categories...", flush=True)
        kw_portal = extract_from_portal_categories(conn)
        all_keywords.extend(kw_portal)
        stats["by_source"]["portal_category"] = len(kw_portal)

        # 5. Google Taxonomy
        print("[5/6] Extracting from Google Taxonomy...", flush=True)
        kw_taxonomy = extract_from_taxonomy(conn)
        all_keywords.extend(kw_taxonomy)
        stats["by_source"]["taxonomy"] = len(kw_taxonomy)

        # 6. Product types
        print("[6/6] Extracting from product types...", flush=True)
        kw_types = extract_from_product_types(conn)
        all_keywords.extend(kw_types)
        stats["by_source"]["product_type"] = len(kw_types)

        stats["total_extracted"] = len(all_keywords)
        print(f"\nTotal raw candidates: {stats['total_extracted']:,}", flush=True)

        # --- Phase 2: Normalization & Dedup ---
        print("\n--- Phase 2: Normalization & Dedup ---", flush=True)
        deduped = normalize_keywords(all_keywords)
        stats["total_deduped"] = len(deduped)

        # --- Phase 3: Upsert ---
        print("\n--- Phase 3: Upsert to seo_keywords ---", flush=True)

        cl_map = get_country_language_map(conn)
        lang_map = get_language_map(conn)
        print(f"  country_languages: {len(cl_map)} entries, languages: {len(lang_map)} entries", flush=True)

        upserted = upsert_keywords(conn, deduped, cl_map, lang_map)
        stats["total_upserted"] = upserted

        elapsed = time.time() - start
        stats["elapsed_s"] = round(elapsed, 1)

        # Final stats
        print(f"\n{'='*60}", flush=True)
        print(f"DONE: Keyword Extraction Pipeline", flush=True)
        print(f"  Extracted: {stats['total_extracted']:,} raw candidates", flush=True)
        print(f"  Deduped:   {stats['total_deduped']:,} unique keywords", flush=True)
        print(f"  Upserted:  {stats['total_upserted']:,} rows to seo_keywords", flush=True)
        print(f"  Duration:  {elapsed:.1f}s ({elapsed/60:.1f}m)", flush=True)
        print(f"  By source:", flush=True)
        for src, cnt in sorted(stats["by_source"].items()):
            print(f"    {src}: {cnt:,}", flush=True)
        print(f"{'='*60}", flush=True)

        return stats
    finally:
        put_conn(conn)


def main():
    print(f"=== Keyword Extractor starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    stats = run_keyword_extraction()
    print(f"\nFinal stats: {stats}", flush=True)


if __name__ == "__main__":
    main()
