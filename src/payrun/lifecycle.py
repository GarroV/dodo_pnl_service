"""Жизненный цикл расчёта периода со стороны приложения.

Правила переходов живут в базе (миграция `0041_payrun_lifecycle`) и здесь
намеренно не повторяются: второй список статусов рядом с первым разошёлся бы с
ним молча, и приложение предлагало бы то, что база отвергнет.

Отсюда только две вещи, которых у базы быть не может: отказ словами до записи и
отметка «расчёт прошёл».
"""
from __future__ import annotations

from core.models import Payrun

from .errors import PayrunRefused

DRAFT = "draft"
CALCULATED = "calculated"
APPROVED = "approved"
REOPENED = "reopened"

APPROVED_REFUSAL = (
    "Период утверждён: расчёт и его данные заморожены. "
    "Чтобы пересчитать, период нужно сначала открыть заново."
)


def refuse_if_approved(tenant_id, period) -> None:
    """Отказать до записи, если расчёт периода уже утверждён.

    База отвергнет запись и без этого — но ошибкой драйвера, из которой человеку
    ничего не понятно. Порядок тот же, что с регистрами учёта: объясняет
    приложение, гарантирует база.
    """
    status = (
        Payrun.objects.filter(tenant_id=tenant_id, period=period)
        .values_list("status", flat=True)
        .first()
    )
    if status == APPROVED:
        raise PayrunRefused(APPROVED_REFUSAL)


def mark_calculated(payrun_id, *, calculated_at) -> None:
    """Отметить, что расчёт прошёл: время и статус «посчитан».

    Статус выставляется безусловно, без проверки текущего. Так и задумано:
    `draft → calculated` и `reopened → calculated` разрешены, повторный расчёт
    статуса не меняет вовсе, а из утверждённого база не выпустит. Законность
    решает она — условие в этой строке было бы вторым экземпляром правила.
    """
    Payrun.objects.filter(pk=payrun_id).update(
        status=CALCULATED, calculated_at=calculated_at
    )
