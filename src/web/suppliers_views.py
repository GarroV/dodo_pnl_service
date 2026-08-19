"""Экраны счетов и платежей поставщикам (T151).

Правила о деньгах живут не здесь, а в `web/suppliers.py`: здесь разбор ввода и
показ. Разложенные по представлению, правила разъехались бы с проверками при
первой правке формы — тот же довод, что записан в шапке `web/cash.py`.

**Три поля дат, и это главное, что видно на экране.** Дата документа, период
учёта и дата денег разведены нарочно: счёт за июнь приходит в июле, а оплачен
может быть в августе. Продукт, у которого дата одна, молча кладёт такой счёт не
в тот месяц — и расхождение всплывает при сборке P&L, когда сходиться уже
поздно.

**Точку и регистр отвергает база, а не форма** (D014). Представление передаёт
то, что пришло, в `upsert_fact`, и чужую точку отвергает политика
`unit_visibility` на `facts`. Список вариантов в форме — удобство, а не защита:
защита, написанная в двух местах, однажды разойдётся молча.

**Статья необязательна, и это решение, а не послабление.** Счёт приходит на
юрлицо целиком, и статью в нём никто не проставлял; заставить человека выбрать
её в момент внесения — значит получить выбор наугад. Такая строка получает
служебную статью «Не разобрано», остаётся видимой в P&L и встаёт в инбокс
(T152), где её разбирают подряд.
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

from core.models import Counterparty, Unit

from . import cash, suppliers
from .cash_views import _ledger, _till, _unit, _vat_rate
from .counterparties_views import found as counterparties_found
from .dbrefusal import BadInput
from .directory_views import LEDGER_CODES, _number, _select, _text
from .format import EMPTY, ledger_title, money
from .i18n import month_title
from .principal import get_current_principal
from .tills_views import visible_tills

# Состояние счёта для человека и для приёмки. Значения короткие и машинные: по
# ним же приёмка сверяет отбор «только неоплаченные» с показанными строками.
PAID, PARTLY, UNPAID = "paid", "partly", "unpaid"

# Что показывать: все счета, только неоплаченные, только оплаченные. Отбор, а не
# другая схема: вопрос «считаем обязательства или только оплаченное» (Q017) ждёт
# владельца, и до ответа продукт умеет оба среза, ничего не выбирая за него.
STATES = ("all", "unpaid", "paid")

LABELS = {
    "from": gettext_lazy("С даты"),
    "to": gettext_lazy("По дату"),
    "counterparty": gettext_lazy("Контрагент"),
    "ledger": gettext_lazy("Регистр учёта"),
    "state": gettext_lazy("Оплата"),
}


def _no_membership(request, what: str):
    return render(request, "web/directory/denied.html", {"message": what}, status=403)


# --- отбор --------------------------------------------------------------------


def filters_default() -> dict:
    """Умолчание — текущий месяц по дате документа: с ним и работают со счетами."""
    first = date.today().replace(day=1)
    return {
        "from": first, "to": _last_day(first),
        "counterparty": "", "ledger": "", "state": "all",
    }


def _last_day(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def filters_from(request) -> dict:
    """Что человек выбрал в адресе — одинаково для экрана и для вызова по HTTP.

    Разбор здесь один на обе поверхности: пока его было два, один и тот же адрес
    отвечал экраном и вызовом по-разному (T133, третья очередь). Неразобранное
    значение — отказ, а не тихое умолчание: тихое умолчание означало бы, что
    человек смотрит не тот период, чем думает.
    """
    chosen = filters_default()
    for name in ("from", "to"):
        raw = (request.GET.get(name) or "").strip()
        if raw:
            chosen[name] = _day(raw, name)
    if chosen["to"] < chosen["from"]:
        raise BadInput(_("Конец периода раньше начала: поправьте даты."))

    raw = (request.GET.get("counterparty") or "").strip()
    # Контрагент уходит в выборку как есть: чужого отсекут политики базы (D014).
    # Проверяется только то, что это вообще номер, — негодное значение
    # отвергается, а не подменяется полным списком (тот же довод, что в T134).
    chosen["counterparty"] = _id_or_refuse(raw, "counterparty") if raw else ""

    # Регистр разбору не подлежит: неизвестное слово отвечает тем же, чем
    # невидимый регистр, — пустотой (D023, разбор в `suppliers.invoices`).
    chosen["ledger"] = (request.GET.get("ledger") or "").strip()

    state = (request.GET.get("state") or "all").strip() or "all"
    if state not in STATES:
        raise BadInput(
            _("«%(label)s»: такого варианта нет.") % {"label": LABELS["state"]}
        )
    chosen["state"] = state
    return chosen


def _day(raw: str, name: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise BadInput(
            _("«%(label)s»: дата пишется как 2026-06-01, а не «%(value)s».")
            % {"label": LABELS[name], "value": raw}
        ) from None


def _id_or_refuse(raw: str, name: str) -> str:
    from uuid import UUID

    try:
        return str(UUID(raw))
    except ValueError:
        raise BadInput(
            _("«%(label)s»: это номер из списка, а не «%(value)s».")
            % {"label": LABELS[name], "value": raw}
        ) from None


# --- список -------------------------------------------------------------------


@login_required
def invoices(request):
    """Счета за период: контрагент, суммы, оплачено или нет."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому счетов у вас нет. "
            "Попросите администратора сети добавить вас."
        ))

    error, status = "", 200
    try:
        chosen = filters_from(request)
    except BadInput as bad:
        error, status = bad.message, bad.http_status
        chosen = filters_default()

    rows = rows_for(who, chosen) if not error else []
    total = sum((row["amount"] for row in rows), Decimal("0"))
    left = sum((row["left"] for row in rows), Decimal("0"))
    return render(request, "web/suppliers/invoices.html", {
        "error": error,
        "notice": _notice(request),
        "rows": rows,
        "total_raw": f"{total}", "total_text": money(total),
        "left_raw": f"{left}", "left_text": money(left),
        "counted": len(rows),
        "filters": _filter_fields(who, chosen),
        "add_url": reverse("invoice-new"),
        "payment_url": reverse("payment-new"),
        "inbox_url": reverse("inbox"),
    }, status=status)


