"""
Job: Shop Page Discovery
Scan shop homepage and find all important pages (shipping, legal, returns, about, contact, FAQ).

Flow:
  1. Fetch homepage HTML (Crawl4AI → plain HTTP fallback)
  2. Parse all <a> tags, score by keyword matching
  3. Save discovered URLs to shops table
  4. Shipping/legal scrapers then use discovered URLs directly — no more brute-force guessing

Cron: every 6h, batch 100. Re-discovery after 90 days.
"""
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

sys.path.insert(0, "/app")
from jobs.db import get_conn, put_conn
from jobs.crawl4ai_client import crawl4ai_scrape_html, crawl4ai_health


MAX_SHOPS_PER_RUN = 100

# Page type → keywords (link text + URL path matching)
PAGE_KEYWORDS = {
    "shipping": [
        "doprava", "doruceni", "doručení", "zásilk", "přeprava",
        "shipping", "delivery", "postage", "dispatch",
        "versand", "lieferung", "versandkosten",
        "livraison", "expédition",
        "spedizione", "consegna",
        "envío", "envio", "entrega",
        "verzending", "bezorging",
        "dostawa", "wysyłka", "wysylka",
        "frakt", "leverans",
        "szállítás", "szallitas",
        "livrare", "доставка", "dostava",
    ],
    "legal": [
        "impressum", "imprint", "legal notice",
        "o nás", "o nas", "o společnosti", "provozovatel",
        "über uns", "about us", "company info",
        "mentions légales", "mentions legales",
        "chi siamo", "note legali",
        "aviso legal", "quiénes somos",
        "over ons",
        "rólunk", "impresszum",
    ],
    "returns": [
        "vrácení", "vraceni", "reklamace", "odstoupení",
        "returns", "return policy", "refund",
        "rückgabe", "widerruf", "retoure", "umtausch",
        "retour", "remboursement",
        "reso", "rimborso",
        "devolución", "devoluciones",
        "retourneren",
        "zwrot", "reklamacja",
        "retur", "ångerrätt",
    ],
    "about": [
        "o nás", "o nas", "o firmě",
        "about us", "about", "who we are", "our story",
        "über uns", "unser team",
        "qui sommes", "à propos",
        "chi siamo",
        "sobre nosotros", "quiénes somos",
        "over ons", "wie zijn wij",
        "o nas", "kim jesteśmy",
        "om oss",
    ],
    "contact": [
        "kontakt", "kontakty", "napište nám",
        "contact", "contact us", "get in touch",
        "kontakt", "kontaktiere",
        "contactez", "nous contacter",
        "contatti", "contattaci",
        "contacto", "contáctenos",
        "kontakt", "kontaktformular",
        "kapcsolat", "kontakta oss",
    ],
    "faq": [
        "časté dotazy", "faq", "otázky",
        "faq", "frequently asked", "help center", "help centre",
        "häufige fragen", "hilfe",
        "foire aux questions", "aide",
        "domande frequenti",
        "preguntas frecuentes",
        "veelgestelde vragen",
        "pytania", "pomoc",
    ],
}

# Negative signals — never match these URLs
NEGATIVE_PATTERNS = [
    "privacy", "datenschutz", "cookie", "gdpr",
    "login", "account", "register", "signup",
    "cart", "checkout", "basket", "warenkorb",
    "product", "produkt", "artikel",
    "category", "kategori", "collection",
    "blog", "news", "magazine", "artikel",
    ".jpg", ".png", ".pdf", ".css", ".js", ".svg",
    "facebook.com", "instagram.com", "twitter.com", "youtube.com", "linkedin.com",
]


def _score_link(href: str, link_text: str, page_type: str) -> int:
    """Score a link for a given page type. Higher = better match."""
    keywords = PAGE_KEYWORDS.get(page_type, [])
    path_lower = urlparse(href).path.lower()
    text_lower = link_text.lower()

    score = 0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            score += 3
        if kw_lower in path_lower:
            score += 2

    # Negative signals
    for neg in NEGATIVE_PATTERNS:
        if neg in path_lower or neg in text_lower:
            score -= 10

    return score


