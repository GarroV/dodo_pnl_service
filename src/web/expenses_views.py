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

from . import cash
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
        "rows": rows,
        "total": total,
        "total_raw": f"{total}",
        "total_text": money(total),
        "counted": sum(1 for row in rows if row["state"] == ACTIVE),
        "filters": _filter_fields(who, chosen),
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

    `ledger` здесь пуст всегда: срез по регистру — параметр вызова по HTTP
    (T112), у экрана его нет. Ключ тем не менее живёт в общем наборе отбора, а
    не приезжает вторым способом: два набора отбора рядом расходятся молча, и
    тогда экран и вызов начинают показывать разное, называя это одним списком.
    """
    first = date.today().replace(day=1)
    return {"from": first, "to": _last_day(first), "unit": "", "item": "", "ledger": ""}


def _last_day(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def filters_from(request) -> dict:
    """Что человек выбрал в адресе. Неразобранная дата — отказ, а не тихое умолчание.

    Тихое умолчание означало бы, что человек смотрит не тот период, чем думает,
    и сверяет кассу с чужим числом.
    """
    chosen = filters_default()
    for name in ("from", "to"):
        raw = (request.GET.get(name) or "").strip()
        if raw:
            chosen[name] = _day(raw, name)
    if chosen["to"] < chosen["from"]:
        raise BadInput(_("Конец периода раньше начала: поправьте даты."))
    # Точка и статья уходят в выборку как есть: чужую отсекут политики базы, а
    # не проверка здесь (D014). Проверяется только то, что это вообще uuid.
    for name in ("unit", "item"):
        raw = (request.GET.get(name) or "").strip()
        chosen[name] = raw if _looks_like_id(raw) else ""
    return chosen


# `gettext_lazy`, а не `gettext`: словарь собирается при импорте модуля, то есть
# ОДИН раз и на том языке, который был активен в тот момент. С обычным gettext
# подписи фильтров навсегда застывали русскими — на англоязычном демо было видно
# «С даты» и «По дату» рядом с полностью английской страницей. Ленивый перевод
# откладывает выбор языка до показа, то есть до запроса конкретного человека.
LABELS = {"from": gettext_lazy("С даты"), "to": gettext_lazy("По дату")}


def _day(raw: str, name: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise BadInput(
            _("«%(label)s»: дата пишется как 2026-06-01, а не «%(value)s».")
            % {"label": LABELS[name], "value": raw}
        ) from None


def _looks_like_id(raw: str) -> bool:
    from uuid import UUID

    try:
        UUID(raw)
    except ValueError:
        return False
    return True


def rows_for(who, chosen: dict, *, window: tuple[int, int] | None = None) -> list[dict]:
    """Строки списка. Выборка одна: из неё же считается итог.

    `window` — «с какой строки и сколько», нужен вызову по HTTP: страница
    показывает месяц целиком, а вызову неограниченную выборку отдавать нельзя
    (T112). У экрана окна нет, и это не забывчивость: человек сверяет кассу за
    месяц, и половина месяца ему бесполезна.
    """
    found = (
        Fact.objects.select_related("unit", "expense_item")
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
        "notice": _spread_notice(request),
        "back_url": reverse("expenses"),
    })


def waiting_rows(who) -> list[dict]:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """select fact_id, period, title, amount, source::text
                 from facts_unallocated
                where tenant_id = %s
                order by period desc, title""",
            [str(who.tenant_id)],
        )
        found = cursor.fetchall()
    return [
        {
            "id": str(fact_id),
            "url": reverse("expense", args=[fact_id]),
            "period": month_title(period),
            "title": title,
            "amount": amount,
            "amount_raw": f"{amount}",
            "amount_text": money(amount),
            "source": source,
        }
        for fact_id, period, title, amount, source in found
    ]


def spread_query(spread) -> str:
    """Итог пересчёта числами в адресе: готовую фразу в адресе не перевести."""
    query = f"?changed={spread.changed}"
    for name, months in (("skipped", spread.skipped), ("refused", spread.refused)):
        if months:
            query += f"&{name}=" + ",".join(f"{month:%Y-%m}" for month in months)
    return query


def _spread_notice(request) -> str:
    """Итог пересчёта словами. Молчать о пропущенных месяцах нельзя.

    Пропущенный молча месяц читается как «пересчитано» — и человек уходит,
    считая, что правило применилось везде.
    """
    changed = request.GET.get("changed")
    if changed is None:
        return ""
    if changed == "0":
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
        Fact.objects.select_related("expense_item", "unit")
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

    return render(request, "web/cash/expense_edit.html", {
        "error": error,
        "fields": expense_fields(who, entered),
        "fact": fact,
        "editable": editable(fact),
        "closed_notice": closed_notice(who, fact),
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
        "ledger": fact.ledger,
        "note": fact.note or "",
    }


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


def _save(request, who, fact) -> str:
    if not editable(fact):
        raise BadInput(
            _("Эта строка уже заменена: правьте ту, что действует.")
        )
    entered = parse_expense(request, who)
    recorded = cash.revise_expense(who, fact, **entered)
    landed = reverse("expenses") + f"?saved={recorded.landing.period:%Y-%m}"
    return landed


@login_required
def expense_delete(request, fact_id):
    """Удаление расхода. Только POST: это запись, а не просмотр."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request)

    fact = expense_or_404(fact_id)
    if not editable(fact):
        # Заменённую строку удалять нечего: она уже вышла из счёта.
        return redirect(reverse("expenses"))

    cash.remove_expense(who, fact)
    return redirect(reverse("expenses"))
