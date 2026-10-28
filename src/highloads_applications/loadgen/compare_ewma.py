import argparse
import csv
from pathlib import Path

from highloads_applications.balancer.algorithms import ewma


def compare() -> list[dict[str, str | int | float]]:
    """Одинаковая трасса: 50 ms → 500 ms → 50 ms, с двумя коэффициентами."""
    rows: list[dict[str, str | int | float]] = []
    phases = [("baseline", 0.05, 10), ("slow", 0.5, 20), ("recovered", 0.05, 30)]
    for alpha in (0.2, 0.8):
        estimate = 0.05
        step = 0
        for phase, measured, count in phases:
            for _ in range(count):
                step += 1
                previous = estimate
                estimate = ewma(previous, measured, alpha)
                rows.append(
                    {
                        "alpha": alpha,
                        "step": step,
                        "phase": phase,
                        "measured_seconds": measured,
                        "ewma_before": previous,
                        "ewma_after": estimate,
                    }
                )
    return rows


def plot(rows: list[dict[str, str | int | float]], output: Path) -> None:
    from matplotlib import pyplot as plt

    figure, axes = plt.subplots(figsize=(9, 4))
    reference = [row for row in rows if row["alpha"] == 0.2]
    steps = [row["step"] for row in reference]
    measurements = [row["measured_seconds"] for row in reference]
    for alpha in (0.2, 0.8):
        samples = [row for row in rows if row["alpha"] == alpha]
        estimates = [row["ewma_after"] for row in samples]
        axes.plot(steps, estimates, label=f"EWMA, α={alpha}")
    axes.step(
        steps,
        measurements,
        where="mid",
        color="gray",
        linestyle="--",
        label="Время ответа",
    )
    axes.set(
        xlabel="Номер измерения",
        ylabel="Время, с",
        title="Замедление и восстановление сервера",
    )
    axes.legend()
    axes.grid(alpha=0.25)
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=150)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сравнение EWMA на одинаковой трассе измерений")
    parser.add_argument("--output", type=Path, default=Path("results/ewma.csv"))
    parser.add_argument(
        "--plot", type=Path, help="Сохранить график; требует группу зависимостей plots"
    )
    args = parser.parse_args()
    rows = compare()
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        if args.plot:
            plot(rows, args.plot)
    except OSError as error:
        parser.exit(1, f"Не удалось сохранить результаты: {error}\n")
    print(f"Измерения и оценки: {args.output}")
    if args.plot:
        print(f"График: {args.plot}")


if __name__ == "__main__":
    main()
