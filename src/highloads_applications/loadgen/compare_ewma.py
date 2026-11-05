import argparse
import csv
from pathlib import Path

from highloads_applications.balancer.algorithms import ewma


def compare() -> list[dict[str, float | int]]:
    # Одинаковые измерения позволяют сравнить именно коэффициенты сглаживания.
    measurements = [50] * 10 + [500] * 20 + [50] * 30
    rows = []

    for alpha in (0.2, 0.8):
        estimate = 50.0

        for step, measured in enumerate(measurements, start=1):
            previous = estimate
            estimate = ewma(previous, measured, alpha)

            rows.append(
                {
                    "alpha": alpha,
                    "step": step,
                    "measured_ms": measured,
                    "estimate_before_ms": previous,
                    "estimate_after_ms": estimate,
                }
            )

    return rows


def plot(rows: list[dict[str, float | int]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")

    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(
        1, 2, figsize=(12, 4.5), sharex=True, sharey=True, layout="constrained"
    )

    for axis, alpha, color in zip(
        axes, (0.2, 0.8), ("#2468A2", "#C35B23"), strict=True
    ):
        selected = [row for row in rows if row["alpha"] == alpha]
        steps = [row["step"] for row in selected]
        measured = [row["measured_ms"] for row in selected]
        estimates = [row["estimate_after_ms"] for row in selected]

        axis.axvspan(10.5, 30.5, color="#FFF0D9", label="Период замедления")
        axis.step(
            steps,
            measured,
            where="mid",
            color="#555555",
            linestyle="--",
            linewidth=1.5,
            label="Заданное время ответа",
        )
        axis.plot(
            steps, estimates, color=color, linewidth=2.5, label="Оценка балансера"
        )

        axis.set_title(f"Коэффициент {str(alpha).replace('.', ',')}")
        axis.set_xlabel("Номер успешного ответа")
        axis.set_xlim(1, 60)
        axis.set_ylim(0, 560)
        axis.grid(alpha=0.2)
        axis.legend(loc="upper right", fontsize=8)

    axes[0].set_ylabel("Время, мс")
    figure.suptitle("Замедление сервера с 50 до 500 мс и возвращение к 50 мс")

    figure.savefig(path, dpi=180)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description="Сравнение коэффициентов сглаживания")
    _ = parser.add_argument("--output", type=Path, default=Path("results/ewma"))
    args = parser.parse_args()

    rows = compare()
    args.output.mkdir(parents=True, exist_ok=True)

    log_path = args.output / "measurements.csv"
    image_path = args.output / "comparison.png"

    with log_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    plot(rows, image_path)

    print(f"Журнал расчёта: {log_path}")
    print(f"График: {image_path}")


if __name__ == "__main__":
    main()