def discover_pages(html: str, base_url: str) -> dict[str, str | None]:
    """
    Parse HTML, find best URL for each page type.
    Returns {"shipping": "https://...", "legal": "https://...", ...}
    """
    base_domain = urlparse(base_url).netloc

    link_pattern = re.compile(
        r'<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    # Collect all internal links
    links: list[tuple[str, str]] = []  # (abs_url, link_text)
    seen = set()

    for match in link_pattern.finditer(html):
        href_raw = match.group(1).strip()
        link_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()

        abs_url = urljoin(base_url, href_raw)
        parsed = urlparse(abs_url)

        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc and parsed.netloc != base_domain:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)

        links.append((abs_url, link_text))

    # Score each link for each page type, pick the best
    results = {}
    for page_type in PAGE_KEYWORDS:
        best_score = 0
        best_url = None

        for url, text in links:
            score = _score_link(url, text, page_type)
            if score > best_score:
                best_score = score
                best_url = url

        results[page_type] = best_url if best_score >= 2 else None

    return results


# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------
def get_shops_to_discover(limit: int = MAX_SHOPS_PER_RUN) -> list[tuple]:
    """Load shops needing discovery. Undiscovered first, then stale (>90 days)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url
                FROM shops
                WHERE deleted_at IS NULL
                  AND url IS NOT NULL
                  AND (
                      discovery_checked_at IS NULL
                      OR discovery_checked_at < NOW() - INTERVAL '90 days'
                  )
                ORDER BY
                    discovery_checked_at IS NULL DESC,
                    discovery_checked_at ASC NULLS FIRST
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        put_conn(conn)


def save_discovery(shop_id: int, pages: dict[str, str | None], engine: str, links_found: int) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE shops SET
                    discovered_shipping_url = %s,
                    discovered_legal_url = %s,
                    discovered_returns_url = %s,
                    discovered_about_url = %s,
                    discovered_contact_url = %s,
                    discovered_faq_url = %s,
                    discovery_checked_at = NOW(),
                    discovery_links_found = %s,
                    discovery_engine = %s
                WHERE id = %s AND deleted_at IS NULL
                """,
                (
                    pages.get("shipping"),
                    pages.get("legal"),
                    pages.get("returns"),
                    pages.get("about"),
                    pages.get("contact"),
                    pages.get("faq"),
                    links_found,
                    engine,
                    shop_id,
                ),
            )
            conn.commit()
    finally:
        put_conn(conn)


# ----------------------------------------------------------------------------
# Per-shop
# ----------------------------------------------------------------------------
def process_shop(shop_id: int, name: str, url: str) -> int:
    """Discover pages for one shop. Returns count of pages found."""
    base = url.rstrip("/")
    engine = "unknown"

    # Try Crawl4AI first (renders JS)
    html = crawl4ai_scrape_html(base, timeout=30)
    if html:
        engine = "crawl4ai"
    else:
        # Fallback: plain HTTP
        try:
            resp = requests.get(
                base,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=15,
                allow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                html = resp.text
                engine = "http"
        except requests.RequestException:
            pass

    if not html:
        print(f"  [{name}] Could not fetch homepage", flush=True)
        save_discovery(shop_id, {}, engine="failed", links_found=0)
        return 0

    pages = discover_pages(html, base)
    found = sum(1 for v in pages.values() if v)

    save_discovery(shop_id, pages, engine=engine, links_found=found)

    if found:
        found_list = [f"{k}={v.split('/')[-1]}" for k, v in pages.items() if v]
        print(f"  [{name}] Found {found} pages: {', '.join(found_list)}", flush=True)
    else:
        print(f"  [{name}] No pages found", flush=True)

    return found


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
def main() -> None:
    start_ts = time.time()
    print(f"=== Shop Discovery starting at {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)

    crawl4ai_up = crawl4ai_health()
    print(f"Crawl4AI: {'UP' if crawl4ai_up else 'DOWN (HTTP fallback only)'}", flush=True)

    shops = get_shops_to_discover()
    print(f"Processing {len(shops)} shops (max {MAX_SHOPS_PER_RUN}/run)", flush=True)

    if not shops:
        print("No shops to discover. Done.", flush=True)
        return

    total_found = 0
    for shop_id, name, url in shops:
        try:
            total_found += process_shop(shop_id, name, url)
        except Exception as e:
            print(f"  [{name}] Error: {e}", flush=True)

    elapsed = time.time() - start_ts
    print(
        f"=== Done: {len(shops)} shops scanned, {total_found} pages discovered | {elapsed:.1f}s ===",
        flush=True,
    )


if __name__ == "__main__":
    main()
