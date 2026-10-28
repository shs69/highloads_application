import pytest

from highloads_applications.balancer.algorithms import LeastTime, ewma
from highloads_applications.loadgen.compare_ewma import compare


@pytest.fixture
def algorithm(config):
    algorithm = LeastTime(
        config.backends,
        exploration_interval=config.balancer.exploration_interval,
        alpha=config.balancer.alpha,
    )
    for backend, measurement in zip(algorithm.backends, (0.05, 0.1, 0.15), strict=True):
        backend.available = True
        backend.estimate = measurement
    return algorithm


def test_минимум_и_независимый_исследующий_цикл(algorithm):
    selected = []
    for _ in range(30):
        selected.append(algorithm.select_backend().config.id)
    assert selected[9::10] == ["A", "B", "C"]
    assert all(backend == "A" for number, backend in enumerate(selected, 1) if number % 10)
    assert algorithm.assignments == 30


def test_равные_оценки_выбираются_в_порядке_id(config):
    algorithm = LeastTime(
        tuple(reversed(config.backends)),
        exploration_interval=config.balancer.exploration_interval,
        alpha=config.balancer.alpha,
    )
    for backend in algorithm.backends:
        backend.available = True
        backend.estimate = 0.1
    assert algorithm.select_backend().config.id == "A"


def test_формула_ewma():
    assert ewma(0.1, 0.3, 0.2) == pytest.approx(0.14)


@pytest.mark.parametrize("alpha, expected", [(0.2, 0.14), (0.8, 0.26)])
def test_успешный_ответ_обновляет_оценку_с_заданным_alpha(config, alpha, expected):
    algorithm = LeastTime(
        config.backends,
        exploration_interval=config.balancer.exploration_interval,
        alpha=alpha,
    )
    backend = algorithm.backends[0]
    backend.available = True
    backend.estimate = 0.1

    algorithm.record_response(backend, 0.3, successful=True)

    assert backend.estimate == pytest.approx(expected)


@pytest.mark.parametrize("successful, available", [(False, True), (True, False), (False, False)])
def test_ошибка_или_отказ_сервера_сохраняет_оценку(algorithm, successful, available):
    backend = algorithm.backends[0]
    backend.available = available

    algorithm.record_response(backend, 0.3, successful=successful)

    assert backend.estimate == 0.05


def test_недоступные_пропускаются_в_обоих_режимах(algorithm):
    algorithm.backends[0].available = False
    selected = [algorithm.select_backend().config.id for _ in range(30)]
    assert selected[9::10] == ["B", "C", "B"]
    assert all(backend == "B" for number, backend in enumerate(selected, 1) if number % 10)
    for backend in algorithm.backends:
        backend.available = False
    assert algorithm.select_backend() is None
    assert algorithm.assignments == 30


def test_больший_alpha_быстрее_реагирует_на_обе_смены_задержки():
    rows = compare()
    slow = [row for row in rows if row["step"] == 11]
    recovered = [row for row in rows if row["step"] == 31]
    assert slow[0]["ewma_after"] == pytest.approx(0.14)
    assert slow[1]["ewma_after"] == pytest.approx(0.41)
    assert recovered[1]["ewma_after"] < recovered[0]["ewma_after"]
