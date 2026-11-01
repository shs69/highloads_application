import ipaddress
import math
import tomllib
from dataclasses import dataclass
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
        raise ConfigurationError(f"{name}: требуется целое {sign} число")

    return result


def integer(value: object, name: str, *, maximum: int | None = None) -> int:
    if (
        type(value) is not int
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        limit = f"до {maximum}" if maximum else ""
        raise ConfigurationError(f"{name:} Требуется целое число от 1{limit}")
    return value


def check_host(value: str) -> None:
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
        check_host(self.host)
        _ = integer(self.port, "backend.port", maximum=65536)
        _ = number(self.delay_ms, "backend.delay", zero=True)
        _ = number(self.weight, "backend.weight")

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True)
class SystemConfig:
    backends: tuple[BackendConfig, ...]


def load_config(path: Path = DEFAULT_CONFIG):
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, ValueError, UnicodeError) as error:
        raise ConfigurationError(f"Не удалось прочитать {path}: {error}") from error

    if set(data) != {"backends"}:
        raise ConfigurationError("Конфигурация должна содержать balancer")
    if not isinstance(data["backends"], list) or not data["backends"]:
        raise ConfigurationError("backends: требуется непустой массив таблиц")

    try:
        backends = tuple(BackendConfig(**item) for item in data["backends"])
    except TypeError as error:
        raise ConfigurationError(f"Неверные поля конфигурации: {error}") from error

    if len({backend.id for backend in backends}) != len(backends):
        raise ConfigurationError("ID бекендов должны быть уникальны")

    adresses = [
        (ipaddress.ip_address(backend.host), backend.port) for backend in backends
    ]
    if len(set(adresses)) != len(adresses):
        raise ConfigurationError("Все адреса должны быть уникальны")

    return SystemConfig(backends)