def rows_for(who, chosen: dict, *, window: tuple[int, int] | None = None) -> list[dict]:
    """Строки списка. Выборка одна: из неё же считаются оба итога.

    Второй запрос `sum(amount)` был бы вторым источником истины и разошёлся бы с
    показанным молча — например, потому что политика сузила не так, как здесь.
    """
    listed = suppliers.invoices(who, chosen)
    if window is not None:
        offset, size = window
        listed = listed[offset:offset + size]

    paid = suppliers.paid_by_document([row.document.id for row in listed])
    shown = [row_of(row, paid.get(str(row.document.id))) for row in listed]
    if chosen.get("state") == "unpaid":
        return [row for row in shown if row["state"] != PAID]
    if chosen.get("state") == "paid":
        return [row for row in shown if row["state"] == PAID]
    return shown


def row_of(listed, paid) -> dict:
    """Строка счёта для списка и для вызова по HTTP — одна и та же."""
    document, fact = listed.document, listed.fact
    paid = Decimal(paid or 0)
    left = suppliers.outstanding(listed.amount, paid)
    state = PAID if left == 0 else (PARTLY if paid > 0 else UNPAID)
    return {
        "id": str(document.id),
        "url": reverse("invoice", args=[document.id]),
        "date": fact.doc_date.isoformat() if fact.doc_date else "",
        "period": month_title(fact.period),
        "period_code": f"{fact.period:%Y-%m}",
        "number": document.doc_number or EMPTY,
        "counterparty": document.counterparty.title if document.counterparty else EMPTY,
        # Точка без строки в справочнике — состояние, которого при исправных
        # политиках не бывает: счёт чужой точки роль не видит вовсе. Но если
        # политики разойдутся, узнать об этом человек должен прочерком, а не
        # оборванным запросом.
        "unit": fact.unit.code if fact.unit else (EMPTY if fact.unit_id else _("Вся сеть")),
        # Название статьи — на языке страницы, из самой статьи. `title` факта —
        # снимок на языке того, кто вносил, и он обязан остаться в данных, но
        # показывать читателю нужно его язык (тот же довод, что в T134).
        "item": (cash.item_title(fact.expense_item.titles) if fact.expense_item
                 else _("Без статьи")),
        "classified": fact.expense_item_id is not None,
        # Сумма счёта СЕГОДНЯ: исходная строка плюс исправления, если они были.
        "amount": listed.amount,
        "amount_raw": f"{listed.amount}",
        "amount_text": money(listed.amount),
        "corrections": listed.corrections,
        "paid": paid,
        "paid_raw": f"{paid}",
        "paid_text": money(paid),
        "left": left,
        "left_raw": f"{left}",
        "left_text": money(left),
        "state": state,
        "state_title": _state_title(state),
        "ledger": ledger_title(fact.ledger),
        "ledger_code": fact.ledger,
        "note": fact.note or "",
        # Месяц учёта называется только тогда, когда он не совпадает с месяцем
        # документа: в обычном случае это шум, а в необычном — единственный
        # способ понять, где искать строку.
        "landed_in": (
            month_title(fact.period)
            if fact.doc_date is None or fact.period != fact.doc_date.replace(day=1)
            else ""
        ),
    }


