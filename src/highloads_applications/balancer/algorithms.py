from dataclasses import dataclass

from highloads_applications.configuration import BackendConfig


@dataclass
class BackendState:
    config: BackendConfig
    available: bool = False
    estimate: float = float("inf")
    active: int = 0
    failures: int = 0
    successes: int = 0


def ewma(previous: float, measured: float, alpha: float) -> float:
    return alpha * measured + (1 - alpha) * previous


class LeastTime:
    def __init__(
        self, backends: tuple[BackendConfig, ...], exploration_interval: int, alpha: float
    ) -> None:
        self.backends = [
            BackendState(backend) for backend in sorted(backends, key=lambda item: item.id)
        ]
        self.exploration_interval = exploration_interval
        self.alpha = alpha
        self.assignments = 0
        self.exploration_position = 0

    def select_backend(self) -> BackendState | None:
        available = [backend for backend in self.backends if backend.available]
        if not available:
            return None

        self.assignments += 1
        if self.assignments % self.exploration_interval != 0:
            return min(available, key=lambda backend: backend.estimate)

        # У каждого десятого назначения свой обход, независимый от обычного выбора.
        while True:
            backend = self.backends[self.exploration_position]
            self.exploration_position = (self.exploration_position + 1) % len(self.backends)
            if backend.available:
                return backend

    def record_response(self, backend: BackendState, duration: float, successful: bool) -> None:
        # Ошибки и старые ответы отказавшего сервера не меняют оценку.
        if successful and backend.available:
            backend.estimate = ewma(backend.estimate, duration, self.alpha)
