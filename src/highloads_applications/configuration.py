import ipaddress
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "config.toml"


class ConfigurationError(ValueError):
    pass


def number(value: object, name: str, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
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
    if (
        type(value) is not int
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        limit = f" до {maximum}" if maximum is not None else ""
        raise ConfigurationError(f"{name}: требуется целое число от 1{limit}")
    return value


def check_host(value: str) -> None:
    if not isinstance(value, str):
        raise ConfigurationError("host: требуется IPv4 или IPv6 адрес")

    try:
        _ = ipaddress.ip_address(address=value)
    except ValueError as error:
        raise ConfigurationError(f"host: некорректный IP-адрес {value!r}") from error


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
        _ = integer(self.port, "backend.port", maximum=65535)
        _ = number(self.delay_ms, "backend.delay", zero=True)
        _ = number(self.weight, "backend.weight")

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True)
class BalancerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    algorithm: str = "least_time"
    alpha: float = 0.2
    request_timeout: float = 2.0
    health_interval: float = 1.0
    health_timeout: float = 0.3
    failure_threshold: int = 2
    recovery_threshold: int = 2
    exploration_interval: int = 10
    shutdown_timeout: float = 5.0

    def __post_init__(self) -> None:
        check_host(self.host)
        _ = integer(self.port, "balancer.port", maximum=65535)

        if self.algorithm != "least_time":
            raise ConfigurationError(
                "balancer.algorithm: поддерживается только least_time"
            )

        if number(self.alpha, "balancer.alpha") > 1:
            raise ConfigurationError("balancer.alpha: требуется 0 < alpha <= 1")

        for name in (
            "request_timeout",
            "health_interval",
            "health_timeout",
            "shutdown_timeout",
        ):
            _ = number(getattr(self, name), f"balancer.{name}")

        for name in ("failure_threshold", "recovery_threshold", "exploration_interval"):
            _ = integer(getattr(self, name), f"balancer.{name}")


@dataclass(frozen=True)
class SystemConfig:
    backends: tuple[BackendConfig, ...]
    balancer: BalancerConfig = field(default_factory=BalancerConfig)


def load_config(path: Path = DEFAULT_CONFIG) -> SystemConfig:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError, UnicodeError) as error:
        raise ConfigurationError(f"Не удалось прочитать {path}: {error}") from error

    if "backends" not in data or set(data) - {"backends", "balancer"}:
        raise ConfigurationError("Допустимы секции backends (обязательная) и balancer")
    if not isinstance(data["backends"], list) or not data["backends"]:
        raise ConfigurationError("backends: требуется непустой массив таблиц")

    try:
        balancer = BalancerConfig(**data.get("balancer", {}))
        backends = tuple(BackendConfig(**item) for item in data["backends"])
    except TypeError as error:
        raise ConfigurationError(f"Неверные поля конфигурации: {error}") from error

    if len({backend.id for backend in backends}) != len(backends):
        raise ConfigurationError("ID бекендов должны быть уникальны")

    addresses = [
        (ipaddress.ip_address(backend.host), backend.port) for backend in backends
    ]
    addresses.append((ipaddress.ip_address(balancer.host), balancer.port))
    if len(set(addresses)) != len(addresses):
        raise ConfigurationError("Адреса бекендов и балансера должны быть уникальны")

    return SystemConfig(backends=backends, balancer=balancer)