def _state_title(state: str) -> str:
    return {
        PAID: _("оплачен"),
        PARTLY: _("оплачен частично"),
        UNPAID: _("не оплачен"),
    }[state]


def _filter_fields(who, chosen: dict) -> list[dict]:
    """Поля отбора. Списки — только из того, что видно роли (D023)."""
    return [
        {"kind": "date", "name": "from", "label": LABELS["from"],
         "value": chosen["from"].isoformat()},
        {"kind": "date", "name": "to", "label": LABELS["to"],
         "value": chosen["to"].isoformat()},
        _select(
            "counterparty", LABELS["counterparty"],
            counterparties_found().values_list("id", "title"),
            chosen["counterparty"], required=False, empty_label=_("Все контрагенты"),
        ),
        _select(
            "ledger", LABELS["ledger"],
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            chosen["ledger"], required=False, empty_label=_("Все регистры"),
        ),
        _select(
            "state", LABELS["state"],
            [("unpaid", _("Только неоплаченные")), ("paid", _("Только оплаченные"))],
            "" if chosen["state"] == "all" else chosen["state"],
            required=False, empty_label=_("Все счета"),
        ),
    ]


def _notice(request) -> str:
    """Что случилось до перехода сюда. Готовую фразу в адрес не положить."""
    said = []
    saved = _month_or_none(request.GET.get("saved"))
    if saved is not None:
        said.append(_("Счёт записан в период %(month)s.") % {"month": month_title(saved)})
    # Признак называется `moved`, а не `from`: на этой странице `from` — поле
    # отбора «С даты», и общее имя означало бы, что после записи счёта список
    # молча показывает не тот период. Найдено приёмкой: список отвечал отказом
    # «дата пишется как 2026-06-01» сразу после успешной записи.
    moved = _month_or_none(request.GET.get("moved"))
    if moved is not None:
        said.append(_(
            "Месяц %(month)s закрыт, поэтому счёт учтён в открытом периоде — "
            "дата документа осталась прежней (правка задним числом идёт "
            "отдельной строкой)."
        ) % {"month": month_title(moved)})
    paid = _month_or_none(request.GET.get("paid"))
    if paid is not None:
        said.append(_("Оплата записана в период %(month)s.") % {"month": month_title(paid)})
    return " ".join(said)


def _month_or_none(raw: str | None) -> date | None:
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m").date().replace(day=1)
    except ValueError:
        return None


# --- счёт: внесение и правка ---------------------------------------------------


