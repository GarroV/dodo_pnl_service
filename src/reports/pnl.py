"""Сборка отчёта P&L: строки, сравнение с прошлым месяцем и раскрытие (issue #183).

То, ради чего собирали данные. До этого P&L существовал только выгрузками строк:
числа были, отчёта не было — и «сколько заработали в июне» складывали в Excel из
файла, который мы же и отдали.

**Считает база, а не Python.** Отчёт собирается из представления `pnl_by_network`
(оно же `security_invoker`), поэтому срез роли делает та же RLS, что и везде:
управляющий одной точки видит свою точку, и это гарантия базы, а не фильтр в
выборке (D014). Складывать факты здесь заново значило бы завести второй способ
считать те же деньги — он разойдётся с первым молча.

**Прошлый месяц приезжает тем же запросом.** Одно число ни о чём не говорит:
«аренда 486 000» читается только рядом с прошлым месяцем. Два запроса вместо
одного дали бы два разных среза при смене прав между ними.

**Что не попало в отчёт — часть отчёта.** Неразобранные строки (служебная
статья `unclassified`) и нераспределённые суммы существуют, и отчёт, который о
них молчит, выглядит полным, не будучи им. Эталон ставит это первой строкой
экрана, и здесь так же: `Report.missing` считается вместе со строками.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

# Служебная строка «Не разобрано»: она в отчёте видна числом, но считается
# отдельно — это не статья расходов, а мера того, чего мы ещё не знаем.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Line:
    """Строка отчёта: сколько сейчас, сколько было и из скольких фактов сложилась."""

    code: str
    title: str
    kind: str            # revenue | expense | subtotal
    amount: Decimal
    previous: Decimal
    facts: int

    @property
    def difference(self) -> Decimal:
        return self.amount - self.previous

    @property
    def is_revenue(self) -> bool:
        return self.kind == "revenue"


@dataclass(frozen=True)
class Report:
    """Отчёт целиком: строки, итог и то, что в него не попало."""

    period: date
    previous_period: date
    lines: list = field(default_factory=list)
    missing: Decimal = Decimal("0")
    missing_facts: int = 0

    @property
    def total(self) -> Decimal:
        """Результат — сложение показанных строк, а не отдельная выборка.

        Второй источник истины здесь опаснее отсутствия итога: человек сверяет
        отчёт по нижней строке и не пересчитывает столбец (тот же довод, что в
        `payrun/sheet.py`).
        """
        return sum((line.amount for line in self.lines), Decimal("0"))

    @property
    def previous_total(self) -> Decimal:
        return sum((line.previous for line in self.lines), Decimal("0"))


def previous_month(period: date) -> date:
    return (period.replace(day=1) - timedelta(days=1)).replace(day=1)


def build(tenant_id, period: date) -> Report:
    """Собрать отчёт за период. Всё, чего роль не видит, сюда не приезжает."""
    from django.db import connection

    was = previous_month(period)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select i.code, i.title, i.kind, i.sort_order,
                   coalesce(now_.amount, 0)  as amount,
                   coalesce(was_.amount, 0)  as previous,
                   coalesce(now_.facts, 0)   as facts
              from pnl_items i
              left join lateral (
                  select sum(amount) as amount, count(*) as facts
                    from pnl_lines l
                   where l.tenant_id = %(tenant)s and l.period = %(now)s
                     and l.pnl_item_id = i.id and l.kind <> 'transfer'
                     and l.allocation <> 'split'
              ) now_ on true
              left join lateral (
                  select sum(amount) as amount
                    from pnl_lines l
                   where l.tenant_id = %(tenant)s and l.period = %(was)s
                     and l.pnl_item_id = i.id and l.kind <> 'transfer'
                     and l.allocation <> 'split'
              ) was_ on true
             where i.kind <> 'transfer'
               and (i.tenant_id is null or i.tenant_id = %(tenant)s)
               and (coalesce(now_.amount, 0) <> 0 or coalesce(was_.amount, 0) <> 0)
             order by i.sort_order, i.code
            """,
            {"tenant": str(tenant_id), "now": period, "was": was},
        )
        rows = cursor.fetchall()

    lines, missing, missing_facts = [], Decimal("0"), 0
    for code, title, kind, _order, amount, previous, facts in rows:
        if code == UNCLASSIFIED:
            # «Не разобрано» — не статья отчёта, а мера того, чего мы не знаем.
            # В строках она стояла бы наравне с арендой, а это разные вещи.
            missing, missing_facts = amount, facts
            continue
        lines.append(Line(code, title, kind, amount, previous, facts))

    return Report(period=period, previous_period=was, lines=lines,
                  missing=missing, missing_facts=missing_facts)


def facts_of(tenant_id, period: date, code: str) -> list:
    """Первичные факты одной строки — «из чего собралось» (модуль 5 эталона).

    Отчёт, которому нельзя задать вопрос «почему столько», проверяют пересчётом
    в Excel — то есть не пользуются им. Тот же приём, что у следа расчёта
    зарплаты (D025), только источник другой.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """
            select l.doc_date, coalesce(l.unit_code, ''), coalesce(c.title, ''),
                   coalesce(l.title, ''), l.amount, l.source::text
              from pnl_lines l
              join pnl_items i on i.id = l.pnl_item_id
              left join counterparties c on c.id = l.counterparty_id
             where l.tenant_id = %s and l.period = %s and i.code = %s
               and l.kind <> 'transfer' and l.allocation <> 'split'
             order by l.doc_date desc nulls last, l.amount
            """,
            [str(tenant_id), period, code],
        )
        return cursor.fetchall()
