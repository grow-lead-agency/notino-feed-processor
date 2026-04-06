"""
Job: Product Categorizer
Assigns category_id to products using 4 signal sources.
Updates: products.category_id, products.category_source, products.category_confidence

Signal priority (cheapest first):
  1. product_type_path matching (confidence: 0.9)
  2. URL breadcrumb parsing (confidence: 0.85)
  3. Shop category mapping via category_mappings + product_shop_categories (confidence: 0.8)
  4. AI classification via Gemini Flash (variable confidence from API)

Interval: on-demand or cron (e.g. daily 5:00)
"""
import os
import sys
import json
import re
import time
from urllib.parse import urlparse

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

BATCH_SIZE = 1000
AI_BATCH_SIZE = 100  # products per Gemini API call
AI_MAX_RETRIES = 3
AI_RETRY_DELAY = 2  # seconds


# ---------------------------------------------------------------------------
# URL keyword → category slug mapping
# ---------------------------------------------------------------------------

URL_CATEGORY_KEYWORDS = {
    # Fragrances
    "parfem": "fragrances",
    "parfemy": "fragrances",
    "perfume": "fragrances",
    "parfum": "fragrances",
    "fragrance": "fragrances",
    "eau-de-parfum": "fragrances",
    "eau-de-toilette": "fragrances",
    "edp": "fragrances",
    "edt": "fragrances",
    "cologne": "fragrances",
    "vune": "fragrances",
    "duft": "fragrances",
    # Gender sub-categories
    "damske": "womens",
    "women": "womens",
    "damen": "womens",
    "panske": "mens",
    "men": "mens",
    "herren": "mens",
    "unisex": "unisex",
    # Skincare
    "pece-o-plet": "skincare",
    "skincare": "skincare",
    "skin-care": "skincare",
    "hautpflege": "skincare",
    "pece-o-telo": "body-care",
    "body-care": "body-care",
    "body": "body-care",
    "koerperpflege": "body-care",
    "krem": "skincare",
    "cream": "skincare",
    "serum": "skincare",
    "moisturizer": "skincare",
    # Hair care
    "vlasy": "hair-care",
    "hair": "hair-care",
    "haarpflege": "hair-care",
    "shampoo": "hair-care",
    "sampon": "hair-care",
    "conditioner": "hair-care",
    # Makeup
    "makeup": "makeup",
    "make-up": "makeup",
    "dekorativni-kosmetika": "makeup",
    "decorative": "makeup",
    "lipstick": "makeup",
    "mascara": "makeup",
    "foundation": "makeup",
    "rtěnka": "makeup",
    "rtenka": "makeup",
    # Sun care
    "opalovani": "sun-care",
    "sunscreen": "sun-care",
    "sun": "sun-care",
    "sonnenschutz": "sun-care",
    "spf": "sun-care",
    # Oral care
    "zubni": "oral-care",
    "dental": "oral-care",
    "oral-care": "oral-care",
    "zahnpflege": "oral-care",
    # Deodorants
    "deodorant": "deodorants",
    "antiperspirant": "deodorants",
    "deo": "deodorants",
    # Bath & Shower
    "sprchov": "bath-shower",
    "shower": "bath-shower",
    "bath": "bath-shower",
    "koupel": "bath-shower",
    # Accessories
    "doplnky": "accessories",
    "accessories": "accessories",
    "zubehor": "accessories",
    # Nails
    "nehty": "nails",
    "nails": "nails",
    "nail-polish": "nails",
    "lak-na-nehty": "nails",
    # Home fragrance
    "svicky": "home-fragrance",
    "candle": "home-fragrance",
    "diffuser": "home-fragrance",
    "home-fragrance": "home-fragrance",
    "raumduft": "home-fragrance",
}