@login_required
def invoice(request, document_id=None):
    """Карточка счёта: внесение, правка и список платежей по нему."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому вносить счёт некуда. "
            "Попросите администратора сети добавить вас."
        ))

    document, fact = None, None
    if document_id is not None:
        document = suppliers.document_or_none(document_id)
        fact = suppliers.invoice_fact(document) if document is not None else None
        if document is None or fact is None:
            # Чужой счёт и несуществующий отвечают одинаково: по ответу нельзя
            # понять, что счёт есть у другой точки (D023).
            raise Http404("счёт не найден")

    error, status = "", 200
    entered = request.POST if request.method == "POST" else _entered(document, fact)
    if request.method == "POST":
        try:
            return redirect(_save(request, who, document, fact))
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/suppliers/invoice.html", {
        "error": error,
        "heading": (_("Счёт %(number)s") % {"number": document.doc_number}
                    if document is not None and document.doc_number
                    else (_("Счёт") if document is not None else _("Новый счёт"))),
        "entry_key": (suppliers.entry_key_of(fact) if fact is not None
                      else cash.new_entry_key()),
        "fields": invoice_fields(who, entered),
        "back_url": reverse("invoices"),
        "closed_note": _closed_note(who, fact),
        "payments": _payments(document) if document is not None else [],
        "pay_url": reverse("invoice-pay", args=[document.id]) if document is not None else "",
        "summary": _summary(document, fact) if document is not None else None,
    }, status=status)


def _save(request, who, document, fact) -> str:
    """Записать счёт и вернуть адрес, на который уводим после записи.

    Возврат адресом, а не страницей: обновление после записи не должно вносить
    счёт второй раз. Что именно случилось, уезжает в адрес месяцами, а не
    готовой фразой — фразу в адресе не перевести.
    """
    entered = parse_invoice(request, who)
    if fact is None:
        entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
        recorded = suppliers.record_invoice(who, entry_key=entry_key, **entered)
    else:
        recorded = suppliers.revise_invoice(who, document, fact, **entered)

    landed = reverse("invoices") + f"?saved={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&moved={recorded.landing.moved_from:%Y-%m}"
    if entered["unit_id"] is None:
        # Счёт на всю сеть разносится сразу, как и расход: узнать через месяц,
        # что сумма висела нераспределённой, — худший из ответов.
        cash.spread_now(recorded.fact_id)
    return landed


def parse_invoice(request, who) -> dict:
    """Разобрать форму счёта — одинаково для внесения и для правки.

    Одной функцией, а не двумя похожими: правила «период учёта — месяц»,
    «контрагент действует на дату» и «точку отвергает база» одни и те же, а две
    копии разъехались бы на первой правке, причём молча — каждая по отдельности
    осталась бы верной.
    """
    doc_date = _required_date(request, "date", _("Дата документа"))
    amount = _number(request, "amount", _("Сумма"))
    if amount == 0:
        raise BadInput(_("«%(label)s»: счёт на ноль не вносится.") % {"label": _("Сумма")})

    return {
        "doc_date": doc_date,
        "period": _period(request, doc_date),
        "counterparty": _counterparty(request, doc_date),
        "item": _item_or_none(request, doc_date),
        "unit_id": _unit(request, who),
        "ledger": _ledger(request, who),
        "amount": amount,
        "vat_rate": _vat_rate(request),
        "number": _text(request, "number", _("Номер счёта"), required=False),
        "note": _text(request, "note", _("Комментарий"), required=False),
    }


def _required_date(request, name: str, label: str) -> date:
    raw = (request.POST.get(name) or "").strip()
    if not raw:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": label})
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise BadInput(
            _("«%(label)s»: дата пишется как 2026-06-01, а не «%(value)s».")
            % {"label": label, "value": raw}
        ) from None


def _period(request, doc_date: date) -> date:
    """Период учёта: месяц, к которому счёт относится. Пусто — месяц документа.

    Умолчание именно такое, а не «текущий месяц»: чаще всего счёт и относится к
    своему месяцу, и требовать выбор там, где выбирать нечего, — лишний шаг.
    Разница появляется у коммунальных счетов, и ради неё поле и стоит на форме.
    """
    raw = (request.POST.get("period") or "").strip()
    if not raw:
        return doc_date.replace(day=1)
    try:
        return datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise BadInput(
            _("«%(label)s»: месяц пишется как 2026-06, а не «%(value)s».")
            % {"label": _("Период учёта"), "value": raw}
        ) from None


def _counterparty(request, on: date) -> Counterparty:
    """Контрагент счёта. Чужой и несуществующий отвечают одинаково (D023)."""
    raw = (request.POST.get("counterparty") or "").strip()
    if not raw:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": _("Контрагент")})

    from uuid import UUID
    try:
        wanted = UUID(raw)
    except ValueError:
        raise BadInput(_("Контрагент не найден.")) from None

    row = Counterparty.objects.filter(pk=wanted).first()
    if row is None:
        raise BadInput(_("Контрагент не найден."))
    if row.valid_from > on or (row.valid_to is not None and row.valid_to <= on):
        raise BadInput(
            _("Контрагент «%(title)s» на %(date)s не действует: выберите другого "
              "или поправьте даты в справочнике.")
            % {"title": row.title, "date": on.isoformat()}
        )
    return row


def _item_or_none(request, on: date):
    """Статья расхода — необязательная. Пусто значит «в инбокс», а не «ошибка».

    Действие статьи на дату проверяется так же, как у расхода из кассы: статья,
    заведённая в августе, не объясняет июньский счёт, и молча принять её значило
    бы поставить в отчёт правило, которого в тот месяц не было.
    """
    raw = (request.POST.get("item") or "").strip()
    if not raw:
        return None

    from uuid import UUID

    from core.models import ExpenseItem
    try:
        wanted = UUID(raw)
    except ValueError as bad_uuid:
        # `from` обязателен: без него в трассировке остаётся только наша фраза, и
        # непонятно, что исходной причиной был неразобранный идентификатор.
        raise BadInput(
            _("«%(label)s»: такого варианта нет.") % {"label": _("Статья расхода")}
        ) from bad_uuid

    item = ExpenseItem.objects.select_related("pnl_item").filter(pk=wanted).first()
    if item is None:
        raise BadInput(_("Статья не найдена."))
    if item.valid_from > on or (item.valid_to is not None and item.valid_to <= on):
        raise BadInput(
            _("Статья «%(title)s» на %(date)s не действует: выберите другую "
              "или поправьте её в справочнике.")
            % {"title": cash.item_title(item.titles), "date": on.isoformat()}
        )
    return item


def _entered(document, fact) -> dict:
    """Что показать в полях карточки: то, что записано."""
    if document is None or fact is None:
        return {}
    return {
        "date": fact.doc_date.isoformat() if fact.doc_date else "",
        "period": f"{fact.period:%Y-%m}",
        "counterparty": str(fact.counterparty_id or ""),
        "number": document.doc_number or "",
        "item": str(fact.expense_item_id or ""),
        "unit": str(fact.unit_id) if fact.unit_id else cash.NETWORK_UNIT,
        "ledger": fact.ledger,
        "amount": f"{fact.amount}",
        "vat_rate": _plain(fact.vat_rate),
        "note": fact.note or "",
    }


def _plain(rate) -> str:
    """Ставка без хвоста нулей: 20.000 в поле формы читается как опечатка."""
    if rate is None:
        return ""
    text = f"{rate.normalize():f}"
    return text


def invoice_fields(who, entered) -> list[dict]:
    """Поля формы счёта — одни и те же на внесении и на правке."""
    today = date.today()
    on = _entered_date(entered) or today
    return [
        {"kind": "date", "name": "date", "label": _("Дата документа"), "required": True,
         "value": entered.get("date") or today.isoformat(),
         "help": _("Дата на самом счёте.")},
        {"kind": "month", "name": "period", "label": _("Период учёта"),
         "value": entered.get("period") or f"{on:%Y-%m}",
         "help": _("Месяц, к которому счёт относится. Счёт за июнь приходит в июле — "
                   "в P&L он должен попасть в июнь. Пусто — месяц документа.")},
        _select(
            "counterparty", _("Контрагент"),
            counterparties_found(open_on=on).values_list("id", "title"),
            entered.get("counterparty"), required=True,
            help=_("Кому платим. Нет в списке — заведите его в справочнике контрагентов."),
        ),
        {"kind": "text", "name": "number", "label": _("Номер счёта"),
         "value": entered.get("number") or "",
         "help": _("Как счёт назван у поставщика. По нему его найдут в переписке.")},
        _select(
            "item", _("Статья расхода"),
            [(item.id, cash.item_title(item.titles))
             for item in cash.items_on(who.tenant_id, on)],
            entered.get("item"), required=False, empty_label=_("Пока не разобрано"),
            help=_("Можно не выбирать: счёт встанет в инбокс, и статью назначат там. "
                   "Сумма при этом видна в P&L, а не пропадает."),
        ),
        _unit_field(who, entered),
        _select(
            "ledger", _("Регистр учёта"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            entered.get("ledger") or "official", required=True,
            help=_("Куда отнести расход. Регистр платежа приезжает из кассы отдельно."),
        ),
        {"kind": "number", "name": "amount", "label": _("Сумма"), "required": True,
         "value": entered.get("amount") or "",
         "help": _("Сумма документа, как она в счёте — с налогом, если он в нём есть.")},
        {"kind": "number", "name": "vat_rate", "label": _("Ставка НДС"),
         "value": entered.get("vat_rate") or "",
         "help": _("В процентах. Пусто — налог не выделен. В P&L по умолчанию "
                   "едет сумма без налога.")},
        {"kind": "text", "name": "note", "label": _("Комментарий"),
         "value": entered.get("note") or ""},
    ]


def _unit_field(who, entered) -> dict:
    """Точка счёта. «Вся сеть» — вариант списка, а не пустое поле.

    Пустое поле означает «не выбрал», и молча превратить его в «на всю сеть»
    значило бы разнести по точкам счёт, который человек просто не дозаполнил.
    """
    units = Unit.objects.order_by("code")
    if who.unit_ids:
        units = units.filter(pk__in=who.unit_ids)
    options = [(code, title) for code, title in units.values_list("id", "code")]
    if not who.unit_ids:
        options = [(cash.NETWORK_UNIT, _("Вся сеть"))] + options
    return _select(
        "unit", _("Точка"), options, entered.get("unit"), required=True,
        help=_("Чей это расход. «Вся сеть» — счёт на юрлицо целиком: его разнесёт "
               "правило контрагента."),
    )


def _entered_date(entered) -> date | None:
    raw = (entered.get("date") or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _closed_note(who, fact) -> str:
    """Что случится с правкой счёта закрытого месяца — сказано ДО кнопки."""
    if fact is None or not cash.month_is_closed(who.tenant_id, fact.period):
        return ""
    return _(
        "Месяц %(month)s закрыт. Правка не тронет его: в открытом периоде "
        "появятся две строки — сторно прежней суммы и исправленная запись."
    ) % {"month": month_title(fact.period)}


def _summary(document, fact) -> dict:
    """Сколько по счёту и сколько осталось. Считается сложением, а не колонкой.

    Сумма счёта — сложение его действующих строк (исходной и исправлений),
    оплата — сложение платежей. Ни того ни другого нет отдельной колонкой
    намеренно: колонка была бы вторым ответом на тот же вопрос и разошлась бы с
    первым на первом же частичном платеже, молча.
    """
    amount = suppliers.invoice_amount(document)
    paid = sum((row.amount for row in suppliers.payments_of(document)), Decimal("0"))
    left = suppliers.outstanding(amount, paid)
    state = PAID if left == 0 else (PARTLY if paid > 0 else UNPAID)
    return {
        "amount_text": money(amount), "amount_raw": f"{amount}",
        "paid_text": money(paid), "paid_raw": f"{paid}",
        "left_text": money(left), "left_raw": f"{left}",
        "state": state, "state_title": _state_title(state),
    }


def _payments(document) -> list[dict]:
    return [
        {
            "date": row.doc_date.isoformat() if row.doc_date else "",
            "period": month_title(row.period),
            "amount_text": money(row.amount), "amount_raw": f"{row.amount}",
            "till": row.till.code if row.till else EMPTY,
            "ledger": ledger_title(row.ledger),
            "channel": _("наличными") if row.channel == "cash" else _("банком"),
            "note": row.note or "",
        }
        for row in suppliers.payments_of(document)
    ]


# --- оплата счёта --------------------------------------------------------------


@login_required
def invoice_pay(request, document_id):
    """Отметка оплаты: отдельное событие с собственной датой."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому отмечать оплату нечему."
        ))

    document = suppliers.document_or_none(document_id)
    fact = suppliers.invoice_fact(document) if document is not None else None
    if document is None or fact is None:
        raise Http404("счёт не найден")

    summary = _summary(document, fact)
    error, status = "", 200
    entered = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        try:
            return redirect(_pay(request, who, document))
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/suppliers/pay.html", {
        "error": error,
        "heading": _("Отметить оплату"),
        "about": (_("Счёт %(number)s") % {"number": document.doc_number}
                  if document.doc_number else document.counterparty.title),
        "summary": summary,
        "entry_key": cash.new_entry_key(),
        "fields": _pay_fields(who, entered, fact, summary),
        "back_url": reverse("invoice", args=[document.id]),
    }, status=status)


