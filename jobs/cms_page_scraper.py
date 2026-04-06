"""
CMS/Static Page Scraper — extracts CMS pages from url_discovery_queue.

Dequeues URLs with url_type='page' from url_discovery_queue, classifies
page type (about, shipping, returns, legal, faq, contact, etc.), extracts
basic metadata, and routes deep extraction to existing domain scrapers.

Pipeline:
  url_discovery_queue (page) → classify → shop_pages (metadata)
                                       → shipping_scraper (if shipping)
                                       → legal_scraper (if legal/imprint)

This scraper does SHALLOW extraction only (title, meta, h1, word count,
page type classification). Deep extraction is delegated to existing
specialized scrapers that already handle shipping_zones, legal_entities etc.

Cron: every 4h, batch 200 URLs per run.
Engine: plain HTTP (no LLM needed for shallow meta extraction).

Usage:
    python jobs/cms_page_scraper.py              # batch 200
    python jobs/cms_page_scraper.py --limit 500  # bigger batch
    python jobs/cms_page_scraper.py --dry-run    # no DB writes
"""
import argparse
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.langfuse_wrapper import flush

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_BATCH = 200
RATE_LIMIT_DELAY = 0.2

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; FeedProcessor/2.0; +https://growlead.cz/bot)",
})
SESSION.timeout = 15

# Page type classification patterns (url + title keywords)
PAGE_TYPE_PATTERNS = {
    "shipping": (
        r"(shipping|doprav|versand|livraison|spedizion|envio|lieferung|doruč|zasielanie|delivery|postage)",
        0.90,
    ),
    "returns": (
        r"(return|vracen|reklamac|umtausch|rückgabe|retour|reso|devoluci|retur|vrátenie)",
        0.90,
    ),
    "legal": (
        r"(imprint|impressum|legal|pravni|podmink|obchodni[\-_\s]podm|agb|datenschutz|privacy|gdpr|cookie|zasady)",
        0.85,
    ),
    "faq": (
        r"(faq|otazk|pomoc|help|häufig|aide|ayuda|domande[\-_\s]frequent)",
        0.85,
    ),
    "contact": (
        r"(contact|kontakt|spojeni|erreichen|nous[\-_\s]contacter|contatto|contacto)",
        0.85,
    ),
    "about": (
        r"(about|o[\-_\s]nas|über[\-_\s]uns|ueber[\-_\s]uns|chi[\-_\s]siamo|sobre[\-_\s]nosotros|over[\-_\s]ons|our[\-_\s]story|pribehy)",
        0.80,
    ),
    "loyalty": (
        r"(loyalty|vernostni|bonus|treueprogramm|fidélit|punti|program[\-_\s]vernost|body|rewards)",
        0.80,
    ),
    "careers": (
        r"(career|kariér|jobs|stellenangebot|offres[\-_\s]emploi|lavora|trabajo|vacatur)",
        0.75,
    ),
    "press": (
        r"(press|tiskove|presse|stampa|prensa|nieuws|media[\-_\s]room)",
        0.75,
    ),
    "sustainability": (
        r"(sustain|udržiteln|nachhaltigkeit|développement[\-_\s]durable|sostenibil|responsab)",
        0.75,
    ),
}

# Mapping page_type → existing domain table for deep extraction
ROUTING_MAP = {
    "shipping": "shipping_zones",
    "returns": "shop_returns_policies",
    "legal": "legal_entities",
    "about": "shop_profiles",
    "loyalty": "shop_loyalty_programs",
}


# ---------------------------------------------------------------------------
# Dequeue
# ---------------------------------------------------------------------------
def dequeue_page_urls(batch_size: int) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dequeue_discovery_urls('page'::url_type, %s, 'cms_page_scraper')",
                (batch_size,)
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Page type classification
# ---------------------------------------------------------------------------
def classify_page_type(url: str, title: str | None = None, h1: str | None = None) -> tuple[str, float]:
    """Classify CMS page type from URL + title + H1. Returns (page_type, confidence)."""
    text = f"{url} {title or ''} {h1 or ''}".lower()

    best_type = "other"
    best_conf = 0.0

    for ptype, (pattern, base_conf) in PAGE_TYPE_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            # Boost confidence if match is in URL (more reliable)
            conf = base_conf
            if re.search(pattern, url.lower()):
                conf = min(conf + 0.05, 0.99)
            if conf > best_conf:
                best_type = ptype
                best_conf = conf

    return best_type, best_conf


