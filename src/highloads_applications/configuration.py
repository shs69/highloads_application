import ipaddress
import math
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "system.toml"


class ConfigurationError(ValueError):
    pass


def number(value: object, name: str, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name}: требуется число")
    try:
        result = float(value)
    except OverflowError as error:
        raise ConfigurationError(f"{name}: слишком большое число") from error
    if not math.isfinite(result) or (result < 0 if zero else result <= 0):
        sign = "неотрицательное" if zero else "положительное"
        raise ConfigurationError(f"{name}: требуется конечное {sign} число")
    return result


def integer(value: object, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        limit = f" до {maximum}" if maximum is not None else ""
        raise ConfigurationError(f"{name}: требуется целое число от 1{limit}")
    return value


def check_host(value: str) -> None:
    if not isinstance(value, str):
        raise ConfigurationError("host: требуется IPv4 или IPv6 адрес")
    try:
        ipaddress.ip_address(value)
    except ValueError as error:
        raise ConfigurationError(f"host: некорректный IP-адрес {value!r}") from error


def http_url(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("loadgen.url: требуется строка")
    try:
        url = urlsplit(value)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.port != 0
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
            and url.username is None
            and url.password is None
        )
    except ValueError as error:
        raise ConfigurationError("loadgen.url: некорректный URL") from error
    if not valid:
        raise ConfigurationError("loadgen.url: требуется базовый HTTP(S) URL без пути")
    return value.rstrip("/")


@dataclass(frozen=True)
class BackendConfig:
    id: str
    host: str
    port: int
    delay_ms: float
    weight: float

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.id):
            raise ConfigurationError("backend.id: допустимы буквы ASCII, цифры, _ и -")
        check_host(self.host)
        integer(self.port, "backend.port", maximum=65535)
        number(self.delay_ms, "backend.delay_ms", zero=True)
        number(self.weight, "backend.weight")

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True)
class BalancerConfig:
    host: str
    port: int
    algorithm: str
    alpha: float
    request_timeout: float
    health_interval: float
    health_timeout: float
    failure_threshold: int
    recovery_threshold: int
    exploration_interval: int
    shutdown_timeout: float

    def __post_init__(self) -> None:
        check_host(self.host)
        integer(self.port, "balancer.port", maximum=65535)
        if self.algorithm != "least_time":
            raise ConfigurationError("algorithm: поддерживается только least_time")
        if number(self.alpha, "alpha") > 1:
            raise ConfigurationError("alpha: требуется 0 < alpha <= 1")
        for name in (
            "request_timeout",
            "health_interval",
            "health_timeout",
            "shutdown_timeout",
        ):
            number(getattr(self, name), name)
        for name in ("failure_threshold", "recovery_threshold", "exploration_interval"):
            integer(getattr(self, name), name)


@dataclass(frozen=True)
class LoadgenConfig:
    url: str
    requests: int
    mode: str
    concurrency: int
    timeout: float
    output: str

    def __post_init__(self) -> None:
        http_url(self.url)
        integer(self.requests, "loadgen.requests")
        integer(self.concurrency, "loadgen.concurrency")
        number(self.timeout, "loadgen.timeout")
        if self.mode not in ("sequential", "concurrent"):
            raise ConfigurationError("loadgen.mode: требуется sequential или concurrent")
        if not isinstance(self.output, str) or not self.output.strip():
            raise ConfigurationError("loadgen.output: требуется путь к файлу")


@dataclass(frozen=True)
class SystemConfig:
    balancer: BalancerConfig
    backends: tuple[BackendConfig, ...]
    loadgen: LoadgenConfig


def load_config(path: Path = DEFAULT_CONFIG) -> SystemConfig:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError, UnicodeError) as error:
        raise ConfigurationError(f"Не удалось прочитать {path}: {error}") from error

    if set(data) != {"balancer", "backends", "loadgen"}:
        raise ConfigurationError("Конфигурация должна содержать balancer, backends и loadgen")
    if not isinstance(data["backends"], list) or not data["backends"]:
        raise ConfigurationError("backends: требуется непустой массив таблиц")

    try:
        balancer = BalancerConfig(**data["balancer"])
        backends = tuple(BackendConfig(**item) for item in data["backends"])
        loadgen = LoadgenConfig(**data["loadgen"])
    except TypeError as error:
        raise ConfigurationError(f"Неверные поля конфигурации: {error}") from error

    if len({backend.id for backend in backends}) != len(backends):
        raise ConfigurationError("ID бэкендов должны быть уникальны")
    addresses = [(ipaddress.ip_address(backend.host), backend.port) for backend in backends]
    addresses.append((ipaddress.ip_address(balancer.host), balancer.port))
    if len(set(addresses)) != len(addresses):
        raise ConfigurationError("Адреса бэкендов и балансировщика должны быть уникальны")

    output = (path.resolve().parent / loadgen.output).resolve()
    return SystemConfig(
        balancer=balancer,
        backends=backends,
        loadgen=replace(loadgen, url=http_url(loadgen.url), output=str(output)),
    )
