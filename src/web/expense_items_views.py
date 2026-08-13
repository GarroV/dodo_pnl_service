"""Экран справочника статей расходов (T108).

Статья расходов — то, чем бухгалтер называет трату («вода», «электричество»),
а не строка отчёта («Коммунальные») — той статья только ссылается. Подробности
модели и решений — в `core.models.ExpenseItem` и `docs/forge/...`; здесь только
показ и разбор ввода, по образцу остальных пяти справочников (`directory_views`).

**Справочник заводится пустым, и список не рисует ничего «на всякий случай»**
(Q015): статьи придёт с файла бухгалтера, а выдуманные разойдутся с её же
названиями на первой сборке P&L.

**Названия — на трёх языках интерфейса, показывается язык страницы.** Из-за
этого у формы одно текстовое поле на язык, а не одно поле «название»: сербский
бухгалтер заводит статью на своём языке, русскоязычный директор должен прочитать
её на своём. Список языков берётся из `settings.LANGUAGES`, а не переписывается
здесь вторым списком — второй список разошёлся бы с настройками молча.

**Даты статьи версионируются, привязка к строке P&L — нет.** Поэтому правка с
датой внутри утверждённого месяца проходит и сопровождается словами о том, что
закрытый месяц ею не переписывается (D020, общее правило продукта с T121), а
смена строки P&L отвергается: её датой не отодвинуть.

**Привязка к строке P&L не версионируется.** У статьи одна строка на всю
историю, и любая её смена задевает уже утверждённый месяц — не потому, что
дату выбрали неудачно, а потому что даты у привязки нет вовсе. Отказ поэтому
идёт через `refuse_if_unversioned_touches_closed_month`, тот же, что у схемы
расчёта группы (T103): общий отказ с датой здесь солгал бы про дату, которой
человек не вводил.
"""
from __future__ import annotations

from datetime import date

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import ExpenseItem, PnlItem

from . import directory
from .cash import item_title
from .directory_views import BadInput, _choice, _date, _guard, _text

# Разбор ввода берётся у соседнего справочника целиком, а не переписывается
# здесь: правила у всех шести экранов одни («поле обязательно», «дата пишется
# как 2026-06-01», «такого варианта нет»), и человек обязан читать один и тот же
# отказ, на каком бы справочнике он ни ошибся. Вторая копия этих функций
# разъехалась бы с первой на первой правке формулировки — молча, потому что
# каждая по отдельности осталась бы верной.
#
# Подытог статье в списке не предлагается (`_pnl_items` его не отдаёт), но
# подобрать его id в форме можно — отказывает тогда `_choice`.


# Строки P&L, в которые статье расходов законно ложиться. Подытог считается из
# детей (`facts_guard` в 0230), а выручкой статья расходов не бывает — значит
# предлагать их в форме означало бы обещать выбор, который отвергнет база при
# первом же расходе.
PNL_KINDS = ("expense", "transfer")


def _pnl_items():
    return PnlItem.objects.filter(kind__in=PNL_KINDS).order_by("title")


def _title_field_name(code: str) -> str:
    """Имя поля формы для языка. Дефис не годится в имени HTML-поля."""
    return "title_" + code.replace("-", "_")


# Как выбирается показываемое название — правило одно на продукт и живёт в
# `web/cash.py` (`item_title`): его спрашивает и этот справочник, и форма
# внесения расхода, и снимок названия, который уезжает в сам факт. Три копии
# одного правила означали бы, что статья называется по-разному в списке, в
# форме и в отчёте, — и разошлись бы они молча.


def _titles_from_post(request) -> dict:
    """Собрать словарь названий из POST — по полю на язык интерфейса.

    Пустое поле у отдельного языка — это нормально: бухгалтер заводит статью
    на своём языке, остальные подтягиваются потом. Пусты все сразу — отказ:
    это и есть ограничение базы `expense_items_titles_not_empty`, только
    сказанное словами до записи, а не после отказа сервера.
    """
    titles = {}
    for code, _label in settings.LANGUAGES:
        value = (request.POST.get(_title_field_name(code)) or "").strip()
        if value:
            titles[code] = value
    if not titles:
        raise BadInput(
            _("Нужно хотя бы одно название статьи — без него её не выбрать глазами.")
        )
    return titles


def _dates_touch_closed_month(tenant_id, item, valid_from: date, valid_to) -> bool:
    """Задевает ли новая или изменённая дата утверждённый месяц.

    Проверяются именно **изменения**, а не даты как таковые: у существующей
    статьи, давно начавшей действовать внутри утверждённого месяца, проверка на
    каждой правке названия срабатывала бы там, где ничего не меняется, — а
    правка одних названий обязана проходить молча (T108).

    Раньше здесь стоял отказ. Он снят при сведении веток с T121, и это не
    послабление, а приведение к общему правилу продукта: у статьи даты
    **версионируются** (`valid_from`, `valid_to`), а правку с датой продукт
    принимает и говорит человеку, что будет с закрытым месяцем (D020). Отказ
    остался ровно там, где версий нет вовсе, — на привязке к строке P&L: её
    смену датой не отодвинуть, поэтому она и отвергается.
    """
    what_changed = item is None or item.valid_from != valid_from
    if what_changed and directory.touches_closed_month(tenant_id, valid_from):
        return True
    if valid_to is not None and (item is None or item.valid_to != valid_to):
        return directory.touches_closed_month(tenant_id, valid_to)
    return False