# product_type_path keywords → category slug
PRODUCT_TYPE_KEYWORDS = {
    "fragrance": "fragrances",
    "perfume": "fragrances",
    "parfum": "fragrances",
    "eau de parfum": "fragrances",
    "eau de toilette": "fragrances",
    "cologne": "fragrances",
    "skincare": "skincare",
    "skin care": "skincare",
    "face cream": "skincare",
    "serum": "skincare",
    "moisturizer": "skincare",
    "cleanser": "skincare",
    "toner": "skincare",
    "hair care": "hair-care",
    "shampoo": "hair-care",
    "conditioner": "hair-care",
    "hair mask": "hair-care",
    "hair oil": "hair-care",
    "makeup": "makeup",
    "make-up": "makeup",
    "lipstick": "makeup",
    "mascara": "makeup",
    "foundation": "makeup",
    "concealer": "makeup",
    "eyeshadow": "makeup",
    "blush": "makeup",
    "powder": "makeup",
    "lip gloss": "makeup",
    "body care": "body-care",
    "body lotion": "body-care",
    "body cream": "body-care",
    "hand cream": "body-care",
    "sun care": "sun-care",
    "sunscreen": "sun-care",
    "sunblock": "sun-care",
    "self-tanning": "sun-care",
    "deodorant": "deodorants",
    "antiperspirant": "deodorants",
    "oral care": "oral-care",
    "toothpaste": "oral-care",
    "mouthwash": "oral-care",
    "shower gel": "bath-shower",
    "bath": "bath-shower",
    "soap": "bath-shower",
    "candle": "home-fragrance",
    "diffuser": "home-fragrance",
    "home fragrance": "home-fragrance",
    "nail": "nails",
    "nail polish": "nails",
    "accessories": "accessories",
    "brush": "accessories",
    "sponge": "accessories",
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_master_categories():
    """Load all product_categories from DB. Returns {id, name, slug, path, level} list + slug→id map."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, slug, path, level
                FROM product_categories
                ORDER BY level, name
            """)
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    cats = []
    slug_map = {}
    for row in rows:
        cat = {
            "id": row[0],
            "name": row[1],
            "slug": row[2],
            "path": row[3],
            "level": row[4],
        }
        cats.append(cat)
        if row[2]:
            slug_map[row[2]] = row[0]

    return cats, slug_map


def get_uncategorized_products(limit=BATCH_SIZE):
    """Fetch products without category_id, with brand + first offer URL."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.title, p.product_type_path, p.description,
                       b.name as brand_name,
                       po.product_url, po.shop_id
                FROM products p
                LEFT JOIN brands b ON b.id = p.brand_id
                LEFT JOIN LATERAL (
                    SELECT product_url, shop_id FROM product_offers
                    WHERE product_id = p.id AND deleted_at IS NULL
                    LIMIT 1
                ) po ON true
                WHERE p.category_id IS NULL AND p.deleted_at IS NULL
                LIMIT %s
            """, (limit,))
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    return [dict(zip(cols, row)) for row in rows]


def update_products(categorizations):
    """
    Batch UPDATE products SET category_id, category_source, category_confidence.
    categorizations: list of (product_id, category_id, source, confidence)
    Returns count of updated rows.
    """
    if not categorizations:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Use VALUES list + UPDATE FROM for batch efficiency
            values = ",".join(
                cur.mogrify("(%s, %s, %s, %s)", (pid, cid, src, conf)).decode()
                for pid, cid, src, conf in categorizations
            )
            cur.execute(f"""
                UPDATE products AS p
                SET category_id = v.category_id,
                    category_source = v.category_source,
                    category_confidence = v.category_confidence,
                    updated_at = NOW()
                FROM (VALUES {values}) AS v(product_id, category_id, category_source, category_confidence)
                WHERE p.id = v.product_id
            """)
            updated = cur.rowcount
        conn.commit()
    finally:
        put_conn(conn)

    return updated


# ---------------------------------------------------------------------------
# Signal 1: product_type_path matching
# ---------------------------------------------------------------------------

