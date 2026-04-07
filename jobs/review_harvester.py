"""
Job: Review Harvester
Cil: sbira zakaznicke recenze ze review sites a uklada do tabulky `reviews`.
Zdroje: Trustpilot, Heureka.cz, Zbozi.cz, product page JSON-LD, Google Reviews (placeholder).

Workflow:
  1. SELECT shops with review_sites config OR top shops without recent reviews
  2. Per shop → scrape Trustpilot, Heureka (CZ/SK), product page JSON-LD reviews
  3. Normalize ratings to 1-5 scale, detect language, clean text
  4. Dedup on (shop_id + source + reviewer_hash), upsert into `reviews`
  5. Optionally update `product_review_sources` aggregate stats

Cron: weekly — batch ~20 shops per run (Firecrawl budget control).
Standalone: `python jobs/review_harvester.py` — runs batch 20 shops.

Notes:
  - Rate limit: max 1 req/s (Trustpilot TOS, Heureka fair use)
  - Max 100 reviews per shop per source (avoid scraping entire history)
  - Language detection: simple heuristic (diacritics, common words)
  - reviews table: product_id nullable (shop-level reviews vs product reviews)
"""
import hashlib
import os
import re
import sys
import time
import json

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.langfuse_wrapper import traced_generation


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()

MAX_SHOPS_PER_RUN = 20
MAX_REVIEWS_PER_SOURCE = 100
FIRECRAWL_TIMEOUT_SECONDS = 60
FIRECRAWL_MAX_RETRIES = 2
REQUEST_DELAY_SECONDS = 1.0  # rate limit between requests


# ---------------------------------------------------------------------------
# Firecrawl extraction schema for reviews
# ---------------------------------------------------------------------------
REVIEW_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "total_review_count": {
            "type": "integer",
            "description": "Total number of reviews for this business/product.",
        },
        "average_rating": {
            "type": "number",
            "description": "Overall average rating (in the site's native scale).",
        },
        "rating_scale_max": {
            "type": "number",
            "description": "Maximum value of the rating scale (e.g. 5, 10, 100). Default 5.",
        },
        "rating_distribution": {
            "type": "object",
            "description": "Rating distribution: keys are star counts (1-5), values are counts.",
            "properties": {
                "1": {"type": "integer"},
                "2": {"type": "integer"},
                "3": {"type": "integer"},
                "4": {"type": "integer"},
                "5": {"type": "integer"},
            },
        },
        "reviews": {
            "type": "array",
            "description": "Individual customer reviews.",
            "items": {
                "type": "object",
                "properties": {
                    "rating": {
                        "type": "number",
                        "description": "Rating given by the reviewer (in site's native scale).",
                    },
                    "review_text": {
                        "type": "string",
                        "description": "Full review text/body.",
                    },
                    "reviewer_name": {
                        "type": "string",
                        "description": "Name of the reviewer (or 'Anonymous').",
                    },
                    "review_date": {
                        "type": "string",
                        "description": "Date of the review (any format, ISO preferred).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Review headline/title if any.",
                    },
                    "pros": {
                        "type": "string",
                        "description": "Pros / positives if structured.",
                    },
                    "cons": {
                        "type": "string",
                        "description": "Cons / negatives if structured.",
                    },
                    "verified_buyer": {
                        "type": "boolean",
                        "description": "Whether the reviewer is a verified buyer.",
                    },
                },
            },
        },
    },
}

REVIEW_EXTRACTION_PROMPT = (
    "Extract customer reviews from this page. "
    "For each review extract: rating (in the site's native scale), review text/body, "
    "reviewer name (or 'Anonymous'), review date, title/headline if any, "
    "pros and cons if structured, and whether the reviewer is a verified buyer. "
    "Also extract: total review count, average rating, rating scale maximum "
    "(5 for 1-5 stars, 10 for 1-10, 100 for percentage), "
    "and rating distribution (count per star level 1-5). "
    "Return up to 100 reviews, most recent first."
)


