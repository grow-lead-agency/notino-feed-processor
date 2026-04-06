"""
Blog/Article Scraper — extracts blog posts from url_discovery_queue.

Dequeues URLs with url_type='blog' from url_discovery_queue, scrapes each
page via Crawl4AI (LLM extraction), and populates shop_blog_articles.
Extracts: title, author, published date, excerpt, topics, brand mentions,
product links, sentiment.

Pipeline: url_discovery_queue (blog) → Crawl4AI → shop_blog_articles

Cron: every 6h, batch 200 URLs per run (blogs are low-priority enrichment).
Engine: Crawl4AI primary, plain HTTP fallback for basic meta extraction.

Usage:
    python jobs/blog_scraper.py                # batch 200
    python jobs/blog_scraper.py --limit 500    # bigger batch
    python jobs/blog_scraper.py --dry-run      # no DB writes
"""
import argparse
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import structlog

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.crawl4ai_client import crawl4ai_scrape, crawl4ai_health
from jobs.langfuse_wrapper import flush

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_BATCH = 200
RATE_LIMIT_DELAY = 0.3

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; FeedProcessor/2.0; +https://growlead.cz/bot)",
})
SESSION.timeout = 20

BLOG_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Article title / headline"},
        "author": {"type": "string", "description": "Author name if visible"},
        "published_date": {"type": "string", "description": "Publish date in ISO format (YYYY-MM-DD) if visible"},
        "category": {"type": "string", "description": "Article category/section if visible"},
        "excerpt": {"type": "string", "description": "First 2-3 sentences of the article text"},
        "word_count_estimate": {"type": "integer", "description": "Rough word count of the article body"},
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Main topics covered (e.g. skincare, fragrance, haircare, makeup, wellness)"
        },
        "brand_mentions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Brand names mentioned in the article"
        },
        "product_links": {
            "type": "array",
            "items": {"type": "string"},
            "description": "URLs linking to product pages within the same shop"
        },
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative", "mixed"],
            "description": "Overall tone of the article"
        },
    },
    "required": ["title"],
}

BLOG_PROMPT = """Extract article/blog post information from this page on a beauty e-commerce shop.
Focus on: title, author, publish date, a short excerpt (first 2-3 sentences), estimated word count,
main beauty topics (skincare, fragrance, haircare, makeup, etc.), brand names mentioned,
internal product page links, and overall sentiment (positive/neutral/negative/mixed).
Return JSON matching the schema. Omit fields that aren't visible on the page."""


