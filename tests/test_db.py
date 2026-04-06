"""Tests for jobs/db.py — pure functions (no DB needed)."""
import pytest
from jobs.db import normalize_title, extract_volume


# ── normalize_title ──────────────────────────────────────────────


class TestNormalizeTitle:
    def test_basic(self):
        assert normalize_title("Chanel No. 5") == "chanel no 5"

    def test_accents_removed(self):
        assert normalize_title("Parfémová voda") == "parfemova voda"

    def test_special_chars_collapsed(self):
        # ™ decomposes to "tm" via NFKD, which is kept as alphanumeric
        assert normalize_title("L'Oréal  Paris™") == "l oreal paristm"

    def test_unicode_normalize(self):
        assert normalize_title("CHANEL N°5") == "chanel n 5"

    def test_empty(self):
        assert normalize_title("") == ""

    def test_none(self):
        assert normalize_title(None) == ""

    def test_numbers_preserved(self):
        assert normalize_title("100ml EdP") == "100ml edp"

    def test_multiple_spaces(self):
        assert normalize_title("  Dolce &   Gabbana  ") == "dolce gabbana"

    def test_german_umlauts(self):
        assert normalize_title("Düfte für Männer") == "dufte fur manner"

    def test_polish_chars(self):
        # Ł doesn't decompose via NFKD (it's a base character, not accented)
        # so it gets stripped by the [^a-z0-9] filter
        assert normalize_title("Łódź perfumy") == "odz perfumy"


# ── extract_volume ───────────────────────────────────────────────


class TestExtractVolume:
    def test_ml(self):
        vol_str, vol_ml = extract_volume("Chanel No 5 EDP 100ml")
        assert vol_str == "100ml"
        assert vol_ml == 100.0

    def test_ml_with_space(self):
        vol_str, vol_ml = extract_volume("100 ml EdP")
        assert vol_str == "100 ml"
        assert vol_ml == 100.0

    def test_decimal_comma(self):
        vol_str, vol_ml = extract_volume("Crème 50,5 ml")
        assert vol_str == "50,5 ml"
        assert vol_ml == 50.5

    def test_decimal_dot(self):
        vol_str, vol_ml = extract_volume("Body lotion 7.5 ml")
        assert vol_ml == 7.5

    def test_grams(self):
        vol_str, vol_ml = extract_volume("Soap 150 g")
        assert vol_ml == 150.0  # approximation

    def test_kg(self):
        vol_str, vol_ml = extract_volume("Bath salt 1.5 kg")
        assert vol_ml == 1500.0

    def test_oz(self):
        vol_str, vol_ml = extract_volume("Perfume 3.4 oz")
        assert vol_ml == pytest.approx(3.4 * 29.5735, rel=1e-3)

    def test_no_volume(self):
        vol_str, vol_ml = extract_volume("Chanel No 5 Parfum")
        assert vol_str is None
        assert vol_ml is None

    def test_empty(self):
        assert extract_volume("") == (None, None)

    def test_none(self):
        assert extract_volume(None) == (None, None)

    def test_case_insensitive(self):
        _, vol_ml = extract_volume("50 ML EdP")
        assert vol_ml == 50.0

    def test_first_match_wins(self):
        vol_str, vol_ml = extract_volume("100ml / 3.4oz")
        assert vol_ml == 100.0