def categorize_by_product_type(products, slug_map):
    """
    Match product_type_path array against master categories via keyword lookup.
    Returns (matched_list, remaining_list).
    matched_list: [(product_id, category_id, 'jsonld_product_type', 0.9), ...]
    """
    matched = []
    remaining = []

    for p in products:
        ptp = p.get("product_type_path")
        if not ptp:
            remaining.append(p)
            continue

        found = False
        # Join path elements and search for keywords
        path_text = " ".join(str(seg) for seg in ptp).lower()

        for keyword, cat_slug in PRODUCT_TYPE_KEYWORDS.items():
            if keyword in path_text:
                cat_id = slug_map.get(cat_slug)
                if cat_id:
                    matched.append((p["id"], cat_id, "jsonld_product_type", 0.9))
                    found = True
                    break

        if not found:
            remaining.append(p)

    return matched, remaining


# ---------------------------------------------------------------------------
# Signal 2: URL breadcrumb parsing
# ---------------------------------------------------------------------------

def categorize_by_url(products, slug_map):
    """
    Parse product_url, extract category signals from URL path segments.
    Returns (matched_list, remaining_list).
    """
    matched = []
    remaining = []

    for p in products:
        url = p.get("product_url")
        if not url:
            remaining.append(p)
            continue

        try:
            parsed = urlparse(url)
            path_parts = [seg.lower() for seg in parsed.path.strip("/").split("/") if seg]
        except Exception:
            remaining.append(p)
            continue

        found = False
        for part in path_parts:
            # Try exact match first
            if part in URL_CATEGORY_KEYWORDS:
                cat_slug = URL_CATEGORY_KEYWORDS[part]
                cat_id = slug_map.get(cat_slug)
                if cat_id:
                    matched.append((p["id"], cat_id, "url_breadcrumb", 0.85))
                    found = True
                    break

            # Try substring match for compound segments (e.g. "damske-parfemy")
            for keyword, cat_slug in URL_CATEGORY_KEYWORDS.items():
                if keyword in part:
                    cat_id = slug_map.get(cat_slug)
                    if cat_id:
                        matched.append((p["id"], cat_id, "url_breadcrumb", 0.85))
                        found = True
                        break
            if found:
                break

        if not found:
            remaining.append(p)

    return matched, remaining


# ---------------------------------------------------------------------------
# Signal 3: Shop category mapping
# ---------------------------------------------------------------------------

def categorize_by_shop_mapping(products):
    """
    Query product_shop_categories → shop_categories → mapped_category_id
    OR product_shop_categories → category_mappings → target_category_id.
    Returns (matched_list, remaining_list).
    """
    if not products:
        return [], []

    product_ids = [p["id"] for p in products]
    product_map = {p["id"]: p for p in products}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Batch query: find category mappings for these products
            # Path 1: product_shop_categories → shop_categories.mapped_category_id
            # Path 2: product_shop_categories → category_mappings.target_category_id
            cur.execute("""
                WITH product_cats AS (
                    SELECT DISTINCT ON (psc.product_id)
                        psc.product_id,
                        COALESCE(
                            sc.mapped_category_id,
                            cm.target_category_id
                        ) AS category_id,
                        COALESCE(
                            sc.mapping_confidence,
                            cm.confidence,
                            0.80
                        ) AS confidence
                    FROM product_shop_categories psc
                    JOIN shop_categories sc ON sc.id = psc.shop_category_id
                    LEFT JOIN category_mappings cm
                        ON cm.shop_id = sc.shop_id
                        AND cm.source_category_normalized = sc.name_normalized
                        AND cm.target_category_id IS NOT NULL
                    WHERE psc.product_id = ANY(%s)
                      AND (sc.mapped_category_id IS NOT NULL OR cm.target_category_id IS NOT NULL)
                    ORDER BY psc.product_id,
                             sc.mapped_category_id IS NOT NULL DESC,
                             COALESCE(sc.mapping_confidence, cm.confidence, 0) DESC
                )
                SELECT product_id, category_id, confidence
                FROM product_cats
            """, (product_ids,))

            mapped_ids = set()
            matched = []
            for row in cur.fetchall():
                pid, cid, conf = row
                if pid not in mapped_ids:
                    mapped_ids.add(pid)
                    matched.append((pid, cid, "shop_category_mapping", float(conf or 0.8)))
    finally:
        put_conn(conn)

    remaining = [p for p in products if p["id"] not in {m[0] for m in matched}]
    return matched, remaining


