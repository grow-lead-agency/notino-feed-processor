"""
Job: Keyword CPC Enrichment + SERP Analysis
Enriches seo_keywords with search volume, CPC, intent, SERP features.

Sources:
  1. Google Ads Keyword Planner API (primary, TODO: needs setup)
  2. Bright Data SERP API (SERP features)
  3. Rule-based intent classification (multilingual, free)

Interval: manual / scheduled (not in crontab yet)
Rate limiting: max 1 req/s for Bright Data SERP API.
"""
import os
import sys
import json
import time
import re

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BRIGHT_DATA_SERP_URL = "https://api.brightdata.com/serp/req"
BRIGHT_DATA_TOKEN = os.environ.get("BRIGHT_DATA_SERP_TOKEN", "")

# Country → default currency for CPC
COUNTRY_CURRENCY = {
    "CZ": "CZK",
    "SK": "EUR",
    "DE": "EUR",
    "PL": "PLN",
    "HU": "HUF",
    "AT": "EUR",
}

# Country → Google search domain
COUNTRY_GOOGLE_DOMAIN = {
    "CZ": "google.cz",
    "SK": "google.sk",
    "DE": "google.de",
    "PL": "google.pl",
    "HU": "google.hu",
    "AT": "google.at",
}

# Country → Google gl parameter
COUNTRY_GL = {
    "CZ": "cz",
    "SK": "sk",
    "DE": "de",
    "PL": "pl",
    "HU": "hu",
    "AT": "at",
}

# Country → primary language hl parameter
COUNTRY_HL = {
    "CZ": "cs",
    "SK": "sk",
    "DE": "de",
    "PL": "pl",
    "HU": "hu",
    "AT": "de",
}

# ---------------------------------------------------------------------------
# Intent classification — multilingual keyword signals
# ---------------------------------------------------------------------------
INTENT_SIGNALS = {
    "transactional": {
        "cs": ["koupit", "objednat", "cena", "levne", "sleva", "akce", "e-shop",
               "eshop", "obchod", "nakup", "doprava zdarma", "skladem"],
        "sk": ["kupit", "objednat", "cena", "lacno", "zlava", "akcia",
               "eshop", "obchod", "nakup", "doprava zadarmo", "skladom"],
        "de": ["kaufen", "bestellen", "preis", "gunstig", "rabatt", "angebot",
               "shop", "versandkostenfrei", "lieferbar", "auf lager"],
        "en": ["buy", "order", "price", "cheap", "discount", "deal", "shop",
               "free shipping", "in stock", "add to cart"],
        "pl": ["kupic", "zamowic", "cena", "tanio", "promocja", "sklep",
               "darmowa dostawa", "dostepny", "w magazynie"],
        "hu": ["vasarlas", "megrendeles", "ar", "olcso", "akcio", "kedvezmeny",
               "bolt", "ingyenes szallitas", "keszleten"],
    },
    "commercial": {
        "cs": ["srovnani", "porovnani", "recenze", "test", "vs", "nejlepsi",
               "top", "ranking", "hodnoceni", "zkusenosti", "alternativa"],
        "sk": ["porovnanie", "recenzia", "test", "vs", "najlepsi",
               "top", "ranking", "hodnotenie", "skusenosti", "alternativa"],
        "de": ["vergleich", "test", "bewertung", "erfahrung", "vs", "beste",
               "top", "ranking", "alternative", "rezension"],
        "en": ["comparison", "review", "test", "vs", "best", "top",
               "ranking", "alternative", "rating", "worth it"],
        "pl": ["porownanie", "recenzja", "test", "vs", "najlepszy",
               "top", "ranking", "opinia", "alternatywa"],
        "hu": ["osszehasonlitas", "velemeny", "teszt", "vs", "legjobb",
               "top", "ranking", "ertekeles", "alternativa"],
    },
    "informational": {
        "cs": ["jak", "co je", "proc", "navod", "slozeni", "pouziti",
               "ucinky", "ingredience", "vyznam", "historie", "typ", "druhy"],
        "sk": ["ako", "co je", "preco", "navod", "zlozenie", "pouzitie",
               "ucinky", "ingrediencie", "vyznam"],
        "de": ["wie", "was ist", "warum", "anleitung", "inhaltsstoffe",
               "anwendung", "wirkung", "bedeutung", "geschichte"],
        "en": ["how", "what is", "why", "guide", "ingredients", "tutorial",
               "benefits", "meaning", "history", "types", "difference"],
        "pl": ["jak", "co to", "dlaczego", "poradnik", "sklad", "stosowanie",
               "dzialanie", "znaczenie", "historia"],
        "hu": ["hogyan", "mi az", "miert", "utmutato", "osszetevo",
               "hasznalat", "hatas", "jelentese", "tortenete"],
    },
    "navigational": {
        "cs": ["notino", "parfums", "elnino", "pilulka", "drogerie", "dm",
               "rossmann", "douglas", "sephora", "marionnaud", "krasa"],
        "sk": ["notino", "parfums", "elnino", "pilulka", "dm", "rossmann",
               "douglas", "sephora", "marionnaud", "heureka"],
        "de": ["notino", "douglas", "parfumdreams", "flaconi", "sephora",
               "dm", "rossmann", "mueller", "idealo"],
        "en": ["notino", "sephora", "ulta", "lookfantastic", "boots",
               "superdrug", "douglas", "beautybay"],
        "pl": ["notino", "douglas", "sephora", "rossmann", "hebe",
               "superpharm", "drogeria", "ceneo"],
        "hu": ["notino", "douglas", "sephora", "rossmann", "dm",
               "muller", "parfumdreams"],
    },
}

