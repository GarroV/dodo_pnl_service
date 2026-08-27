"""Справочник контрагентов (T150).

Восьмой справочник, и единственный, который **читают все, а ведёт один**.
Остальные семь живут под `directory.manage` целиком: их и открывает, и правит
администратор сети. Здесь так нельзя — контрагента выбирает бухгалтер, внося
счёт, и закрытый от него список означал бы форму с пустым обязательным полем.

Отсюда устройство экрана: список открыт каждому, кто вошёл, а заведение и правка
закрыты правом — и в базе (`0242`, три ограничивающие политики), и на экране
(`_guard`). Два места, но не два источника истины: решает база, экран объясняет
словами. Без объяснения кнопка просто исчезала бы, и это читалось бы как поломка.

**Поиск ищет по написаниям, а не только по названию.** Смысл справочника —
чтобы траты одного поставщика складывались; человек, который ищет «EPS», должен
находить карточку, даже если в выписке она приходит как `EPS DISTRIBUCIJA AD`.
Поэтому в поиск входят и `aliases`, и налоговый номер, и ключ Dodo IS.

**Ничего не фильтруется по правам здесь.** Изоляция партнёров — политика
`tenant_isolation` на самой таблице (D014). Забытый фильтр в новом экране обязан
давать пустой список, а не чужой.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import Counterparty

from . import permissions
from .dbrefusal import BadInput, ConstraintRefused, saving
from .directory_views import _date, _guard, _text
from .format import EMPTY, day
from .principal import get_current_principal


@login_required
def counterparties(request):
    """Список контрагентов с поиском. Открыт каждому, кто вошёл."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return render(request, "web/directory/denied.html", {
            "message": _(
                "Вас ещё не завели ни к одному партнёру, поэтому контрагентов у вас нет. "
                "Попросите администратора сети добавить вас."
            ),
        }, status=403)

    query = (request.GET.get("q") or "").strip()
    may_manage = permissions.has(who, permissions.DIRECTORY_MANAGE)
    rows = [
        {
            "url": reverse("directory-counterparty", args=[row.id]),
            "cells": [
                {"text": row.title},
                {"text": row.tax_number or EMPTY},
                {"text": row.external_id or EMPTY},
                {"text": day(row.valid_from)},
                {"text": day(row.valid_to)},
            ],
        }
        for row in found(query)
    ]
    return render(request, "web/directory/list.html", {
        "heading": _("Контрагенты"),
        "about": _("Поставщики и получатели платежей. Один контрагент — одна карточка: "
                   "иначе траты одного поставщика рассыпаются по написаниям названия."),
        # Куда возвращаться, зависит от того, кто смотрит: администратор сети
        # пришёл сюда из справочников, остальные — из навигации, и раздела
        # справочников у них нет вовсе. Ссылка на раздел, который ответит
        # отказом, была бы обещанием отказа.
        "back_url": reverse("directory") if may_manage else "",
        "back_label": _("← К справочникам"),
        "standalone": not may_manage,
        "search_label": _("Поиск по названию, написаниям и номеру"),
        "search_value": query,
        # Кнопки нет у того, кто справочник не ведёт, и это не сокрытие адреса:
        # `/directory/counterparties/new/` по-прежнему отвечает отказом словами.
        "add_url": reverse("directory-counterparty-new") if may_manage else "",
        "add_label": _("Завести контрагента"),
        "columns": [
            {"label": _("Название")}, {"label": _("Налоговый номер")},
            {"label": _("Ключ Dodo IS")}, {"label": _("Действует с")},
            {"label": _("Закрыт с")},
        ],
        "rows": rows,
        "empty": _("Контрагентов нет.") if not query else _("Ничего не нашлось."),
        "empty_next": (
            _("Пока справочник пуст, счёт выписать не на кого.") if not query
            else _("Попробуйте другое написание: поиск смотрит и на название, "
                   "и на другие написания, и на номера.")
        ),
    })


def found(query: str = "", *, open_on=None):
    """Контрагенты, видимые роли. Срез делает база, здесь только отбор и порядок.

    `open_on` — «действующие на дату», для списков выбора: закрытый контрагент
    остаётся в справочнике (на него ссылаются факты закрытых месяцев), но
    предлагать его к новому счёту незачем.
    """
    rows = Counterparty.objects.order_by("title")
    if open_on is not None:
        rows = rows.filter(valid_from__lte=open_on).exclude(valid_to__lte=open_on)
    if query:
        rows = rows.annotate(
            # Написания хранятся массивом, а искать по ним надо так же, как по
            # названию — куском строки. У массива такого сравнения нет: его
            # `contains` спрашивает про целый элемент, то есть нашёл бы только
            # точное совпадение написания. Приводим массив к тексту и ищем в
            # нём. Иначе строка из выписки не находилась бы ровно тогда, когда
            # написания и заводили.
            spellings=Cast("aliases", TextField()),
        ).filter(
            Q(title__icontains=query)
            | Q(tax_number__icontains=query)
            | Q(external_id__icontains=query)
            | Q(spellings__icontains=query)
        )
    return rows


