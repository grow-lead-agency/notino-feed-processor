"""
Image Downloader — download product images to Cloudflare R2.

Reads: images table (status='indexed')
Downloads image from source_url → compute SHA256 → detect dimensions → upload to R2
Updates: images (cdn_url, hash_sha256, width, height, file_size_bytes, mime_type, status='downloaded')

R2 path: {vertical}/{hash_prefix}/{sha256}.{ext}
  e.g. beauty/ab/abcdef1234...jpg

Dedup: if hash already exists in R2, skip upload, just update DB with cdn_url.

Env vars:
  DATABASE_URL — Neon connection string
  R2_ENDPOINT — CF R2 S3 endpoint
  R2_ACCESS_KEY_ID — CF R2 access key
  R2_SECRET_ACCESS_KEY — CF R2 secret key
  R2_BUCKET_PRODUCT_IMAGES — bucket name (default: product-images)
  R2_PUBLIC_DOMAIN — public domain for serving (optional, falls back to R2 URL)

Run: python jobs/image_downloader.py [--batch-size 200] [--concurrency 10] [--vertical beauty]
"""
import argparse
import hashlib
import io
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from PIL import Image

from db import get_conn, put_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# R2 config — two modes: boto3 (S3 API) or wrangler CLI
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET_PRODUCT_IMAGES", "ecomradar-images")
R2_PUBLIC_DOMAIN = os.environ.get("R2_PUBLIC_DOMAIN", "")
CF_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "e381088a0c46e9e2ecd5ff945bafcbc6")
UPLOAD_MODE = os.environ.get("R2_UPLOAD_MODE", "wrangler")  # "wrangler" or "boto3"

# HTTP session
_http = requests.Session()
_http.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ProductImageBot/1.0)"
})

# S3 client (lazy init, only for boto3 mode)
_s3 = None


def get_s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "adaptive"},
                signature_version="s3v4",
            ),
            region_name="auto",
        )
    return _s3


def upload_to_r2(key: str, content: bytes, content_type: str) -> bool:
    """Upload to R2 via wrangler CLI or boto3."""
    if UPLOAD_MODE == "wrangler":
        return _upload_wrangler(key, content, content_type)
    else:
        return _upload_boto3(key, content, content_type)


