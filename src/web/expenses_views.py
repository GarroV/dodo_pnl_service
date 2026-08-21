"""Список расходов, правка и удаление (T110).

Экран, ради которого управляющий вообще открывает продукт в конце смены: он
сверяет список с тем, что реально лежит в кассе. Отсюда четыре решения, и все
четыре — про доверие к числу внизу.

**Срез роли делает база, а не выборка.** Ни `filter(unit_id__in=...)`, ни
проверки в цикле здесь нет: выборка идёт как есть, а лишнее отсекают политики
`facts` (D014). Забытый фильтр в новом отчёте обязан давать пустой результат, а
не чужие строки, — и на этом же стоит поведение фильтров в адресе: чужая точка
в `?unit=` даёт пустой список, неотличимый от «такого нет» (D023).

**Итог — сумма показанных строк, а не отдельная выборка.** Считается он прямо
по тому списку, который ушёл в разметку. Второй запрос `sum(amount)` был бы
вторым источником истины: он разошёлся бы с таблицей молча — например, потому
что политика на представлении сужает не так, как на таблице, — и человек сверял
бы кассу с числом, которого в таблице нет. Тот же довод записан в
`payrun/sheet.py`.

**Удалённое остаётся видимым.** Удаление помечает строку заменённой, а не
стирает её; в списке она видна с состоянием и в итог не входит. Деньги,
пропавшие без следа, через месяц не проверить ничем.

**Дети разнесения в списке не показываются.** Список отвечает на вопрос «что
внесли», а не «как это разошлось по точкам»: показать и родителя, и детей
значило бы удвоить итог. Разнесение видно в карточке самой записи (T111).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from core.models import ExpenseItem, Fact, Unit

from . import allocation, cash
from .cash_views import expense_fields, parse_expense
from .directory_views import LEDGER_CODES, BadInput, _select
from .format import EMPTY, ledger_title, money
from .i18n import month_title
from .principal import get_current_principal

# Состояние строки для человека и для приёмки. Значения короткие и машинные:
# по ним же тест сверяет итог с показанными строками.
ACTIVE, REMOVED, REPLACED = "active", "removed", "replaced"


@login_required
def expenses(request):
    """Список расходов за выбранный период с итогом."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request)

    error, status = "", 200
    try:
        chosen = filters_from(request)
    except BadInput as bad:
        error, status = bad.message, bad.http_status
        chosen = filters_default()

    rows = rows_for(who, chosen) if not error else []
    total = sum((row["amount"] for row in rows if row["state"] == ACTIVE), Decimal("0"))
    return render(request, "web/cash/expenses.html", {
        "error": error,
        # Что случилось с расходом до того, как человека увели сюда: удаление
        # (T154) и правка. Оба ответа приезжают признаками в адресе и
        # превращаются в слова здесь — готовую фразу в адрес не положить.
        "notice": " ".join(filter(None, [
            _removed_notice(request), _saved_notice(request),
        ])),
        "rows": rows,
        "total": total,
        "total_raw": f"{total}",
        "total_text": money(total),
        "counted": sum(1 for row in rows if row["state"] == ACTIVE),
        "filters": _filter_fields(who, chosen),
        # Сужен ли отбор — от этого зависит, что говорит пустой список (T160).
        "narrowed": narrowed(chosen),
        "add_url": reverse("expense-new"),
    }, status=status)


def _no_membership(request):
    """Учётка без членства: политики без членства пусты, показывать нечего."""
    return render(request, "web/directory/denied.html", {
        "message": _(
            "Вас ещё не завели ни к одному партнёру, поэтому расходов у вас нет. "
            "Попросите администратора сети добавить вас."
        ),
    }, status=403)


# --- отбор --------------------------------------------------------------------


def filters_default() -> dict:
    """Умолчание — текущий месяц: с ним человек и сверяет кассу.

    `ledger` пуст — это «все видимые роли регистры», а не четвёртый безымянный.
    """
    first = date.today().replace(day=1)
    return {"from": first, "to": _last_day(first), "unit": "", "item": "", "ledger": ""}