# Flatten to lang → intent → set(words) for fast lookup
_INTENT_LOOKUP = {}
for intent, langs in INTENT_SIGNALS.items():
    for lang, words in langs.items():
        if lang not in _INTENT_LOOKUP:
            _INTENT_LOOKUP[lang] = {}
        _INTENT_LOOKUP[lang][intent] = set(words)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_country_iso2(country_id: int) -> str | None:
    """Resolve country_id → iso2 code (e.g. 1 → 'CZ')."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT iso2 FROM countries WHERE id = %s", (country_id,))
            row = cur.fetchone()
            return row[0].strip() if row else None
    finally:
        put_conn(conn)


def get_language_code(language_id: int) -> str | None:
    """Resolve language_id → 2-letter code (e.g. 1 → 'cs')."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM languages WHERE id = %s", (language_id,))
            row = cur.fetchone()
            return row[0].strip() if row else None
    finally:
        put_conn(conn)


def _load_country_id_map() -> dict:
    """Load all country iso2 → id mappings. Used for batch lookups."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, iso2 FROM countries")
            return {row[1].strip(): row[0] for row in cur.fetchall()}
    finally:
        put_conn(conn)


def get_unenriched_keywords(country_iso2: str, limit: int = 500) -> list[dict]:
    """
    Fetch keywords where search_volume IS NULL for given country.
    Returns list of dicts with id, keyword, language_id, language_code, country_id.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sk.id, sk.keyword, sk.language_id, l.code, sk.country_id
                FROM seo_keywords sk
                JOIN languages l ON l.id = sk.language_id
                JOIN countries c ON c.id = sk.country_id
                WHERE sk.search_volume IS NULL
                  AND c.iso2 = %s
                ORDER BY sk.created_at ASC
                LIMIT %s
            """, (country_iso2, limit))
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "keyword": r[1],
                    "language_id": r[2],
                    "language_code": r[3].strip(),
                    "country_id": r[4],
                }
                for r in rows
            ]
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Intent classification (rule-based, multilingual, free)
# ---------------------------------------------------------------------------

def classify_intent(keyword: str, language: str) -> str:
    """
    Rule-based intent classification.
    Returns: transactional / commercial / informational / navigational.
    Falls back to 'informational' if no signal matches.

    Args:
        keyword: raw keyword string
        language: 2-letter language code (cs, sk, de, en, pl, hu)
    """
    kw_lower = keyword.lower()
    lang = language.lower().strip()

    # Priority: transactional > commercial > navigational > informational
    priority = ["transactional", "commercial", "navigational", "informational"]

    lang_signals = _INTENT_LOOKUP.get(lang, {})
    if not lang_signals:
        # Fallback: try all languages
        for fallback_lang in _INTENT_LOOKUP:
            lang_signals.update(_INTENT_LOOKUP[fallback_lang])

    for intent in priority:
        signals = lang_signals.get(intent, set())
        for signal in signals:
            # Match whole word or at word boundary
            if re.search(r'(?:^|\s)' + re.escape(signal) + r'(?:\s|$)', kw_lower):
                return intent

    return "informational"


# ---------------------------------------------------------------------------
# Source 1: Google Ads Keyword Planner API (PLACEHOLDER)
# ---------------------------------------------------------------------------

def enrich_from_keyword_planner(keywords: list[dict], country_iso2: str) -> dict:
    """
    Google Ads Keyword Planner API — batch up to 1000 keywords per request.

    PLACEHOLDER: Requires Google Ads developer token + customer ID.
    When configured, returns dict keyed by keyword_id with:
      - search_volume (int)
      - cpc_avg (float)
      - cpc_max (float)
      - competition (float 0-1)
      - competition_level (str LOW/MEDIUM/HIGH)

    Returns empty dict until API is configured.
    """
    # Check required env vars
    google_ads_token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    google_ads_customer_id = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")

    if not google_ads_token or not google_ads_customer_id:
        print("[KeywordEnricher] Google Ads Keyword Planner API not configured "
              "(missing GOOGLE_ADS_DEVELOPER_TOKEN / GOOGLE_ADS_CUSTOMER_ID). "
              "Skipping Keyword Planner enrichment.", flush=True)
        return {}

    # TODO: Implement Google Ads API call when credentials are available
    # google-ads-python library: GoogleAdsClient → KeywordPlanIdeaService
    # Batch: max 1000 keywords per GenerateKeywordIdeasRequest
    # Response: keyword_idea.keyword_idea_metrics.avg_monthly_searches, etc.
    print(f"[KeywordEnricher] Google Ads API configured but not yet implemented. "
          f"Would process {len(keywords)} keywords for {country_iso2}.", flush=True)
    return {}


# ---------------------------------------------------------------------------
# Source 2: Bright Data SERP API
# ---------------------------------------------------------------------------

def enrich_from_bright_data_serp(keywords: list[dict], country_iso2: str) -> dict:
    """
    Bright Data SERP API — extract SERP features, top competitors, MSV estimates.
    Rate limited to 1 req/s. Returns dict keyed by keyword_id.

    Cost: ~$5 per 1,000 queries.

    Returns dict: {keyword_id: {serp_features: [...], top_competitors: [...], search_volume_est: int}}
    """
    if not BRIGHT_DATA_TOKEN:
        print("[KeywordEnricher] Bright Data SERP API not configured "
              "(missing BRIGHT_DATA_SERP_TOKEN). Skipping SERP enrichment.", flush=True)
        return {}

    import requests

    results = {}
    gl = COUNTRY_GL.get(country_iso2, "cz")
    hl = COUNTRY_HL.get(country_iso2, "cs")
    domain = COUNTRY_GOOGLE_DOMAIN.get(country_iso2, "google.com")

    total = len(keywords)
    success = 0
    errors = 0

    for i, kw in enumerate(keywords, 1):
        keyword_id = kw["id"]
        keyword_text = kw["keyword"]

        try:
            resp = requests.post(
                BRIGHT_DATA_SERP_URL,
                headers={
                    "Authorization": f"Bearer {BRIGHT_DATA_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": keyword_text,
                    "search_engine": "google",
                    "country": gl,
                    "language": hl,
                    "domain": domain,
                    "num_results": 10,
                    "parse": True,
                },
                timeout=30,
            )

            if resp.status_code != 200:
                errors += 1
                if errors <= 3:
                    print(f"[KeywordEnricher] BD SERP error for '{keyword_text}': "
                          f"HTTP {resp.status_code}", flush=True)
                continue

            data = resp.json()
            serp_features = _extract_serp_features(data)
            top_competitors = _extract_top_competitors(data)
            msv_estimate = _estimate_msv_from_serp(data)

            results[keyword_id] = {
                "serp_features": serp_features,
                "top_competitors": top_competitors,
                "search_volume_est": msv_estimate,
            }
            success += 1

        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"[KeywordEnricher] BD SERP exception for '{keyword_text}': {e}", flush=True)

        # Rate limit: 1 req/s
        if i < total:
            time.sleep(1.0)

        # Progress every 50 keywords
        if i % 50 == 0:
            print(f"[KeywordEnricher] BD SERP progress: {i}/{total} "
                  f"(ok: {success}, err: {errors})", flush=True)

    print(f"[KeywordEnricher] BD SERP done: {success}/{total} enriched, "
          f"{errors} errors", flush=True)
    return results


def _extract_serp_features(serp_data: dict) -> list[str]:
    """Extract detected SERP features from Bright Data SERP response."""
    features = []

    # Common SERP feature keys in BD response
    feature_checks = {
        "ai_overview": ["ai_overview", "ai_overviews", "sge"],
        "shopping_ads": ["shopping", "shopping_ads", "product_listing"],
        "featured_snippet": ["featured_snippet", "answer_box"],
        "local_pack": ["local_pack", "local_results", "maps"],
        "people_also_ask": ["people_also_ask", "related_questions"],
        "knowledge_panel": ["knowledge_graph", "knowledge_panel"],
        "image_pack": ["image_results", "images"],
        "video_results": ["video_results", "videos"],
        "news_results": ["news_results", "top_stories"],
        "sitelinks": ["sitelinks"],
        "reviews": ["reviews", "review_rating"],
    }

    for feature_name, keys in feature_checks.items():
        for key in keys:
            if key in serp_data and serp_data[key]:
                features.append(feature_name)
                break

    return features


def _extract_top_competitors(serp_data: dict) -> list[str]:
    """Extract top 10 organic result domains from SERP response."""
    competitors = []

    organic = serp_data.get("organic", serp_data.get("organic_results", []))
    if not isinstance(organic, list):
        return competitors

    for result in organic[:10]:
        domain = result.get("domain", result.get("displayed_link", ""))
        if domain and domain not in competitors:
            competitors.append(domain)

    return competitors


def _estimate_msv_from_serp(serp_data: dict) -> int | None:
    """
    Estimate monthly search volume from SERP signals.
    Returns rough estimate or None if not determinable.

    Heuristic based on result count + ad presence:
    - 1B+ results + shopping ads → high volume (10K+)
    - 100M+ results → medium (1K-10K)
    - 10M+ results → low-medium (100-1K)
    - < 10M results → low (<100)
    """
    total_results = serp_data.get("total_results", serp_data.get("search_information", {}).get("total_results", 0))
    has_ads = bool(serp_data.get("shopping", serp_data.get("ads", [])))

    if not total_results:
        return None

    if isinstance(total_results, str):
        total_results = int(re.sub(r"[^\d]", "", total_results) or 0)

    if total_results >= 1_000_000_000 and has_ads:
        return 10000
    elif total_results >= 100_000_000:
        return 5000 if has_ads else 1000
    elif total_results >= 10_000_000:
        return 500 if has_ads else 100
    else:
        return 50


# ---------------------------------------------------------------------------
# Merge + DB update
# ---------------------------------------------------------------------------

def merge_and_update(keywords: list[dict], kp_data: dict, serp_data: dict) -> dict:
    """
    Merge all enrichment data and batch-update seo_keywords + keyword_cpc_estimates.
    Returns stats dict.
    """
    stats = {
        "intent_classified": 0,
        "seo_keywords_updated": 0,
        "cpc_estimates_upserted": 0,
        "errors": 0,
    }

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for kw in keywords:
                keyword_id = kw["id"]
                keyword_text = kw["keyword"]
                language_code = kw["language_code"]
                country_id = kw["country_id"]

                # --- Merge enrichment sources ---
                intent = kw.get("intent", "informational")
                search_volume = None
                cpc_avg = None
                cpc_max = None
                difficulty = None
                competition = None
                competition_level = None
                serp_features = None
                top_competitors = None

                # Keyword Planner data (if available)
                kp = kp_data.get(keyword_id, {})
                if kp:
                    search_volume = kp.get("search_volume")
                    cpc_avg = kp.get("cpc_avg")
                    cpc_max = kp.get("cpc_max")
                    competition = kp.get("competition")
                    competition_level = kp.get("competition_level")

                # Bright Data SERP data (if available)
                bd = serp_data.get(keyword_id, {})
                if bd:
                    serp_features = bd.get("serp_features")
                    top_competitors = bd.get("top_competitors")
                    # Use BD MSV estimate only if KP didn't provide one
                    if search_volume is None:
                        search_volume = bd.get("search_volume_est")

                # If no API data at all, still set search_volume = 0 so we don't
                # re-process this keyword (mark as "enriched with no data")
                if search_volume is None and not kp and not bd:
                    search_volume = 0

                # --- Update seo_keywords ---
                try:
                    cur.execute("""
                        UPDATE seo_keywords SET
                            search_volume = COALESCE(%s, search_volume),
                            cpc = COALESCE(%s, cpc),
                            difficulty = COALESCE(%s, difficulty),
                            competition = COALESCE(%s, competition),
                            intent = COALESCE(%s, intent),
                            serp_features = COALESCE(%s, serp_features),
                            top_competitors = COALESCE(%s, top_competitors),
                            measured_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        search_volume,
                        cpc_avg,
                        difficulty,
                        competition,
                        intent,
                        serp_features,
                        top_competitors,
                        keyword_id,
                    ))
                    stats["seo_keywords_updated"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    print(f"[KeywordEnricher] Error updating seo_keywords id={keyword_id}: {e}", flush=True)

                # --- Upsert keyword_cpc_estimates (historical tracking) ---
                if cpc_avg is not None or search_volume is not None:
                    try:
                        # Resolve country iso2 for currency lookup
                        cur.execute("SELECT iso2 FROM countries WHERE id = %s", (country_id,))
                        iso2_row = cur.fetchone()
                        country_iso2 = iso2_row[0].strip() if iso2_row else "CZ"
                        currency = COUNTRY_CURRENCY.get(country_iso2, "EUR")

                        cur.execute("""
                            INSERT INTO keyword_cpc_estimates (
                                keyword, country_id, avg_cpc, min_cpc, max_cpc,
                                currency, monthly_search_volume,
                                competition_level, competition_index,
                                data_source, fetched_at
                            ) VALUES (
                                %s, %s, COALESCE(%s, 0), %s, %s,
                                %s, %s,
                                %s, %s,
                                %s, NOW()
                            )
                            ON CONFLICT (keyword, country_id, device, ad_format)
                            DO UPDATE SET
                                avg_cpc = COALESCE(EXCLUDED.avg_cpc, keyword_cpc_estimates.avg_cpc),
                                min_cpc = COALESCE(EXCLUDED.min_cpc, keyword_cpc_estimates.min_cpc),
                                max_cpc = COALESCE(EXCLUDED.max_cpc, keyword_cpc_estimates.max_cpc),
                                monthly_search_volume = COALESCE(EXCLUDED.monthly_search_volume, keyword_cpc_estimates.monthly_search_volume),
                                competition_level = COALESCE(EXCLUDED.competition_level, keyword_cpc_estimates.competition_level),
                                competition_index = COALESCE(EXCLUDED.competition_index, keyword_cpc_estimates.competition_index),
                                data_source = EXCLUDED.data_source,
                                fetched_at = NOW(),
                                updated_at = NOW()
                        """, (
                            keyword_text, country_id, cpc_avg, None, cpc_max,
                            currency, search_volume,
                            competition_level, competition,
                            "keyword_enricher",
                        ))
                        stats["cpc_estimates_upserted"] += 1
                    except Exception as e:
                        stats["errors"] += 1
                        if stats["errors"] <= 5:
                            print(f"[KeywordEnricher] Error upserting CPC estimate "
                                  f"for '{keyword_text}': {e}", flush=True)

            conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"[KeywordEnricher] Transaction error: {e}", flush=True)
        stats["errors"] += 1
    finally:
        put_conn(conn)

    return stats


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_keyword_enrichment(batch_size: int = 100, country: str = "CZ") -> dict:
    """
    Main entry point. Processes un-enriched keywords for given country.

    Args:
        batch_size: max keywords to process in one run
        country: 2-letter country code (CZ, SK, DE, PL, HU, AT)

    Returns:
        Stats dict with counts of processed/enriched/errors.
    """
    country = country.upper().strip()
    print(f"\n{'='*60}", flush=True)
    print(f"[KeywordEnricher] Starting enrichment for country={country}, "
          f"batch_size={batch_size}", flush=True)
    start_ts = time.time()

    # 1. Fetch un-enriched keywords
    keywords = get_unenriched_keywords(country, limit=batch_size)
    if not keywords:
        print(f"[KeywordEnricher] No un-enriched keywords found for {country}. Done.", flush=True)
        return {"country": country, "total": 0, "message": "no keywords to enrich"}

    print(f"[KeywordEnricher] Found {len(keywords)} un-enriched keywords", flush=True)

    # 2. Intent classification (free, instant — always runs)
    intent_count = 0
    for kw in keywords:
        kw["intent"] = classify_intent(kw["keyword"], kw["language_code"])
        intent_count += 1

    intent_dist = {}
    for kw in keywords:
        i = kw["intent"]
        intent_dist[i] = intent_dist.get(i, 0) + 1
    print(f"[KeywordEnricher] Intent classified: {intent_count} keywords "
          f"({intent_dist})", flush=True)

    # 3. Google Ads Keyword Planner (free with Ads account — placeholder)
    kp_data = enrich_from_keyword_planner(keywords, country)

    # 4. Bright Data SERP API (paid, ~$5/1K — limited batch)
    #    Only send first `batch_size` keywords (cost control)
    bd_batch = keywords[:batch_size]
    serp_data = enrich_from_bright_data_serp(bd_batch, country)

    # 5. Merge all sources + write to DB
    stats = merge_and_update(keywords, kp_data, serp_data)
    stats["country"] = country
    stats["total_keywords"] = len(keywords)
    stats["intent_classified"] = intent_count

    elapsed = time.time() - start_ts
    stats["duration_s"] = round(elapsed, 1)

    print(f"\n[KeywordEnricher] DONE in {elapsed:.1f}s", flush=True)
    print(f"  Keywords processed: {len(keywords)}", flush=True)
    print(f"  Intent classified:  {intent_count}", flush=True)
    print(f"  seo_keywords updated: {stats['seo_keywords_updated']}", flush=True)
    print(f"  CPC estimates upserted: {stats['cpc_estimates_upserted']}", flush=True)
    print(f"  Errors: {stats['errors']}", flush=True)
    print(f"{'='*60}\n", flush=True)

    return stats


