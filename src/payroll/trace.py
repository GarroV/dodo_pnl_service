"""След расчёта: какие правила сработали, с какими входами, какой версией ставки.

D025 и принцип объяснимости из конституции: к любой сумме можно дойти до входных
часов и до версии правила, по которой она посчитана. Число, происхождение
которого нельзя показать, бухгалтер не примет — и будет прав.

**След пишет сам движок, по ходу расчёта.** Отдельная функция, которая повторяет
формулы «для объяснения», разъехалась бы с расчётом на первой же правке правила,
и разъехалась бы молча: сумма на экране одна, объяснение — от другой версии
формулы. Поэтому `explain()` не считает ничего сам, а возвращает след, который
движок сложил, пока считал.

Проверяемое свойство, ради которого всё и делается: **сумма шагов равна итогу
строки**. Шаги начисления помечены `contributes_to="net"` и складываются в нето;
производные величины (бруто, налог, взносы, полная стоимость) — отдельные шаги
со своим `contributes_to`, потому что они не слагаемые, а следствия.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

__all__ = ["TraceStep", "explain"]


@dataclass(frozen=True)
class TraceStep:
    """Один шаг расчёта.

    `input_values` — числа и признаки, из которых собрана сумма: ключи английские
    и стабильные, подписи переводит интерфейс. `rule_version_id` пуст, если
    правило пришло из файла-пресета, а не из версионированной строки в базе.
    """

    rule_code: str
    title: str
    applied_value: Decimal
    rule_code_path: str = ""
    input_values: dict[str, Any] = field(default_factory=dict)
    rule_version_id: Any = None
    source_level: str = "country"
    # net — слагаемое нето; gross / tax / contributions / total_cost — производная
    # величина, которую нельзя складывать с начислениями.
    contributes_to: str = "net"


def explain(employee, timesheet, preset) -> list[TraceStep]:
    """След расчёта одной строки ведомости.

    Считает тем же движком и теми же правилами, что и обычный расчёт: другого
    источника у следа нет и быть не должно.
    """
    from .engine import PayrollEngine

    return PayrollEngine(preset).calculate(employee, timesheet).trace
