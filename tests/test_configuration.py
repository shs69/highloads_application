from dataclasses import replace

import pytest

from highloads_applications.configuration import (
    BalancerConfig,
    ConfigurationError,
    load_config,
)


@pytest.mark.parametrize(
    "name,value",
    [
        ("host", 123),
        ("host", "invalid"),
        ("port", 65536),
        ("port", True),
        ("algorithm", "unknown"),
        ("alpha", 0),
        ("alpha", 1.1),
        ("alpha", float("nan")),
        ("request_timeout", -1),
        ("health_timeout", float("inf")),
        ("health_interval", 0),
        ("shutdown_timeout", False),
        ("failure_threshold", 0),
        ("recovery_threshold", 1.5),
        ("exploration_interval", True),
    ],
)
def test_invalid_balancer_settings(name, value):
    with pytest.raises(ConfigurationError):
        BalancerConfig(**{name: value})


def test_backend_only_config_keeps_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[backends]]\nid = "A"\nhost = "127.0.0.1"\n'
        "port = 9001\ndelay_ms = 50\nweight = 1\n"
    )
    config = load_config(path)
    assert config.balancer == BalancerConfig()
    assert config.backends[0].id == "A"

    path.write_text("[balancer]\nport = 9001\n" + path.read_text())
    with pytest.raises(ConfigurationError, match="уникальны"):
        load_config(path)


@pytest.mark.parametrize(
    "value", ["balancer = []", "unknown = 1", "[balancer]\nunknown = 1"]
)
def test_invalid_config_sections(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(
        value + '\n[[backends]]\nid = "A"\nhost = "127.0.0.1"\n'
        "port = 9001\ndelay_ms = 50\nweight = 1\n"
    )
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_backend_id_is_safe_for_response_header(config):
    with pytest.raises(ConfigurationError):
        replace(config.backends[0], id="A\r\ninvalid")
