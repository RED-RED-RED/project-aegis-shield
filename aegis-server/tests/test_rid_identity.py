"""
Tests for Remote ID make/model enrichment (analysis/rid_identity.py).

Pure functions only: CTA-2063-A parsing, the graded fallback chain, and the
tolerant CSV/column normalizer. No DB.
"""

import pytest

from analysis import rid_identity
from analysis.rid_identity import (
    parse_cta2063a,
    enrich,
    normalize_rows,
    parse_csv,
    IdentityIndex,
)


# ── CTA-2063-A structural parse ─────────────────────────────────────────────

def test_parse_valid_dji_serial():
    # 1581 (mfr) + F (15 body chars) + 15 chars = valid
    p = parse_cta2063a("1581F1234567890ABCDE")
    assert p["valid"] is True
    assert p["mfr_code"] == "1581"
    assert p["length"] == 15
    assert p["body"] == "1234567890ABCD"[:15] or len(p["body"]) == 15


def test_parse_length_mismatch_is_invalid():
    # length code says 15 but only 3 body chars present
    p = parse_cta2063a("1581FABC")
    assert p["valid"] is False
    assert p["mfr_code"] == "1581"   # still exposed for triage


def test_parse_rejects_excluded_letters():
    # 'O' and 'I' are not allowed in the standard
    p = parse_cta2063a("15O1F1234567890ABCD")
    assert p["valid"] is False


def test_parse_short_and_empty():
    assert parse_cta2063a("")["valid"] is False
    assert parse_cta2063a("ABC")["valid"] is False
    assert parse_cta2063a("ABC")["mfr_code"] is None  # <4 chars


def test_parse_zero_length_code_invalid():
    # length code must be 1..F, never 0
    assert parse_cta2063a("15810")["valid"] is False


# ── Graded fallback chain ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_index(monkeypatch):
    """Give each test a clean, controlled index."""
    idx = IdentityIndex()
    monkeypatch.setattr(rid_identity, "_index", idx)
    return idx


def test_enrich_non_serial_id_type_is_na(fresh_index):
    out = enrich("caa_reg", "FA3TESTREG1")
    assert out["id_source"] == "na"
    assert out["make"] is None and out["model"] is None


def test_enrich_make_only_from_prefix(fresh_index):
    fresh_index.add("1581", manufacturer="DJI")
    out = enrich("serial_number", "1581F1234567890ABCDE")
    assert out["id_source"] == "prefix"
    assert out["make"] == "DJI"
    assert out["model"] is None
    assert out["mfr_code"] == "1581"
    assert out["serial_valid"] is True


def test_enrich_make_and_model_from_doc(fresh_index):
    fresh_index.add("1581", manufacturer="DJI")
    fresh_index.add("1581", manufacturer="DJI", model="Mavic 3",
                    serial_prefix="1581F15")
    out = enrich("serial_number", "1581F1534567890ABCDE")
    assert out["id_source"] == "doc"
    assert out["make"] == "DJI"
    assert out["model"] == "Mavic 3"


def test_enrich_unknown_mfr_valid_serial(fresh_index):
    out = enrich("serial_number", "9ZZ9F1234567890ABCDE")
    assert out["id_source"] == "unknown_mfr"
    assert out["make"] is None
    assert out["mfr_code"] == "9ZZ9"
    assert out["serial_valid"] is True


def test_enrich_unverified_bad_serial(fresh_index):
    # id_type claims serial but the string isn't valid CTA-2063-A
    out = enrich("serial_number", "NOTASERIAL!!")
    assert out["id_source"] == "unverified"
    assert out["serial_valid"] is False


def test_enrich_empty_serial(fresh_index):
    out = enrich("serial_number", "")
    assert out["id_source"] == "unverified"
    assert out["serial_valid"] is False


# ── Longest-prefix model matching ───────────────────────────────────────────

def test_model_match_prefers_longest_prefix(fresh_index):
    fresh_index.add("1581", manufacturer="DJI")
    fresh_index.add("1581", manufacturer="DJI", model="Generic", serial_prefix="1581")
    fresh_index.add("1581", manufacturer="DJI", model="Mini 4 Pro", serial_prefix="1581F9A")
    out = enrich("serial_number", "1581F9A34567890ABCDE")
    assert out["model"] == "Mini 4 Pro"


# ── Tolerant CSV / column normalization ─────────────────────────────────────

def test_normalize_rows_flexible_columns():
    rows = [
        {"Manufacturer Code": "1581", "Make": "DJI", "Model/Series": "Mavic 3"},
        {"code": "26X8", "company": "Autel Robotics"},
    ]
    out = normalize_rows(rows)
    assert {"mfr_code": "1581", "manufacturer": "DJI",
            "model": "Mavic 3", "serial_prefix": None} in out
    assert any(e["manufacturer"] == "Autel Robotics" and e["mfr_code"] == "26X8"
               for e in out)


def test_normalize_derives_code_from_serial_when_no_code_column():
    rows = [{"manufacturer": "DJI", "serial number": "1581F1234567890ABCDE"}]
    out = normalize_rows(rows)
    assert out[0]["mfr_code"] == "1581"


def test_normalize_drops_empty_rows():
    rows = [{"foo": "bar"}, {"manufacturer": "", "mfr_code": ""}]
    assert normalize_rows(rows) == []


def test_parse_csv_roundtrip():
    csv_text = "manufacturer,mfr_code,model\nDJI,1581,Mavic 3\nAutel,26X8,\n"
    out = parse_csv(csv_text)
    assert len(out) == 2
    assert out[0]["manufacturer"] == "DJI"
    assert out[0]["model"] == "Mavic 3"
    assert out[1]["model"] is None
