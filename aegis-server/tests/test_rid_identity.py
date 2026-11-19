"""
Tests for Remote ID make/model enrichment (analysis/rid_identity.py).

Pure functions only: CTA-2063-A validity, longest-prefix matching, prefix
extraction (against real FAA serials), the FAA envelope parser, and the
tolerant CSV normalizer. No DB, no network.
"""

import pytest

from analysis import rid_identity
from analysis.rid_identity import (
    parse_cta2063a,
    enrich,
    extract_prefixes,
    normalize_rows,
    parse_csv,
    _faa_items,
    PrefixIndex,
)

# Real serials from the FAA DoC filing for the Skydio X10 w/ Parachute (OOP000000259).
SKYDIO_X10PCH = [
    "X10PCH25X7", "X10PCH33Z2", "X10PCH35S7", "X10PCH46V5", "X10PCH4NK8",
    "X10PCHR37H", "X10PCHS57K", "X10PCHVF3A", "X10PCHVZ54", "X10PCHZ4N9",
]


# ── CTA-2063-A validity (spoof signal only) ─────────────────────────────────

def test_parse_valid_dji_serial():
    p = parse_cta2063a("1581F1234567890ABCDE")  # 1581 + F(15) + 15 chars
    assert p["valid"] is True
    assert p["mfr_code"] == "1581"


def test_parse_length_mismatch_invalid():
    p = parse_cta2063a("1581FABC")
    assert p["valid"] is False
    assert p["mfr_code"] == "1581"


def test_skydio_serial_is_not_cta_valid():
    # Skydio's product serials are NO_FORMAT per the FAA — not CTA-2063-A.
    assert parse_cta2063a("X10PCH25X7")["valid"] is False


# ── Prefix extraction from a filing's serials ───────────────────────────────

def test_extract_prefix_skydio():
    assert extract_prefixes(SKYDIO_X10PCH) == ["X10PCH"]


def test_extract_prefix_requires_model_specific_length():
    # "1581F" (5) is just DJI's code + length char — not model-specific — so no
    # prefix is emitted; a genuinely shared 6+ prefix is.
    assert extract_prefixes(["1581F1A2B3C4D5E6F", "1581F9Z8Y7X6W5V4U"]) == []
    assert extract_prefixes(["X10PCHAA", "X10PCHBB", "X10PCHCC"]) == ["X10PCH"]


def test_extract_prefix_short_divergence_emits_nothing():
    # Serials that diverge early (effectively random after the maker region)
    # have no reliable model prefix, so nothing is emitted — make is anchored
    # separately by the maker code.
    vals = ["1581F1AAA", "1581F9ZZZ", "1581FQ123"]  # common prefix "1581F" (5) < 6
    assert extract_prefixes(vals) == []


def test_extract_prefix_empty():
    assert extract_prefixes([]) == []
    assert extract_prefixes(["", "  "]) == []


# ── Longest-prefix matching + graded fallback ───────────────────────────────

@pytest.fixture(autouse=True)
def fresh_index(monkeypatch):
    idx = PrefixIndex()
    monkeypatch.setattr(rid_identity, "_index", idx)
    return idx


def test_enrich_non_serial_id_type_is_na(fresh_index):
    out = enrich("caa_reg", "FA3TESTREG1")
    assert out["id_source"] == "na"
    assert out["make"] is None


def test_enrich_doc_make_and_model(fresh_index):
    fresh_index.add("X10PCH", "Skydio", "X10 with Parachute")
    out = enrich("serial_number", "X10PCH25X7")
    assert out["id_source"] == "doc"
    assert out["make"] == "Skydio"
    assert out["model"] == "X10 with Parachute"
    # Known serial wins even though it isn't CTA-valid.
    assert out["serial_valid"] is False


def test_enrich_prefix_make_only(fresh_index):
    fresh_index.add("1581", "DJI", None)
    out = enrich("serial_number", "1581F1234567890ABCDE")
    assert out["id_source"] == "prefix"
    assert out["make"] == "DJI"
    assert out["model"] is None
    assert out["serial_valid"] is True


def test_enrich_longest_prefix_wins(fresh_index):
    fresh_index.add("1581", "DJI", None)
    fresh_index.add("1581F9A", "DJI", "Mini 4 Pro")
    out = enrich("serial_number", "1581F9A4567890ABCDE")
    assert out["model"] == "Mini 4 Pro"
    assert out["id_source"] == "doc"


def test_enrich_rejects_third_party_in_code_space(fresh_index):
    # DJI owns 1581. A third-party "SBG Modified DJI Mini" filing lives in that
    # space; a real DJI Matrice serial must NOT be labeled the SBG Mini — it
    # falls back to make DJI, model unknown.
    fresh_index.add("1581", "DJI", None)                       # code anchor
    fresh_index.add("1581F8", "SBG Modified DJI Mini", "Mini") # third-party filing
    out = enrich("serial_number", "1581F8HGX265700A1Q0L40")
    assert out["make"] == "DJI"
    assert out["model"] is None
    assert out["id_source"] == "prefix"


def test_enrich_trusts_matching_make_in_code_space(fresh_index):
    # A genuine DJI filing (make starts with "DJI") keeps its model.
    fresh_index.add("1581", "DJI", None)
    fresh_index.add("1581F8HG", "DJI", "Matrice 4TD")
    out = enrich("serial_number", "1581F8HGX265700A1Q0L40")
    assert out["make"] == "DJI"
    assert out["model"] == "Matrice 4TD"
    assert out["id_source"] == "doc"


def test_enrich_unknown_mfr_valid_serial(fresh_index):
    out = enrich("serial_number", "9ZZ9F1234567890ABCDE")
    assert out["id_source"] == "unknown_mfr"
    assert out["mfr_code"] == "9ZZ9"
    assert out["serial_valid"] is True


def test_enrich_unverified_bad_serial(fresh_index):
    out = enrich("serial_number", "NOTASERIAL!!")
    assert out["id_source"] == "unverified"
    assert out["serial_valid"] is False


# ── FAA envelope parsing ────────────────────────────────────────────────────

def test_faa_items_unwraps_envelope():
    payload = {"data": {"items": [{"value": "X10PCH25X7"}, {"value": "X10PCH33Z2"}]}}
    items = _faa_items(payload)
    assert [it["value"] for it in items] == ["X10PCH25X7", "X10PCH33Z2"]


def test_faa_items_handles_bare_list_and_empty():
    assert _faa_items([{"value": "A"}]) == [{"value": "A"}]
    assert _faa_items({"data": {}}) == []
    assert _faa_items({}) == []


# ── CSV import ──────────────────────────────────────────────────────────────

def test_normalize_rows_flexible_columns():
    rows = [{"Serial Prefix": "X10PCH", "Make": "Skydio", "Model": "X10"}]
    out = normalize_rows(rows)
    assert out == [{"serial_prefix": "X10PCH", "manufacturer": "Skydio", "model": "X10"}]


def test_normalize_rows_drops_rows_without_prefix():
    assert normalize_rows([{"manufacturer": "DJI"}]) == []


def test_parse_csv_roundtrip():
    text = "serial_prefix,manufacturer,model\nX10PCH,Skydio,X10\n1581,DJI,\n"
    out = parse_csv(text)
    assert len(out) == 2
    assert out[0]["manufacturer"] == "Skydio"
    assert out[1]["model"] is None