# ---------------------------------------------------------------------------
# Firecrawl helpers
# ---------------------------------------------------------------------------
def firecrawl_scrape(url: str) -> dict | None:
    """Call Firecrawl /v1/scrape with review extraction schema. Traced via Langfuse."""
    if not FIRECRAWL_API_KEY:
        print("[reviews] FATAL: FIRECRAWL_API_KEY missing", flush=True)
        return None

    payload = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "schema": REVIEW_EXTRACTION_SCHEMA,
            "prompt": REVIEW_EXTRACTION_PROMPT,
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

    with traced_generation(
        name="firecrawl-reviews",
        model="firecrawl/extract",
        input_data={"url": url, "prompt": REVIEW_EXTRACTION_PROMPT},
        metadata={"job": "review_harvester"},
        tags=["feed-processor", "firecrawl", "reviews"],
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
                print(f"[reviews] Network error for {url}: {e}", flush=True)
                gen.end(level="ERROR", status_message=f"Network error: {e}")
                return None

            if resp.status_code == 429:
                if attempt < FIRECRAWL_MAX_RETRIES:
                    wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
                    time.sleep(min(wait, 30))
                    continue
                print(f"[reviews] Rate limited on {url}", flush=True)
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


def _head_check(url: str) -> bool:
    """Quick HEAD check to see if URL is reachable (avoid wasting Firecrawl credits)."""
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
            timeout=10,
            allow_redirects=True,
        )
        return resp.status_code < 400
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_rating(raw_rating, scale_max=5) -> float | None:
    """Normalize any rating to 1-5 scale."""
    if raw_rating is None:
        return None
    try:
        rating = float(raw_rating)
    except (TypeError, ValueError):
        return None
    if rating <= 0:
        return None

    try:
        scale = float(scale_max)
    except (TypeError, ValueError):
        scale = 5.0
    if scale <= 0:
        scale = 5.0

    # Normalize to 1-5
    if scale == 5:
        normalized = rating
    elif scale == 10:
        normalized = rating / 2.0
    elif scale == 100:
        normalized = rating / 20.0
    else:
        normalized = (rating / scale) * 5.0

    return round(max(1.0, min(5.0, normalized)), 2)


def detect_language(text: str) -> str | None:
    """Simple heuristic language detection from review text."""
    if not text or len(text) < 10:
        return None

    text_lower = text.lower()

    # Czech indicators
    cz_patterns = ["doporučuji", "nedoporučuji", "kvalita", "dodání", "objednávk",
                   "zboží", "velmi", "špatn", "skvěl", "spokojen", "nespokoje",
                   "doprava", "cena", "výrob", "balení"]
    cz_score = sum(1 for p in cz_patterns if p in text_lower)
    # Czech diacritics
    cz_score += sum(1 for c in text if c in "áčďéěíňóřšťúůýž")

    # Slovak indicators
    sk_patterns = ["odporúčam", "neodporúčam", "tovar", "dodanie", "kvalita",
                   "spokojn", "nespokojn", "veľmi", "výborn"]
    sk_score = sum(1 for p in sk_patterns if p in text_lower)
    sk_score += sum(1 for c in text if c in "ĺľŕôä")

    # German indicators
    de_patterns = ["sehr", "empfehle", "qualität", "lieferung", "preis",
                   "bestellung", "zufrieden", "schlecht", "ausgezeichnet",
                   "schnell", "würde"]
    de_score = sum(1 for p in de_patterns if p in text_lower)
    de_score += sum(1 for c in text if c in "äöüß")

    # Polish indicators
    pl_patterns = ["polecam", "jakość", "dostawa", "zamówien", "bardzo",
                   "produkt", "szybk", "zadowol"]
    pl_score = sum(1 for p in pl_patterns if p in text_lower)
    pl_score += sum(1 for c in text if c in "ąćęłńóśźż")

    # English indicators
    en_patterns = ["recommend", "quality", "delivery", "shipping", "product",
                   "great", "terrible", "excellent", "good", "bad", "purchase"]
    en_score = sum(1 for p in en_patterns if p in text_lower)

    # French indicators
    fr_patterns = ["recommande", "qualité", "livraison", "produit", "très",
                   "excellent", "commande"]
    fr_score = sum(1 for p in fr_patterns if p in text_lower)
    fr_score += sum(1 for c in text if c in "àâçèéêëîïôùûü")

    scores = {
        "cs": cz_score, "sk": sk_score, "de": de_score,
        "pl": pl_score, "en": en_score, "fr": fr_score,
    }
    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best
    return None