def _pay(request, who, document) -> str:
    entered = parse_payment(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
    recorded = suppliers.pay(who, document, entry_key=entry_key, **entered)
    landed = reverse("invoices") + f"?paid={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&moved={recorded.landing.moved_from:%Y-%m}"
    return landed


def parse_payment(request, who) -> dict:
    """Разобрать форму оплаты. Регистр приезжает из кассы, если она указана (D039)."""
    amount = _number(request, "amount", _("Сумма оплаты"))
    if amount == 0:
        raise BadInput(
            _("«%(label)s»: оплата на ноль не вносится.") % {"label": _("Сумма оплаты")}
        )
    till_id, till = _till(request)
    return {
        "on": _required_date(request, "date", _("Дата оплаты")),
        "amount": amount,
        "till_id": till_id,
        "ledger": _ledger(request, who, till),
        "note": _text(request, "note", _("Комментарий"), required=False),
    }


def _pay_fields(who, entered, fact, summary) -> list[dict]:
    today = date.today()
    return [
        {"kind": "date", "name": "date", "label": _("Дата оплаты"), "required": True,
         "value": entered.get("date") or today.isoformat(),
         "help": _("Когда деньги ушли. Это своя дата: она не обязана совпадать "
                   "ни с датой счёта, ни с периодом учёта.")},
        {"kind": "number", "name": "amount", "label": _("Сумма оплаты"), "required": True,
         "value": entered.get("amount") or summary["left_raw"],
         "help": _("Меньше остатка — частичная оплата: остаток по счёту уменьшится "
                   "на неё, а не закроется.")},
        _select(
            "till", _("Касса"),
            visible_tills(open_only=True).values_list("id", "code"),
            entered.get("till"), required=False, empty_label=_("Оплачено банком"),
            help=_("Если платили наличными. Регистр учёта тогда приезжает из кассы."),
        ),
        _select(
            "ledger", _("Регистр учёта"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            entered.get("ledger") or fact.ledger, required=False,
            empty_label=_("Как у кассы"),
            help=_("Пусто — регистр берётся из кассы, а без кассы остаётся официальным."),
        ),
        {"kind": "text", "name": "note", "label": _("Комментарий"),
         "value": entered.get("note") or ""},
    ]


# --- оплата без счёта ----------------------------------------------------------


@login_required
def payment_new(request):
    """Оплата без счёта: мелкая покупка, за которую бумаги не будет."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому вносить оплату некуда."
        ))

    error, status = "", 200
    entered = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        try:
            return redirect(_purchase(request, who))
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/suppliers/payment.html", {
        "error": error,
        "entry_key": cash.new_entry_key(),
        "fields": _purchase_fields(who, entered),
        "back_url": reverse("invoices"),
    }, status=status)


def _purchase(request, who) -> str:
    entered = parse_purchase(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
    recorded = suppliers.record_purchase(who, entry_key=entry_key, **entered)
    landed = reverse("invoices") + f"?paid={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&moved={recorded.landing.moved_from:%Y-%m}"
    if entered["unit_id"] is None:
        cash.spread_now(recorded.fact_id)
    return landed


def parse_purchase(request, who) -> dict:
    on = _required_date(request, "date", _("Дата оплаты"))
    amount = _number(request, "amount", _("Сумма"))
    if amount == 0:
        raise BadInput(_("«%(label)s»: оплата на ноль не вносится.") % {"label": _("Сумма")})
    return {
        "on": on,
        "counterparty": _counterparty(request, on),
        "item": _item_or_none(request, on),
        "unit_id": _unit(request, who),
        "ledger": _ledger(request, who),
        "amount": amount,
        "vat_rate": _vat_rate(request),
        "note": _text(request, "note", _("Комментарий"), required=False),
    }


def _purchase_fields(who, entered) -> list[dict]:
    today = date.today()
    on = _entered_date(entered) or today
    return [
        {"kind": "date", "name": "date", "label": _("Дата оплаты"), "required": True,
         "value": entered.get("date") or today.isoformat(),
         "help": _("Когда деньги ушли. Счёта нет, поэтому расход признаётся этой датой.")},
        _select(
            "counterparty", _("Контрагент"),
            counterparties_found(open_on=on).values_list("id", "title"),
            entered.get("counterparty"), required=True,
            help=_("Кому заплатили. Нет в списке — заведите его в справочнике."),
        ),
        _select(
            "item", _("Статья расхода"),
            [(item.id, cash.item_title(item.titles))
             for item in cash.items_on(who.tenant_id, on)],
            entered.get("item"), required=False, empty_label=_("Пока не разобрано"),
            help=_("Можно не выбирать: строка встанет в инбокс и будет видна числом."),
        ),
        _unit_field(who, entered),
        _select(
            "ledger", _("Регистр учёта"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            entered.get("ledger") or "official", required=True,
        ),
        {"kind": "number", "name": "amount", "label": _("Сумма"), "required": True,
         "value": entered.get("amount") or ""},
        {"kind": "number", "name": "vat_rate", "label": _("Ставка НДС"),
         "value": entered.get("vat_rate") or ""},
        {"kind": "text", "name": "note", "label": _("Комментарий"),
         "value": entered.get("note") or ""},
    ]




# --- инбокс классификации (T152) ----------------------------------------------


@login_required
def inbox(request):
    """Строки без статьи одним списком с суммой и разбором прямо в нём.

    **Сумма сверху — главное на экране.** Неразобранная трата опаснее
    отсутствующей: она уже сидит в P&L, но не там, где её будут искать. Число
    наверху и есть ответ на вопрос «сколько денег сейчас не разложено» — без
    него список читается как техническая мелочь.

    **Разбор идёт в самом списке, а не по странице на строку.** Их разбирают
    подряд, десятками; переход на карточку и обратно на каждую строку означал бы
    вдвое больше нажатий, чем самой работы.

    **Кому что видно, решает база.** Строку чужой точки и невидимого регистра
    отсекают политики `facts`, а строку без точки видит только тот, кто ведёт
    все точки партнёра (`app_network_row_is_visible`). Проверять роль здесь
    значило бы завести второй ответ на вопрос о доступе — тот самый, который
    однажды разойдётся с первым молча (D014).
    """
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому разбирать нечего."
        ))

    rows = inbox_rows(who)
    total = sum((row["amount"] for row in rows), Decimal("0"))
    return render(request, "web/suppliers/inbox.html", {
        "rows": rows,
        "total_raw": f"{total}", "total_text": money(total),
        "counted": len(rows),
        "notice": _inbox_notice(request),
        "back_url": reverse("invoices"),
    })


def inbox_rows(who) -> list[dict]:
    """Строки инбокса и поля разбора у каждой. Выборка одна: из неё же итог."""
    items = [
        (item.id, cash.item_title(item.titles))
        for item in cash.items_on(who.tenant_id, date.today())
    ]
    units = Unit.objects.order_by("code")
    if who.unit_ids:
        units = units.filter(pk__in=who.unit_ids)
    unit_options = [(cash.NETWORK_UNIT, _("Вся сеть"))] + [
        (code, title) for code, title in units.values_list("id", "code")
    ]

    return [
        {
            "id": str(row.id),
            "date": row.doc_date.isoformat() if row.doc_date else "",
            "period": month_title(row.period),
            "counterparty": row.counterparty.title if row.counterparty else EMPTY,
            # Название позиции: до разбора это имя контрагента или строка
            # источника. Показывается как есть — по нему человек и опознаёт трату.
            "title": row.title,
            "amount": row.amount,
            "amount_raw": f"{row.amount}",
            "amount_text": money(row.amount),
            "ledger": ledger_title(row.ledger),
            "unit": (row.unit.code if row.unit
                     else (EMPTY if row.unit_id else _("Вся сеть"))),
            "note": row.note or "",
            "url": reverse("inbox-classify", args=[row.id]),
            "invoice_url": (reverse("invoice", args=[row.document_id])
                            if row.document_id else ""),
            "items": [
                {"code": str(code), "title": title, "selected": False}
                for code, title in items
            ],
            "units": [
                {"code": str(code), "title": title,
                 "selected": str(code) == (str(row.unit_id) if row.unit_id
                                           else cash.NETWORK_UNIT)}
                for code, title in unit_options
            ],
        }
        for row in suppliers.waiting_for_an_article(who)
    ]


@login_required
def inbox_classify(request, fact_id):
    """Назначить строке статью и точку. Только POST: это запись денег."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому разбирать нечего."
        ))

    fact = suppliers.unclassified_fact(fact_id)
    if fact is None:
        # Чужая строка, выдуманный номер и уже разобранная отвечают одинаково:
        # по ответу нельзя понять, что строка существует (D023).
        raise Http404("строка не найдена")

    try:
        item = _item_or_none(request, fact.doc_date or fact.period)
        if item is None:
            raise BadInput(
                _("Поле «%(label)s» обязательно.") % {"label": _("Статья расхода")}
            )
        recorded = suppliers.classify(
            who, fact, item=item, unit_id=_unit(request, who),
        )
    except (BadInput, cash.UnitRefused, cash.CashRefused):
        # Отказ уезжает в адрес признаком, а не готовой фразой: фразу в адресе не
        # перевести и подставить в неё можно что угодно. Строка при этом остаётся
        # в инбоксе — молча уйти она не может.
        return redirect(reverse("inbox") + "?failed=1")

    landed = reverse("inbox") + f"?done={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&moved={recorded.landing.moved_from:%Y-%m}"
    if _unit(request, who) is None:
        # Строка на всю сеть разносится сразу, как расход и как счёт: узнать
        # через месяц, что сумма висела нераспределённой, — худший из ответов.
        cash.spread_now(recorded.fact_id)
    return redirect(landed)


def _inbox_notice(request) -> str:
    """Что случилось с разбором. Молчание после нажатия читается как успех."""
    if request.GET.get("failed"):
        return _(
            "Строка не разобрана: выберите статью и точку и попробуйте ещё раз. "
            "Строка осталась в списке."
        )
    said = []
    done = _month_or_none(request.GET.get("done"))
    if done is not None:
        said.append(_("Строка разобрана и учтена в периоде %(month)s.")
                    % {"month": month_title(done)})
    moved = _month_or_none(request.GET.get("moved"))
    if moved is not None:
        said.append(_(
            "Месяц %(month)s закрыт, поэтому разбор лёг в открытый период двумя "
            "строками: сторно прежней и разобранная запись."
        ) % {"month": month_title(moved)})
    return " ".join(said)



__all__ = [
    "filters_default",
    "inbox",
    "inbox_classify",
    "inbox_rows",
    "filters_from",
    "invoice",
    "invoice_pay",
    "invoices",
    "parse_invoice",
    "parse_payment",
    "parse_purchase",
    "payment_new",
    "row_of",
    "rows_for",
]