@login_required
def counterparty(request, counterparty_id=None):
    """Карточка контрагента: заведение и правка. Только для `directory.manage`."""
    who, denied = _guard(request)
    if denied is not None:
        return denied

    item = None
    if counterparty_id is not None:
        item = Counterparty.objects.filter(pk=counterparty_id).first()
        if item is None:
            # Контрагент чужого партнёра и несуществующий отвечают одинаково:
            # по ответу нельзя понять, что строка существует у соседа (D023).
            raise Http404("контрагент не найден")

    error, status = "", 200
    if request.method == "POST":
        try:
            title = _text(request, "title", _("Название"))
            valid_from = _date(request, "valid_from", _("Действует с"), required=True)
            valid_to = _date(request, "valid_to", _("Закрыт с"))
            # Порядок дат проверяется здесь, а не отказом базы. Проверка `23514`
            # в `web/dbrefusal.py` намеренно не переводится в слова: она значит
            # либо дефект кода, либо правило, которое форма обязана объяснить
            # сама. Ограничение в базе при этом остаётся — оно гарантия на все
            # пути записи, а не подсказка человеку.
            if valid_to is not None and valid_to <= valid_from:
                raise BadInput(
                    _("«%(label)s»: дата закрытия должна быть позже даты начала "
                      "(%(from)s).")
                    % {"label": _("Закрыт с"), "from": day(valid_from)}
                )

            if item is None:
                item = Counterparty(tenant_id=who.tenant_id, created_by=who.user_id)
            item.title = title
            item.tax_number = _text(request, "tax_number", _("Налоговый номер"),
                                    required=False) or None
            item.external_id = _text(request, "external_id", _("Ключ Dodo IS"),
                                     required=False) or None
            item.aliases = _aliases(request)
            item.note = _text(request, "note", _("Примечание"), required=False) or None
            item.valid_from, item.valid_to = valid_from, valid_to
            with saving():
                item.save()
            return redirect(reverse("directory-counterparties"))
        except ConstraintRefused as refused:
            error, status = refused.message, refused.http_status
        except BadInput as bad:
            error, status = bad.message, bad.http_status

    return render(request, "web/directory/form.html", {
        "heading": item.title if item and item.pk else _("Новый контрагент"),
        "back_url": reverse("directory-counterparties"),
        "back_label": _("← К контрагентам"),
        "error": error,
        "submit_label": _("Сохранить"),
        "fields": _fields(request, item),
    }, status=status)


def _aliases(request) -> list[str]:
    """Другие написания — списком, разделитель запятая.

    Массивом, а не второй карточкой: карточка означала бы второго контрагента, а
    смысл справочника ровно обратный. Пустые куски выбрасываются — иначе
    висящая запятая заводила бы пустое написание, которое совпадёт с чем угодно.
    """
    raw = (request.POST.get("aliases") or "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def _fields(request, item) -> list[dict]:
    """Поля карточки. При отказе показывается введённое, а не то, что в базе."""
    entered = request.POST if request.method == "POST" else {}

    def value(name: str, saved) -> str:
        if entered:
            return entered.get(name, "")
        return saved or ""

    return [
        {"kind": "text", "name": "title", "label": _("Название"), "required": True,
         "value": value("title", item.title if item else ""),
         "help": _("Как этот поставщик называется у вас. Название у партнёра одно: "
                   "две карточки означают, что траты одного поставщика не сложатся.")},
        {"kind": "text", "name": "tax_number", "label": _("Налоговый номер"),
         "value": value("tax_number", item.tax_number if item else ""),
         "help": _("ПИБ или его аналог. По нему сходятся фактура и выписка.")},
        {"kind": "text", "name": "aliases", "label": _("Другие написания"),
         "value": value("aliases", ", ".join(item.aliases) if item else ""),
         "help": _("Через запятую: как этого поставщика пишут в выписке и в файлах. "
                   "Поиск смотрит и сюда.")},
        {"kind": "text", "name": "external_id", "label": _("Ключ Dodo IS"),
         "value": value("external_id", item.external_id if item else ""),
         "help": _("Идентификатор поставщика в справочнике Dodo IS. Можно оставить "
                   "пустым: он понадобится, когда появится коннектор.")},
        {"kind": "date", "name": "valid_from", "label": _("Действует с"), "required": True,
         "value": value("valid_from", item.valid_from.isoformat() if item else ""),
         "help": _("С какой даты работаем с этим контрагентом.")},
        {"kind": "date", "name": "valid_to", "label": _("Закрыт с"),
         "value": value("valid_to",
                        item.valid_to.isoformat() if item and item.valid_to else ""),
         "help": _("Контрагент закрывается датой, а не удалением: закрытые месяцы "
                   "на него ссылаются.")},
        {"kind": "text", "name": "note", "label": _("Примечание"),
         "value": value("note", item.note if item else ""),
         "help": _("Для человека: чем занимается, кто ведёт переписку.")},
    ]
