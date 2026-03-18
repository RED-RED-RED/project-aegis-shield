"""
tests/test_algo_notifier.py
============================
Unit tests for the Algo 8128 IP Visual Alerter integration.

All HTTP calls are intercepted with respx so no real network access is needed.
"""

import pytest
import respx
import httpx

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.config import Settings
from integrations.algo_notifier import AlgoNotifier


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def make_settings(**overrides) -> Settings:
    defaults = dict(
        algo_8128_enabled=True,
        algo_8128_url="http://192.168.1.50",
        algo_8128_api_key="test-key",
        algo_8128_cooldown_seconds=30,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_notifier(**overrides) -> AlgoNotifier:
    return AlgoNotifier(make_settings(**overrides))


# ------------------------------------------------------------------ #
# Pattern mapping
# ------------------------------------------------------------------ #

class TestPatternMapping:
    @pytest.mark.asyncio
    @respx.mock
    async def test_low_fires_pattern_1_intensity_1(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        result = await algo.trigger("low", "DRONE-001")
        assert result is True
        assert route.called
        body = route.calls[0].request.content
        import json
        payload = json.loads(body)
        assert payload == {"pattern": 1, "intensity": 1}

    @pytest.mark.asyncio
    @respx.mock
    async def test_medium_fires_pattern_5_intensity_2(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        result = await algo.trigger("medium", "DRONE-001")
        assert result is True
        import json
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"pattern": 5, "intensity": 2}

    @pytest.mark.asyncio
    @respx.mock
    async def test_high_fires_pattern_9_intensity_3(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        result = await algo.trigger("high", "DRONE-001")
        assert result is True
        import json
        payload = json.loads(route.calls[0].request.content)
        assert payload == {"pattern": 9, "intensity": 3}

    @pytest.mark.asyncio
    @respx.mock
    async def test_unknown_level_returns_false_no_request(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        result = await algo.trigger("critical", "DRONE-001")
        assert result is False

    @pytest.mark.asyncio
    async def test_disabled_returns_false_no_request(self):
        algo = make_notifier(algo_8128_enabled=False)
        result = await algo.trigger("high", "DRONE-001")
        assert result is False


# ------------------------------------------------------------------ #
# Cooldown
# ------------------------------------------------------------------ #

class TestCooldown:
    @pytest.mark.asyncio
    @respx.mock
    async def test_same_level_within_cooldown_is_suppressed(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)

        # First trigger succeeds
        r1 = await algo.trigger("medium", "DRONE-001")
        assert r1 is True
        assert route.call_count == 1

        # Second trigger at same level within cooldown is suppressed
        r2 = await algo.trigger("medium", "DRONE-001")
        assert r2 is False
        assert route.call_count == 1  # No new HTTP call

    @pytest.mark.asyncio
    @respx.mock
    async def test_same_level_after_cooldown_fires_again(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=0)  # Zero cooldown

        r1 = await algo.trigger("high", "DRONE-001")
        r2 = await algo.trigger("high", "DRONE-001")
        assert r1 is True
        assert r2 is True
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_cooldown_is_per_drone(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)

        await algo.trigger("medium", "DRONE-A")
        r2 = await algo.trigger("medium", "DRONE-B")  # Different drone, different cooldown
        assert r2 is True
        assert route.call_count == 2


# ------------------------------------------------------------------ #
# Escalation logic
# ------------------------------------------------------------------ #

class TestEscalationLogic:
    @pytest.mark.asyncio
    @respx.mock
    async def test_low_to_high_triggers(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)

        r_low = await algo.trigger("low", "DRONE-001")
        r_high = await algo.trigger("high", "DRONE-001")  # Escalation bypasses cooldown
        assert r_low is True
        assert r_high is True
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_medium_to_high_triggers(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)

        await algo.trigger("medium", "DRONE-001")
        r = await algo.trigger("high", "DRONE-001")
        assert r is True
        assert route.call_count == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_high_to_high_within_cooldown_does_not_trigger(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)

        await algo.trigger("high", "DRONE-001")
        r2 = await algo.trigger("high", "DRONE-001")
        assert r2 is False
        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_high_to_low_triggers_clear(self):
        trigger_route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        await algo.trigger("high", "DRONE-001")

        r = await algo.clear()
        assert r is True
        # Second call should be the stop command
        import json
        stop_payload = json.loads(trigger_route.calls[-1].request.content)
        assert stop_payload == {"action": "stop"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_de_escalation_does_not_re_trigger(self):
        route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)

        await algo.trigger("high", "DRONE-001")
        r = await algo.trigger("low", "DRONE-001")  # Lower level within cooldown
        assert r is False
        assert route.call_count == 1


# ------------------------------------------------------------------ #
# Graceful failure
# ------------------------------------------------------------------ #

class TestGracefulFailure:
    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_returns_false_on_timeout(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        algo = make_notifier()
        result = await algo.trigger("high", "DRONE-001")
        assert result is False  # No exception raised

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_returns_false_on_connection_error(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        algo = make_notifier()
        result = await algo.trigger("high", "DRONE-001")
        assert result is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_clear_returns_false_on_timeout(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        algo = make_notifier()
        result = await algo.clear()
        assert result is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_trigger_returns_false_on_http_error(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(503)
        )
        algo = make_notifier()
        result = await algo.trigger("high", "DRONE-001")
        assert result is False


# ------------------------------------------------------------------ #
# Drone timeout / state management
# ------------------------------------------------------------------ #

class TestDroneTimeout:
    @pytest.mark.asyncio
    @respx.mock
    async def test_clear_is_called_when_drone_state_cleared(self):
        trigger_route = respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        await algo.trigger("high", "DRONE-001")
        assert algo.get_drone_level("DRONE-001") == "high"

        # Simulate clear + state reset (what alert_engine does on 60s quiet)
        await algo.clear()
        algo.clear_drone_state("DRONE-001")
        assert algo.get_drone_level("DRONE-001") == ""

        import json
        stop_calls = [
            c for c in trigger_route.calls
            if json.loads(c.request.content).get("action") == "stop"
        ]
        assert len(stop_calls) == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_after_clear_drone_state_retrigger_works(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier(algo_8128_cooldown_seconds=30)
        await algo.trigger("high", "DRONE-001")
        algo.clear_drone_state("DRONE-001")

        # After state cleared, same drone can be triggered again even within cooldown window
        r = await algo.trigger("high", "DRONE-001")
        assert r is True

    def test_get_drone_level_returns_empty_for_unknown(self):
        algo = make_notifier()
        assert algo.get_drone_level("UNKNOWN") == ""


# ------------------------------------------------------------------ #
# Status properties
# ------------------------------------------------------------------ #

class TestStatusProperties:
    @pytest.mark.asyncio
    @respx.mock
    async def test_last_trigger_metadata_updated_on_success(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            return_value=httpx.Response(200)
        )
        algo = make_notifier()
        assert algo.last_trigger_ts is None
        assert algo.last_trigger_level is None

        await algo.trigger("high", "DRONE-001")
        assert algo.last_trigger_ts is not None
        assert algo.last_trigger_level == "high"

    @pytest.mark.asyncio
    @respx.mock
    async def test_last_trigger_metadata_not_updated_on_failure(self):
        respx.post("http://192.168.1.50/api/v1/trigger").mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        algo = make_notifier()
        await algo.trigger("high", "DRONE-001")
        assert algo.last_trigger_ts is None
        assert algo.last_trigger_level is None

    def test_disabled_notifier_reports_correct_enabled_state(self):
        algo = make_notifier(algo_8128_enabled=False)
        assert algo.enabled is False

    def test_url_reflects_config(self):
        algo = make_notifier(algo_8128_url="http://10.0.0.200")
        assert algo.url == "http://10.0.0.200"