# ---------------------------------------------------------------------------
# Dequeue
# ---------------------------------------------------------------------------
def dequeue_blog_urls(batch_size: int) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dequeue_discovery_urls('blog'::url_type, %s, 'blog_scraper')",
                (batch_size,)
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Plain HTTP fallback
# ---------------------------------------------------------------------------
def extract_meta_blog(url: str) -> dict | None:
    """Fallback: basic blog metadata from HTML meta tags."""
    try:
        resp = SESSION.get(url)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        title_tag = soup.find("title")
        h1 = soup.find("h1")
        meta_desc = soup.find("meta", attrs={"name": "description"})
        og_image = soup.find("meta", attrs={"property": "og:image"})

        # Article published time
        pub_time = soup.find("meta", attrs={"property": "article:published_time"})
        mod_time = soup.find("meta", attrs={"property": "article:modified_time"})

        # Author
        author_meta = soup.find("meta", attrs={"name": "author"})

        title = None
        if h1:
            title = h1.get_text(strip=True)
        elif title_tag:
            title = title_tag.get_text(strip=True).split("|")[0].split("-")[0].strip()

        if not title:
            return None

        # Rough word count from article/main content
        article = soup.find("article") or soup.find("main") or soup.find("div", class_=re.compile(r"(content|article|post|entry)"))
        word_count = len(article.get_text().split()) if article else None

        return {
            "title": title,
            "author": author_meta["content"] if author_meta and author_meta.get("content") else None,
            "published_date": pub_time["content"][:10] if pub_time and pub_time.get("content") else None,
            "excerpt": meta_desc["content"][:500] if meta_desc and meta_desc.get("content") else None,
            "word_count_estimate": word_count,
            "hero_image_url": og_image["content"] if og_image and og_image.get("content") else None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Save to DB
# ---------------------------------------------------------------------------
def parse_date(date_str: str | None) -> datetime | None:
    """Try to parse various date formats."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S%z", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def detect_language(url: str, title: str | None) -> str | None:
    """Simple language detection from URL path."""
    lower = url.lower()
    if "/cs/" in lower or "/cz/" in lower or ".cz/" in lower:
        return "cs"
    if "/sk/" in lower or ".sk/" in lower:
        return "sk"
    if "/de/" in lower or ".de/" in lower:
        return "de"
    if "/fr/" in lower or ".fr/" in lower:
        return "fr"
    if "/it/" in lower or ".it/" in lower:
        return "it"
    if "/es/" in lower or ".es/" in lower:
        return "es"
    if "/nl/" in lower or ".nl/" in lower:
        return "nl"
    if "/pl/" in lower or ".pl/" in lower:
        return "pl"
    if "/hu/" in lower or ".hu/" in lower:
        return "hu"
    if "/en/" in lower or ".co.uk/" in lower or ".com/" in lower:
        return "en"
    return None


def save_blog_article(queue_row: dict, data: dict) -> int | None:
    shop_id = queue_row["shop_id"]
    url = queue_row["url"]
    queue_id = queue_row["id"]

    title = (data.get("title") or "").strip()
    if not title:
        _mark_queue(queue_id, "skipped", "no_title")
        return None

    published_at = parse_date(data.get("published_date"))
    language = detect_language(url, title)
    slug_match = re.search(r'/([^/]+?)(?:\.\w+)?$', url)
    slug = slug_match.group(1) if slug_match else None

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shop_blog_articles (
                    shop_id, url, slug, title, author, published_at,
                    category_raw, tags, excerpt, word_count, language,
                    hero_image_url, product_urls, brand_mentions, topics,
                    sentiment, content_extracted_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, NOW()
                )
                ON CONFLICT (shop_id, url) DO UPDATE SET
                    title = EXCLUDED.title,
                    author = EXCLUDED.author,
                    published_at = COALESCE(EXCLUDED.published_at, shop_blog_articles.published_at),
                    excerpt = EXCLUDED.excerpt,
                    word_count = EXCLUDED.word_count,
                    topics = EXCLUDED.topics,
                    brand_mentions = EXCLUDED.brand_mentions,
                    product_urls = EXCLUDED.product_urls,
                    sentiment = EXCLUDED.sentiment,
                    content_extracted_at = NOW()
                RETURNING id
            """, (
                shop_id, url, slug, title, data.get("author"), published_at,
                data.get("category"),
                data.get("tags") or None,
                (data.get("excerpt") or "")[:500] or None,
                data.get("word_count_estimate"),
                language,
                data.get("hero_image_url"),
                data.get("product_links") or None,
                data.get("brand_mentions") or None,
                data.get("topics") or None,
                data.get("sentiment"),
            ))
            entity_id = cur.fetchone()[0]

            cur.execute("""
                UPDATE url_discovery_queue
                SET status = 'done', processed_at = NOW(),
                    result_entity_type = 'shop_blog_article', result_entity_id = %s
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
    logger.info("blog_scraper_start", batch_size=batch_size, dry_run=dry_run)

    urls = dequeue_blog_urls(batch_size)
    logger.info("dequeued", count=len(urls))

    use_crawl4ai = crawl4ai_health()
    stats = {"processed": 0, "saved": 0, "skipped": 0, "errors": 0}

    for row in urls:
        url = row["url"]
        try:
            data = None

            if use_crawl4ai:
                data = crawl4ai_scrape(url, schema=BLOG_SCHEMA, prompt=BLOG_PROMPT, timeout=60)

            if not data:
                data = extract_meta_blog(url)

            if not data or not data.get("title"):
                stats["skipped"] += 1
                if not dry_run:
                    _mark_queue(row["id"], "skipped", "no_data_extracted")
                continue

            if dry_run:
                logger.info("dry_run", url=url[:80], title=data.get("title", "")[:60],
                           topics=data.get("topics"), brands=data.get("brand_mentions"))
                stats["processed"] += 1
                continue

            entity_id = save_blog_article(row, data)
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
    logger.info("blog_scraper_done", duration_s=round(elapsed, 1), **stats)
    flush()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blog/Article Scraper")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(batch_size=args.limit, dry_run=args.dry_run)
