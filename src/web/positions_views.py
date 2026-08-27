"""Справочник должностей (issue #181).

Восьмой справочник, и заведён он ради одного: человека надо заводить в два
клика, а не собирать с нуля. Условия найма — семь полей, и каждому новому
сотруднику они набирались заново: группа, точка, ставка, коэффициент, схема
расчёта, мера работы, регистр. Должность отвечает на них один раз за всех, кто
на ней сидит.

**Должность не повторяет группу, и это проверено вопросом.** Владелец, читая
гайд, спросил про шаги «Группы» и «Должности»: «разве не об одном и том же?» —
и был прав: у должности стояли те же схема, регистр и мера работы, что задаёт
группа. Три места на один факт расходятся молча, поэтому их здесь больше нет.

Разделение такое: **группа** отвечает, КАК считаются деньги и куда они попадают
в отчёте (схема, регистр, мера, строка P&L); **должность** — КОГО нанимаем: в
какую группу человек попадает, в каких границах его ставка и сколько часов у
него по договору. Одной группе законно соответствуют несколько должностей —
пиццамейкер и старший смены считаются одинаково, а нанимаются по-разному.

**Вилки ставки здесь нет** (снято 27.08.2026, решение владельца): она не
участвовала ни в одной формуле, а вести её при каждой индексации пришлось бы
руками. Опечатку в ставке ловим сравнением с тем, что уже стоит у людей той же
группы, — без справочника, который надо поддерживать.

**Правка должности не трогает нанятых.** Условия найма версионируются по датам
(D020), и молчаливый пересчёт закрытых месяцев всем, кто сидит на этой
должности, — ровно то, что версионирование запрещает. Связь односторонняя:
условия помнят, из какой должности собраны, и живут дальше сами.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import EmployeeGroup, Position

from .dbrefusal import BadInput, ConstraintRefused, saving
from .directory_views import (
    _choice,
    _choice_field,
    _guard,
    _number,
    _text,
)
from .format import exact

__all__ = ["position", "positions"]

EMPTY = "—"


@login_required
def positions(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    rows = [
        {
            "url": reverse("directory-position", args=[item.id]),
            "cells": [
                {"text": item.code},
                {"text": item.title},
                {"text": item.group.title},
                {"text": exact(item.contract_hours) if item.contract_hours is not None else EMPTY,
                 "num": True},
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
            {"label": _("Часы по договору")},
        ],
        "rows": rows,
        "empty": _("Должностей нет."),
        "empty_next": _("Пока их нет, условия найма набираются каждому человеку "
                        "руками — это работает, просто дольше."),
    })


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

    error, status = "", 200
    if request.method == "POST":
        try:
            code = _text(request, "code", _("Код"))
            title = _text(request, "title", _("Название"))
            group_id = _choice(
                request, "group", _("Группа"),
                list(EmployeeGroup.objects.values_list("id", flat=True)),
            )
            contract_hours = _number(
                request, "contract_hours", _("Часы по договору"), required=False,
            )
            if item is None:
                item = Position(tenant_id=who.tenant_id)
            item.code, item.title, item.group_id = code, title, group_id
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
            {"kind": "number", "name": "contract_hours", "label": _("Часы по договору"),
             "value": (exact(item.contract_hours)
                       if item and item.contract_hours is not None else ""),
             "help": _("Умолчание для нанятых на эту должность. В условиях найма "
                       "человека величину можно поставить свою.")},
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
