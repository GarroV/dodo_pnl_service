"""Экран следа расчёта: от суммы до входных часов и версии правила (T029, D025).

Три решения, на которых всё держится.

**Второго следа рядом с движковым здесь нет.** Шаги приезжают из `payroll.trace`
(T013) — их пишет сам движок, пока считает. Своя функция «для объяснения»
разъехалась бы с расчётом на первой же правке правила, и разъехалась бы молча:
сумма на экране одна, объяснение — от другой версии формулы. Здесь только
отбор видимого, сверка с сохранённым и оформление данных для страницы.

**След пересобирается, а не хранится** (issue #48, T056). У закрытого месяца,
чьи правила с тех пор поменяли, пересобранный след объясняет **не то**, чем
считали. Молчать об этом нельзя, и одной оговорки на экране мало: оговорку
читают один раз и перестают замечать. Поэтому пересобранное **сверяется с
сохранённым** покомпонентно (`stored`, `agrees`, `Step.differs`), и экран
говорит не «след может не сходиться», а «сошёлся» или «разошёлся вот здесь».
Хранение следа этим не заменяется — оно остаётся отдельной задачей; но экран,
который врёт, отличается от экрана, который честно говорит о своей границе.

**Ни строк, ни следа** (D023). Шаг чужого регистра — это сразу и сумма, и
правило, и человек, то есть утечка худшего вида. Поэтому:

- шаги отбираются по регистру своего компонента, а не показываются все;
- итог следа считается **заново по видимым шагам** — не маскируется на выводе,
  иначе скрытое вычислялось бы вычитанием (так устроены T050 и T071);
- сверка «сошлось / не сошлось» тоже идёт по видимому срезу: роль, которой не
  видно половины строки, иначе прочитала бы величину скрытого в размере
  расхождения;
- производные величины (бруто, налог, взносы, полная стоимость) посчитаны по
  всем регистрам сразу и показываются, только когда роль видит строку целиком.
  Тот же довод, по которому `payslip_totals` закрыты своей политикой (T050).

Разрез по регистру берётся тот же, что у ведомости (`reports.sheet`): один
способ сузить на оба экрана, иначе они разъедутся.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from reports.sheet import ALL

__all__ = [
    "ALL", "Carried", "RowTrace", "Step", "TraceNotFound", "build_trace", "to_cents",
    "trace_row",
]

# Производные величины: не слагаемые строки, а следствия. Складывать их с
# начислениями нельзя, поэтому они и живут отдельным списком.
NET = "net"

CENT = Decimal("0.01")


def to_cents(value) -> Decimal:
    """Округление шага до копейки — ровно то же, каким записана строка.

    Второе определение вместо `payrun.calc.money` не по недосмотру: этот модуль
    остаётся чистым Python без Django (как и `reports.sheet`), а `payrun.calc`
    тянет за собой модели и настройки. Расхождение двух округлений сделало бы
    след «не сходящимся» на копейку у всех подряд, поэтому равенство закреплено
    тестом `test_the_rounding_is_the_same_one_that_wrote_the_row`, а не
    обещанием.
    """
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class TraceNotFound(LookupError):
    """Строки нет или она не видна. Две причины намеренно неразличимы снаружи."""


@dataclass(frozen=True)
class Step:
    """Один шаг расчёта на экране.

    `stored` — что по этому же коду и регистру лежит в базе. `None` значит, что
    сохранённой пары нет вовсе: шаг появился после расчёта, и это расхождение,
    а не «просто новая строка».
    """

    code: str
    title: str
    amount: Decimal
    ledger: str
    inputs: dict[str, Any] = field(default_factory=dict)
    level: str = "country"
    version_id: Any = None
    kind: str = NET
    stored: Decimal | None = None

    @property
    def differs(self) -> bool:
        return self.stored is None or self.stored != self.amount


@dataclass(frozen=True)
class Carried:
    """Разница, приехавшая из закрытого месяца (T026).

    Правилом этого месяца она не объясняется и объясняться не должна: её след
    живёт в периоде-источнике. Здесь она стоит отдельной строкой со ссылкой
    туда и в сверку следа не входит — ровно так же, как в `payrun.retro._stored`.
    """

    code: str
    title: str
    amount: Decimal
    ledger: str
    source_period: date


@dataclass(frozen=True)
class RowTrace:
    """Что показывает экран следа одной строки ведомости."""

    steps: list[Step] = field(default_factory=list)
    derived: list[Step] = field(default_factory=list)
    carried: list[Carried] = field(default_factory=list)
    cut: str = ALL
    # Что дал бы расчёт сегодня (по видимым шагам) и что лежит в базе.
    traced_total: Decimal = Decimal(0)
    stored_total: Decimal = Decimal(0)
    # Пересчитать не удалось: данные ушли так, что расчёт вообще не собирается.
    # Это не то же самое, что «след сошёлся», и на экране читается по-разному.
    error: str = ""
    employee: str = ""
    unit: str = ""
    period: date | None = None
    approved: bool = False

    @property
    def agrees(self) -> bool:
        """Сошёлся ли пересобранный след с сохранённой суммой видимого среза."""
        return not self.error and self.traced_total == self.stored_total and not any(
            step.differs for step in self.steps
        )

    @property
    def row_total(self) -> Decimal:
        """Итог строки ведомости: сохранённое плюс перенос. То, что видно глазами."""
        return self.stored_total + sum(
            (line.amount for line in self.carried), Decimal(0)
        )


def _visible(ledger: str, visible_ledgers, cut: str) -> bool:
    return ledger in visible_ledgers and (cut == ALL or ledger == cut)


def _chosen_cut(cut: str, ledgers: set[str]) -> str:
    """Разрез, которого в видимых регистрах нет, — это «все видимые».

    Не отказ и не пустая страница: и то и другое было бы ответом на вопрос
    «а есть ли такой регистр». Правило одно с ведомостью (`reports.sheet`).
    """
    return cut if cut in ledgers else ALL


def trace_row(
    employee,
    timesheet,
    preset,
    *,
    stored: dict[tuple[str, str], Decimal],
    visible_ledgers,
    cut: str = ALL,
    carried=(),
    approved: bool = False,
    name: str = "",
    unit: str = "",
    period: date | None = None,
) -> RowTrace:
    """След одной строки. Чистая функция: базы здесь нет, поэтому она проверяема.

    `stored` — сохранённые суммы этой строки, ключ `(код, регистр)`. Приезжают
    уже отобранными политиками базы, поэтому шире того, что роли видно, они
    быть не могут.
    """
    from payroll import PayrollEngine

    # Один вызов движка на всё: `payroll.trace.explain` — это ровно
    # `calculate(...).trace`, и звать его вторым разом значило бы считать
    # человека дважды ради того же самого следа.
    slip = PayrollEngine(preset).calculate(employee, timesheet)
    net_steps = [step for step in slip.trace if step.contributes_to == NET]

    visible_ledgers = set(visible_ledgers or [])
    # Разрез выбирается из регистров **сохранённой строки**, а не из роли и не
    # из справочника: пустой разрез — тоже сообщение о существовании регистра.
    row_ledgers = {ledger for _code, ledger in stored} & visible_ledgers
    chosen = _chosen_cut(cut, row_ledgers)

    # Регистр шага — регистр его компонента: движок кладёт их парой (`Payslip.add`),
    # и это свойство закреплено тестом `test_every_component_has_a_step_behind_it`.
    steps: list[Step] = []
    for component, step in zip(slip.components, net_steps, strict=True):
        if not _visible(component.ledger, visible_ledgers, chosen):
            continue
        steps.append(
            Step(
                code=step.rule_code,
                title=step.title,
                amount=to_cents(step.applied_value),
                ledger=component.ledger,
                inputs=dict(step.input_values),
                level=step.source_level,
                version_id=step.rule_version_id,
                stored=stored.get((step.rule_code, component.ledger)),
            )
        )

    # Производные величины — про строку целиком, а не про её половину. Поэтому
    # их нет ни при неполной видимости, ни в разрезе.
    all_ledgers = {component.ledger for component in slip.components} | {
        ledger for _code, ledger in stored
    }
    derived = (
        [
            Step(
                code=step.rule_code, title=step.title, amount=to_cents(step.applied_value),
                ledger="", inputs=dict(step.input_values), level=step.source_level,
                version_id=step.rule_version_id, kind=step.contributes_to,
            )
            for step in slip.trace
            if step.contributes_to != NET
        ]
        if all_ledgers <= visible_ledgers and chosen == ALL
        else []
    )

    shown_carried = [
        line for line in carried
        if _visible(line.ledger, visible_ledgers, chosen)
    ]

    return RowTrace(
        steps=steps,
        derived=derived,
        carried=shown_carried,
        cut=chosen,
        traced_total=sum((step.amount for step in steps), Decimal(0)),
        stored_total=sum(
            (amount for (_code, ledger), amount in stored.items()
             if _visible(ledger, visible_ledgers, chosen)),
            Decimal(0),
        ),
        employee=name or employee.name,
        unit=unit,
        period=period,
        approved=approved,
    )


def build_trace(
    tenant_id: UUID, payslip_id: UUID, visible_ledgers, cut: str = ALL
) -> RowTrace:
    """След строки ведомости из базы.

    Вход расчёта собирается **тем же кодом**, что и обычный расчёт периода
    (`payrun.calc`): вторая сборка входов разъехалась бы с первой молча, и след
    объяснял бы расчёт, которого не было.

    `visible_ledgers` приходит от вызывающего — так же, как у `calculate_period`.
    Сохранённые суммы отбирает база сама, а вот пересобранный след живёт в
    памяти, и политике его не отфильтровать: чем сузить, приходится сказать.
    """
    from core.models import PayComponent, Payslip, Tenant
    from payrun.calc import PayrunRefused, collect_cases
    from payrun.lifecycle import APPROVED
    from payrun.rules import select_rules

    row = (
        Payslip.objects.select_related("employee", "unit", "payrun")
        .filter(pk=payslip_id, tenant_id=tenant_id)
        .first()
    )
    if row is None:
        # Чужая строка и несуществующая отвечают одинаково: по ответу нельзя
        # понять, что строка существует и просто не видна.
        raise TraceNotFound("строка ведомости не найдена")

    period = row.payrun.period
    stored: dict[tuple[str, str], Decimal] = {}
    carried: list[Carried] = []
    for component in PayComponent.objects.filter(tenant_id=tenant_id, payslip_id=row.pk):
        if component.retro_source_period is not None:
            carried.append(
                Carried(
                    code=component.code, title=component.title, amount=component.amount,
                    ledger=component.ledger, source_period=component.retro_source_period,
                )
            )
            continue
        key = (component.code, component.ledger)
        stored[key] = stored.get(key, Decimal(0)) + component.amount

    name = f"{row.employee.last_name} {row.employee.first_name}".strip()
    unit = row.unit.code if row.unit_id else ""
    approved = row.payrun.status == APPROVED

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        raise TraceNotFound("партнёр недоступен")

    try:
        rules = select_rules(tenant_id, tenant.country_code, period)
        case = next(
            (c for c in collect_cases(tenant_id, period) if c.employee_id == row.employee_id),
            None,
        )
    except PayrunRefused as refusal:
        # Пересчитать нечем — но сохранённые суммы показать обязаны: экран без
        # них выглядел бы как «строки нет», хотя она есть.
        case, rules = None, None
        return _without_steps(
            stored, carried, visible_ledgers, cut, refusal.message,
            name=name, unit=unit, period=period, approved=approved,
        )

    if case is None:
        # Табеля в этом месяце у человека нет — так бывает у строки, заведённой
        # только под перенос разницы (T026). Объяснять нечего, и врать об этом
        # не надо.
        return _without_steps(
            stored, carried, visible_ledgers, cut,
            "своего расчёта в этом месяце у строки нет — только перенос за прошлый",
            name=name, unit=unit, period=period, approved=approved,
        )

    return trace_row(
        case.employee, case.timesheet,
        rules.preset(group_id=case.group_id, employee_id=case.employee_id),
        stored=stored, visible_ledgers=visible_ledgers, cut=cut, carried=carried,
        approved=approved, name=name, unit=unit, period=period,
    )


def _without_steps(
    stored, carried, visible_ledgers, cut, error, *, name, unit, period, approved
) -> RowTrace:
    """Строка есть, объяснить нечем. Суммы показываем, шагов не выдумываем."""
    visible_ledgers = set(visible_ledgers or [])
    chosen = _chosen_cut(cut, {ledger for _code, ledger in stored} & visible_ledgers)
    return RowTrace(
        carried=[line for line in carried if _visible(line.ledger, visible_ledgers, chosen)],
        cut=chosen,
        stored_total=sum(
            (amount for (_code, ledger), amount in stored.items()
             if _visible(ledger, visible_ledgers, chosen)),
            Decimal(0),
        ),
        error=error, employee=name, unit=unit, period=period, approved=approved,
    )
