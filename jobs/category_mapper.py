"""
Job: AI Category Mapper
Maps shop_categories, portal_categories, and google_taxonomy to master product_categories.
Two-pass: rule-based first, then AI (Gemini Flash) for unmatched.

Writes to: category_mappings + source tables (mapped_category_id, mapping_confidence)
Reads from: product_categories, shop_categories, portal_categories, google_taxonomy

Cron: on demand / daily (after new categories are scraped)
"""
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.langfuse_wrapper import traced_openrouter_call, flush as langfuse_flush


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# OpenRouter model for category mapping ($0.03/M input, 131K ctx, multilingual)
OPENROUTER_MODEL = os.environ.get("CATEGORY_MAPPER_MODEL", "google/gemma-4-26b-a4b-it")
AI_TIMEOUT = 60
AI_MAX_RETRIES = 2
AI_BATCH_SIZE = 50
AI_REVIEW_THRESHOLD = 0.8

# Confidence scores per match method
CONF_EXACT_SLUG = 1.0
CONF_EXACT_NAME = 0.95
CONF_FUZZY_CONTAINS = 0.85
CONF_KEYWORD = 0.80

# Beauty keyword map: source keyword → master slug(s)
# Covers CZ, SK, DE, EN, FR, IT, ES, PL, NL beauty terms
KEYWORD_MAP = {
    # Fragrances
    "parfem": "fragrances", "parfém": "fragrances", "parfum": "fragrances",
    "perfume": "fragrances", "fragrance": "fragrances", "eau de toilette": "fragrances",
    "eau de parfum": "fragrances", "edt": "fragrances", "edp": "fragrances",
    "cologne": "fragrances", "kolínská": "fragrances", "toaletní voda": "fragrances",
    "parfémová voda": "fragrances", "duft": "fragrances", "profumo": "fragrances",
    # Skincare
    "péče o pleť": "skincare", "plet": "skincare", "pleťový": "skincare",
    "skincare": "skincare", "skin care": "skincare", "hautpflege": "skincare",
    "gesichtspflege": "skincare", "soin visage": "skincare", "cura viso": "skincare",
    "krém": "skincare", "cream": "skincare", "creme": "skincare", "crema": "skincare",
    "sérum": "skincare", "serum": "skincare",
    "moisturizer": "skincare", "hydratační": "skincare",
    "čistící": "skincare", "cleanser": "skincare",
    "toner": "skincare", "tonikum": "skincare",
    "maska": "skincare", "mask": "skincare", "masque": "skincare",
    # Makeup
    "makeup": "makeup", "make-up": "makeup", "make up": "makeup",
    "líčení": "makeup", "dekorativní kosmetika": "makeup", "dekorativka": "makeup",
    "schminke": "makeup", "maquillage": "makeup", "trucco": "makeup",
    "rtěnka": "lipstick", "lipstick": "lipstick", "rúž": "lipstick",
    "lippenstift": "lipstick", "rouge à lèvres": "lipstick",
    "řasenka": "mascara", "mascara": "mascara", "maskara": "mascara",
    "oční stíny": "eyeshadow", "eyeshadow": "eyeshadow", "eye shadow": "eyeshadow",
    "lidschatten": "eyeshadow", "ombre à paupières": "eyeshadow",
    "pudr": "powder", "powder": "powder", "puder": "powder", "poudre": "powder",
    "make-up": "foundation", "foundation": "foundation", "fond de teint": "foundation",
    "korektor": "concealer", "concealer": "concealer",
    "tvářenka": "blush", "blush": "blush", "rouge": "blush",
    "lesk na rty": "lip-gloss", "lip gloss": "lip-gloss",
    "tužka na oči": "eyeliner", "eyeliner": "eyeliner", "eye liner": "eyeliner",
    "lak na nehty": "nail-polish", "nail polish": "nail-polish", "nagellack": "nail-polish",
    "vernis": "nail-polish",
    # Haircare
    "vlasy": "haircare", "hair": "haircare", "haare": "haircare",
    "péče o vlasy": "haircare", "haircare": "haircare", "hair care": "haircare",
    "haarpflege": "haircare", "soin cheveux": "haircare",
    "šampon": "shampoo", "šampón": "shampoo", "shampoo": "shampoo",
    "kondicionér": "conditioner", "conditioner": "conditioner",
    "barva na vlasy": "hair-color", "hair color": "hair-color", "hair dye": "hair-color",
    "haarfarbe": "hair-color", "coloration": "hair-color",
    # Body care
    "tělo": "body-care", "body": "body-care", "körper": "body-care",
    "péče o tělo": "body-care", "body care": "body-care", "körperpflege": "body-care",
    "soin corps": "body-care", "cura corpo": "body-care",
    "sprchový gel": "shower-gel", "shower gel": "shower-gel", "duschgel": "shower-gel",
    "gel douche": "shower-gel",
    "tělové mléko": "body-lotion", "body lotion": "body-lotion",
    "deodorant": "deodorant", "antiperspirant": "deodorant", "deo": "deodorant",
    "mýdlo": "soap", "soap": "soap", "seife": "soap", "savon": "soap",
    # Sun & tanning
    "opalování": "sun-care", "sunscreen": "sun-care", "sun care": "sun-care",
    "sonnenschutz": "sun-care", "solaire": "sun-care", "spf": "sun-care",
    "opalovací": "sun-care", "samoopalovací": "self-tanning",
    "self-tanning": "self-tanning", "selbstbräuner": "self-tanning",
    # Oral care
    "zuby": "oral-care", "ústní": "oral-care", "oral care": "oral-care",
    "zubní pasta": "oral-care", "toothpaste": "oral-care", "zahnpasta": "oral-care",
    "dentifrice": "oral-care",
    # Men
    "pro muže": "mens", "for men": "mens", "men": "mens", "für herren": "mens",
    "homme": "mens", "uomo": "mens", "pánský": "mens",
    "holení": "shaving", "shaving": "shaving", "rasur": "shaving", "rasage": "shaving",
    # Kids / baby
    "dětský": "kids", "baby": "kids", "kids": "kids", "kinder": "kids",
    "enfant": "kids", "bambino": "kids",
    # Accessories
    "příslušenství": "accessories", "accessories": "accessories",
    "zubehör": "accessories", "accessoires": "accessories",
    "kartáč": "accessories", "brush": "accessories", "pinsel": "accessories",
    "pinceau": "accessories",
    # Niche / luxury
    "niche": "niche-perfumes", "luxury": "luxury",
    # Sets
    "dárková sada": "gift-sets", "gift set": "gift-sets", "geschenkset": "gift-sets",
    "coffret": "gift-sets", "set": "gift-sets", "sada": "gift-sets",
    # Aromatherapy
    "svíčka": "candles", "candle": "candles", "kerze": "candles", "bougie": "candles",
    "difuzér": "diffusers", "diffuser": "diffusers",
    "aroma": "aromatherapy", "aromatherapy": "aromatherapy",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    """Lowercase + remove accents + collapse non-alnum to single space."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents.lower()).strip()


def _slugify(text: str) -> str:
    """Normalized text → slug (spaces→dashes)."""
    return re.sub(r"\s+", "-", _normalize(text))


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------
def get_master_categories() -> list[dict]:
    """Load all product_categories (master taxonomy)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, name_cs, slug, parent_id, level, path
                FROM product_categories
                ORDER BY level, name
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)