def _last_day(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def narrowed(chosen: dict) -> bool:
    """Отбор сужен по сравнению с умолчанием — то есть пустота может быть от него.

    Нужно пустому состоянию (T160): «за этот месяц расходов ещё нет» и «отбор
    ничего не пропустил» — это два разных сообщения и два разных следующих
    действия. Первое предлагает внести первый расход, второе — снять отбор;
    свести их в одно значило бы предлагать человеку заводить запись, которая у
    него, возможно, уже есть и просто отфильтрована.

    Считается по тому, что человек прислал сам, а не по данным: сравнить с
    умолчанием можно, ничего не спрашивая у базы. Это важно ровно тем, что
    сообщение о сужении не может выдать существование скрытых строк (D023) —
    оно про запрос, а не про их наличие.
    """
    return chosen != filters_default()


def filters_from(request) -> dict:
    """Что человек выбрал в адресе — одинаково для экрана и для вызова по HTTP.

    Разбор здесь **один** на обе поверхности (T133). Пока `ledger` читал только
    вызов, один и тот же адрес отвечал по-разному: `/expenses/?ledger=official`
    показывал две строки, а `/api/expenses/?ledger=official` — одну. Спека
    (`spec.md`, условие 2) требует обратного: срез — параметр запроса, и ответ у
    двух ручек одной двери обязан совпадать.

    **Неразобранное значение — отказ, а не тихое умолчание.** Тихое умолчание
    означало бы, что человек смотрит не тот период (или не ту точку), чем
    думает, и сверяет кассу с чужим числом.
    """
    chosen = filters_default()
    for name in ("from", "to"):
        raw = (request.GET.get(name) or "").strip()
        if raw:
            chosen[name] = _day(raw, name)
    if chosen["to"] < chosen["from"]:
        raise BadInput(_("Конец периода раньше начала: поправьте даты."))
    # Точка и статья уходят в выборку как есть: чужую отсекут политики базы, а
    # не проверка здесь (D014). Проверяется только то, что это вообще номер, —
    # и негодное значение отвергается, а не подменяется полным списком (T134).
    for name in ("unit", "item"):
        raw = (request.GET.get(name) or "").strip()
        chosen[name] = _id_or_refuse(raw, name) if raw else ""
    # Регистр разбору не подлежит: неизвестное слово отвечает тем же, чем
    # невидимый регистр, — пустотой (D023, разбор в `rows_for`). Отказ здесь
    # сделал бы выдуманное слово отличимым от скрытого, то есть превратил бы
    # перебор в способ узнать состав регистров партнёра.
    chosen["ledger"] = (request.GET.get("ledger") or "").strip()
    return chosen


# `gettext_lazy`, а не `gettext`: словарь собирается при импорте модуля, то есть
# ОДИН раз и на том языке, который был активен в тот момент. С обычным gettext
# подписи фильтров навсегда застывали русскими — на англоязычном демо было видно
# «С даты» и «По дату» рядом с полностью английской страницей. Ленивый перевод
# откладывает выбор языка до показа, то есть до запроса конкретного человека.
LABELS = {
    "from": gettext_lazy("С даты"), "to": gettext_lazy("По дату"),
    "unit": gettext_lazy("Точка"), "item": gettext_lazy("Статья расхода"),
    "ledger": gettext_lazy("Регистр учёта"),
}


def _day(raw: str, name: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise BadInput(
            _("«%(label)s»: дата пишется как 2026-06-01, а не «%(value)s».")
            % {"label": LABELS[name], "value": raw}
        ) from None


def _id_or_refuse(raw: str, name: str) -> str:
    """Номер точки или статьи из адреса. Не номер — отказ, а не полный список.

    Раньше негодное значение молча обнулялось: `?item=zzz` отдавал **все**
    статьи с итогом по всем (T134). На экране это ещё видно глазами, а вызов по
    HTTP заведён ради бота — и бот, спросивший «расходы по статье X», получал
    расходы по всем и докладывал это как ответ на свой вопрос. Ровно от этого
    предостерегает комментарий у разбора дат двумя функциями выше; для точки и
    статьи было сделано наоборот.

    Отказ ничего не рассказывает о существовании строки: номер, которого нет, и
    номер чужой точки по-прежнему дают пустой список (D023). Отвергается только
    то, что номером не является вовсе, — а значит существовать не может.
    """
    from uuid import UUID

    try:
        return str(UUID(raw))
    except ValueError:
        raise BadInput(
            _("«%(label)s»: это номер из списка, а не «%(value)s».")
            % {"label": LABELS[name], "value": raw}
        ) from None


def rows_for(who, chosen: dict, *, window: tuple[int, int] | None = None) -> list[dict]:
    """Строки списка. Выборка одна: из неё же считается итог.

    `window` — «с какой строки и сколько», нужен вызову по HTTP: страница
    показывает месяц целиком, а вызову неограниченную выборку отдавать нельзя
    (T112). У экрана окна нет, и это не забывчивость: человек сверяет кассу за
    месяц, и половина месяца ему бесполезна.
    """
    found = (
        Fact.objects.select_related("unit", "expense_item", "till")
        .filter(
            source=cash.MANUAL_SOURCE,
            channel=cash.CASH_CHANNEL,
            doc_date__gte=chosen["from"],
            doc_date__lte=chosen["to"],
        )
        # Дети разнесения — следствие родителя, а не отдельный расход: показать
        # и то и другое значило бы удвоить итог.
        .exclude(allocation="allocated")
        .order_by("-doc_date", "created_at")
    )
    if chosen["unit"]:
        found = found.filter(unit_id=chosen["unit"])
    if chosen["item"]:
        found = found.filter(expense_item_id=chosen["item"])

    cut = chosen.get("ledger") or ""
    if cut:
        if cut not in LEDGER_CODES:
            # Регистр — нативный enum базы, и выдуманное слово оборвало бы
            # запрос ошибкой приведения типа. Оборванный запрос — это отличимый
            # ответ: по нему видно, что слово не из списка, а значит перебором
            # значений составляется список регистров партнёра. Поэтому неизвестное
            # значение отвечает ровно тем же, чем невидимый регистр, — пустотой
            # (D023). Это не проверка доступа в приложении: что видно из
            # существующих регистров, по-прежнему решают политики.
            return []
        found = found.filter(ledger=cut)

    if window is not None:
        offset, size = window
        found = found[offset:offset + size]
    return [row_of(fact) for fact in found]


def row_of(fact) -> dict:
    state = ACTIVE
    if fact.superseded_at is not None:
        state = REPLACED if fact.superseded_by is not None else REMOVED
    return {
        "id": str(fact.id),
        "url": reverse("expense", args=[fact.id]),
        "date": fact.doc_date.isoformat() if fact.doc_date else "",
        # Точка без строки в справочнике — состояние, которого при исправных
        # политиках не бывает: факт чужой точки роль не видит вовсе. Но если
        # политика `facts` однажды разойдётся с политикой `units`, узнать об
        # этом человек должен прочерком в ячейке, а не оборванным запросом.
        "unit": fact.unit.code if fact.unit else (EMPTY if fact.unit_id else _("Вся сеть")),
        "item": cash.item_title(fact.expense_item.titles) if fact.expense_item
                else fact.title,
        # Из какой кассы платили (T145). Прочерк — расход мимо кассы: так внесены
        # все расходы до появления справочника, и это законное состояние.
        "till": fact.till.code if fact.till else EMPTY,
        # Сумма налога внутри суммы (T146). Прочерк — налога нет вовсе, а не
        # ноль: ноль читался бы как «налог посчитан и он нулевой».
        "vat": money(fact.vat_amount) if fact.vat_amount is not None else EMPTY,
        "amount": fact.amount,
        # Машиночитаемая сумма строкой, а не числом: числа в шаблоне Django
        # локализует (`100,00`), и приёмка сверяла бы итог с отформатированным
        # значением — то есть с языком страницы, а не с деньгами.
        "amount_raw": f"{fact.amount}",
        "amount_text": money(fact.amount),
        "ledger": ledger_title(fact.ledger),
        # Код регистра рядом с названием: название переводится и годится только
        # глазам, а вызову по HTTP нужен разбираемый машиной ответ (T112).
        "ledger_code": fact.ledger,
        "note": fact.note or "",
        "state": state,
        "state_title": state_title(fact, state),
        # Месяц учёта называется только тогда, когда он **не** совпадает с
        # месяцем траты: человек, вводивший июньскую дату в августе, обязан
        # знать, где искать строку. В обычном случае это шум.
        "landed_in": (
            month_title(fact.period)
            if fact.doc_date is None or fact.period != fact.doc_date.replace(day=1)
            else ""
        ),
    }


def state_title(fact, state: str) -> str:
    if state == REMOVED:
        return _("удалён")
    if state == REPLACED:
        return _("заменён")
    correction = cash.is_correction(fact)
    if correction == "storno":
        return _("сторно")
    if correction == "fix":
        return _("исправление")
    return ""


def _filter_fields(who, chosen: dict) -> list[dict]:
    """Поля отбора. Списки — только из того, что видно роли (D023)."""
    units = Unit.objects.order_by("code")
    if who.unit_ids:
        units = units.filter(pk__in=who.unit_ids)
    return [
        {"kind": "date", "name": "from", "label": LABELS["from"],
         "value": chosen["from"].isoformat()},
        {"kind": "date", "name": "to", "label": LABELS["to"],
         "value": chosen["to"].isoformat()},
        _select(
            "unit", _("Точка"), units.values_list("id", "code"), chosen["unit"],
            required=False, empty_label=_("Все точки"),
        ),
        _select(
            "item", _("Статья расхода"),
            [
                (item.id, cash.item_title(item.titles))
                for item in ExpenseItem.objects.order_by("code")
            ],
            chosen["item"], required=False, empty_label=_("Все статьи"),
        ),
        # Срез по регистру — тот же параметр, что у вызова по HTTP (T133).
        # Список только из видимых роли: предложить регистр, которого человек не
        # видит, значило бы обещать пустой ответ и назвать его срезом.
        _select(
            "ledger", LABELS["ledger"],
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            chosen["ledger"], required=False, empty_label=_("Все регистры"),
        ),
    ]


# --- нераспределённое ---------------------------------------------------------


@login_required
def unallocated(request):
    """Суммы без точки: что мешает закрыть месяц (T111).

    Отдельный экран, а не отметка в общем списке: этот список — рабочий, по нему
    бухгалтер понимает, чего не хватает, чтобы закрыть месяц. Сумма,
    потерявшаяся между «по точкам» и «по сети», — дыра в P&L, которая не кричит.

    Читается представление `facts_unallocated`, а не таблица: оно
    `security_invoker`, то есть срез роли делает та же RLS, что и везде.
    """
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request)

    if request.method == "POST":
        spread = cash.reallocate(who.tenant_id, cash.periods_waiting(who.tenant_id))
        return redirect(reverse("expenses-unallocated") + spread_query(spread))

    rows = waiting_rows(who)
    total = sum((row["amount"] for row in rows), Decimal("0"))
    return render(request, "web/cash/unallocated.html", {
        "rows": rows,
        "total": total,
        "total_raw": f"{total}",
        "total_text": money(total),
        "notice": _spread_notice(request, waiting=len(rows)),
        "back_url": reverse("expenses"),
    })


def waiting_rows(who) -> list[dict]:
    """Строки, ждущие разнесения, — и почему каждая ждёт (T132).

    Причину считает база (`allocation_reason`), потому что там же живёт поиск
    правила: вторая копия этого поиска здесь осталась бы верной по отдельности и
    разошлась бы с планом молча.

    Название статьи спрашивается у самой статьи, а не берётся из `title` факта
    (T134). `title` — снимок названия на языке того, кто вносил расход; он обязан
    остаться в данных (закрытый отчёт должен выглядеть как в день закрытия), но
    показывать читателю нужно название на **его** языке. Оттого на английской
    странице здесь стояла «Аренда», пока соседний экран и выгрузка звали ту же
    статью `Rent`.
    """
    from django.db import connection

    from core.models import ExpenseItem

    with connection.cursor() as cursor:
        cursor.execute(
            """select fact_id, period, title, amount, source::text,
                      expense_item_id, allocation_reason(fact_id)
                 from facts_unallocated
                where tenant_id = %s
                order by period desc, title""",
            [str(who.tenant_id)],
        )
        found = cursor.fetchall()

    titles = {
        item.id: item.titles
        for item in ExpenseItem.objects.filter(
            pk__in=[row[5] for row in found if row[5] is not None]
        )
    }
    return [
        {
            "id": str(fact_id),
            "url": reverse("expense", args=[fact_id]),
            "period": month_title(period),
            # Статьи у факта может не быть вовсе (фактура поставщика придёт со
            # своим контрагентом) — тогда остаётся снимок названия.
            "title": cash.item_title(titles[item_id]) if item_id in titles else title,
            "amount": amount,
            "amount_raw": f"{amount}",
            "amount_text": money(amount),
            "source": source,
            "why": allocation.waiting_title(reason) if reason else "",
        }
        for fact_id, period, title, amount, source, item_id, reason in found
    ]


def spread_query(spread) -> str:
    """Итог пересчёта числами в адресе: готовую фразу в адресе не перевести."""
    query = f"?changed={spread.changed}"
    for name, months in (("skipped", spread.skipped), ("refused", spread.refused)):
        if months:
            query += f"&{name}=" + ",".join(f"{month:%Y-%m}" for month in months)
    return query


def _spread_notice(request, *, waiting: int = 0) -> str:
    """Итог пересчёта словами. Молчать о пропущенных месяцах нельзя.

    Пропущенный молча месяц читается как «пересчитано» — и человек уходит,
    считая, что правило применилось везде.

    **«Менять было нечего» говорится только тогда, когда и правда нечего
    (T132).** Раньше эта фраза приходила и при висящих суммах — при 1 490,00
    нераспределённого продукт отвечал, что менять нечего, и человек уходил
    уверенный, что всё разнесено. Теперь ноль изменений при ждущих строках
    отсылает к причине, написанной у каждой строки.
    """
    changed = request.GET.get("changed")
    if changed is None:
        return ""
    if changed == "0" and waiting:
        said = _(
            "Пересчёт прошёл: ни одна строка не изменилась, а ждут разнесения "
            "%(count)s. Что мешает каждой — в колонке «Почему ждёт»."
        ) % {"count": waiting}
    elif changed == "0":
        said = _("Пересчёт прошёл: менять было нечего, ни одна строка не тронута.")
    else:
        said = _("Пересчёт прошёл: строк изменено — %(count)s.") % {"count": changed}

    closed = _months(request, "skipped")
    if closed:
        said += " " + _(
            "Закрытые месяцы не пересчитывались: %(months)s. Чтобы пересчитать "
            "их, месяц придётся открыть заново с причиной."
        ) % {"months": ", ".join(closed)}
    denied = _months(request, "refused")
    if denied:
        said += " " + _(
            "Эти месяцы не пересчитаны: %(months)s — разносит расходы по точкам "
            "тот, кто ведёт все точки партнёра."
        ) % {"months": ", ".join(denied)}
    return said


def _months(request, name: str) -> list[str]:
    return [
        month_title(parsed)
        for raw in (request.GET.get(name) or "").split(",")
        if (parsed := _month_or_none(raw)) is not None
    ]


def _month_or_none(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m").date().replace(day=1)
    except ValueError:
        return None


# --- карточка расхода ---------------------------------------------------------


def expense_or_404(fact_id) -> Fact:
    """Расход по номеру — под политиками базы.

    Чужой расход и несуществующий отвечают одинаково: по ответу нельзя понять,
    что расход существует у другой точки (D023). Строки, которые этим экраном
    не вносятся (зарплата, выручка коннектора, дети разнесения), тоже 404 —
    править их здесь нечем.
    """
    fact = (
        Fact.objects.select_related("expense_item", "unit", "till")
        .filter(pk=fact_id, source=cash.MANUAL_SOURCE, channel=cash.CASH_CHANNEL)
        .exclude(allocation="allocated")
        .first()
    )
    if fact is None:
        raise Http404("расход не найден")
    return fact


@login_required
def expense(request, fact_id):
    """Карточка расхода: правка и удаление."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request)

    fact = expense_or_404(fact_id)
    error, status = "", 200
    entered = request.POST if request.method == "POST" else _entered(fact)

    if request.method == "POST":
        try:
            return redirect(_save(request, who, fact))
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return _card(request, who, fact, entered=entered, error=error, status=status)


def _card(request, who, fact, *, entered=None, error: str = "", status: int = 200):
    """Карточка расхода. Одна на показ, на отказ формы и на отказ удаления.

    Собиралась она в одном месте и раньше; отдельной функцией — потому что
    удаление теперь тоже может отказать словами (T154), а собранная во второй раз
    карточка разъехалась бы с первой молча.
    """
    return render(request, "web/cash/expense_edit.html", {
        "error": error,
        "fields": expense_fields(who, entered if entered is not None else _entered(fact)),
        "fact": fact,
        "editable": editable(fact),
        "closed_notice": closed_notice(who, fact),
        "delete_notice": delete_notice(who, fact),
        "state": row_of(fact)["state"],
        "delete_url": reverse("expense-delete", args=[fact.id]),
        "back_url": reverse("expenses"),
    }, status=status)


def _entered(fact) -> dict:
    """Что показать в форме: то, что записано в самой строке."""
    return {
        "date": fact.doc_date.isoformat() if fact.doc_date else "",
        "amount": str(fact.amount),
        "item": str(fact.expense_item_id or ""),
        "unit": str(fact.unit_id or ""),
        "till": str(fact.till_id or ""),
        "ledger": fact.ledger,
        # Ставка возвращается в форму той же, что записана: правка расхода не
        # должна молча снимать с него налог.
        "vat_rate": _plain(fact.vat_rate) if fact.vat_rate is not None else "",
        "note": fact.note or "",
    }


def _plain(rate) -> str:
    """Ставка без хвоста нулей: «20», а не «20.000».

    В базе у ставки три знака после запятой (бывают дробные ставки), но в поле
    формы человек вводил «20» и обязан увидеть «20». Иначе каждая правка чужого
    расхода выглядит как чья-то предыдущая правка ставки.
    """
    return format(rate.normalize(), "f")


def editable(fact) -> bool:
    """Заменённую строку не правят: правят ту, что действует.

    Иначе из истории вырастала бы вторая ветка версий, и «какая строка сейчас
    верна» перестал бы быть вопросом с одним ответом.
    """
    return fact.superseded_at is None


def closed_notice(who, fact) -> str:
    """Куда уйдёт правка, если месяц строки уже закрыт. Молчать здесь нельзя."""
    if not cash.month_is_closed(who.tenant_id, fact.period):
        return ""
    try:
        landing = cash.landing_for(who.tenant_id, fact.doc_date or fact.period)
    except cash.CashRefused as refusal:
        return refusal.message
    return _(
        "Месяц %(closed)s закрыт: строку в нём не изменить. Правка ляжет в "
        "%(month)s двумя строками — сторно этой записи и новая, — а закрытый "
        "месяц не сдвинется ни на копейку."
    ) % {"closed": month_title(fact.period), "month": month_title(landing.period)}


def delete_notice(who, fact) -> str:
    """Что сделает кнопка «Удалить расход» с ЭТОЙ строкой (T157, находка Н4).

    Раньше рядом с кнопкой стояла одна фраза на оба случая — «строка останется в
    списке помеченной, а из итога выйдет», — верная только для открытого месяца.
    В закрытом не происходит ни того, ни другого: исходную строку не тронуть
    вовсе, а в текущий месяц ложится сторно. Человек, прочитавший это перед
    нажатием, искал помеченную строку в июне и не находил, а изменение в августе
    со своим действием не связывал.
    """
    if not cash.month_is_closed(who.tenant_id, fact.period):
        return _(
            "Удалённый расход не стирается: строка останется в списке "
            "помеченной, а из итога выйдет."
        )
    try:
        landing = cash.landing_for(who.tenant_id, fact.doc_date or fact.period)
    except cash.CashRefused as refusal:
        return refusal.message
    said = _(
        "Строку закрытого месяца не тронуть и здесь: удаление положит в "
        "%(month)s сторно на её сумму, а %(closed)s не сдвинется. Помеченной в "
        "списке она не станет — из итога её выводит сторно."
    ) % {"closed": month_title(fact.period), "month": month_title(landing.period)}
    if _has_correction(fact):
        said += " " + _(
            "Исправленная строка прежней правки при этом снимется — денег этого "
            "расхода в P&L не останется."
        )
    return said


def _has_correction(fact) -> bool:
    """Лежит ли в текущем месяце исправленная строка от прежней правки."""
    return Fact.objects.filter(
        dedup_key=cash.DEDUP_PREFIX + cash.entry_key_of(fact) + cash.FIX_SUFFIX,
        superseded_at__isnull=True,
    ).exists()


def _save(request, who, fact) -> str:
    if not editable(fact):
        raise BadInput(
            _("Эта строка уже заменена: правьте ту, что действует.")
        )
    entered = parse_expense(request, who)
    recorded = cash.revise_expense(who, fact, **entered)
    landed = reverse("expenses") + f"?saved={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        # Из какого месяца правку пришлось перенести: без этого признака список
        # сказал бы «правка записана в август» и умолчал бы о том, что июнь
        # закрыт и не сдвинулся, — а спрашивают ровно об этом.
        landed += f"&from={recorded.landing.moved_from:%Y-%m}"
    return landed


@login_required
def expense_delete(request, fact_id):
    """Удаление расхода. Только POST: это запись, а не просмотр.

    **Молчаливого возврата в список отсюда нет ни в одном случае (T154).** Кнопка
    удаления — то место, где «302 и ни слова» читается как успех: человек уходит
    уверенный, что расхода в учёте больше нет. Поэтому каждый исход называется
    словами на том экране, куда человека увели.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request)

    fact = expense_or_404(fact_id)
    if not editable(fact):
        # Заменённую строку удалять нечего: она уже вышла из счёта. Но сказать
        # об этом надо — иначе второе нажатие выглядит как второе удаление.
        return redirect(reverse("expenses") + "?removed=already")

    try:
        removal = cash.remove_expense(who, fact)
    except cash.CashRefused as refusal:
        # Снять расход некуда: и его месяц, и текущий закрыты. Отказ читается на
        # той же карточке, с которой нажимали, — возвращать в список с ошибкой
        # значило бы уводить от кнопки, которую человек только что нажал.
        return _card(request, who, fact, error=refusal.message,
                     status=refusal.http_status)
    return redirect(reverse("expenses") + _removal_query(removal))


def _removal_query(removal) -> str:
    """Итог удаления числами и месяцами в адресе: готовую фразу туда не положить.

    Тот же приём, что у пересчёта разнесения и у внесения расхода: фраза в адресе
    не переводится, а подставить в неё можно что угодно.
    """
    query = f"?removed={removal.state}"
    if removal.landing is not None:
        query += f"&month={removal.landing.period:%Y-%m}"
        if removal.landing.moved_from is not None:
            query += f"&closed={removal.landing.moved_from:%Y-%m}"
    if removal.withdrew_correction:
        query += "&fix=1"
    return query


def _removed_notice(request) -> str:
    """Что случилось с удалением — словами. Пусто только тогда, когда не удаляли."""
    state = request.GET.get("removed") or ""
    if not state:
        return ""
    month = _month_or_none(request.GET.get("month") or "")
    closed = _month_or_none(request.GET.get("closed") or "")

    if state == "already":
        return _(
            "Этот расход уже удалён: отменяющая строка за него записана раньше. "
            "Ничего не изменилось."
        )
    if state == "stornoed" and month is not None and closed is not None:
        said = _(
            "Расход удалён. Месяц %(closed)s закрыт, строку в нём не тронуть, "
            "поэтому в %(month)s легло сторно на её сумму — закрытый месяц не "
            "сдвинулся ни на копейку."
        ) % {"closed": month_title(closed), "month": month_title(month)}
        if request.GET.get("fix") == "1":
            said += " " + _(
                "Исправленная строка прежней правки снята — денег этого расхода "
                "в P&L больше нет."
            )
        return said
    if state == "stornoed":
        return _("Расход удалён: в текущий месяц легло сторно на его сумму.")
    return _(
        "Расход удалён: строка осталась в списке помеченной, а из итога вышла."
    )


def _saved_notice(request) -> str:
    """Что случилось с правкой расхода. Список этот ответ раньше не читал.

    Карточка уводила сюда с `?saved=2026-08`, а список параметр не разбирал — то
    есть подтверждения «легло в август» человек не получал вовсе, хотя форма
    внесения свой `?saved=` читает и говорит.
    """
    saved = _month_or_none(request.GET.get("saved") or "")
    if saved is None:
        return ""
    closed = _month_or_none(request.GET.get("from") or "")
    if closed is None:
        return _("Правка записана в месяц %(month)s.") % {"month": month_title(saved)}
    return _(
        "Месяц %(closed)s закрыт, поэтому правка записана в %(month)s двумя "
        "строками — сторно прежней записи и новой. Закрытый месяц не сдвинулся."
    ) % {"closed": month_title(closed), "month": month_title(saved)}
