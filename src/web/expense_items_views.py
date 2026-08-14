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
from django.utils.translation import get_language
from django.utils.translation import gettext as _

from core.models import ExpenseItem, PnlItem, Unit

from . import allocation, cash, directory
from . import expense_items_upload as uploads
from .cash import item_title
from .dbrefusal import saving
from .directory_views import (
    LEDGER_CODES,
    BadInput,
    _choice,
    _date,
    _guard,
    _select,
    _text,
)
from .format import ledger_title

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

# С какой даты действуют статьи, загруженные файлом, если своей даты в файле нет.
# Намеренно раньше любого месяца, который может быть в продукте: справочник
# наполняется первично, а статья, начавшая действовать сегодня, не подошла бы ни
# одному уже внесённому расходу — их даты в прошлом.
UPLOAD_FROM = date(2020, 1, 1)


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
    return _list_page(request, who)


def _list_page(request, who, *, error: str = "", status: int = 200):
    """Страница справочника — одна на показ и на отказ по файлу (T147).

    Отказ рисуется тем же списком с той же формой загрузки: человек только что
    выбрал файл и должен выбрать другой, не возвращаясь назад руками. Второй
    способ собрать этот список означал бы два списка, которые однажды покажут
    разное.
    """
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
        # Сообщение об отказе по файлу и рассказ об успешной загрузке — одно
        # место: два сообщения наверху одной страницы человек читает как одно.
        "error": error,
        "notice": notice or request.session.pop("expense_items_upload", ""),
        # Загрузка живёт на самом справочнике (T147, D041): она и есть его
        # первичное наполнение, а не отдельный раздел, в который надо знать
        # дорогу.
        "upload_url": reverse("directory-expense-items-upload"),
        "upload_label": _("Загрузить из файла"),
        "upload_about": _(
            "Список статей бухгалтера книгой Excel. Колонки продукт ищет по "
            "названию — «Название», «Код», «Строка P&L», — порядок и лишние "
            "колонки значения не имеют. Повторная загрузка того же файла ничего "
            "не удвоит: статьи сходятся по коду. Строки справочника, которых в "
            "файле нет, останутся — их судьбу решаете вы."
        ),
        "upload_lines": [
            {"id": line.id, "title": line.title} for line in _pnl_items()
        ],
        "upload_languages": [
            {"code": code, "title": title,
             "selected": code == get_language()}
            for code, title in settings.LANGUAGES
        ],
        # Умолчание намеренно раннее: справочник наполняется первично, и статья,
        # начавшая действовать сегодня, не подойдёт ни одному уже внесённому
        # расходу — их даты в прошлом (`cash.items_on` отбирает статьи по дате).
        "upload_from": UPLOAD_FROM.isoformat(),
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
    }, status=status)


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
            # Статья и её правило разнесения — одной точкой сохранения (T136):
            # повторный код (issue #109) и пересечение версий правила обязаны
            # стать отказом формы, а не оборванным запросом, и отвергнутая форма
            # не должна оставлять за собой статью без правила.
            #
            # Куда уходит человек, решает `_after_save`: правка правила
            # разнесения запускает пересчёт, и его итог показывается на карточке
            # (T111). К этому добавляется признак `retro=1`, если дата задела
            # утверждённый месяц (T121): промолчать тут нельзя — человек решит,
            # что месяц переписан. Своего механизма сообщений в продукте нет,
            # поэтому признак едет параметром адреса, как у экрана сотрудника.
            with saving():
                item.save()
                target = _after_save(request, who, item)
            if touches_closed:
                target += ("&" if "?" in target else "?") + "retro=1"
            return redirect(target)
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
        "notice": _rule_notice(request, who),
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
            *_rule_fields(request, who, item),
        ],
    }, status=status)


# --- правило разнесения (T111) ------------------------------------------------
#
# Правило живёт в карточке статьи, а не отдельным разделом, потому что человек
# думает о нём именно так: «аренда офиса разносится поровну». Заводить под это
# седьмой справочник значило бы развести по двум экранам одно решение.


def _rule_ledger(request, who) -> str:
    """Регистр правила. Умолчание — официальный, выбор — из видимых роли (D023)."""
    raw = (request.POST.get("alloc_ledger") or "").strip()
    if not raw:
        return "official"
    return _choice(
        request, "alloc_ledger", _("Регистр учёта"),
        [code for code in LEDGER_CODES if code in who.visible_ledgers],
    )


