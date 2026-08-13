"""Отчёт расхождений: этот период против прошлого, пороги на каждый компонент.

Четыре решения, на которых всё держится.

**Пороги живут в конфигурации, а не в коде.** Они приезжают из пресета страны
(`variance:` в теле пресета), то есть версионированы по датам и переопределяемы
партнёром тем же механизмом, что и всё остальное — `variance.components.<код>.percent`.
Константа в коде выглядела бы на экране ровно как настроенный порог, и отличить
«партнёр так решил» от «мы так зашили» было бы нечем. Порогов нет — отчёт
отказывается словами (`ThresholdsMissing`), а не берёт число из воздуха.

**Порог — на компонент, и порогов два.** Процент без абсолютного пола шумит:
надбавка в 20 динаров, выросшая вдвое, — это +20 динаров, и рядом с окладом она
топит настоящую находку. Абсолютный пол без процента слеп: +600 на ста тысячах —
это 0,6%, тоже не новость. Строка попадает в отчёт, когда превышены **оба**, и у
каждого компонента они свои: 5% нормально для сверхурочных и ненормально для
оклада.

**Единица сравнения — сотрудник × компонент.** Отклонение живёт там. Прибавка
одному и такая же убавка другому дают нулевой итог компонента: отчёт, считающий
по итогам, не увидел бы ничего, хотя изменились две зарплаты.

**Обе стороны собираются `reports.sheet`, тем же способом, что ведомость.**
Значит сравниваются суммы, уже отобранные политиками базы, и «сумма изменилась
на X», где X посчитан по всем регистрам, здесь не может возникнуть физически
(D023). Разрез по регистру сужает обе стороны одинаково — иначе сравнивались бы
несопоставимые срезы.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.utils.translation import gettext as _

from payrun.sheet import LEDGER_ORDER, Cell
from reports.sheet import ALL

__all__ = [
    "Line", "Report", "Threshold", "Thresholds", "ThresholdsMissing",
    "build_variance", "compare", "previous_month", "thresholds_from",
]

# Ключ настройки в теле пресета. Здесь, а не в вызывающем коде: путь к правилу
# — часть самого правила, и переопределение партнёра пишется по нему же.
SECTION = "variance"


class ThresholdsMissing(LookupError):
    """Порогов отклонений в правилах страны нет — сравнивать не с чем.

    Текст умолчания не вынесен атрибутом класса намеренно: класс объявляется
    при импорте модуля, а модуль обязан импортироваться и без настроенного
    Django (см. `reports.export`, ту же причину). `gettext`/`gettext_noop`
    трогают `settings` при первом вызове, поэтому перевод собирается только
    здесь, в момент создания исключения, а не на уровне тела класса.
    """

    def __init__(self, message: str = ""):
        super().__init__(message or _(
            "В правилах страны не заданы пороги отклонений. Отчёт без них показывал "
            "бы либо всё подряд, либо ничего — и то и другое выглядело бы как "
            "настроенное поведение. Задайте раздел «variance» в пресете страны "
            "(и перезагрузите его: python manage.py load_presets)."
        ))


@dataclass(frozen=True)
class Threshold:
    """Мерка одного компонента: во сколько процентов и в сколько денег.

    Оба порога обязаны быть превышены сразу. Ноль означает «этой меркой не
    ограничиваем» — не «показывать всё»: вторая мерка остаётся.
    """

    percent: Decimal = Decimal(0)
    absolute: Decimal = Decimal(0)

    def exceeded_by(self, delta: Decimal, previous: Decimal) -> bool:
        if abs(delta) <= self.absolute:
            return False
        if previous == 0:
            # Компонент появился с нуля: процент не определён. Считать его
            # бесконечным честнее, чем нулём, — событие есть, и молчать о нём
            # значит терять появившуюся из ниоткуда сумму.
            return True
        return abs(delta) / abs(previous) * 100 > self.percent


@dataclass(frozen=True)
class Thresholds:
    """Набор мерок страны: умолчание плюс исключения по компонентам."""

    default: Threshold
    components: dict[str, Threshold] = field(default_factory=dict)

    def for_code(self, code: str) -> Threshold:
        return self.components.get(code, self.default)


def _threshold(body) -> Threshold:
    body = body or {}
    return Threshold(
        percent=Decimal(str(body.get("percent", 0))),
        absolute=Decimal(str(body.get("absolute", 0))),
    )


def thresholds_from(body) -> Thresholds:
    """Пороги из тела пресета. Нет умолчания — отказ, а не выдуманное число."""
    body = body or {}
    if not body.get("default"):
        raise ThresholdsMissing
    return Thresholds(
        default=_threshold(body["default"]),
        components={
            code: _threshold(values)
            for code, values in (body.get("components") or {}).items()
        },
    )


@dataclass(frozen=True)
class Line:
    """Одно отклонение: чьё, по какому компоненту, в каком регистре, насколько."""

    employee: str
    unit: str
    ledger: str
    code: str
    title: str
    previous: Decimal
    current: Decimal
    threshold: Threshold

    @property
    def delta(self) -> Decimal:
        return self.current - self.previous

    @property
    def percent(self) -> Decimal | None:
        """Насколько выросло. `None` — росло с нуля, процента у этого нет."""
        if self.previous == 0:
            return None
        return self.delta / abs(self.previous) * 100

    @property
    def exceeded(self) -> bool:
        return self.threshold.exceeded_by(self.delta, self.previous)


@dataclass(frozen=True)
class Report:
    """Что показывает экран расхождений."""

    lines: list[Line] = field(default_factory=list)
    cut: str = ALL
    cuts: list[str] = field(default_factory=list)
    # Сколько пар «сотрудник × компонент» вообще сравнивалось. «Отклонений нет»
    # и «сравнивать было нечего» — разные ответы, и путать их нельзя.
    compared: int = 0
    period: date | None = None
    previous_period: date | None = None
    # Прошлого периода нет или он пуст: сравнивать не с чем.
    nothing_to_compare: bool = False

    # Рост и снижение — врозь, одним числом отклонения не складываются (T096).
    # Алгебраическая сумма отвечает на вопрос «на сколько изменился фонд», а
    # экран отвечает на другой — «что разошлось»; при взаимно погасившихся
    # отклонениях она даёт ноль, и над списком настоящих расхождений стоит
    # число, которое читается как «всё сошлось». Поэтому её здесь нет вовсе, а
    # не спрятана в шаблоне: свойство, которое нельзя показать не соврав, — это
    # приглашение показать его снова.
    @property
    def total_up(self) -> Decimal:
        return sum((line.delta for line in self.lines if line.delta > 0), Decimal(0))

    @property
    def total_down(self) -> Decimal:
        return sum((line.delta for line in self.lines if line.delta < 0), Decimal(0))

    @property
    def grew(self) -> int:
        return sum(1 for line in self.lines if line.delta > 0)

    @property
    def fell(self) -> int:
        return sum(1 for line in self.lines if line.delta < 0)

    @property
    def employees(self) -> int:
        return len({line.employee for line in self.lines})


def previous_month(period: date) -> date:
    """Предыдущий месяц, первым числом — как все периоды в этом продукте."""
    if period.month == 1:
        return period.replace(year=period.year - 1, month=12, day=1)
    return period.replace(month=period.month - 1, day=1)


def _by_key(cells, cut: str) -> dict[tuple[str, str, str], dict]:
    """Суммы по ключу «человек × регистр × компонент», уже суженные разрезом.

    Перенос из закрытого месяца (T026) сюда попадает наравне с остальным: для
    сравнения периодов важно, что человеку начислено в этом месяце, а не по
    какому поводу. Иначе месяц с перерасчётом выглядел бы как обычный.
    """
    rows: dict[tuple[str, str, str], dict] = {}
    for cell in cells:
        if cut != ALL and cell.ledger != cut:
            continue
        key = (cell.employee_key, cell.ledger, cell.code)
        body = rows.setdefault(key, {
            "amount": Decimal(0), "employee": cell.employee,
            "unit": cell.unit, "title": cell.title,
        })
        body["amount"] += cell.amount
    return rows


def _ledger_key(ledger: str) -> tuple[int, str]:
    return (
        LEDGER_ORDER.index(ledger) if ledger in LEDGER_ORDER else len(LEDGER_ORDER),
        ledger,
    )


def compare(
    current: list[Cell],
    previous: list[Cell],
    *,
    thresholds,
    cut: str = ALL,
    period: date | None = None,
    previous_period: date | None = None,
) -> Report:
    """Сравнить два месяца по видимым суммам. Чистая функция: базы здесь нет."""
    rules = thresholds if isinstance(thresholds, Thresholds) else thresholds_from(thresholds)

    # Разрез собирается из показанных строк обоих месяцев — как у ведомости, и
    # по той же причине: пустая кнопка с названием регистра тоже сообщает, что
    # он существует.
    available = sorted(
        {cell.ledger for cell in current} | {cell.ledger for cell in previous},
        key=_ledger_key,
    )
    chosen = cut if cut in available else ALL

    now = _by_key(current, chosen)
    before = _by_key(previous, chosen)

    lines: list[Line] = []
    for key in sorted(set(now) | set(before), key=lambda k: (k[0], _ledger_key(k[1]), k[2])):
        _employee_key, ledger, code = key
        body = now.get(key) or before[key]
        line = Line(
            employee=body["employee"], unit=body["unit"], ledger=ledger, code=code,
            title=body["title"],
            previous=before.get(key, {}).get("amount", Decimal(0)),
            current=now.get(key, {}).get("amount", Decimal(0)),
            threshold=rules.for_code(code),
        )
        if line.exceeded:
            lines.append(line)

    return Report(
        lines=lines,
        cut=chosen,
        cuts=available if len(available) > 1 else [],
        compared=len(set(now) | set(before)),
        period=period,
        previous_period=previous_period,
        nothing_to_compare=not before,
    )


def build_variance(tenant_id: UUID, period: date, cut: str = ALL) -> Report:
    """Расхождения периода с предыдущим — из базы, тем же срезом, что ведомость."""
    from core.models import Tenant
    from payrun.rules import select_rules
    from payrun.sheet import collect_cells

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        # Не пустой отчёт: партнёра не видно — значит и права на него нет.
        raise ThresholdsMissing(_("партнёр недоступен"))

    # Правила берутся на **сравниваемый** месяц: пороги версионированы по датам,
    # и отчёт за июнь обязан меряться июньскими, даже если в июле их поменяли.
    rules = select_rules(tenant_id, tenant.country_code, period)
    thresholds = thresholds_from((rules.base or {}).get(SECTION))

    earlier = previous_month(period)
    return compare(
        collect_cells(tenant_id, period),
        collect_cells(tenant_id, earlier),
        thresholds=thresholds, cut=cut, period=period, previous_period=earlier,
    )
