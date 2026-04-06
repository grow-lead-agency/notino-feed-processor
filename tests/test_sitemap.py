"""Tests for sitemap URL extraction."""
import pytest
from unittest.mock import patch

from jobs.jsonld_scraper import get_product_urls_from_sitemap


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.com/product/chanel-n5</loc></url>
  <url><loc>https://shop.com/product/dior-sauvage</loc></url>
  <url><loc>https://shop.com/product/tom-ford</loc></url>
  <url><loc>https://shop.com/category/perfumes</loc></url>
  <url><loc>https://shop.com/about-us</loc></url>
</urlset>"""


class TestGetProductUrlsFromSitemap:
    @patch("jobs.jsonld_scraper.fetch")
    def test_extracts_all_urls(self, mock_fetch):
        mock_fetch.return_value = SITEMAP_XML
        urls = get_product_urls_from_sitemap("https://shop.com/sitemap.xml")
        assert len(urls) == 5  # all <loc> URLs, not just product ones

    @patch("jobs.jsonld_scraper.fetch")
    def test_respects_limit(self, mock_fetch):
        mock_fetch.return_value = SITEMAP_XML
        urls = get_product_urls_from_sitemap("https://shop.com/sitemap.xml", limit=2)
        assert len(urls) == 2

    @patch("jobs.jsonld_scraper.fetch")
    def test_empty_on_failure(self, mock_fetch):
        mock_fetch.return_value = None
        urls = get_product_urls_from_sitemap("https://dead.com/sitemap.xml")
        assert urls == []

    @patch("jobs.jsonld_scraper.fetch")
    def test_handles_amp_in_url(self, mock_fetch):
        mock_fetch.return_value = SITEMAP_XML
        # Function should replace &amp; with &
        urls = get_product_urls_from_sitemap("https://shop.com/sitemap.xml?page=1&amp;type=product")
        mock_fetch.assert_called_once()
        # Check the URL was cleaned
        called_url = mock_fetch.call_args[0][0]
        assert "&amp;" not in called_url

    @patch("jobs.jsonld_scraper.fetch")
    def test_whitespace_in_loc(self, mock_fetch):
        xml = """<urlset>
        <url><loc>
          https://shop.com/product/spaced
        </loc></url>
        </urlset>"""
        mock_fetch.return_value = xml
        urls = get_product_urls_from_sitemap("https://shop.com/sitemap.xml")
        assert len(urls) == 1
        assert urls[0].strip() == "https://shop.com/product/spaced"
