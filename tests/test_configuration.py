from pathlib import Path

import pytest

from highloads_applications.configuration import DEFAULT_CONFIG, ConfigurationError, load_config


@pytest.mark.parametrize(
    "old, new",
    [
        ("port = 8080", "port = 0"),
        ("port = 8080", "port = 65536"),
        ("port = 8080", "port = true"),
        ("alpha = 0.2", "alpha = 0"),
        ("alpha = 0.2", "alpha = 1.1"),
        ("alpha = 0.2", "alpha = nan"),
        ("alpha = 0.2", "alpha = inf"),
        ("request_timeout = 2.0", "request_timeout = -1"),
        ("health_interval = 1.0", 'health_interval = "1"'),
        ("health_timeout = 0.3", "health_timeout = false"),
        ("failure_threshold = 2", "failure_threshold = 1.5"),
        ("recovery_threshold = 2", "recovery_threshold = 0"),
        ("exploration_interval = 10", "exploration_interval = 0"),
        ("shutdown_timeout = 5.0", "shutdown_timeout = 0"),
        ('algorithm = "least_time"', 'algorithm = "round_robin"'),
        ('id = "B"', 'id = "A"'),
        ('id = "B"', 'id = "B B"'),
        ("port = 9002", "port = 9001"),
        ("port = 9001", "port = 8080"),
        ("delay_ms = 50", "delay_ms = -1"),
        ("weight = 1", "weight = 0"),
        ("alpha = 0.2\n", ""),
        ("alpha = 0.2", "alpha = 0.2\ntypo = 1"),
        ('mode = "sequential"', 'mode = "random"'),
        ("concurrency = 20", "concurrency = -1"),
        ("requests = 600", "requests = true"),
        ("http://127.0.0.1:8080", "ftp://127.0.0.1:8080"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/work"),
        ('host = "127.0.0.1"', 'host = "wrong host"'),
    ],
)
def test_ошибочные_параметры_отклоняются(tmp_path, old, new):
    target = tmp_path / "system.toml"
    target.write_text(DEFAULT_CONFIG.read_text().replace(old, new))
    with pytest.raises(ConfigurationError):
        load_config(target)


def test_конфигурация_не_зависит_от_рабочей_директории(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert [backend.id for backend in config.backends] == ["A", "B", "C"]
    assert config.loadgen.output == str((DEFAULT_CONFIG.parent / "../results/load.json").resolve())


def test_ошибка_файла_и_синтаксиса(tmp_path):
    with pytest.raises(ConfigurationError, match="Не удалось прочитать"):
        load_config(Path(tmp_path / "absent.toml"))
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[[broken")
    with pytest.raises(ConfigurationError, match="Не удалось прочитать"):
        load_config(invalid)