def get_unmapped_sources() -> dict:
    """
    Load unmapped categories from all three source tables.
    Returns dict with keys: shop, portal, google — each a list of dicts.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Shop categories without mapping
            cur.execute("""
                SELECT id, shop_id, name, name_normalized, slug, path, breadcrumb, level
                FROM shop_categories
                WHERE mapped_category_id IS NULL
                  AND name IS NOT NULL AND name != ''
                ORDER BY id
            """)
            cols = [d[0] for d in cur.description]
            shop_cats = [dict(zip(cols, row)) for row in cur.fetchall()]

            # Portal categories without mapping
            cur.execute("""
                SELECT id, portal_id, name, name_en, slug, path, breadcrumb, level
                FROM portal_categories
                WHERE mapped_category_id IS NULL
                  AND is_beauty_relevant = true
                  AND name IS NOT NULL AND name != ''
                ORDER BY id
            """)
            cols = [d[0] for d in cur.description]
            portal_cats = [dict(zip(cols, row)) for row in cur.fetchall()]

            # Google taxonomy without mapping (beauty only)
            cur.execute("""
                SELECT google_id AS id, name_en AS name, name_cs, path_en AS path,
                       level, parent_google_id
                FROM google_taxonomy
                WHERE mapped_category_id IS NULL
                  AND is_beauty = true
                ORDER BY google_id
            """)
            cols = [d[0] for d in cur.description]
            google_cats = [dict(zip(cols, row)) for row in cur.fetchall()]

        return {
            "shop": shop_cats,
            "portal": portal_cats,
            "google": google_cats,
        }
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Rule-based pass
# ---------------------------------------------------------------------------
def rule_based_pass(master_cats: list[dict], source_cats: dict) -> list[dict]:
    """
    Attempt to match source categories to master via rules (no AI).
    Priority: exact slug → exact name → fuzzy contains → keyword extraction.
    Returns list of mapping dicts.
    """
    # Build master lookup indices
    slug_index: dict[str, dict] = {}     # slug → master cat
    name_index: dict[str, dict] = {}     # normalized name → master cat
    name_contains: list[dict] = []       # for fuzzy substring
    slug_keyword: dict[str, dict] = {}   # keyword slug → master cat

    for mc in master_cats:
        slug = mc["slug"] or ""
        norm_name = _normalize(mc["name"])
        norm_name_cs = _normalize(mc.get("name_cs") or "")

        if slug:
            slug_index[slug] = mc
        if norm_name:
            name_index[norm_name] = mc
        if norm_name_cs:
            name_index[norm_name_cs] = mc
        name_contains.append(mc)
        # Also index by slug for keyword map values
        slug_keyword[slug] = mc

    mappings: list[dict] = []
    matched_ids: dict[str, set] = {"shop": set(), "portal": set(), "google": set()}

    for source_type, cats in source_cats.items():
        for cat in cats:
            cat_id = cat["id"]
            cat_name = cat.get("name") or ""
            cat_slug = cat.get("slug") or ""
            cat_name_norm = _normalize(cat_name)
            cat_slug_norm = _slugify(cat_name) if not cat_slug else _slugify(cat_slug)

            # Also try name_en for portal/google
            cat_name_en = cat.get("name_en") or ""
            cat_name_en_norm = _normalize(cat_name_en)

            # Also try name_cs for google
            cat_name_cs = cat.get("name_cs") or ""
            cat_name_cs_norm = _normalize(cat_name_cs)

            match = None

            # --- Strategy 1: Exact slug match ---
            for try_slug in [cat_slug_norm, _slugify(cat_name_en), _slugify(cat_name_cs)]:
                if try_slug and try_slug in slug_index:
                    match = {
                        "source_type": source_type,
                        "source_id": cat_id,
                        "master_category_id": slug_index[try_slug]["id"],
                        "confidence": CONF_EXACT_SLUG,
                        "match_method": "exact_slug",
                    }
                    break

            # --- Strategy 2: Exact name match (case-insensitive, unaccented) ---
            if not match:
                for try_name in [cat_name_norm, cat_name_en_norm, cat_name_cs_norm]:
                    if try_name and try_name in name_index:
                        match = {
                            "source_type": source_type,
                            "source_id": cat_id,
                            "master_category_id": name_index[try_name]["id"],
                            "confidence": CONF_EXACT_NAME,
                            "match_method": "exact_name",
                        }
                        break

            # --- Strategy 3: Fuzzy — master name contained in source name ---
            if not match:
                best_fuzzy = None
                best_fuzzy_len = 0
                for mc in name_contains:
                    mc_norm = _normalize(mc["name"])
                    mc_norm_cs = _normalize(mc.get("name_cs") or "")
                    for try_name in [cat_name_norm, cat_name_en_norm, cat_name_cs_norm]:
                        if not try_name:
                            continue
                        for mc_n in [mc_norm, mc_norm_cs]:
                            if not mc_n or len(mc_n) < 3:
                                continue
                            if mc_n in try_name and len(mc_n) > best_fuzzy_len:
                                best_fuzzy = mc
                                best_fuzzy_len = len(mc_n)
                if best_fuzzy and best_fuzzy_len >= 4:
                    match = {
                        "source_type": source_type,
                        "source_id": cat_id,
                        "master_category_id": best_fuzzy["id"],
                        "confidence": CONF_FUZZY_CONTAINS,
                        "match_method": "fuzzy_contains",
                    }

            # --- Strategy 4: Keyword extraction ---
            if not match:
                for try_name in [cat_name_norm, cat_name_en_norm, cat_name_cs_norm]:
                    if not try_name:
                        continue
                    for keyword, target_slug in KEYWORD_MAP.items():
                        kw_norm = _normalize(keyword)
                        if kw_norm and kw_norm in try_name and target_slug in slug_keyword:
                            match = {
                                "source_type": source_type,
                                "source_id": cat_id,
                                "master_category_id": slug_keyword[target_slug]["id"],
                                "confidence": CONF_KEYWORD,
                                "match_method": "keyword",
                            }
                            break
                    if match:
                        break

            if match:
                mappings.append(match)
                matched_ids[source_type].add(cat_id)

    return mappings


# ---------------------------------------------------------------------------
# AI pass (Gemini Flash) — traced via Langfuse
# ---------------------------------------------------------------------------


def _parse_ai_response(text: str) -> list[dict]:
    """Parse Gemini JSON response → list of {source_id, master_id, confidence}."""
    if not text:
        return []
    try:
        # Try direct JSON parse
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("mappings", "results", "categories", "data"):
                if key in result and isinstance(result[key], list):
                    return result[key]
        return []
    except json.JSONDecodeError:
        # Try to extract JSON array from markdown code block
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        print(f"[category_mapper] Failed to parse AI response: {text[:200]}", flush=True)
        return []


def ai_pass(master_cats: list[dict], unmatched_cats: dict) -> list[dict]:
    """
    Use OpenRouter (open-source models) to map categories not matched by rules.
    Batches into groups of AI_BATCH_SIZE.
    Returns list of mapping dicts.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        return []

    # Build master taxonomy context (compact)
    master_context = json.dumps(
        [{"id": mc["id"], "name": mc["name"], "path": mc.get("path") or mc["name"]}
         for mc in master_cats],
        ensure_ascii=False,
    )

    # Flatten all unmatched into one list with source_type tag
    flat: list[dict] = []
    for source_type, cats in unmatched_cats.items():
        for cat in cats:
            entry = {
                "source_id": cat["id"],
                "source_type": source_type,
                "name": cat.get("name") or "",
                "breadcrumb": cat.get("breadcrumb") or cat.get("path") or "",
            }
            # Include name_en for portal/google if available
            name_en = cat.get("name_en") or cat.get("name_cs") or ""
            if name_en:
                entry["name_alt"] = name_en
            flat.append(entry)

    if not flat:
        return []

    print(f"[category_mapper] AI pass: {len(flat)} unmatched categories in {len(flat) // AI_BATCH_SIZE + 1} batches", flush=True)

    all_mappings: list[dict] = []

    # Process in batches
    for i in range(0, len(flat), AI_BATCH_SIZE):
        batch = flat[i:i + AI_BATCH_SIZE]
        batch_num = i // AI_BATCH_SIZE + 1
        total_batches = (len(flat) - 1) // AI_BATCH_SIZE + 1

        source_context = json.dumps(
            [{"source_id": b["source_id"], "name": b["name"],
              "breadcrumb": str(b["breadcrumb"]),
              **({"name_alt": b["name_alt"]} if b.get("name_alt") else {})}
             for b in batch],
            ensure_ascii=False,
        )

        prompt = (
            "You are a beauty product category classifier.\n\n"
            "Master taxonomy:\n"
            f"{master_context}\n\n"
            "Map each of these source categories to the most appropriate master category.\n"
            "Return JSON array: [{\"source_id\": X, \"master_id\": Y, \"confidence\": 0.95}]\n"
            "Rules:\n"
            "- confidence: 0.95+ for exact match, 0.80-0.94 for good match, below 0.80 for uncertain\n"
            "- If no reasonable match exists, set master_id to null and confidence to 0.0\n"
            "- Consider that names may be in Czech, Slovak, German, French, Italian, Spanish, Polish, etc.\n\n"
            "Source categories to map:\n"
            f"{source_context}"
        )

        print(f"[category_mapper] AI batch {batch_num}/{total_batches} ({len(batch)} cats)...", flush=True)
        # Gemma models break with json_mode (return single object instead of array)
        # — use raw mode and parse markdown ```json``` code blocks
        response_text = traced_openrouter_call(
            name="category-mapping",
            prompt=prompt,
            model=OPENROUTER_MODEL,
            timeout=AI_TIMEOUT,
            max_retries=AI_MAX_RETRIES,
            json_mode=False,
            metadata={"batch": batch_num, "total_batches": total_batches, "batch_size": len(batch)},
        )
        parsed = _parse_ai_response(response_text)

        # Build source_id → source_type lookup for this batch
        id_to_type = {b["source_id"]: b["source_type"] for b in batch}

        for item in parsed:
            source_id = item.get("source_id")
            master_id = item.get("master_id")
            confidence = item.get("confidence", 0.0)

            if source_id is None or master_id is None:
                continue

            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = 0.0

            source_type = id_to_type.get(source_id, "unknown")

            all_mappings.append({
                "source_type": source_type,
                "source_id": source_id,
                "master_category_id": master_id,
                "confidence": round(confidence, 2),
                "match_method": "ai_gemini",
            })

        # Polite delay between batches
        if i + AI_BATCH_SIZE < len(flat):
            time.sleep(2)

    return all_mappings


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------
def upsert_mappings(mappings: list[dict]) -> int:
    """
    Batch upsert mappings into:
    1. category_mappings bridge table
    2. Source tables (mapped_category_id, mapping_confidence)
    Returns count of upserted rows.
    """
    if not mappings:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            count = 0
            for m in mappings:
                source_type = m["source_type"]
                source_id = m["source_id"]
                master_id = m["master_category_id"]
                confidence = m["confidence"]
                method = m["match_method"]

                # Determine source_category text + shop_id for bridge table
                source_name = ""
                shop_id = None
                breadcrumb = None

                if source_type == "shop":
                    cur.execute(
                        "SELECT name, name_normalized, breadcrumb, shop_id FROM shop_categories WHERE id = %s",
                        (source_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        source_name = row[0] or ""
                        breadcrumb = row[2]
                        shop_id = row[3]
                elif source_type == "portal":
                    cur.execute(
                        "SELECT name, name_en, breadcrumb FROM portal_categories WHERE id = %s",
                        (source_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        source_name = row[0] or row[1] or ""
                        breadcrumb = row[2]
                elif source_type == "google":
                    cur.execute(
                        "SELECT name_en, path_en FROM google_taxonomy WHERE google_id = %s",
                        (source_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        source_name = row[0] or ""

                source_norm = _normalize(source_name)

                # 1. Upsert into category_mappings bridge table
                #    Unique constraint: (shop_id, source_category)
                cur.execute("""
                    INSERT INTO category_mappings
                        (shop_id, source_category, source_category_normalized,
                         source_breadcrumb, target_category_id,
                         mapping_type, confidence, verified, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, false, NOW())
                    ON CONFLICT (shop_id, source_category)
                        DO UPDATE SET
                            target_category_id = EXCLUDED.target_category_id,
                            confidence = EXCLUDED.confidence,
                            mapping_type = EXCLUDED.mapping_type,
                            verified = false,
                            updated_at = NOW()
                """, (
                    shop_id,
                    source_name,
                    source_norm,
                    breadcrumb,
                    master_id,
                    f"{source_type}:{method}",
                    confidence,
                ))

                # 2. Update mapped_category_id on source table
                if source_type == "shop":
                    cur.execute("""
                        UPDATE shop_categories
                        SET mapped_category_id = %s, mapping_confidence = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (master_id, confidence, source_id))
                elif source_type == "portal":
                    cur.execute("""
                        UPDATE portal_categories
                        SET mapped_category_id = %s, mapping_confidence = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (master_id, confidence, source_id))
                elif source_type == "google":
                    cur.execute("""
                        UPDATE google_taxonomy
                        SET mapped_category_id = %s, updated_at = NOW()
                        WHERE google_id = %s
                    """, (master_id, source_id))

                count += 1

            conn.commit()
        return count
    except Exception as e:
        conn.rollback()
        print(f"[category_mapper] DB error in upsert_mappings: {e}", flush=True)
        raise
    finally:
        put_conn(conn)


def add_to_review_queue(items: list[dict]) -> int:
    """Insert low-confidence AI mappings into review_queue for manual verification."""
    if not items:
        return 0

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            count = 0
            for item in items:
                cur.execute("""
                    INSERT INTO review_queue
                        (issue_type, priority, entity_type, entity_id,
                         title, detail, ai_suggestion, ai_confidence, ai_model,
                         status, auto_detected)
                    VALUES (
                        'category_mapping', 'low',
                        %s, %s,
                        %s, %s, %s, %s, %s,
                        'open', true
                    )
                """, (
                    item["source_type"],
                    item["source_id"],
                    f"Low confidence category mapping ({item['confidence']:.0%})",
                    json.dumps({
                        "source_type": item["source_type"],
                        "source_id": item["source_id"],
                        "master_category_id": item["master_category_id"],
                        "match_method": item["match_method"],
                    }),
                    f"Mapped to master category {item['master_category_id']}",
                    item["confidence"],
                    OPENROUTER_MODEL,
                ))
                count += 1
            conn.commit()
        return count
    except Exception as e:
        conn.rollback()
        print(f"[category_mapper] DB error in add_to_review_queue: {e}", flush=True)
        return 0
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_category_mapping() -> dict:
    """
    Main entry point. Two-pass category mapping.
    Returns stats dict.
    """
    start = time.time()
    print("=== Category Mapper starting ===", flush=True)

    # 1. Load master taxonomy
    master_cats = get_master_categories()
    if not master_cats:
        print("[category_mapper] ERROR: No master categories found — run migration 016 first", flush=True)
        return {"error": "no_master_categories", "rule_matched": 0, "ai_matched": 0,
                "review_queue": 0, "total": 0}

    print(f"[category_mapper] Master taxonomy: {len(master_cats)} categories", flush=True)

    # 2. Load unmapped sources
    sources = get_unmapped_sources()
    total_unmapped = sum(len(v) for v in sources.values())
    print(f"[category_mapper] Unmapped sources: "
          f"shop={len(sources['shop'])}, portal={len(sources['portal'])}, "
          f"google={len(sources['google'])} (total={total_unmapped})", flush=True)

    if total_unmapped == 0:
        print("[category_mapper] All categories already mapped — nothing to do", flush=True)
        return {"rule_matched": 0, "ai_matched": 0, "review_queue": 0, "total": 0}

    # 3. Rule-based pass
    print("[category_mapper] --- Rule-based pass ---", flush=True)
    rule_mappings = rule_based_pass(master_cats, sources)
    print(f"[category_mapper] Rule-based: {len(rule_mappings)} matched", flush=True)

    # Save rule-based mappings
    if rule_mappings:
        upserted = upsert_mappings(rule_mappings)
        print(f"[category_mapper] Rule-based upserted: {upserted}", flush=True)

    # 4. Build set of matched IDs per source type
    matched_ids: dict[str, set] = {"shop": set(), "portal": set(), "google": set()}
    for m in rule_mappings:
        matched_ids[m["source_type"]].add(m["source_id"])

    # 5. Filter unmatched for AI pass
    unmatched: dict[str, list] = {}
    for source_type, cats in sources.items():
        unmatched[source_type] = [
            c for c in cats if c["id"] not in matched_ids[source_type]
        ]
    total_unmatched = sum(len(v) for v in unmatched.values())
    print(f"[category_mapper] Remaining for AI pass: {total_unmatched}", flush=True)

    # 6. AI pass (Gemini Flash)
    ai_mappings: list[dict] = []
    review_items: list[dict] = []

    if total_unmatched > 0:
        print(f"[category_mapper] --- AI pass ({OPENROUTER_MODEL}) ---", flush=True)
        ai_mappings = ai_pass(master_cats, unmatched)
        print(f"[category_mapper] AI: {len(ai_mappings)} matched", flush=True)

        # Split into confident vs review-needed
        confident = [m for m in ai_mappings if m["confidence"] >= AI_REVIEW_THRESHOLD]
        review_items = [m for m in ai_mappings if m["confidence"] < AI_REVIEW_THRESHOLD]

        # Save confident AI mappings
        if confident:
            upserted = upsert_mappings(confident)
            print(f"[category_mapper] AI confident upserted: {upserted}", flush=True)

        # Queue low-confidence for review
        if review_items:
            queued = add_to_review_queue(review_items)
            print(f"[category_mapper] Queued for review: {queued}", flush=True)

            # Still save them with low confidence (can be overridden later)
            upserted = upsert_mappings(review_items)
            print(f"[category_mapper] AI low-confidence upserted: {upserted}", flush=True)

    # 7. Stats
    elapsed = time.time() - start
    stats = {
        "rule_matched": len(rule_mappings),
        "ai_matched": len(ai_mappings),
        "review_queue": len(review_items),
        "total": len(rule_mappings) + len(ai_mappings),
        "duration_s": round(elapsed, 1),
    }

    print(f"\n{'=' * 60}", flush=True)
    print(f"Category Mapper DONE in {elapsed:.1f}s", flush=True)
    print(f"  Rule-based: {stats['rule_matched']}", flush=True)
    print(f"  AI (OpenRouter): {stats['ai_matched']}", flush=True)
    print(f"  Review queue: {stats['review_queue']}", flush=True)
    print(f"  Total mapped: {stats['total']}", flush=True)

    langfuse_flush()
    return stats


def main():
    """CLI entry point."""
    run_category_mapping()


if __name__ == "__main__":
    main()
