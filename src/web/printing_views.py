"""Печатные формы на экране: платёжная ведомость и расчётный листок (T187).

Разделение то же, что во всём блоке: `reports.printing` собирает документ и
считает, на сколько листов он ложится, а здесь подбираются слова, формат чисел
и адреса. Причина не в аккуратности — печатная ведомость обязана содержать те же
числа, что видит человек, и собираться она должна тем же кодом, а не вторым
рядом.

**Разрез по регистру эти формы не принимают.** «Начислено», «удержано» и «к
выплате» посчитаны по строке ведомости целиком; регистру они не принадлежат
(тот же довод, что у налогов в T141). Поэтому ссылка со страницы периода уходит
БЕЗ `?ledger=`, а подобранный руками адрес с разрезом получает отказ со своими
словами — молча показать документ всего расчёта там, где человек просил разрез,
значило бы разойтись с экраном ровно на бумаге.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext as _

from payrun.sheet import LEDGER_ORDER
from reports import printing
from reports.words import in_words

from . import runslice
from .format import day, hours, money
from .i18n import month_title
from .labels import labeller
from .principal import get_current_principal
from .views import find_period


def asked_cut(request) -> str:
    """Разрез, который человек действительно просил.

    Значение из адреса — не разрез, пока оно не название регистра: мусор в
    `?ledger=` продукт уже сводит ко «всем видимым» (D023), и отказывать на него
    было бы отказом на опечатку. А вот настоящий регистр — просьба, на которую
    честного ответа нет, и она получает отказ словами.
    """
    asked = request.GET.get("ledger", "")
    return asked if asked in LEDGER_ORDER else ""


def refused(request, code: str, back: str, status: int = 200):
    """Страница вместо документа: почему бумаги нет и что делать дальше."""
    return render(
        request,
        "web/print/refused.html",
        {
            "title": _("Печатная форма"),
            "reason": printing.refusal_text(code),
            "back": back,
        },
        status=status,
    )


def stamp() -> str:
    """Когда документ выведен. Дата и время: за день ведомость пересчитывают."""
    now = timezone.localtime()
    return f"{day(now.date())}, {now:%H:%M}"


@login_required
def payout(request, period_id):
    """Платёжная ведомость периода — та, под которой расписываются."""
    period = find_period(period_id)
    cut = asked_cut(request)
    whole_run = runslice.sees_whole_run(period.tenant_id)

    # Документ не собирается, пока не ясно, что его вообще можно собрать: у
    # роли без итогов выборка вернула бы пустоту, и «расчёт не выполнен» было
    # бы прямой неправдой (порядок причин — в `printing.payout_refusal`).
    doc = (
        printing.build_payout(period.tenant_id, period.period)
        if not cut and whole_run else None
    )
    back = f"/periods/{period.id}/"
    reason = printing.payout_refusal(
        has_rows=bool(doc), whole_run=whole_run, cut=cut,
    )
    if reason:
        return refused(request, reason, back)

    return render(
        request,
        "web/print/payout.html",
        {
            "title": _("Платёжная ведомость"),
            "back": back,
            "head": {
                "entity": doc.entity,
                "tax_number": doc.tax_number,
                "title": _("Платёжная ведомость за %(month)s")
                % {"month": month_title(doc.period).lower()},
                # Регистры не перечисляются: ведомость печатается по всему
                # расчёту, и «все регистры» — это и есть весь расчёт.
                "sub": " · ".join(
                    part for part in (", ".join(doc.units), doc.currency) if part
                ),
                "number": doc.number,
                "stamp": stamp(),
            },
            "leaves": [
                {
                    "first": leaf.first,
                    "last": leaf.last,
                    "number": leaf.number,
                    "of": leaf.of,
                    "rows": [
                        {
                            "number": row.number,
                            "employee": row.employee,
                            "position": row.position,
                            "hours": hours(row.hours),
                            "accrued": money(row.accrued),
                            "held": money(row.held),
                            "paid": money(row.paid),
                        }
                        for row in leaf.rows
                    ],
                }
                for leaf in doc.leaves
            ],
            "totals": {
                "people": doc.people,
                "hours": hours(doc.hours),
                "accrued": money(doc.accrued),
                "held": money(doc.held),
                "paid": money(doc.paid),
            },
            # Сумма прописью — не украшение: по ней сверяют цифру при подписи,
            # и она обязана быть на языке документа, а не на языке исходника.
            "in_words": in_words(
                doc.paid, language=get_language() or "", currency=doc.currency,
            ),
            "calculated_by": doc.calculated_by,
            "approved_by": doc.approved_by,
        },
    )


@login_required
def payslip(request, payslip_id):
    """Расчётный листок одного человека — тот, что отдают ему на руки."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        raise Http404("строка ведомости не найдена")

    back = f"/payslips/{payslip_id}/trace/"
    try:
        doc = printing.build_slip(who.tenant_id, payslip_id)
    except printing.SlipWithheld:
        # Строка видна, а её итоги — нет: причина называется словами. Молчание
        # тут читалось бы как поломка, а нули на бумаге — как утверждение о
        # деньгах, которого никто не делал.
        return refused(request, printing.TOTALS_WITHHELD, back)
    except printing.SlipNotFound as missing:
        # Чужая строка и несуществующая отвечают одинаково — иначе перебором
        # адресов узнаётся, что строка есть и просто не видна (как у следа).
        raise Http404("строка ведомости не найдена") from missing

    # Подписи позиций — на языке страницы, по правилам того же периода (T092).
    label = labeller(who.tenant_id, _country_of(who.tenant_id), doc.period)

    return render(
        request,
        "web/print/payslip.html",
        {
            "title": _("Расчётный листок"),
            "back": back,
            "head": {
                "entity": doc.entity,
                "tax_number": doc.tax_number,
                "title": _("Расчётный листок за %(month)s")
                % {"month": month_title(doc.period).lower()},
                "sub": doc.currency,
                "number": doc.number,
                "stamp": stamp(),
            },
            "employee": doc.employee,
            "position": " · ".join(part for part in (doc.position, doc.unit) if part),
            "hours": hours(doc.hours),
            "norm_hours": hours(doc.norm_hours),
            "lines": [
                {
                    "title": label(line.code, line.title),
                    "formula": line.formula,
                    "amount": money(line.amount),
                }
                for line in doc.lines
            ],
            "accrued": money(doc.accrued),
            "held": money(doc.held) if doc.held else "",
            "to_bank": money(doc.to_bank),
            "to_cash": money(doc.to_cash) if doc.to_cash else "",
            "paid": money(doc.paid),
            "gross": money(doc.gross),
            "tax": money(doc.tax),
            "contributions": money(doc.contributions),
            "currency": doc.currency,
        },
    )


def _country_of(tenant_id) -> str:
    from core.models import Tenant

    return Tenant.objects.filter(id=tenant_id).values_list(
        "country_code", flat=True
    ).first() or ""