# ---------------------------------------------------------------------------
# Extract basic page metadata
# ---------------------------------------------------------------------------
def extract_page_meta(url: str) -> dict | None:
    """Extract shallow metadata from a CMS page via plain HTTP."""
    try:
        resp = SESSION.get(url)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        title_tag = soup.find("title")
        h1 = soup.find("h1")
        meta_desc = soup.find("meta", attrs={"name": "description"})
        canonical = soup.find("link", attrs={"rel": "canonical"})
        lang = soup.find("html")

        title = title_tag.get_text(strip=True) if title_tag else None
        h1_text = h1.get_text(strip=True) if h1 else None

        # Word count from main content
        main = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile(r"(content|main|page)"))
        word_count = len(main.get_text().split()) if main else None

        # Language from <html lang="...">
        language = None
        if lang and lang.get("lang"):
            language = lang["lang"][:2].lower()

        return {
            "title": title,
            "h1": h1_text,
            "meta_description": meta_desc["content"][:500] if meta_desc and meta_desc.get("content") else None,
            "canonical_url": canonical["href"] if canonical and canonical.get("href") else None,
            "language": language,
            "word_count": word_count,
        }
    except Exception as e:
        logger.debug("extract_failed", url=url[:80], error=str(e)[:100])
        return None


# ---------------------------------------------------------------------------
# Save to DB
# ---------------------------------------------------------------------------
def save_cms_page(queue_row: dict, data: dict, page_type: str) -> int | None:
    shop_id = queue_row["shop_id"]
    url = queue_row["url"]
    queue_id = queue_row["id"]

    routing = ROUTING_MAP.get(page_type)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shop_pages (
                    shop_id, url, page_type, title, meta_description,
                    h1, canonical_url, language, word_count,
                    sitemap_lastmod, content_extracted_at,
                    extraction_routed_to
                ) VALUES (
                    %s, %s, %s::cms_page_type, %s, %s,
                    %s, %s, %s, %s,
                    %s, NOW(),
                    %s
                )
                ON CONFLICT (shop_id, url) DO UPDATE SET
                    page_type = EXCLUDED.page_type,
                    title = EXCLUDED.title,
                    meta_description = EXCLUDED.meta_description,
                    h1 = EXCLUDED.h1,
                    canonical_url = EXCLUDED.canonical_url,
                    language = EXCLUDED.language,
                    word_count = EXCLUDED.word_count,
                    content_extracted_at = NOW(),
                    extraction_routed_to = EXCLUDED.extraction_routed_to
                RETURNING id
            """, (
                shop_id, url, page_type,
                data.get("title"), data.get("meta_description"),
                data.get("h1"), data.get("canonical_url"),
                data.get("language"), data.get("word_count"),
                queue_row.get("lastmod"),
                routing,
            ))
            entity_id = cur.fetchone()[0]

            cur.execute("""
                UPDATE url_discovery_queue
                SET status = 'done', processed_at = NOW(),
                    result_entity_type = 'shop_page', result_entity_id = %s
                WHERE id = %s
            """, (entity_id, queue_id))

            conn.commit()
            return entity_id
    finally:
        put_conn(conn)


def _mark_queue(queue_id: int, status: str, error: str | None = None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE url_discovery_queue
                SET status = %s, processed_at = NOW(), last_error = %s
                WHERE id = %s
            """, (status, error, queue_id))
        conn.commit()
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(batch_size: int = DEFAULT_BATCH, dry_run: bool = False):
    start = time.time()
    logger.info("cms_page_scraper_start", batch_size=batch_size, dry_run=dry_run)

    urls = dequeue_page_urls(batch_size)
    logger.info("dequeued", count=len(urls))

    stats = {"processed": 0, "saved": 0, "skipped": 0, "errors": 0, "by_type": {}}

    for row in urls:
        url = row["url"]
        try:
            data = extract_page_meta(url)

            if not data or not data.get("title"):
                stats["skipped"] += 1
                if not dry_run:
                    _mark_queue(row["id"], "skipped", "no_meta_extracted")
                continue

            # Classify page type
            page_type, confidence = classify_page_type(
                url, data.get("title"), data.get("h1")
            )
            stats["by_type"][page_type] = stats["by_type"].get(page_type, 0) + 1

            if dry_run:
                logger.info("dry_run", url=url[:80], page_type=page_type,
                           confidence=confidence, title=(data.get("title") or "")[:60])
                stats["processed"] += 1
                continue

            entity_id = save_cms_page(row, data, page_type)
            if entity_id:
                stats["saved"] += 1
            else:
                stats["skipped"] += 1

            stats["processed"] += 1
            time.sleep(RATE_LIMIT_DELAY)

        except Exception as e:
            stats["errors"] += 1
            logger.error("error", url=url[:80], error=str(e)[:200])
            if not dry_run:
                _mark_queue(row["id"], "error", str(e)[:500])

    elapsed = time.time() - start
    logger.info("cms_page_scraper_done", duration_s=round(elapsed, 1), **stats)
    flush()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CMS/Static Page Scraper")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(batch_size=args.limit, dry_run=args.dry_run)
