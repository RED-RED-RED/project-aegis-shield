"""Runtime settings store — coercion, override fallback, secret masking."""

import pytest

from core import runtime_settings as rt


@pytest.fixture(autouse=True)
def _clean():
    rt._overrides.clear()
    yield
    rt._overrides.clear()


def test_coerce_types():
    assert rt._coerce("pushover_enabled", "true") is True
    assert rt._coerce("pushover_enabled", "0") is False
    assert rt._coerce("pushover_cooldown_s", "900") == 900
    assert rt._coerce("pushover_temp_crit_c", "82.5") == 82.5
    assert rt._coerce("pushover_min_level", "high") == "high"


def test_get_falls_back_to_env_default():
    # No override -> the .env default from Settings
    assert rt.get("pushover_cooldown_s") == 600
    assert rt.get("pushover_enabled") is False


def test_get_uses_override():
    rt._overrides["pushover_cooldown_s"] = 120
    assert rt.get("pushover_cooldown_s") == 120


def test_emergency_set_from_override():
    rt._overrides["pushover_emergency_categories"] = "spoofed_position, gps_jamming"
    assert rt.emergency_set() == {"spoofed_position", "gps_jamming"}


def test_snapshot_masks_secrets():
    rt._overrides["pushover_token"] = "supersecret"
    snap = rt.snapshot()
    tok = next(e for e in snap["editable"] if e["key"] == "pushover_token")
    assert tok["secret"] is True
    assert "value" not in tok           # never echoed
    assert tok["configured"] is True    # but we say it's set

    # non-secret editable keys expose their value
    cd = next(e for e in snap["editable"] if e["key"] == "pushover_cooldown_s")
    assert "value" in cd


def test_snapshot_has_readonly():
    snap = rt.snapshot()
    keys = {e["key"] for e in snap["readonly"]}
    assert "db_host" in keys and "mqtt_host" in keys
    # read-only section must not contain secrets
    assert "db_password" not in keys and "pushover_token" not in keys
