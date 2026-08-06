"""
Проверки расчёта, общие для двух наборов данных.

Наборов два, и они проверяют разное:

* `plata-sample.xlsx` — обезличенный файл в репозитории. Ожидаемые значения в нём
  посчитаны движком, поэтому он ловит **изменение поведения**: если правило поехало,
  тест краснеет. Гоняется всегда и в CI.
* настоящая таблица бухгалтерии по пути из `PAYROLL_FIXTURE` — сверка с **реальным
  расчётом**. В репозиторий не попадает никогда: там ФИО и суммы живых людей.
  Пропускается, если файла нет.

Отсюда правило: зелёный CI означает «движок не изменился», а не «сходится
с бухгалтерией». Второе проверяется только там, где есть настоящий файл.
"""
from __future__ import annotations

from decimal import Decimal

FIELDS = ("net", "gross", "contributions", "total_cost")


def calculate(engine, row, tolerance: Decimal):
    """Считает листок, подставляя надбавку из таблицы, если она проставлена руками."""
    slip = engine.calculate(row.employee, row.timesheet)

    if row.sheet_meal is not None:
        rule_meal = next(
            (c.amount for c in slip.components if c.code == "meal_and_vacation_bonus"),
            Decimal(0),
        )
        # Бухгалтер иногда ставит надбавку руками. Сверяем схемы расчёта, а не ввод.
        if abs(rule_meal - row.sheet_meal) >= tolerance:
            scheme = engine.schemes[row.employee.scheme]
            slip.net += row.sheet_meal - rule_meal
            engine.gross_up(slip, row.timesheet, scheme)
            engine.contributions(slip, row.timesheet, scheme)
    return slip


def check_field(engine, rows, field: str, tolerance: Decimal) -> None:
    mismatches = []

    for row in rows:
        expected = row.expected[field]
        if expected is None:
            continue

        got = getattr(calculate(engine, row, tolerance), field)
        if abs(got - expected) >= tolerance:
            mismatches.append(
                f"{row.sheet} / {row.name}: движок {got:.2f}, "
                f"таблица {expected:.2f}, разница {got - expected:+.2f}"
            )

    assert not mismatches, f"расхождения по полю «{field}»:\n  " + "\n  ".join(mismatches)


def check_schemes_covered(rows, minimum: int) -> None:
    assert {r.scheme for r in rows} == {
        "standard", "half_time", "half_time_min_base", "temporary",
    }, "набор не покрывает все схемы — регрессия дырявая"
    assert len(rows) >= minimum, f"строк меньше {minimum}: {len(rows)}"


def check_expected_present(rows) -> None:
    """Если импорт сломается, тесты не должны молча позеленеть на пустых значениях."""
    for row in rows:
        assert row.expected["net"] is not None, f"{row.sheet} / {row.name}: не прочитано нето"


def check_components_sum_to_net(engine, rows, tolerance: Decimal) -> None:
    for row in rows:
        slip = engine.calculate(row.employee, row.timesheet)
        total = sum((c.amount for c in slip.components), Decimal(0))
        assert abs(total - slip.net) < tolerance, f"{row.name}: компоненты не сходятся с нето"


def check_ledgers_assigned(engine, rows) -> None:
    """У каждого компонента должен быть регистр учёта — на нём строится видимость."""
    valid = {"white", "grey", "black"}
    for row in rows:
        for component in engine.calculate(row.employee, row.timesheet).components:
            assert component.layer in valid, f"{row.name}: неизвестный регистр {component.layer}"