def reviewer_hash(reviewer_name: str | None, review_text: str | None) -> str:
    """Create a stable hash for dedup. Combines reviewer name + first 200 chars of text."""
    parts = [
        (reviewer_name or "anonymous").strip().lower(),
        (review_text or "")[:200].strip().lower(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalize_review(raw: dict, source: str, scale_max=5) -> dict:
    """Normalize a single raw review dict into our DB format."""
    rating = normalize_rating(raw.get("rating"), scale_max)
    text = (raw.get("review_text") or "").strip() or None
    reviewer = (raw.get("reviewer_name") or "Anonymous").strip()
    title = (raw.get("title") or "").strip() or None
    pros = (raw.get("pros") or "").strip() or None
    cons = (raw.get("cons") or "").strip() or None

    # Combine title + text + pros/cons into review_text
    parts = []
    if title:
        parts.append(title)
    if text:
        parts.append(text)
    if pros:
        parts.append(f"Pros: {pros}")
    if cons:
        parts.append(f"Cons: {cons}")
    full_text = "\n".join(parts) if parts else None

    lang = detect_language(full_text)
    rhash = reviewer_hash(reviewer, full_text)

    return {
        "rating": rating,
        "review_text": full_text,
        "reviewer_name": reviewer,
        "source": source,
        "language": lang,
        "reviewer_hash": rhash,
    }


# ---------------------------------------------------------------------------
# Source harvesters
# ---------------------------------------------------------------------------

def harvest_trustpilot(shop_id: int, domain: str) -> list[dict]:
    """Scrape Trustpilot review page for a domain."""
    # Clean domain — remove protocol, trailing path
    clean = domain.lower().strip().rstrip("/")
    clean = re.sub(r"^https?://", "", clean)
    clean = re.sub(r"^www\.", "", clean)
    clean = clean.split("/")[0]

    url = f"https://www.trustpilot.com/review/{clean}"
    print(f"  [trustpilot] Scraping {url}", flush=True)

    if not _head_check(url):
        print(f"  [trustpilot] Page not found for {clean}", flush=True)
        return []

    time.sleep(REQUEST_DELAY_SECONDS)

    data = firecrawl_scrape(url)
    if not data:
        print(f"  [trustpilot] No data extracted for {clean}", flush=True)
        return []

    scale_max = data.get("rating_scale_max", 5)
    raw_reviews = data.get("reviews") or []
    if not isinstance(raw_reviews, list):
        raw_reviews = []

    reviews = []
    for raw in raw_reviews[:MAX_REVIEWS_PER_SOURCE]:
        if not isinstance(raw, dict):
            continue
        r = normalize_review(raw, "trustpilot", scale_max)
        r["source_url"] = url
        reviews.append(r)

    total = data.get("total_review_count")
    avg = data.get("average_rating")
    print(f"  [trustpilot] Extracted {len(reviews)} reviews "
          f"(total: {total}, avg: {avg})", flush=True)
    return reviews


def harvest_heureka(shop_id: int, domain: str) -> list[dict]:
    """Scrape Heureka.cz review page for a domain."""
    clean = domain.lower().strip().rstrip("/")
    clean = re.sub(r"^https?://", "", clean)
    clean = re.sub(r"^www\.", "", clean)
    clean = clean.split("/")[0]

    # Heureka uses domain slug without TLD typically
    # e.g. notino.cz → notino-cz or notino
    slug_candidates = [
        clean.replace(".", "-"),  # notino.cz → notino-cz
        clean.split(".")[0],       # notino.cz → notino
        clean,                     # full domain as-is
    ]

    for slug in slug_candidates:
        url = f"https://obchody.heureka.cz/{slug}/recenze"
        print(f"  [heureka] Trying {url}", flush=True)

        if not _head_check(url):
            continue

        time.sleep(REQUEST_DELAY_SECONDS)

        data = firecrawl_scrape(url)
        if not data:
            continue

        raw_reviews = data.get("reviews") or []
        if not isinstance(raw_reviews, list) or len(raw_reviews) == 0:
            continue

        scale_max = data.get("rating_scale_max", 5)
        reviews = []
        for raw in raw_reviews[:MAX_REVIEWS_PER_SOURCE]:
            if not isinstance(raw, dict):
                continue
            r = normalize_review(raw, "heureka", scale_max)
            r["source_url"] = url
            reviews.append(r)

        total = data.get("total_review_count")
        avg = data.get("average_rating")
        print(f"  [heureka] Extracted {len(reviews)} reviews "
              f"(total: {total}, avg: {avg})", flush=True)
        return reviews

    print(f"  [heureka] No reviews found for {clean}", flush=True)
    return []


def harvest_zbozi(shop_id: int, domain: str) -> list[dict]:
    """Scrape Zbozi.cz review page for a domain."""
    clean = domain.lower().strip().rstrip("/")
    clean = re.sub(r"^https?://", "", clean)
    clean = re.sub(r"^www\.", "", clean)
    clean = clean.split("/")[0]

    slug = clean.replace(".", "-")
    url = f"https://www.zbozi.cz/obchod/{slug}/hodnoceni/"

    print(f"  [zbozi] Trying {url}", flush=True)

    if not _head_check(url):
        print(f"  [zbozi] Page not found for {clean}", flush=True)
        return []

    time.sleep(REQUEST_DELAY_SECONDS)

    data = firecrawl_scrape(url)
    if not data:
        print(f"  [zbozi] No data extracted for {clean}", flush=True)
        return []

    scale_max = data.get("rating_scale_max", 5)
    raw_reviews = data.get("reviews") or []
    if not isinstance(raw_reviews, list):
        raw_reviews = []

    reviews = []
    for raw in raw_reviews[:MAX_REVIEWS_PER_SOURCE]:
        if not isinstance(raw, dict):
            continue
        r = normalize_review(raw, "zbozi", scale_max)
        r["source_url"] = url
        reviews.append(r)

    total = data.get("total_review_count")
    avg = data.get("average_rating")
    print(f"  [zbozi] Extracted {len(reviews)} reviews "
          f"(total: {total}, avg: {avg})", flush=True)
    return reviews


def harvest_google_reviews(shop_id: int, domain: str) -> list[dict]:
    """Google Places API reviews — placeholder, needs API key + place_id lookup."""
    # Google Places API requires:
    # 1. API key with Places API enabled
    # 2. place_id lookup from domain/business name
    # 3. Place Details request with reviews field
    # Not implemented yet — returns empty list.
    print(f"  [google] Skipping — Google Reviews not yet implemented", flush=True)
    return []


def harvest_product_reviews(shop_id: int) -> list[dict]:
    """
    Parse JSON-LD Review/AggregateRating from existing product pages.
    Reads product_offers for this shop and checks for cached JSON-LD data.
    This is a secondary enrichment — does NOT make external requests.
    """
    conn = get_conn()
    reviews = []
    try:
        with conn.cursor() as cur:
            # Look for products with JSON-LD review data in raw_offer_data
            cur.execute(
                """
                SELECT po.product_id, po.product_url, po.raw_offer_data
                FROM product_offers po
                WHERE po.shop_id = %s
                  AND po.raw_offer_data IS NOT NULL
                  AND po.is_active = TRUE
                LIMIT 500
                """,
                (shop_id,),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    for product_id, product_url, raw_data in rows:
        if not raw_data:
            continue
        try:
            if isinstance(raw_data, str):
                data = json.loads(raw_data)
            else:
                data = raw_data
        except (json.JSONDecodeError, TypeError):
            continue

        # Look for Review or AggregateRating in JSON-LD
        _extract_jsonld_reviews(reviews, data, product_id, product_url, shop_id)

        if len(reviews) >= MAX_REVIEWS_PER_SOURCE:
            break

    print(f"  [jsonld] Extracted {len(reviews)} product reviews from cached data", flush=True)
    return reviews


def _extract_jsonld_reviews(
    reviews: list, data: dict | list, product_id: int,
    product_url: str, shop_id: int,
) -> None:
    """Recursively extract Review objects from JSON-LD data."""
    if isinstance(data, list):
        for item in data:
            _extract_jsonld_reviews(reviews, item, product_id, product_url, shop_id)
        return

    if not isinstance(data, dict):
        return

    obj_type = data.get("@type", "")

    # AggregateRating — store as shop-level stat, not individual reviews
    if obj_type == "Review" or (isinstance(obj_type, list) and "Review" in obj_type):
        rating_val = data.get("reviewRating", {})
        if isinstance(rating_val, dict):
            raw_rating = rating_val.get("ratingValue")
            best = rating_val.get("bestRating", 5)
        else:
            raw_rating = rating_val
            best = 5

        r = normalize_review({
            "rating": raw_rating,
            "review_text": data.get("reviewBody") or data.get("description") or "",
            "reviewer_name": _get_author(data),
            "review_date": data.get("datePublished") or data.get("dateCreated"),
            "title": data.get("name"),
        }, "jsonld_product", best)
        r["product_id"] = product_id
        r["source_url"] = product_url
        reviews.append(r)
        return

    # Check nested review/reviews fields
    for key in ("review", "reviews"):
        nested = data.get(key)
        if nested:
            if isinstance(nested, list):
                for item in nested:
                    _extract_jsonld_reviews(reviews, item, product_id, product_url, shop_id)
            elif isinstance(nested, dict):
                _extract_jsonld_reviews(reviews, nested, product_id, product_url, shop_id)


def _get_author(review_data: dict) -> str:
    """Extract author name from JSON-LD Review."""
    author = review_data.get("author")
    if isinstance(author, dict):
        return author.get("name", "Anonymous")
    if isinstance(author, str):
        return author
    return "Anonymous"


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def get_shops_to_harvest(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """
    Select shops for review harvesting.
    Priority: shops with most offers (biggest), not harvested recently.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.name, s.url, s.country_id
                FROM shops s
                WHERE s.deleted_at IS NULL
                  AND s.url IS NOT NULL
                  AND (
                    s.id NOT IN (
                      SELECT DISTINCT shop_id FROM reviews
                      WHERE shop_id IS NOT NULL
                        AND scraped_at > NOW() - INTERVAL '7 days'
                    )
                  )
                ORDER BY
                    (s.offers_checked_at IS NOT NULL) DESC,
                    s.id
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


def upsert_reviews(shop_id: int, reviews: list[dict]) -> int:
    """
    Batch upsert reviews into DB. Dedup on shop_id + source + reviewer_hash.
    Returns count of actually inserted reviews.
    """
    if not reviews:
        return 0

    conn = get_conn()
    inserted = 0
    try:
        with conn.cursor() as cur:
            # Load existing hashes for this shop to deduplicate
            cur.execute(
                """
                SELECT source, md5(COALESCE(reviewer_name,'') || '|' || LEFT(COALESCE(review_text,''),200))
                FROM reviews
                WHERE shop_id = %s
                """,
                (shop_id,),
            )
            existing_keys = set()
            for source, hash_val in cur.fetchall():
                existing_keys.add((source, hash_val))

            for r in reviews:
                # Build the same hash the DB would compute for dedup check
                dedup_key = (
                    r["source"],
                    hashlib.md5(
                        ((r.get("reviewer_name") or "") + "|" +
                         (r.get("review_text") or "")[:200]).encode("utf-8")
                    ).hexdigest(),
                )
                if dedup_key in existing_keys:
                    continue

                product_id = r.get("product_id")

                try:
                    cur.execute(
                        """
                        INSERT INTO reviews (
                            product_id, shop_id, rating, review_text,
                            reviewer_name, source, language, scraped_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            product_id, shop_id, r.get("rating"),
                            r.get("review_text"), r.get("reviewer_name"),
                            r["source"], r.get("language"),
                        ),
                    )
                    inserted += 1
                    existing_keys.add(dedup_key)
                except Exception as e:
                    print(f"  [db] Insert error: {e}", flush=True)
                    conn.rollback()
                    continue

            conn.commit()
    finally:
        put_conn(conn)

    return inserted


def update_review_aggregate(shop_id: int, source: str) -> None:
    """Update aggregate rating stats in review_sites / product_review_sources if applicable."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Compute aggregate for this shop + source
            cur.execute(
                """
                SELECT COUNT(*), ROUND(AVG(rating), 2)
                FROM reviews
                WHERE shop_id = %s AND source = %s AND rating IS NOT NULL
                """,
                (shop_id, source),
            )
            count, avg_rating = cur.fetchone()
            if count and count > 0:
                cur.execute(
                    """
                    UPDATE reviews SET rating_count = %s
                    WHERE shop_id = %s AND source = %s AND rating_count IS DISTINCT FROM %s
                    """,
                    (count, shop_id, source, count),
                )
            conn.commit()
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main harvest orchestrator
# ---------------------------------------------------------------------------

def harvest_reviews(
    shop_id: int, domain: str, shop_url: str, config_override: dict | None = None,
) -> int:
    """
    Main entry point. Harvest reviews for one shop from all available sources.
    Returns total reviews saved.
    """
    print(f"[{domain}] Starting review harvest...", flush=True)

    all_reviews: list[dict] = []
    sources_tried = []

    # 1. Trustpilot (universal, works for any domain)
    try:
        tp_reviews = harvest_trustpilot(shop_id, domain)
        all_reviews.extend(tp_reviews)
        sources_tried.append(f"trustpilot:{len(tp_reviews)}")
    except Exception as e:
        print(f"  [trustpilot] Error: {e}", flush=True)

    time.sleep(REQUEST_DELAY_SECONDS)

    # 2. Heureka (CZ/SK shops)
    # Check if domain is CZ/SK
    domain_lower = domain.lower()
    is_cz_sk = any(domain_lower.endswith(tld) for tld in (".cz", ".sk"))
    if is_cz_sk:
        try:
            hk_reviews = harvest_heureka(shop_id, domain)
            all_reviews.extend(hk_reviews)
            sources_tried.append(f"heureka:{len(hk_reviews)}")
        except Exception as e:
            print(f"  [heureka] Error: {e}", flush=True)

        time.sleep(REQUEST_DELAY_SECONDS)

        # 3. Zbozi.cz (CZ only)
        if domain_lower.endswith(".cz"):
            try:
                zb_reviews = harvest_zbozi(shop_id, domain)
                all_reviews.extend(zb_reviews)
                sources_tried.append(f"zbozi:{len(zb_reviews)}")
            except Exception as e:
                print(f"  [zbozi] Error: {e}", flush=True)

    # 4. Product page JSON-LD reviews (no external requests — uses cached data)
    try:
        jl_reviews = harvest_product_reviews(shop_id)
        all_reviews.extend(jl_reviews)
        sources_tried.append(f"jsonld:{len(jl_reviews)}")
    except Exception as e:
        print(f"  [jsonld] Error: {e}", flush=True)

    # 5. Google Reviews (placeholder)
    # harvest_google_reviews(shop_id, domain)

    if not all_reviews:
        print(f"[{domain}] No reviews found from any source", flush=True)
        return 0

    # Upsert all reviews
    saved = upsert_reviews(shop_id, all_reviews)

    # Update aggregates per source
    sources_used = set(r["source"] for r in all_reviews)
    for source in sources_used:
        try:
            update_review_aggregate(shop_id, source)
        except Exception as e:
            print(f"  [aggregate] Error updating {source}: {e}", flush=True)

    print(f"[{domain}] Saved {saved}/{len(all_reviews)} reviews "
          f"({', '.join(sources_tried)})", flush=True)
    return saved


# ---------------------------------------------------------------------------
# Queue worker entry point
# ---------------------------------------------------------------------------

def run_review_harvest(job: dict) -> dict:
    """
    Entry point for queue_worker routing.
    job keys: shop_id, domain, shop_url (or feed_url as fallback)
    Returns result dict compatible with queue_worker.process_job.
    """
    shop_id = job["shop_id"]
    domain = job["domain"]
    shop_url = job.get("shop_url") or job.get("feed_url", "")

    saved = harvest_reviews(shop_id, domain, shop_url)
    return {"saved": saved, "domain": domain, "scrape_type": "review_harvest"}


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    start_ts = time.time()
    print(f"=== Review Harvester starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    if not FIRECRAWL_API_KEY:
        print("[reviews] WARNING: FIRECRAWL_API_KEY missing — only JSON-LD parsing will work", flush=True)

    shops = get_shops_to_harvest()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run)", flush=True)

    if not shops:
        print("No shops to harvest reviews from. Done.", flush=True)
        return

    total_saved = 0
    shops_processed = 0

    for shop_row in shops:
        shop_id, name, url, _country_id = shop_row
        if not url:
            continue

        try:
            saved = harvest_reviews(shop_id, name, url)
            total_saved += saved
            shops_processed += 1
        except Exception as e:
            print(f"[{name}] Fatal error: {e}", flush=True)

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {total_saved} reviews saved | "
        f"{shops_processed} shops processed | {elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
