"""Справочник должностей (issue #181).

Восьмой справочник, и заведён он ради одного: человека надо заводить в два
клика, а не собирать с нуля. Условия найма — семь полей, и каждому новому
сотруднику они набирались заново: группа, точка, ставка, коэффициент, схема
расчёта, мера работы, регистр. Должность отвечает на них один раз за всех, кто
на ней сидит.

**Должность не заменяет группу.** Группа отвечает на вопрос «как считается
работа этих людей» — схема, регистр, чем меряется труд; она приходит из правил
страны и попадает в P&L строкой затрат. Должность отвечает на вопрос «кого мы
нанимаем»: пиццамейкер, курьер, управляющий. Одной группе законно соответствуют
несколько должностей.

**Вилка ставки — проверка ввода, а не политика оплаты.** Она ловит опечатку
там, где её ещё дёшево исправить: 420 вместо 4200 — это лишняя цифра, а не
низкая ставка, и узнавать о ней на закрытии месяца поздно. Поэтому границы
необязательны: у части должностей ставка договорная, и выдуманная вилка мешала
бы заводить людей.

**Правка должности не трогает нанятых.** Условия найма версионируются по датам
(D020), и молчаливый пересчёт закрытых месяцев всем, кто сидит на этой
должности, — ровно то, что версионирование запрещает. Связь односторонняя:
условия помнят, из какой должности собраны, и живут дальше сами.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import EmployeeGroup, Position

from . import rules
from .dbrefusal import BadInput, ConstraintRefused, saving
from .directory_views import (
    LEDGER_CODES,
    _choice,
    _choice_field,
    _guard,
    _number,
    _preset_now,
    _text,
)
from .format import exact, ledger_title

__all__ = ["position", "positions", "rate_refusal"]

EMPTY = "—"


@login_required
def positions(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    preset = _preset_now(who)
    rows = [
        {
            "url": reverse("directory-position", args=[item.id]),
            "cells": [
                {"text": item.code},
                {"text": item.title},
                {"text": item.group.title},
                {"text": _measure_title(preset, item)},
                {"text": _range_text(item), "num": True},
            ],
        }
        for item in Position.objects.select_related("group").order_by("code")
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Должности"),
        "about": _("Шаблон условий найма: кого нанимаем, в какую группу он попадает "
                   "и в каких границах бывает его ставка."),
        "add_url": reverse("directory-position-new"),
        "add_label": _("Завести должность"),
        "columns": [
            {"label": _("Код")}, {"label": _("Название")}, {"label": _("Группа")},
            {"label": _("Чем меряется работа")}, {"label": _("Вилка ставки")},
        ],
        "rows": rows,
        "empty": _("Должностей нет."),
        "empty_next": _("Пока их нет, условия найма набираются каждому человеку "
                        "руками — это работает, просто дольше."),
    })


def _measure_title(preset, item: Position) -> str:
    """Чем меряется работа — словом из правил страны, а не ключом."""
    if not item.work_measure:
        return _("как у группы")
    measures = (preset or {}).get("work_measures") or {}
    return (measures.get(item.work_measure) or {}).get("title") or item.work_measure


def _range_text(item: Position) -> str:
    """Вилка одной строкой. Пустая граница — открытая, и это видно."""
    if item.rate_from is None and item.rate_to is None:
        return EMPTY
    low = exact(item.rate_from) if item.rate_from is not None else EMPTY
    high = exact(item.rate_to) if item.rate_to is not None else EMPTY
    return f"{low} … {high}"


def rate_refusal(position: Position | None, rate: Decimal | None) -> str:
    """Почему эта ставка не годится должности. Пусто — годится.

    Отдельной функцией, потому что спрашивают её двое: форма должности (при
    правке границ) и форма найма (при вводе ставки человеку). Две копии одного
    правила разъехались бы молча — и вилка перестала бы держать ровно там, где
    она нужна, то есть при заведении человека.
    """
    if position is None or rate is None:
        return ""
    if position.rate_from is not None and rate < position.rate_from:
        return _("Ставка %(rate)s ниже вилки должности «%(title)s»: от %(low)s.") % {
            "rate": exact(rate), "title": position.title,
            "low": exact(position.rate_from),
        }
    if position.rate_to is not None and rate > position.rate_to:
        return _("Ставка %(rate)s выше вилки должности «%(title)s»: до %(high)s.") % {
            "rate": exact(rate), "title": position.title,
            "high": exact(position.rate_to),
        }
    return ""


@login_required
def position(request, position_id=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    item = None
    if position_id is not None:
        item = Position.objects.filter(pk=position_id).first()
        if item is None:
            # Чужая должность и несуществующая отвечают одинаково (D023).
            raise Http404("должность не найдена")

    preset = _preset_now(who)
    error, status = "", 200
    if request.method == "POST":
        try:
            code = _text(request, "code", _("Код"))
            title = _text(request, "title", _("Название"))
            group_id = _choice(
                request, "group", _("Группа"),
                list(EmployeeGroup.objects.values_list("id", flat=True)),
            )
            measure = request.POST.get("work_measure") or None
            scheme = request.POST.get("scheme") or None
            ledger = request.POST.get("ledger") or None
            contract_hours = _number(
                request, "contract_hours", _("Часы по договору"), required=False,
            )
            rate_from = _number(request, "rate_from", _("Ставка от"), required=False)
            rate_to = _number(request, "rate_to", _("Ставка до"), required=False)
            if rate_from is not None and rate_to is not None and rate_to < rate_from:
                # То же, что стережёт ограничение базы, — но сказанное словами:
                # перевёрнутая вилка не пропустит ни одной ставки, и заведение
                # людей встало бы без объяснения.
                raise BadInput(
                    _("Вилка перевёрнута: «до» меньше, чем «от». В такую границу "
                      "не попадёт ни одна ставка.")
                )

            if item is None:
                item = Position(tenant_id=who.tenant_id)
            item.code, item.title, item.group_id = code, title, group_id
            item.work_measure, item.scheme, item.ledger = measure, scheme, ledger
            item.rate_from, item.rate_to = rate_from, rate_to
            item.contract_hours = contract_hours
            with saving():
                item.save()
            return redirect(reverse("directory-positions"))
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error, status = bad.message, bad.http_status

    return render(request, "web/directory/form.html", {
        "heading": item.title if item else _("Новая должность"),
        "back_url": reverse("directory-positions"),
        "back_label": _("← К должностям"),
        "error": error,
        "submit_label": _("Сохранить"),
        "fields": [
            {"kind": "text", "name": "code", "label": _("Код"), "required": True,
             "value": item.code if item else "",
             "help": _("Короткий код, например pizzamaker.")},
            {"kind": "text", "name": "title", "label": _("Название"), "required": True,
             "value": item.title if item else ""},
            _select_group(item),
            _choice_field(
                "work_measure", _("Чем меряется работа"),
                rules.measure_choices(preset or {}),
                (item.work_measure if item else None) or None,
                required=False, empty_label=_("как у группы"),
                help=_("Пусто — как у группы. Оклад ставится здесь: тогда «Ставка» "
                       "у человека будет означать сумму за месяц."),
            ),
            _choice_field(
                "scheme", _("Схема расчёта"), rules.scheme_choices(preset or {}),
                (item.scheme if item else None) or None,
                required=False, empty_label=_("как у группы"),
            ),
            _choice_field(
                "ledger", _("Регистр учёта"),
                [(code, ledger_title(code)) for code in LEDGER_CODES
                 if code in who.visible_ledgers],
                (item.ledger if item else None) or None,
                required=False, empty_label=_("как у группы"),
            ),
            {"kind": "number", "name": "contract_hours", "label": _("Часы по договору"),
             "value": (exact(item.contract_hours)
                       if item and item.contract_hours is not None else ""),
             "help": _("Умолчание для нанятых на эту должность. В условиях найма "
                       "человека величину можно поставить свою.")},
            {"kind": "number", "name": "rate_from", "label": _("Ставка от"),
             "value": exact(item.rate_from) if item and item.rate_from is not None else "",
             "help": _("Нижняя граница. Пусто — границы нет: ставка договорная.")},
            {"kind": "number", "name": "rate_to", "label": _("Ставка до"),
             "value": exact(item.rate_to) if item and item.rate_to is not None else "",
             "help": _("Верхняя граница. Она ловит лишнюю цифру при заведении "
                       "человека, пока ошибку ещё дёшево исправить.")},
        ],
    }, status=status)


def _select_group(item: Position | None) -> dict:
    return _choice_field(
        "group", _("Группа"),
        list(EmployeeGroup.objects.order_by("title").values_list("id", "title")),
        item.group_id if item else None, required=True,
        help=_("Куда попадёт нанятый: группа решает, как считается его работа "
               "и в какую строку P&L уходят его деньги."),
    )