@login_required
def expense_items(request):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    rows = [
        {
            "url": reverse("directory-expense-item", args=[item.id]),
            "cells": [
                {"text": item.code},
                {"text": item_title(item.titles)},
                {"text": item.pnl_item.title},
                {"text": item.valid_from.isoformat()},
                {"text": item.valid_to.isoformat() if item.valid_to else "—"},
            ],
        }
        for item in ExpenseItem.objects.select_related("pnl_item").order_by("code")
    ]
    # Правка задела утверждённый месяц (см. форму ниже): человеку говорится, что
    # закрытый месяц ею не переписан и где искать разницу. Признак приезжает
    # параметром адреса — своего механизма сообщений в продукте нет.
    notice = (
        directory.closed_month_notice(who.tenant_id)
        if request.GET.get("retro") == "1" else ""
    )
    return render(request, "web/directory/list.html", {
        "notice": notice,
        "heading": _("Статьи расходов"),
        "about": _("Чем называют траты и в какую строку P&L они попадают."),
        "add_url": reverse("directory-expense-item-new"),
        "add_label": _("Завести статью"),
        "columns": [
            {"label": _("Код")}, {"label": _("Название")}, {"label": _("Строка P&L")},
            {"label": _("Действует с")}, {"label": _("Закрыта")},
        ],
        "rows": rows,
        # Пустое состояние объясняет решение (Q015), а не извиняется за
        # пустоту: справочник поставляется пустым намеренно, и следующий шаг —
        # не «завести первую статью самому», а дождаться файла бухгалтера.
        "empty": _("Статей расходов нет."),
        "empty_next": _(
            "Справочник поставляется пустым намеренно: список статей придёт с "
            "файла бухгалтера, а не выдумывается здесь — иначе одна и та же "
            "трата называлась бы по-разному у нас и у неё."
        ),
    })


@login_required
def expense_item(request, item_id=None):
    who, denied = _guard(request)
    if denied is not None:
        return denied

    item = None
    if item_id is not None:
        item = ExpenseItem.objects.filter(pk=item_id).first()
        if item is None:
            raise Http404("статья не найдена")

    error, status = "", 200
    if request.method == "POST":
        try:
            code = _text(request, "code", _("Код"))
            titles = _titles_from_post(request)
            pnl_ids = list(_pnl_items().values_list("id", flat=True))
            pnl_item_id = _choice(request, "pnl_item", _("Строка P&L"), pnl_ids)
            valid_from = _date(request, "valid_from", _("Действует с"), required=True)
            valid_to = _date(request, "valid_to", _("Закрыта"), required=False)
            if valid_to and valid_to <= valid_from:
                raise BadInput(_("Дата закрытия раньше или равна дате начала действия."))

            touches_closed = _dates_touch_closed_month(
                who.tenant_id, item, valid_from, valid_to,
            )
            if item is not None and item.pnl_item_id != pnl_item_id:
                directory.refuse_if_unversioned_touches_closed_month(
                    who.tenant_id, _("строка P&L статьи расходов"),
                )

            if item is None:
                item = ExpenseItem(tenant_id=who.tenant_id)
            item.code, item.titles, item.pnl_item_id = code, titles, pnl_item_id
            item.valid_from, item.valid_to = valid_from, valid_to
            item.save()
            # Сказать после правки, а не промолчать: человек выбрал дату внутри
            # утверждённого месяца, и он должен узнать, что месяц ею не
            # переписан, — иначе он решит, что переписан. Признак едет
            # параметром адреса, как у соседнего экрана сотрудника (T121):
            # своего механизма сообщений в продукте нет, и заводить его ради
            # одной фразы значило бы поставить второй способ делать то же самое.
            return redirect(
                reverse("directory-expense-items") + ("?retro=1" if touches_closed else "")
            )
        except BadInput as bad:
            # Свой статус, а не умолчание 200: контракт задачи прямо требует
            # 400 на пустой ввод и неверный выбор, а не только на отказ по
            # закрытому месяцу. `bad.http_status` уже несёт нужное значение —
            # дублировать его числом здесь означало бы завести второй источник
            # правды на первой же правке класса.
            error, status = bad.message, bad.http_status
        except directory.DirectoryRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/directory/form.html", {
        "heading": item_title(item.titles) if item else _("Новая статья"),
        "back_url": reverse("directory-expense-items"),
        "back_label": _("← К статьям расходов"),
        "error": error,
        "submit_label": _("Сохранить"),
        "fields": [
            {"kind": "text", "name": "code", "label": _("Код"), "required": True,
             "value": item.code if item else "",
             "help": _("По нему статья сходится с файлом бухгалтера при загрузке.")},
            *[
                {"kind": "text", "name": _title_field_name(code),
                 "label": _("Название (%(language)s)") % {"language": language},
                 "value": (item.titles.get(code) if item else "") or ""}
                for code, language in settings.LANGUAGES
            ],
            {
                "kind": "select", "name": "pnl_item", "label": _("Строка P&L"),
                "required": True,
                "options": [
                    {
                        "code": str(pnl.id), "title": pnl.title,
                        "selected": item is not None and item.pnl_item_id == pnl.id,
                    }
                    for pnl in _pnl_items()
                ],
                "empty_selected": item is None or item.pnl_item_id is None,
            },
            {"kind": "date", "name": "valid_from", "label": _("Действует с"), "required": True,
             "value": item.valid_from.isoformat() if item and item.valid_from else ""},
            {"kind": "date", "name": "valid_to", "label": _("Закрыта"),
             "value": item.valid_to.isoformat() if item and item.valid_to else "",
             "help": _("Статья закрывается датой, а не удалением: "
                       "закрытые месяцы на неё ссылаются.")},
        ],
    }, status=status)
