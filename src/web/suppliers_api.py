"""Счета, платежи и инбокс по HTTP: та же дверь, что у экрана (T153).

Продолжение `web/api.py`, а не второй API. Обвязка вызова (`endpoint`), формат
ответа и разбор тела JSON берутся **оттуда же**: два набора правил о том, что
такое отказ и как выглядит ответ, разъехались бы молча — экран проверяется
смоуком, а вызов нет.

Пять условий спеки, те же самые, и вот где они здесь:

1. **Роль и тенант приезжают контекстом базы.** Ни тенанта, ни роли в параметрах
   нет и быть не может: срез делает `DbContextMiddleware`, тот же, что рисует
   страницы. Здесь это выражено отсутствием кода.
2. **Срез по регистру — параметр запроса** (`?ledger=`), разбирается тем же
   `filters_from`, что у экрана. Одна и та же ссылка обязана отвечать экраном и
   вызовом одинаково; пока разборов было два, они разъезжались (T133).
3. **Записывающие вызовы только POST.** GET на них — 405 с `Allow`.
4. **Отказ по невидимому неотличим от несуществующего** (D014, D023): чужой
   счёт, чужая точка и чужой контрагент отсекаются политиками ещё до
   представления, поэтому им физически нечем отличаться от выдуманных.
5. **Долгих операций нет.** Список ограничен окном и сам говорит, что есть ещё.

**Деньги отдаются строками.** Числом их отдавать нельзя: двоичная дробь
превращает 24000.10 в 24000.099999999999 на первом же потребителе, а
локализация — в «24 000,10».

Собственных правил о деньгах здесь ноль: разбор — `suppliers_views.parse_*`,
запись — `suppliers.*`, строки списка — `suppliers_views.row_of`. Своя копия
хоть одного из них означала бы два ответа на вопрос «что можно записать».
"""
from __future__ import annotations

from decimal import Decimal

from django.utils.translation import gettext as _

from . import cash, permissions, suppliers, suppliers_views
from .api import _json, _window, endpoint
from .dbrefusal import BadInput

__all__ = [
    "inbox",
    "inbox_classify",
    "invoice",
    "invoice_pay",
    "invoices",
    "payments",
]


# --- счета --------------------------------------------------------------------


@endpoint("GET", "POST")
def invoices(request, who):
    """Список счетов (GET) и внесение счёта (POST)."""
    if request.method == "POST":
        return _record(request, who)
    return _listing(request, who)


def _listing(request, who):
    """Тот же отбор и те же строки, что на экране, плюс окно.

    Итогов два, как и на экране, и считаются они по той же выборке: второй
    запрос `sum(amount)` был бы вторым источником истины и разошёлся бы с
    показанным молча.
    """
    chosen = suppliers_views.filters_from(request)
    limit, offset = _window(request)

    rows = suppliers_views.rows_for(who, chosen, window=(offset, limit + 1))
    has_more = len(rows) > limit
    rows = rows[:limit]
    total = sum((row["amount"] for row in rows), Decimal("0"))
    left = sum((row["left"] for row in rows), Decimal("0"))
    return _json({
        "rows": [_row(row) for row in rows],
        "count": len(rows),
        "total": f"{total}",
        "outstanding": f"{left}",
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    })


def _row(row: dict) -> dict:
    """Строка списка для вызывающего: та же, что уходит в разметку.

    Отформатированные для человека значения (`*_text`) остаются рядом с
    машинными: они нужны тому, кто показывает ответ человеку, и собраны на языке
    запроса. Сами суммы отдаются строками, теми же, по которым экран считает
    итог, — то есть сверить ответ с экраном можно посимвольно.
    """
    out = {
        name: value for name, value in row.items()
        if name not in ("amount", "paid", "left")
    }
    out["amount"] = row["amount_raw"]
    out["paid"] = row["paid_raw"]
    out["left"] = row["left_raw"]
    return out