# ---------------------------------------------------------------------------
# Standalone execution
# ---------------------------------------------------------------------------

def main():
    """
    Run keyword enrichment for all 6 target markets.
    Usage: python jobs/keyword_enricher.py [country] [batch_size]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Keyword CPC Enrichment Worker")
    parser.add_argument("--country", "-c", default=None,
                        help="Country code (CZ/SK/DE/PL/HU/AT). Default: all 6.")
    parser.add_argument("--batch-size", "-b", type=int, default=100,
                        help="Max keywords per country (default: 100)")
    args = parser.parse_args()

    print(f"=== Keyword Enricher starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    countries = [args.country.upper()] if args.country else ["CZ", "SK", "DE", "PL", "HU", "AT"]
    all_stats = []

    for c in countries:
        stats = run_keyword_enrichment(batch_size=args.batch_size, country=c)
        all_stats.append(stats)

    # Summary
    total_kw = sum(s.get("total_keywords", 0) for s in all_stats)
    total_updated = sum(s.get("seo_keywords_updated", 0) for s in all_stats)
    total_cpc = sum(s.get("cpc_estimates_upserted", 0) for s in all_stats)
    total_errors = sum(s.get("errors", 0) for s in all_stats)

    print(f"\n{'='*60}", flush=True)
    print(f"SUMMARY — {len(countries)} countries", flush=True)
    print(f"  Keywords processed: {total_kw}", flush=True)
    print(f"  seo_keywords updated: {total_updated}", flush=True)
    print(f"  CPC estimates upserted: {total_cpc}", flush=True)
    print(f"  Errors: {total_errors}", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    main()
