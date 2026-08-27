"""Готовность месяца к закрытию: чего не хватает и что подозрительно (#175).

Эталон (модуль 4) формулирует правило дословно: «список „прежде чем закрыть“ —
не отчёт, который надо открыть, а условие, без которого кнопка не нажимается».
До этого утвердить период можно было при незакрытых часах, неразобранных
бумагах и строках без статьи — и P&L получался неверным молча.

**Три вида находок, и разница между ними существенная.**

* `BLOCKING` — из-за этого P&L будет неверным. Утверждение не проходит.
* `WARNING` — стоит посмотреть, но закрыть не мешает.
* `SUSPICIOUS` — может быть правдой: рост вдвое, ноль там, где обычно не ноль.
  Система не знает, ошибка это или жизнь, поэтому предупреждает и не мешает.

**Блокирующее можно отложить с причиной.** Без этого проверка превращается в
тупик: часть находок законна («выписка придёт послезавтра, а зарплату платить
сегодня»), и запрет без выхода заставит закрывать месяц мимо продукта — то есть
хуже, чем не проверять вовсе. Причина остаётся в протоколе закрытия.

**Почему модуль отдельный, а не проверка внутри `approve`.** Спрашивают его
двое: страница месяца — чтобы показать список рядом с кнопкой, и само
утверждение — чтобы отказать. Разъехавшись, они дали бы худшее из возможного:
экран говорит «всё чисто», а кнопка отвечает отказом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.utils.translation import gettext as _

__all__ = ["BLOCKING", "SUSPICIOUS", "WARNING", "Finding", "Readiness", "check"]

BLOCKING = "blocking"
WARNING = "warning"
SUSPICIOUS = "suspicious"


@dataclass(frozen=True)
class Finding:
    """Одна находка. `code` — чтобы её можно было отложить по имени."""

    code: str
    kind: str
    title: str
    detail: str = ""


@dataclass(frozen=True)
class Readiness:
    findings: list[Finding]
    postponed: dict[str, str]

    @property
    def blocking(self) -> list[Finding]:
        """Только то, что мешает закрытию и ещё не отложено."""
        return [
            found for found in self.findings
            if found.kind == BLOCKING and found.code not in self.postponed
        ]

    @property
    def ready(self) -> bool:
        return not self.blocking

    def of_kind(self, kind: str) -> list[Finding]:
        return [found for found in self.findings if found.kind == kind]


def check(tenant_id: UUID, period: date) -> Readiness:
    """Что мешает закрыть этот месяц. Ничего не пишет и ничего не решает."""
    findings: list[Finding] = []
    findings += _unit_hours(tenant_id, period)
    findings += _papers(tenant_id, period)
    findings += _unclassified(tenant_id, period)
    findings += _suspicious(tenant_id, period)
    return Readiness(findings=findings, postponed=_postponed(tenant_id, period))


def _unit_hours(tenant_id: UUID, period: date) -> list[Finding]:
    """Точки, у которых часы за месяц не закрыты.

    Часы — вход расчёта. Утверждённый месяц с открытым табелем означает, что
    завтра кто-то поправит часы, а ведомость останется прежней.
    """
    from core.models import Unit
    from timesheets.closing import open_closures

    closed = set(open_closures(tenant_id, period))
    units = list(Unit.objects.filter(tenant_id=tenant_id, closed_at__isnull=True))
    waiting = [unit for unit in units if unit.id not in closed]
    if not waiting:
        return []

    # Блокирует только там, где закрытием часов пользуются. Если партнёр не
    # закрыл ни одной точки, механизм у него не в ходу — требовать его при
    # утверждении значит запереть месяц кнопкой, о которой человек не знает.
    # Закрыл хоть одну — значит работает так, и остальные точки не доделаны.
    #
    # Само по себе незакрытие часов не делает P&L неверным: числа уже посчитаны
    # из внесённых часов. Оно означает другое — что работа над месяцем не
    # закончена, и это разной силы утверждение в зависимости от того, как
    # партнёр ведёт месяц.
    kind = BLOCKING if closed else WARNING
    return [Finding(
        code="unit_hours",
        kind=kind,
        title=_("Часы точки не закрыты: %(units)s") % {
            "units": ", ".join(unit.code for unit in waiting),
        },
        detail=_("Пока часы открыты, их могут поправить после утверждения — и "
                 "ведомость разойдётся с табелем."),
    )]


def _papers(tenant_id: UUID, period: date) -> list[Finding]:
    """Бумаги с точек, которые никто не разобрал.

    Разобрана — значит у документа появились строки; отдельного признака нет
    намеренно (`web/papers`). Пока строк нет, денег этой бумаги в P&L нет вовсе.
    """
    from core.models import SourceDocument

    waiting = SourceDocument.objects.filter(
        tenant_id=tenant_id, handed_over_at__isnull=False, fact__isnull=True,
    ).count()
    if not waiting:
        return []
    return [Finding(
        code="papers",
        kind=BLOCKING,
        title=_("Бумаг с точек ждут разбора: %(count)s") % {"count": waiting},
        detail=_("Пока бумага не разобрана, её денег в отчёте нет — не потому "
                 "что отфильтровали, а потому что фактов по ней не существует."),
    )]


def _unclassified(tenant_id: UUID, period: date) -> list[Finding]:
    """Строки без статьи: деньги уже в отчёте, но не там, где их будут искать."""
    # Тем же запросом, что и сам инбокс (`web.suppliers.waiting_for_an_article`):
    # отбор идёт по служебной строке P&L, а не по пустой статье. Пусто у
    # половины продукта — у зарплаты, выручки, переводов, — и отбор по нему
    # втащил бы сюда всё, что статьи не имеет по своей природе. Вторая копия
    # этого правила разъехалась бы с инбоксом молча: закрытие требовало бы
    # разобрать то, чего в инбоксе нет.
    from web.suppliers import waiting_for_an_article

    waiting = len([
        row for row in waiting_for_an_article(None)
        if row.tenant_id == tenant_id and row.period == period
    ])
    if not waiting:
        return []
    return [Finding(
        code="inbox",
        kind=BLOCKING,
        title=_("Строк без статьи: %(count)s") % {"count": waiting},
        detail=_("Деньги в отчёте есть, но лежат не в той строке, где их "
                 "будут искать."),
    )]


def _suspicious(tenant_id: UUID, period: date) -> list[Finding]:
    """Отклонения от прошлого месяца — то, что может быть правдой.

    Закрыть не мешают: система не знает, ошибка это или жизнь. Но молчать
    нельзя — рост вдвое человек обязан увидеть до утверждения, а не в отчёте.
    """
    from reports.variance import ThresholdsMissing, build_variance

    try:
        report = build_variance(tenant_id, period)
    except (ThresholdsMissing, Exception):  # noqa: BLE001
        # Расхождения — подсказка, а не условие: их отсутствие не должно мешать
        # закрыть месяц и не должно ронять страницу.
        return []
    rows = getattr(report, "rows", [])
    if not rows:
        return []
    return [Finding(
        code="variance",
        kind=SUSPICIOUS,
        title=_("Отклонений от прошлого месяца: %(count)s") % {"count": len(rows)},
        detail=_("Может быть правдой — но проверить стоит до утверждения."),
    )]


def _postponed(tenant_id: UUID, period: date) -> dict[str, str]:
    """Что уже отложено с причиной: код находки → причина."""
    from core.models import ClosingWaiver

    return {
        waiver.finding: waiver.reason
        for waiver in ClosingWaiver.objects.filter(tenant_id=tenant_id, period=period)
    }