def _record(request, who):
    """Внести счёт. Разбор и запись — те же, что у формы."""
    entered = suppliers_views.parse_invoice(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
    recorded = suppliers.record_invoice(who, entry_key=entry_key, **entered)

    answer = _recorded(recorded)
    if entered["unit_id"] is None:
        # Счёт на всю сеть разносится сразу, как и с экрана. `reason` — код
        # причины ожидания, а не переведённая фраза: бот обязан отличать
        # «правила нет» от «правило есть, но выручки в продукте пока нет».
        outcome = cash.spread_now(recorded.fact_id)
        answer["allocation"] = {
            "state": outcome.state, "rows": outcome.rows, "reason": outcome.reason,
        }
    return _json(answer)


def _recorded(recorded) -> dict:
    """Что случилось с деньгами: куда легли и завели ли новую версию."""
    return {
        "invoice_id": recorded.document_id or None,
        "fact_id": recorded.fact_id,
        "action": recorded.action,
        "period": f"{recorded.landing.period:%Y-%m}",
        "moved_from": (
            f"{recorded.landing.moved_from:%Y-%m}"
            if recorded.landing.moved_from else None
        ),
    }


@endpoint("GET", "POST")
def invoice(request, who, document_id):
    """Карточка счёта (GET) и правка (POST)."""
    document = suppliers.document_or_none(document_id)
    fact = suppliers.invoice_fact(document) if document is not None else None
    if document is None or fact is None:
        # Чужой счёт и выдуманный номер отвечают одинаково (D023).
        return _json({"error": _("Счёт не найден.")}, 404)

    if request.method == "GET":
        paid = suppliers.paid_by_document([document.id]).get(str(document.id))
        listed = suppliers.Listed(
            document=document, fact=fact,
            amount=suppliers.invoice_amount(document),
            corrections=len(suppliers.invoice_lines(document)) - 1,
        )
        return _json({
            "invoice": _row(suppliers_views.row_of(listed, paid)),
            "payments": [_payment(row) for row in suppliers.payments_of(document)],
        })

    entered = suppliers_views.parse_invoice(request, who)
    return _json(_recorded(suppliers.revise_invoice(who, document, fact, **entered)))


def _payment(row) -> dict:
    return {
        "fact_id": str(row.id),
        "date": row.doc_date.isoformat() if row.doc_date else None,
        "period": f"{row.period:%Y-%m}",
        "amount": f"{row.amount}",
        "channel": row.channel,
        "ledger": row.ledger,
        "till": str(row.till_id) if row.till_id else None,
        "note": row.note or "",
    }


@endpoint("POST")
def invoice_pay(request, who, document_id):
    """Отметить оплату счёта. Только POST: это запись денег."""
    document = suppliers.document_or_none(document_id)
    fact = suppliers.invoice_fact(document) if document is not None else None
    if document is None or fact is None:
        return _json({"error": _("Счёт не найден.")}, 404)

    entered = suppliers_views.parse_payment(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
    recorded = suppliers.pay(who, document, entry_key=entry_key, **entered)

    answer = _recorded(recorded)
    # Остаток по счёту после этой оплаты — числом, а не «оплачено да/нет»:
    # частичная оплата это обычное дело, и бот обязан узнать про неё сразу.
    paid = sum((row.amount for row in suppliers.payments_of(document)), Decimal("0"))
    answer["paid"] = f"{paid}"
    answer["left"] = f"{suppliers.outstanding(suppliers.invoice_amount(document), paid)}"
    return _json(answer)


@endpoint("POST")
def payments(request, who):
    """Оплата без счёта: расход признаётся датой денег."""
    entered = suppliers_views.parse_purchase(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
    recorded = suppliers.record_purchase(who, entry_key=entry_key, **entered)

    answer = _recorded(recorded)
    if entered["unit_id"] is None:
        outcome = cash.spread_now(recorded.fact_id)
        answer["allocation"] = {
            "state": outcome.state, "rows": outcome.rows, "reason": outcome.reason,
        }
    return _json(answer)


# --- инбокс -------------------------------------------------------------------


@endpoint("GET")
def inbox(request, who):
    """Строки без статьи: сумма и список. Та же выборка, что на экране."""
    rows = suppliers_views.inbox_rows(who)
    total = sum((row["amount"] for row in rows), Decimal("0"))
    return _json({
        "rows": [_inbox_row(row) for row in rows],
        "count": len(rows),
        "total": f"{total}",
    })


def _inbox_row(row: dict) -> dict:
    """Строка инбокса без вариантов выбора: списки статей нужны разметке, не боту."""
    out = {
        name: value for name, value in row.items()
        if name not in ("amount", "items", "units")
    }
    out["amount"] = row["amount_raw"]
    return out


@endpoint("POST")
def inbox_classify(request, who, fact_id):
    """Назначить строке статью и точку. Только POST: это запись денег.

    Право спрашивается первым, до поиска строки: разбор первички — то же
    привилегированное действие, что и на карточке бумаги, и закрывать его надо
    на всех поверхностях сразу. Закрыть одну значило бы оставить дыру в
    остальных, которые ходят в тот же `record_invoice` (T174, issue #143).
    """
    if not permissions.has(who, permissions.SUPPLIERS_CLASSIFY):
        return _json(
            {"error": permissions.explain(who, permissions.SUPPLIERS_CLASSIFY)}, 403,
        )
    fact = suppliers.unclassified_fact(fact_id)
    if fact is None:
        # Чужая строка, выдуманный номер и уже разобранная — один ответ (D023).
        return _json({"error": _("Строка не найдена.")}, 404)

    item = suppliers_views._item_or_none(request, fact.doc_date or fact.period)
    if item is None:
        raise BadInput(
            _("Поле «%(label)s» обязательно.") % {"label": _("Статья расхода")}
        )
    unit_id = suppliers_views._unit(request, who)
    return _json(_recorded(suppliers.classify(who, fact, item=item, unit_id=unit_id)))