# ---------------------------------------------------------------------------
# Signal 4: AI classification (Gemini Flash)
# ---------------------------------------------------------------------------

def _call_gemini(prompt, retries=AI_MAX_RETRIES):
    """Call Gemini API with retry logic. Returns parsed JSON or None."""
    if not GEMINI_API_KEY:
        print("[categorizer] WARNING: GEMINI_API_KEY not set, skipping AI classification", flush=True)
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        }
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429:
                wait = AI_RETRY_DELAY * attempt
                print(f"[categorizer] Gemini rate limited, waiting {wait}s (attempt {attempt}/{retries})", flush=True)
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                print(f"[categorizer] Gemini API error {resp.status_code}: {resp.text[:200]}", flush=True)
                if attempt < retries:
                    time.sleep(AI_RETRY_DELAY)
                continue

            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"[categorizer] Gemini parse error (attempt {attempt}): {e}", flush=True)
            if attempt < retries:
                time.sleep(AI_RETRY_DELAY)
        except requests.RequestException as e:
            print(f"[categorizer] Gemini request error (attempt {attempt}): {e}", flush=True)
            if attempt < retries:
                time.sleep(AI_RETRY_DELAY)

    return None


def categorize_by_ai(products, master_cats, slug_map):
    """
    Classify remaining products using Gemini Flash.
    Sends batches of AI_BATCH_SIZE products.
    Returns list of (product_id, category_id, 'ai_classification', confidence).
    """
    if not products:
        return []
    if not GEMINI_API_KEY:
        print("[categorizer] No GEMINI_API_KEY — skipping AI classification for "
              f"{len(products)} products", flush=True)
        return []

    # Build category reference for the prompt
    cat_ref = []
    for c in master_cats:
        cat_ref.append({"id": c["id"], "name": c["name"], "path": c["path"] or c["name"]})

    all_matched = []

    for i in range(0, len(products), AI_BATCH_SIZE):
        batch = products[i:i + AI_BATCH_SIZE]

        product_list = []
        for p in batch:
            entry = {"id": p["id"], "name": p.get("title") or ""}
            brand = p.get("brand_name")
            if brand:
                entry["brand"] = brand
            product_list.append(entry)

        prompt = f"""Classify these beauty/cosmetics products into the most appropriate category.
Return a JSON array of objects with product_id, category_id, and confidence (0.0-1.0).

Available categories:
{json.dumps(cat_ref, ensure_ascii=False)}

Products to classify:
{json.dumps(product_list, ensure_ascii=False)}

Rules:
- Match each product to exactly ONE category from the list above.
- Use the category_id field from the categories list.
- confidence should reflect how certain you are (0.5 = guess, 0.9+ = very sure).
- If a product cannot be classified at all, use confidence < 0.3.
- Return ONLY the JSON array, no other text.

Example output:
[{{"product_id": 1, "category_id": 5, "confidence": 0.92}}]"""

        result = _call_gemini(prompt)
        if not result or not isinstance(result, list):
            print(f"[categorizer] AI batch {i // AI_BATCH_SIZE + 1}: no valid response", flush=True)
            continue

        valid_cat_ids = {c["id"] for c in master_cats}
        batch_matched = 0
        for item in result:
            try:
                pid = int(item["product_id"])
                cid = int(item["category_id"])
                conf = float(item.get("confidence", 0.5))

                # Validate
                if cid not in valid_cat_ids:
                    continue
                if conf < 0.3:
                    continue
                # Ensure product_id is actually in this batch
                if pid not in {p["id"] for p in batch}:
                    continue

                conf = min(conf, 0.99)  # Cap AI confidence below manual
                all_matched.append((pid, cid, "ai_classification", round(conf, 2)))
                batch_matched += 1
            except (KeyError, ValueError, TypeError):
                continue

        print(f"[categorizer] AI batch {i // AI_BATCH_SIZE + 1}: "
              f"{batch_matched}/{len(batch)} classified", flush=True)

        # Polite delay between API calls
        if i + AI_BATCH_SIZE < len(products):
            time.sleep(0.5)

    return all_matched


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_product_categorizer(batch_size=BATCH_SIZE):
    """
    Main entry point. Processes uncategorized products in batches.
    Returns stats dict.
    """
    start = time.time()
    print("=" * 60, flush=True)
    print(f"[categorizer] Starting product categorizer at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    # Load master categories once
    master_cats, slug_map = load_master_categories()
    print(f"[categorizer] Loaded {len(master_cats)} master categories", flush=True)

    if not master_cats:
        print("[categorizer] ERROR: No master categories found. Aborting.", flush=True)
        return {"total": 0, "error": "no master categories"}

    stats = {
        "total": 0,
        "by_product_type": 0,
        "by_url": 0,
        "by_mapping": 0,
        "by_ai": 0,
        "failed": 0,
        "batches": 0,
    }

    while True:
        batch = get_uncategorized_products(limit=batch_size)
        if not batch:
            break

        stats["batches"] += 1
        batch_total = len(batch)
        stats["total"] += batch_total
        remaining = batch

        print(f"\n[categorizer] Batch {stats['batches']}: {batch_total} products", flush=True)

        # Pass 1: product_type_path
        matched_ptype, remaining = categorize_by_product_type(remaining, slug_map)
        if matched_ptype:
            saved = update_products(matched_ptype)
            stats["by_product_type"] += saved
            print(f"  [product_type] {saved} classified", flush=True)

        # Pass 2: URL breadcrumb
        matched_url, remaining = categorize_by_url(remaining, slug_map)
        if matched_url:
            saved = update_products(matched_url)
            stats["by_url"] += saved
            print(f"  [url_breadcrumb] {saved} classified", flush=True)

        # Pass 3: Shop category mapping
        matched_mapping, remaining = categorize_by_shop_mapping(remaining)
        if matched_mapping:
            saved = update_products(matched_mapping)
            stats["by_mapping"] += saved
            print(f"  [shop_mapping] {saved} classified", flush=True)

        # Pass 4: AI classification (only remaining)
        if remaining:
            matched_ai = categorize_by_ai(remaining, master_cats, slug_map)
            if matched_ai:
                saved = update_products(matched_ai)
                stats["by_ai"] += saved
                print(f"  [ai] {saved} classified", flush=True)

            # Products that couldn't be classified by any method
            ai_ids = {m[0] for m in matched_ai} if matched_ai else set()
            failed_count = len([p for p in remaining if p["id"] not in ai_ids])
            stats["failed"] += failed_count
            if failed_count:
                print(f"  [failed] {failed_count} unclassified", flush=True)

        batch_classified = (
            len(matched_ptype) + len(matched_url) + len(matched_mapping)
            + (len(matched_ai) if remaining else 0)
        )
        print(f"  [batch total] {batch_classified}/{batch_total} classified", flush=True)

    elapsed = time.time() - start
    total_classified = stats["by_product_type"] + stats["by_url"] + stats["by_mapping"] + stats["by_ai"]

    print(f"\n{'=' * 60}", flush=True)
    print(f"[categorizer] DONE in {elapsed:.1f}s ({elapsed / 60:.1f}m)", flush=True)
    print(f"  Total processed:   {stats['total']}", flush=True)
    print(f"  By product_type:   {stats['by_product_type']}", flush=True)
    print(f"  By URL breadcrumb: {stats['by_url']}", flush=True)
    print(f"  By shop mapping:   {stats['by_mapping']}", flush=True)
    print(f"  By AI (Gemini):    {stats['by_ai']}", flush=True)
    print(f"  Failed:            {stats['failed']}", flush=True)
    print(f"  Classification:    {total_classified}/{stats['total']} "
          f"({total_classified / max(stats['total'], 1) * 100:.1f}%)", flush=True)

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    stats = run_product_categorizer()
    print(f"\nStats: {json.dumps(stats, indent=2)}", flush=True)