def _after_save(request, who, item) -> str:
    """Сохранить правило разнесения и увести человека обратно в список.

    Пересчёт идёт сразу же: правило, которое поменяли и не применили, — это
    правило, о котором человек думает, что оно работает. Закрытые месяцы
    пересчёт пропускает и называет вслух — числа уезжают в адрес, потому что
    готовую фразу в адресе не перевести.
    """
    ledger = _rule_ledger(request, who)
    method = (request.POST.get("alloc_method") or "").strip()
    unit_id = _choice(
        request, "alloc_unit", _("Точка разнесения"),
        list(Unit.objects.values_list("id", flat=True)), required=False,
    )
    valid_from = _date(request, "alloc_from", _("Правило действует с"), required=False)

    changed = allocation.save_rule(
        who, item, ledger=ledger, method=method, unit_id=unit_id,
        valid_from=valid_from or item.valid_from,
    )
    if not changed:
        return reverse("directory-expense-items")

    from .expenses_views import spread_query

    spread = cash.reallocate(who.tenant_id, cash.periods_waiting(who.tenant_id))
    return reverse("directory-expense-item", args=[item.id]) + spread_query(spread)


def _rule_notice(request, who) -> str:
    """Что сказать после сохранения: итог пересчёта и судьба закрытого месяца.

    Итог пересчёта — теми же словами, что на экране нераспределённого: один и
    тот же пересчёт не должен объясняться по-разному. Рядом — фраза о закрытом
    месяце, если дата правки его задела (T121): два разных последствия одной
    кнопки, и человек должен прочитать оба, а не то из них, которое случилось
    последним.
    """
    from .expenses_views import _spread_notice, waiting_rows

    # Число ждущих строк спрашивается и здесь: без него «менять было нечего»
    # приходит и тогда, когда ждать есть чему (T132). Один и тот же пересчёт не
    # должен объясняться на двух экранах по-разному.
    parts = [_spread_notice(request, waiting=len(waiting_rows(who)))]
    if request.GET.get("retro") == "1":
        parts.append(directory.closed_month_notice(who.tenant_id))
    return " ".join(filter(None, parts))