def _upload_wrangler(key: str, content: bytes, content_type: str) -> bool:
    """Upload via wrangler r2 object put (uses OAuth, works without S3 token)."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(key)[1]) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["wrangler", "r2", "object", "put", f"{R2_BUCKET}/{key}",
             "--content-type", content_type,
             "--cache-control", "public, max-age=31536000, immutable",
             "--file", tmp_path, "--remote"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT_ID}
        )
        return result.returncode == 0
    except Exception as e:
        log.debug(f"Wrangler upload failed for {key}: {e}")
        return False
    finally:
        os.unlink(tmp_path)


def _upload_boto3(key: str, content: bytes, content_type: str) -> bool:
    """Upload via S3-compatible API (requires R2 API token with bucket access)."""
    try:
        s3 = get_s3()
        s3.put_object(
            Bucket=R2_BUCKET, Key=key, Body=content,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )
        return True
    except Exception as e:
        log.debug(f"Boto3 upload failed for {key}: {e}")
        return False


def check_exists_r2(key: str) -> bool:
    """Check if object exists in R2."""
    if UPLOAD_MODE == "wrangler":
        result = subprocess.run(
            ["wrangler", "r2", "object", "head", f"{R2_BUCKET}/{key}", "--remote"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT_ID}
        )
        return result.returncode == 0
    else:
        try:
            get_s3().head_object(Bucket=R2_BUCKET, Key=key)
            return True
        except Exception:
            return False


def ext_from_url(url: str) -> str:
    """Extract file extension from URL, default to jpg."""
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    ext = ext.lower().strip(".")
    if ext in ("jpg", "jpeg", "png", "webp", "gif", "avif", "svg"):
        return ext
    return "jpg"


def r2_key(sha256: str, ext: str, vertical: str = "beauty") -> str:
    """Generate R2 object key: {vertical}/{hash_prefix}/{sha256}.{ext}"""
    return f"{vertical}/{sha256[:2]}/{sha256}.{ext}"


def download_and_process(image_id: int, source_url: str, vertical: str = "beauty") -> dict | None:
    """
    Download image, compute hash, get dimensions, upload to R2.
    Returns dict with metadata or None on failure.
    """
    try:
        resp = _http.get(source_url, timeout=15, stream=True)
        resp.raise_for_status()

        # Read content
        content = resp.content
        if len(content) < 100:  # too small, probably error page
            return None

        # SHA256 hash
        sha256 = hashlib.sha256(content).hexdigest()

        # Detect mime type
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        ext = ext_from_url(source_url)
        if "png" in content_type:
            ext = "png"
        elif "webp" in content_type:
            ext = "webp"
        elif "gif" in content_type:
            ext = "gif"

        # Get dimensions via Pillow
        width, height = None, None
        try:
            img = Image.open(io.BytesIO(content))
            width, height = img.size
        except Exception:
            pass  # SVG or broken image, skip dimensions

        # Upload to R2 (skip if hash already exists)
        key = r2_key(sha256, ext, vertical)

        if not check_exists_r2(key):
            if not upload_to_r2(key, content, content_type):
                return None

        # Build CDN URL
        if R2_PUBLIC_DOMAIN:
            cdn_url = f"https://{R2_PUBLIC_DOMAIN}/{key}"
        else:
            cdn_url = f"{R2_ENDPOINT}/{R2_BUCKET}/{key}"

        return {
            "image_id": image_id,
            "cdn_url": cdn_url,
            "storage_path": key,
            "hash_sha256": sha256,
            "width": width,
            "height": height,
            "file_size_bytes": len(content),
            "mime_type": content_type,
        }

    except requests.RequestException as e:
        log.debug(f"Failed to download {source_url}: {e}")
        return None
    except Exception as e:
        log.warning(f"Error processing image {image_id}: {e}")
        return None


def process_batch(batch_size: int = 200, concurrency: int = 10, vertical: str | None = None):
    """Fetch pending images from DB, download + upload in parallel, update DB."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            where = "WHERE i.status = 'indexed'"
            params = []
            if vertical:
                # Join through product_images → products to filter by vertical
                where += """
                    AND EXISTS (
                        SELECT 1 FROM product_images pi
                        JOIN products p ON p.id = pi.product_id
                        WHERE pi.image_id = i.id AND p.vertical = %s::vertical_type
                    )
                """
                params.append(vertical)

            cur.execute(f"""
                SELECT i.id, i.source_url
                FROM images i
                {where}
                ORDER BY i.id
                LIMIT %s
            """, params + [batch_size])

            images = cur.fetchall()

        put_conn(conn)

        if not images:
            log.info("No pending images to download.")
            return 0

        log.info(f"Downloading {len(images)} images (concurrency={concurrency})...")

        # Resolve vertical for R2 path
        v = vertical or "beauty"

        results = []
        failed = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(download_and_process, img_id, url, v): img_id
                for img_id, url in images
            }
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
                else:
                    failed += 1

        # Batch update DB
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for r in results:
                    cur.execute("""
                        UPDATE images SET
                            cdn_url = %s,
                            storage_path = %s,
                            cdn_provider = 'cloudflare_r2',
                            hash_sha256 = %s,
                            width = %s,
                            height = %s,
                            file_size_bytes = %s,
                            mime_type = %s,
                            status = 'downloaded',
                            downloaded_at = NOW(),
                            processed_at = NOW()
                        WHERE id = %s
                    """, (
                        r["cdn_url"], r["storage_path"], r["hash_sha256"],
                        r["width"], r["height"], r["file_size_bytes"],
                        r["mime_type"], r["image_id"]
                    ))

                # Mark failed ones
                if failed > 0:
                    downloaded_ids = [r["image_id"] for r in results]
                    all_ids = [img_id for img_id, _ in images]
                    failed_ids = [i for i in all_ids if i not in downloaded_ids]
                    for fid in failed_ids:
                        cur.execute(
                            "UPDATE images SET status = 'failed' WHERE id = %s",
                            (fid,)
                        )

            conn.commit()
        finally:
            put_conn(conn)

        log.info(f"Batch done: {len(results)} downloaded, {failed} failed.")
        return len(results)

    except Exception:
        put_conn(conn)
        raise


def run_full(batch_size: int = 200, concurrency: int = 10, vertical: str | None = None):
    """Run downloader in batches until all indexed images are processed."""
    total_downloaded = 0
    batch_num = 0

    while True:
        batch_num += 1
        count = process_batch(batch_size=batch_size, concurrency=concurrency, vertical=vertical)
        total_downloaded += count

        if count < batch_size:
            break

        log.info(f"Batch {batch_num} complete ({total_downloaded} total). Continuing...")
        time.sleep(0.5)  # brief pause between batches

    log.info(f"Full download complete: {total_downloaded} images downloaded.")
    return total_downloaded


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download product images to Cloudflare R2")
    parser.add_argument("--batch-size", type=int, default=200, help="Images per batch")
    parser.add_argument("--concurrency", type=int, default=10, help="Parallel downloads")
    parser.add_argument("--vertical", type=str, default=None, help="Filter by vertical")
    args = parser.parse_args()

    run_full(
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        vertical=args.vertical,
    )
