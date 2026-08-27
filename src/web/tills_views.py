"""Справочник касс (T145, D039).

Седьмой справочник, и заведён он не ради полноты, а потому что регистр учёта
расхода следует из кассы. Ответ владельца на Q013 дословно: «Из кассы берем
только официально. Но есть чёрная касса, где по дефолту идёт в чёрную». Человек
на точке не выбирает регистр учёта — он берёт деньги из одной коробки или из
другой. Значит коробок у точки несколько, у каждой своя точка и свой регистр, и
это справочник, а не поле у расхода.

**Остатка по кассе здесь нет и не будет** (D040): «Кассу не трогаем нигде же? У
нас сервис по сборке ПНЛ». Ни прихода, ни сальдо, ни движения — касса нужна как
источник денег и признак регистра, а не как кассовая книга.

**Что видно роли, решает база.** Ни `filter(unit_id__in=...)`, ни проверки
регистра здесь нет: список идёт выборкой как есть, а лишнее отсекают
ограничивающие политики `unit_visibility` и `ledger_visibility` на `tills`
(D014). Отсюда же поведение адреса по номеру: чужая касса и несуществующая
отвечают одинаково — 404 (D023).

**Регистр в списке касс не лишний.** Управляющий точки видит два регистра из
трёх (D031), поэтому касса внутреннего регистра ему не видна вовсе. Это не
решение этого экрана, а D031, применённый к новому справочнику: предлагать
кассу, из которой запись всё равно отвергнет `ledger_visibility` на `facts`,
означало бы обещать отказ.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import Till, Unit

from .dbrefusal import BadInput, ConstraintRefused, saving
from .directory_views import LEDGER_CODES, _choice, _date, _guard, _select, _text
from .format import day, ledger_title


@login_required
def tills(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    rows = [
        {
            "url": reverse("directory-till", args=[till.id]),
            "cells": [
                {"text": till.code},
                {"text": till.title},
                {"text": till.unit.code},
                {"text": ledger_title(till.ledger)},
                {"text": day(till.closed_at)},
            ],
        }
        for till in visible_tills()
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Кассы"),
        "about": _("Коробки, из которых платят наличными. Регистр учёта расхода "
                   "приезжает из кассы, а не выбирается к каждой трате."),
        "add_url": reverse("directory-till-new"),
        "add_label": _("Завести кассу"),
        "columns": [
            {"label": _("Код")}, {"label": _("Название")}, {"label": _("Точка")},
            {"label": _("Регистр учёта")}, {"label": _("Закрыта")},
        ],
        "rows": rows,
        "empty": _("Касс нет."),
        "empty_next": _("Пока кассы не заведены, расход вносится без неё, а регистр "
                        "учёта выбирается руками."),
    })


def visible_tills(*, open_only: bool = False):
    """Кассы, видимые роли. Срез делает база, здесь только порядок.

    `open_only` — для списков выбора: закрытая касса остаётся в справочнике
    (на неё ссылаются факты закрытых месяцев), но предлагать её к новой трате
    незачем.
    """
    found = Till.objects.select_related("unit").order_by("unit__code", "code")
    if open_only:
        found = found.filter(closed_at__isnull=True)
    return found


@login_required
def till(request, till_id=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    item = None
    if till_id is not None:
        item = Till.objects.filter(pk=till_id).first()
        if item is None:
            # Чужая касса и несуществующая — один ответ: по нему нельзя понять,
            # что касса существует у другой точки (D023).
            raise Http404("касса не найдена")

    error, status = "", 200
    if request.method == "POST":
        try:
            code = _text(request, "code", _("Код"))
            title = _text(request, "title", _("Название"))
            # Точка уходит в базу выбором из списка, но решает не список:
            # запись кассы на чужую точку отвергает политика `unit_visibility`
            # на `tills`. Список здесь — удобство, а не защита (D014).
            unit_id = _choice(
                request, "unit", _("Точка"),
                list(Unit.objects.values_list("id", flat=True)),
            )
            # Регистр — только из видимых роли: завести кассу в регистр,
            # которого сам не видишь, значит потерять её из своего же списка.
            ledger = _choice(
                request, "ledger", _("Регистр учёта"),
                [code for code in LEDGER_CODES if code in who.visible_ledgers],
            )
            closed_at = _date(request, "closed_at", _("Закрыта"))

            if item is None:
                item = Till(tenant_id=who.tenant_id)
            item.code, item.title = code, title
            item.unit_id, item.ledger, item.closed_at = unit_id, ledger, closed_at
            with saving():
                item.save()
            return redirect(reverse("directory-tills"))
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error, status = bad.message, bad.http_status

    return render(request, "web/directory/form.html", {
        "heading": item.title if item else _("Новая касса"),
        "back_url": reverse("directory-tills"),
        "back_label": _("← К кассам"),
        "error": error,
        "submit_label": _("Сохранить"),
        "fields": [
            {"kind": "text", "name": "code", "label": _("Код"), "required": True,
             "value": item.code if item else "",
             "help": _("Короткий код кассы, например NS1-main.")},
            {"kind": "text", "name": "title", "label": _("Название"), "required": True,
             "value": item.title if item else ""},
            _select(
                "unit", _("Точка"),
                Unit.objects.order_by("code").values_list("id", "code"),
                item.unit_id if item else None, required=True,
                help=_("Касса стоит на точке, и расход из неё — расход этой точки."),
            ),
            _select(
                "ledger", _("Регистр учёта"),
                [(code, ledger_title(code)) for code in LEDGER_CODES
                 if code in who.visible_ledgers],
                item.ledger if item else "official", required=True,
                help=_("В него по умолчанию попадёт расход, оплаченный из этой кассы. "
                       "Поменять регистр у отдельной траты по-прежнему можно."),
            ),
            {"kind": "date", "name": "closed_at", "label": _("Закрыта"),
             "value": item.closed_at.isoformat() if item and item.closed_at else "",
             "help": _("Касса закрывается датой, а не удалением: "
                       "закрытые месяцы на неё ссылаются.")},
        ],
    }, status=status)
