"""Заморозка строки ведомости: спорный сотрудник не держит остальных (T027).

Порядок тот же, что у прав роли, видимости регистров и закрытия точек:
**гарантирует база** (триггеры и политики миграции `0050`), **объясняет
приложение**. Второго движка запретов здесь нет — есть чтение состояния и
формулировки, которых у базы быть не может: она отвечает кодом ошибки, из
которого человеку ничего не понятно.

Заморозка относится к одной строке ведомости и живёт **внутри неутверждённого
периода**: утверждение морозит расчёт целиком и поглощает её. Отсюда порядок
отказов — от периода к строке, и он написан в одном месте (`refuse_if_frozen`
зовётся после `refuse_if_approved`), а не разбросан по обработчикам.

Чего здесь нет: заморозка **не трогает табель**. Замороженная строка означает
«не пересчитывать этого человека», а не «не править его часы» — правка входных
данных задним числом это T026, и запрет на неё здесь решил бы её за неё.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from django.utils.timezone import now
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop as noop

from core.models import Payslip, PayslipFreeze

from .errors import PayrunRefused, ReasonRequired

__all__ = [
    "FROZEN_REFUSAL", "REASON_REFUSAL", "active_freezes", "freeze",
    "frozen_payslip_ids", "refuse_if_frozen", "release",
]

REASON_REFUSAL = noop(
    "Заморозка строки требует причины: напишите, из-за чего идёт спор. "
    "Причина попадёт в историю рядом с вашим именем."
)

FROZEN_REFUSAL = noop(
    "Строка сотрудника заморожена: её числа не меняются и пересчёт её обходит. "
    "Чтобы вернуть человека в общий расчёт, заморозку нужно снять."
)

APPROVED_REFUSAL = noop(
    "Период утверждён: замораживать в нём нечего — заморожен весь расчёт. "
    "Чтобы менять расчёт, период нужно сначала открыть заново."
)


def active_freezes(tenant_id: UUID, period: date) -> dict[UUID, PayslipFreeze]:
    """Действующие заморозки периода — по строкам ведомости.

    Что видно, решают политики: у управляющего в ответе будут только строки его
    точки. Приложение выборку не сужает — второй фильтр рядом с политикой и есть
    тот способ, которым доступ расходится сам с собой (D014).
    """
    return {
        item.payslip_id: item
        for item in PayslipFreeze.objects.filter(
            tenant_id=tenant_id,
            payslip__payrun__period=period,
            released_at__isnull=True,
        )
    }


def frozen_payslip_ids(payrun_id: UUID) -> set[UUID]:
    """Строки расчёта, которые пересчёт обязан обойти стороной."""
    return set(
        PayslipFreeze.objects.filter(
            payslip__payrun_id=payrun_id, released_at__isnull=True
        ).values_list("payslip_id", flat=True)
    )


def refuse_if_frozen(payslip_id: UUID) -> None:
    """Отказать словами, если строка заморожена.

    База отвергнет запись и без этого — сообщением триггера, в котором человеку
    непонятно ни что случилось, ни что делать.
    """
    if PayslipFreeze.objects.filter(
        payslip_id=payslip_id, released_at__isnull=True
    ).exists():
        raise PayrunRefused(_(FROZEN_REFUSAL))


def _refuse_if_period_approved(payslip: Payslip) -> None:
    """Порядок отказов — снаружи внутрь: сначала период, потом строка.

    У утверждённого периода причина одна и та же, сколько бы ни было других
    поводов, и человеку нужна именно она: «строка заморожена» ничего не
    объясняет там, где заморожен весь расчёт.
    """
    if payslip.payrun.status == "approved":
        raise PayrunRefused(_(APPROVED_REFUSAL))


def freeze(payslip: Payslip, *, actor_id, reason: str) -> str:
    """Заморозить строку. Без причины — отказ, и не только в форме.

    Причина проверяется здесь, чтобы человек получил текст, а не ошибку
    драйвера; сам запрет держит ограничение схемы
    (`payslip_freezes_reason_not_blank`), то есть любой путь записи.
    """
    _refuse_if_period_approved(payslip)
    reason = (reason or "").strip()
    if not reason:
        raise ReasonRequired(_(REASON_REFUSAL))

    if PayslipFreeze.objects.filter(
        payslip_id=payslip.pk, released_at__isnull=True
    ).exists():
        # Повторное нажатие не должно падать ошибкой уникального индекса: строка
        # уже заморожена, а значит, человек получил то, чего хотел.
        return reason

    PayslipFreeze.objects.create(
        tenant_id=payslip.tenant_id, payslip_id=payslip.pk,
        reason=reason, frozen_by=actor_id,
    )
    return reason


def release(payslip: Payslip, *, actor_id) -> None:
    """Снять заморозку: человек возвращается в общий расчёт.

    Заморозка не удаляется, а помечается — «почему морозили и кто» единственная
    запись о споре, и стирать её нельзя (то же решение, что у закрытия точки).

    Причина не требуется, в отличие от отката утверждённого периода: снятие
    ничего не переписывает, а нажавший кнопку по ошибке не должен ждать, пока
    его разморозят обратно.

    Снятие разрешено и в утверждённом периоде: числа там держит заморозка
    периода, а застрявшую навсегда строку нечем было бы убрать.
    """
    PayslipFreeze.objects.filter(
        payslip_id=payslip.pk, released_at__isnull=True
    ).update(released_at=now(), released_by=actor_id)
