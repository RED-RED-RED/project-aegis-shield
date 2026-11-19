"""
tests/test_mqtt_fallback.py
===========================
LAN-first / Tailscale-fallback behaviour of the MQTT publisher.

The node binds to its primary host (the LAN IP when on-site). When that host
can't be reached, a supervisor thread rotates to the configured fallback host
(the server's Tailscale name) and keeps flipping until one connects. These
tests exercise host-list construction and the rotation, with the paho client
mocked so nothing touches the network.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from config.settings import NodeConfig


def _make_publisher(**mqtt_kwargs):
    """Build an MQTTPublisher with paho.Client mocked out."""
    cfg = NodeConfig()
    cfg.node_id = "ARGUS-TEST"
    for k, v in mqtt_kwargs.items():
        setattr(cfg, k, v)
    with patch("publisher.mqtt_client.mqtt.Client", return_value=MagicMock()):
        from publisher.mqtt_client import MQTTPublisher
        return MQTTPublisher(cfg)


def test_host_list_primary_only():
    pub = _make_publisher(mqtt_host="192.168.1.100", mqtt_host_fallback="")
    assert pub._hosts == ["192.168.1.100"]


def test_host_list_with_fallback():
    pub = _make_publisher(mqtt_host="192.168.1.100", mqtt_host_fallback="aegis-server")
    assert pub._hosts == ["192.168.1.100", "aegis-server"]


def test_host_list_dedupes_identical_fallback():
    # A fallback equal to the primary adds nothing (and would make rotation a no-op).
    pub = _make_publisher(mqtt_host="aegis-server", mqtt_host_fallback="aegis-server")
    assert pub._hosts == ["aegis-server"]


def test_connect_binds_primary_first():
    pub = _make_publisher(mqtt_host="192.168.1.100", mqtt_host_fallback="aegis-server")
    pub.connect()
    try:
        first_call = pub._client.connect_async.call_args_list[0]
        assert first_call.args[0] == "192.168.1.100"
    finally:
        pub._stop.set()


def test_supervisor_rotates_to_fallback_when_primary_unreachable():
    # Primary never "connects" (on_connect never fires with the mock), so the
    # supervisor should flip to the fallback host after the connect timeout.
    pub = _make_publisher(
        mqtt_host="192.168.1.100",
        mqtt_host_fallback="aegis-server",
        mqtt_connect_timeout_s=0.05,
    )
    pub.connect()
    try:
        # Give the supervisor a few timeout windows to rotate.
        deadline = time.time() + 2.0
        hosts_tried = set()
        while time.time() < deadline:
            hosts_tried = {c.args[0] for c in pub._client.connect_async.call_args_list}
            if "aegis-server" in hosts_tried:
                break
            time.sleep(0.05)
        assert "192.168.1.100" in hosts_tried
        assert "aegis-server" in hosts_tried, "supervisor never fell back to Tailscale host"
    finally:
        pub._stop.set()


def test_no_supervisor_without_fallback():
    # With no fallback there's nothing to rotate to; connect_async is called
    # once for the primary and the supervisor thread is never started.
    pub = _make_publisher(mqtt_host="192.168.1.100", mqtt_host_fallback="")
    pub.connect()
    try:
        time.sleep(0.2)
        hosts = [c.args[0] for c in pub._client.connect_async.call_args_list]
        assert hosts == ["192.168.1.100"]
        assert not hasattr(pub, "_supervisor_thread")
    finally:
        pub._stop.set()
