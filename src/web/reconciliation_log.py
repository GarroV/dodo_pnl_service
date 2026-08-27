"""Протокол сверки: что записываем и что показываем потом (issue #172).

Эталон (модуль 2): «остаётся в системе: файл, дата, кто сверял, каждое
расхождение и решение по нему. Через полгода на вопрос „почему в мае было так“
отвечает протокол, а не память».

**Файла в протоколе нет** — D028: в таблице партнёра ФИО и суммы живых людей.
Записывается результат сравнения: имя файла, счётчики и расхождения со ссылкой
на наших же сотрудников. Новых персональных данных не появляется.

**Записывает тот, кто сверяет.** Своего права у сверки нет: она видит то, что
база отдаёт роли открывшего. Но протокол — это запись, и её пускает политика
`payrun.calculate`: сверяет тот, кто ведёт месяц.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from django.utils import timezone

from core.models import Employee, Reconciliation, ReconciliationFinding

__all__ = ["DECISIONS", "decide", "history", "record"]

# Решения человека по расхождению — из эталона: «три признаны нашей стороной,
# одно — ошибкой в файле, одно — разной трактовкой правила».
DECISIONS = {
    "ours": "наша сторона",
    "theirs": "ошибка в файле",
    "rule": "разная трактовка правила",
}


def record(*, tenant_id: UUID, period: date, file_name: str, result, actor_id):
    """Записать протокол сверки. Возвращает запись или None, если нельзя.

    Отказ политики здесь не ошибка экрана: сверку смотрит и тот, кто месяц не
    ведёт, — ему показывается результат, а протокол не пишется. Молчать об этом
    можно: он и не просил ничего сохранять.
    """
    from django.db import DatabaseError, transaction

    try:
        with transaction.atomic():
            entry = Reconciliation.objects.create(
                tenant_id=tenant_id, period=period,
                file_name=file_name[:200],
                lines=len(result.lines),
                matched=result.matched,
                rounding=result.rounding,
                differing=sum(
                    1 for line in result.lines
                    if line.compared and not line.matched and not line.rounding_only
                ),
                created_by=actor_id,
            )
            _store_findings(entry, result)
            return entry
    except DatabaseError:
        # Право на запись даёт политика базы; её отказ означает «этой роли
        # протокол вести не положено», а не поломку сверки.
        return None


def _store_findings(entry: Reconciliation, result) -> None:
    """Расхождения — по нашим сотрудникам, найденным по сквозному ключу."""
    keys = {line.key for line in result.lines if line.key}
    people = {
        person.external_id: person.id
        for person in Employee.objects.filter(
            tenant_id=entry.tenant_id, external_id__in=list(keys),
        )
    }
    rows = []
    for line in result.lines:
        if not line.compared or line.matched or line.rounding_only:
            continue
        employee_id = people.get(line.key)
        if employee_id is None:
            # Человека из файла у нас нет — это не расхождение суммы, а другая
            # находка, и она уже показана отдельным списком «есть в таблице,
            # нет в расчёте». Привязывать её не к кому.
            continue
        for amount in line.amounts:
            # `actual` — наш расчёт, `expected` — то, что в файле бухгалтера.
            # Сравниваются только те, где есть обе стороны: непрочитанное поле
            # не ноль, и принять его за ноль значит объявить расхождение на всю
            # сумму там, где сравнивать нечего.
            if not amount.comparable or amount.matches or amount.rounding:
                continue
            ours = _number(amount.actual)
            theirs = _number(amount.expected)
            if ours is None or theirs is None:
                continue
            rows.append(ReconciliationFinding(
                tenant_id=entry.tenant_id, record=entry, employee_id=employee_id,
                component=amount.code,
                ours=ours, theirs=theirs,
            ))
    if rows:
        ReconciliationFinding.objects.bulk_create(rows)


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — чужой файл приносит что угодно
        return None


def history(tenant_id: UUID, period: date) -> list[dict]:
    """Прошлые сверки этого месяца — от свежей к старой."""
    from .format import day

    return [
        {
            "id": str(entry.id),
            "file_name": entry.file_name,
            "when": day(entry.created_at.date()),
            "lines": entry.lines,
            "matched": entry.matched,
            "rounding": entry.rounding,
            "differing": entry.differing,
            "undecided": entry.findings.filter(decision__isnull=True).count(),
        }
        for entry in (
            Reconciliation.objects.filter(tenant_id=tenant_id, period=period)
            .order_by("-created_at")[:10]
        )
    ]


def decide(finding: ReconciliationFinding, *, decision: str, note: str, actor_id):
    """Записать решение по расхождению. Без объяснения — отказ.

    Объяснение обязательно и в базе: «ошибка в файле» без слов через полгода не
    отличить от «не разбирались».
    """
    if decision not in DECISIONS or not note.strip():
        return False
    finding.decision = decision
    finding.note = note.strip()
    finding.decided_at = timezone.now()
    finding.decided_by = actor_id
    finding.save(update_fields=["decision", "note", "decided_at", "decided_by"])
    return True