def _rule_fields(request, who, item) -> list[dict]:
    """Поля правила разнесения и его история."""
    ledger = (request.POST.get("alloc_ledger") or "official") if request.method == "POST" \
        else "official"
    current = allocation.current_rule(item, ledger) if item is not None else None
    units = Unit.objects.order_by("code")
    return [
        _select(
            "alloc_method", _("Разнесение расхода без точки"),
            [(code, allocation.method_title(code)) for code in allocation.METHODS],
            (request.POST.get("alloc_method") if request.method == "POST"
             else (current.method if current else "")),
            required=False,
            empty_label=_("Правила нет — расход будет ждать"),
            # Оговорка про два способа, которые выбрать можно, а исполнить пока
            # нечем, стоит здесь же (T132): узнать об этом по висящей через
            # месяц сумме — худший из способов.
            help=_("Как разложить по точкам расход, внесённый на всю сеть "
                   "(аренда офиса, реклама). Без правила он остаётся в списке "
                   "нераспределённых и в разрез по точкам не попадает.")
                 + " " + allocation.methods_warning(),
        ),
        _select(
            "alloc_unit", _("Точка разнесения"),
            units.values_list("id", "code"),
            (request.POST.get("alloc_unit") if request.method == "POST"
             else (current.unit_id if current else "")),
            required=False, empty_label=_("Не выбрана"),
            help=_("Нужна только для разнесения на одну точку."),
        ),
        _select(
            "alloc_ledger", _("Регистр учёта правила"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            ledger, required=False,
            help=_("У одной статьи законно по правилу на регистр: одна и та же "
                   "трата бывает и официальной, и из кассы."),
        ),
        {"kind": "date", "name": "alloc_from", "label": _("Правило действует с"),
         "value": (current.valid_from.isoformat() if current
                   else (item.valid_from.isoformat() if item else "")),
         "help": _("Смена правила — новая версия с этой даты, а не правка "
                   "прежней: закрытые месяцы обязаны считаться тем правилом, "
                   "которым были посчитаны.")},
        *_rule_history(item),
    ]


def _rule_history(item) -> list[dict]:
    """Прошлые версии правила — надписью, а не полем: их не правят, их читают."""
    if item is None:
        return []
    rules = allocation.rules_of(item)
    if not rules:
        return []
    return [{
        "kind": "note", "name": "alloc_history", "label": _("Версии правила"),
        "value": "; ".join(
            _("%(method)s%(unit)s, %(ledger)s, с %(from)s%(to)s") % {
                "method": allocation.method_title(rule.method),
                "unit": f" ({rule.unit.code})" if rule.unit_id else "",
                "ledger": ledger_title(rule.ledger),
                "from": rule.valid_from.isoformat(),
                "to": _(" по %(date)s") % {"date": rule.valid_to.isoformat()}
                      if rule.valid_to else "",
            }
            for rule in rules
        ),
        "help": _("По ним видно, каким правилом посчитан закрытый месяц."),
    }]


# --- наполнение справочника файлом (T147, D041) -----------------------------------


@login_required
def expense_items_upload(request):
    """Загрузить список статей книгой Excel.

    Разбор и запись живут в `expense_items_upload.py`, здесь только приём файла и
    слова человеку. Такое разделение не ради красоты: разбор чужого формата
    проверяется без базы, а «не плодить дублей» — без файла.

    **Отказ отвечает 400 и остаётся на справочнике**, а не уводит на отдельную
    страницу ошибки: человек только что выбрал файл и должен выбрать другой, не
    возвращаясь назад руками.

    **Итог загрузки уезжает через сессию**, а не параметром адреса: он длинный,
    в нём перечислены коды статей, и собран он уже переведённым. Готовую фразу в
    адрес не положить — её не перевести и подставить в неё можно что угодно.
    """
    who, denied = _guard(request)
    if denied is not None:
        return denied
    if request.method != "POST":
        return redirect(reverse("directory-expense-items"))

    upload = request.FILES.get("file")
    if upload is None:
        return _upload_refused(request, who, _("Файл не выбран."))
    if upload.size > uploads.MAX_UPLOAD:
        return _upload_refused(request, who, _(
            "Файл больше %(limit)s МБ — это не список статей."
        ) % {"limit": uploads.MAX_UPLOAD // 1024 // 1024})

    try:
        rows = uploads.read_rows(upload)
    except uploads.FileRefused as refused:
        return _upload_refused(request, who, refused.message)

    if not rows:
        return _upload_refused(request, who, _(
            "В файле не нашлось ни одной статьи: под строкой заголовков пусто."
        ))

    default_pnl = (request.POST.get("pnl_item") or "").strip() or None
    if default_pnl is not None:
        # Чужой номер строки P&L отвергается тем же способом, что и в форме:
        # молча подставить свой означало бы разложить чужой справочник по
        # статьям, которых человек не выбирал.
        default_pnl = _choice(request, "pnl_item", _("Строка P&L"),
                              list(_pnl_items().values_list("id", flat=True)),
                              required=False)

    language = _choice(request, "language", _("Язык названий в файле"),
                       [code for code, _title in settings.LANGUAGES], required=False)
    starts = _date(request, "valid_from", _("Статьи действуют с"), required=False)

    with saving():
        outcome = uploads.apply_rows(
            who, rows, language=language or get_language(),
            default_pnl_id=default_pnl, default_from=starts or UPLOAD_FROM,
        )
    request.session["expense_items_upload"] = _outcome_words(outcome)
    return redirect(reverse("directory-expense-items"))


def _upload_refused(request, who, message: str):
    """Отказ по файлу: тот же справочник, те же кнопки, 400 и слова наверху."""
    return _list_page(request, who, error=message, status=400)


def _outcome_words(outcome) -> str:
    """Итог загрузки словами: что завелось, что обновилось, что осталось.

    Числа и коды, а не «готово»: человек грузит чужой список и обязан увидеть,
    сошёлся ли он с тем, что уже есть. Пропущенные строки называются поимённо —
    молча потерянная статья всплывёт только при сборке P&L.
    """
    parts = [_("Загружено: %(new)s новых, %(same)s без изменений, %(fixed)s обновлено.") % {
        "new": len(outcome.created), "same": len(outcome.unchanged),
        "fixed": len(outcome.updated),
    }]
    if outcome.skipped:
        parts.append(_("Пропущены: %(rows)s.") % {
            "rows": "; ".join(f"{code} — {reason}" for code, reason in outcome.skipped)
        })
    if outcome.kept:
        parts.append(_(
            "В справочнике остались статьи, которых в файле нет: %(codes)s. "
            "Продукт их не трогает — закройте датой те, что больше не нужны."
        ) % {"codes": ", ".join(outcome.kept)})
    return " ".join(parts)
