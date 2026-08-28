"""Счета поставщиков и платежи по ним (T151).

Модуль отвечает на один вопрос: **что такое счёт и что такое платёж в данных**.
Экраны (`suppliers_views`) и вызовы по HTTP (`suppliers_api`) берут ответ отсюда,
своего не заводят.

## Два события, а не одно с признаком «оплачено»

Счёт можно получить в июле, отнести к июню и оплатить в августе. Три даты, и все
три нужны:

| дата | где живёт |
|---|---|
| дата документа | `doc_date` строки счёта |
| период учёта | `period` строки счёта — по нему собирается P&L |
| дата денег | `doc_date` строки платежа, отдельной строки |

Колонки «оплачено» у счёта нет намеренно. Она была бы вторым ответом на вопрос,
на который отвечает сумма платежей, — и разошлась бы с ней на первом же
частичном платеже, молча. Оплачен счёт или нет, считается сложением; отсюда же
бесплатно получаются **аванс** (платёж без счёта) и **частичная оплата**
(платёж меньше остатка) — то есть оба варианта развилки Q018 поддержаны формой
данных, а не выбором за владельца.

## Что чем выражено

* **Счёт** — `source_documents` вида `invoice` (шапка: номер, дата, контрагент,
  итог) плюс строка `facts` с `document_id`. Позиция и есть факт, отдельной
  таблицы позиций в схеме нет (`0230_facts`).
* **Платёж по счёту** — строка `facts` со служебной статьёй P&L
  `supplier_payment` вида `transfer` и ссылкой на тот же документ. Переводы из
  P&L исключены по `kind` во всех отчётах сразу, поэтому платёж не удваивает
  расход, признанный счётом.
* **Оплата без счёта** — обычная расходная строка `facts` с контрагентом и
  каналом `bank`: документа нет, значит расход признаётся в момент оплаты.
  Наличная мелкая покупка сюда не относится — она вносится расходом из кассы
  (третья очередь), где регистр приезжает из самой кассы (D039).
* **Строка без статьи** — та же строка счёта, но со служебной статьёй
  `unclassified`. Она **видна числом** в P&L и стоит в инбоксе (T152), а не
  исчезает.

## Чего здесь нет и почему

Своего `insert` в `facts` нет ни одного: запись идёт через `cash.write_fact`,
то есть через `upsert_fact` базы. Там живут идемпотентность по `dedup_key`,
версионирование заменой и защита закрытого месяца (`facts_guard`), и второй путь
записи обошёл бы их все разом.

Проверок прав тоже нет: чужую точку, невидимый регистр и чужую кассу отвергают
политики `facts` (D014). Здесь только перевод их отказа в слова — и тот взят у
третьей очереди, чтобы формулировки на соседних экранах не разъезжались.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import Error as DatabaseError
from django.db import connection, models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import (
    ClassificationRule,
    Counterparty,
    ExpenseItem,
    Fact,
    PnlItem,
    SourceDocument,
)

from . import cash, permissions

# Приставки ключей идемпотентности. Разные у счёта, платежа и оплаты без счёта:
# ключ — это ответ на вопрос «то же самое событие или другое», и общая приставка
# означала бы, что платёж может заменить собой счёт.
INVOICE_PREFIX = "manual:invoice:"
PAYMENT_PREFIX = "manual:payment:"
PURCHASE_PREFIX = "manual:purchase:"

# Источник и вид документа. Строками, а не настройкой: это и есть определение
# того, что такое «счёт, внесённый руками».
MANUAL_SOURCE = "manual"
INVOICE_KIND = "invoice"
# Чек — тоже документ с расходными строками, только денег по нему больше не
# будет: их отдали на месте. Экраны счетов работают с ним так же, потому что
# работа с ним та же — разобрать позиции и увидеть их в P&L. Появился вид с
# приходом бумаг с точек (T174): управляющий приносит и накладную, и чек, а
# спрятать разобранный чек значило бы оставить в списке строку, ведущую в 404.
RECEIPT_KIND = "receipt"
DOCUMENT_KINDS = (INVOICE_KIND, RECEIPT_KIND)

# Служебные строки P&L, заведённые миграцией `0243`. Ищутся по коду, а не по
# приколоченному uuid: код — то, чем строка названа в схеме, и его же видно в
# отказе, если строки вдруг не окажется.
UNCLASSIFIED = "unclassified"
SUPPLIER_PAYMENT = "supplier_payment"


class SupplierRefused(cash.CashRefused):
    """Счёт или платёж не принят по состоянию данных. Сообщение — как есть.

    Наследник отказа третьей очереди намеренно: его уже ловят и формы, и вызовы
    по HTTP, и отдельный класс без родства остался бы неперехваченным ровно
    там, где перехватывать некому, — то есть стал бы пятисоткой.
    """


def line(code: str) -> PnlItem:
    """Служебная строка P&L по коду. Её отсутствие — дефект схемы, не ввод."""
    found = PnlItem.objects.filter(tenant__isnull=True, code=code).first()
    if found is None:
        raise RuntimeError(
            f"в справочнике P&L нет служебной строки {code!r}: миграция 0243 не накатана"
        )
    return found


# --- счёт ---------------------------------------------------------------------


@dataclass(frozen=True)
class Recorded:
    """Что случилось: какой документ, какая строка, куда легла, завелась ли версия."""

    document_id: str
    fact_id: str
    action: str            # inserted | updated | unchanged
    landing: cash.Landing


def record_invoice(
    who,
    *,
    entry_key: str,
    document_key: str = "",
    doc_date: date,
    period: date,
    counterparty: Counterparty,
    item,
    unit_id,
    ledger: str,
    amount,
    vat_rate,
    number: str = "",
    note: str = "",
) -> Recorded:
    """Записать счёт: шапку документа и его строку.

    Период учёта выбирает человек, а не дата документа: счёт за электричество за
    июнь приходит в июле, и в P&L он обязан лечь в июнь. Закрытый месяц при этом
    не переписывается — строка уезжает в текущий с исходной датой документа
    (D020), и об этом сказано словами, а не молча.

    Статья может быть не выбрана. Это не недосмотр формы, а рабочее состояние:
    счёт приходит на юрлицо целиком, и статью в нём никто не проставлял. Такая
    строка получает служебную статью «Не разобрано», остаётся видимой в P&L и
    встаёт в инбокс (T152).
    """
    landing = cash.landing_for(who.tenant_id, period)
    # Ключ документа отдельно от ключа строки. Совпадают они почти всегда, и
    # расходятся ровно в одном случае — исправление счёта закрытого месяца
    # (`revise_invoice`): у строки ключ свой, чтобы не столкнуться с исходной,
    # а документ обязан остаться ТЕМ ЖЕ. Иначе исправление завело бы второй
    # счёт с тем же номером, и оплата легла бы на один из двух — на какой,
    # человек бы не выбирал.
    document_id = _write_document(
        who,
        entry_key=document_key or entry_key,
        counterparty=counterparty,
        doc_date=doc_date,
        period=landing.period,
        number=number,
        amount=amount,
    )

    pnl_item, title = _article(item, counterparty)
    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        # Дата документа остаётся при факте всегда, даже когда период учёта
        # уехал в текущий месяц: именно она отвечает на вопрос «когда это было».
        "doc_date": doc_date.isoformat(),
        "unit_id": str(unit_id) if unit_id else None,
        "pnl_item_id": str(pnl_item.id),
        "expense_item_id": str(item.id) if item is not None else None,
        "counterparty_id": str(counterparty.id),
        "ledger": ledger,
        "amount": str(amount),
        "vat_rate": str(vat_rate) if vat_rate is not None else None,
        "title": title,
        "note": note or None,
        # Канала у счёта нет: движения денег не было. Оно будет у платежа, и это
        # ровно та разница, ради которой события разведены.
        "source": MANUAL_SOURCE,
        "document_id": document_id,
        "line_no": 1,
        "dedup_key": INVOICE_PREFIX + entry_key,
        "allocation": "direct" if unit_id else "pending",
    }
    fact_id, action = cash.write_fact(_filled(payload))
    return Recorded(
        document_id=document_id, fact_id=str(fact_id), action=action, landing=landing,
    )


def add_position(who, document, *, entry_key: str, item, unit_id, amount,
                 vat_rate=None, note: str = "") -> Recorded:
    """Дописать в счёт ещё одну позицию — со своей статьёй и своей точкой (T204).

    Накладная из Метро — это еда и канцелярия в одной бумаге, и это **разные
    строки P&L**. До этой задачи документ можно было отнести только к одной
    статье: либо одна на всё (и отчёт врёт), либо два счёта на одну бумагу (и
    оплата разъезжается с документом).

    Отдельной таблицы позиций нет намеренно: позиция и есть факт (`document_id`
    + `line_no`, разбор в `0230_facts`). Поэтому здесь нет ничего нового — та же
    запись факта, что в `record_invoice`, только номер строки следующий, а шапка
    документа уже есть и не трогается: сумма в шапке — это то, что написано на
    бумаге, а сумма позиций — то, как её разложили. Разойтись они могут, и
    именно это расхождение показывает карточка.

    Закрытый месяц не переписывается (D020): позиция ложится в открытый период
    вместе с исходной датой документа, как и всякая правка задним числом.
    """
    landing = cash.landing_for(who.tenant_id, document.period or document.doc_date)
    pnl_item, title = _article(item, document.counterparty)
    # Знак позиции — такой же, как у первой строки документа. Спрашивать его у
    # человека нечего: позиция это часть той же бумаги, и половина счёта с
    # обратным знаком означала бы возврат, а не позицию.
    amount = abs(Decimal(str(amount))) * _sign_of(document)
    existing = Fact.objects.filter(
        document_id=document.id, superseded_at__isnull=True,
    ).exclude(allocation="allocated").aggregate(last=models.Max("line_no"))["last"]

    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        "doc_date": document.doc_date.isoformat(),
        "unit_id": str(unit_id) if unit_id else None,
        "pnl_item_id": str(pnl_item.id),
        "expense_item_id": str(item.id) if item is not None else None,
        "counterparty_id": str(document.counterparty_id) if document.counterparty_id else None,
        "ledger": _ledger_of(document),
        "amount": str(amount),
        "vat_rate": str(vat_rate) if vat_rate is not None else None,
        "title": title,
        "note": note or None,
        "source": MANUAL_SOURCE,
        "document_id": str(document.id),
        "line_no": (existing or 0) + 1,
        # Ключ позиции — ключ счёта плюс номер строки: устойчив между
        # отправками, поэтому повторное нажатие не плодит позиций.
        "dedup_key": INVOICE_PREFIX + entry_key,
        "allocation": "direct" if unit_id else "pending",
    }
    fact_id, action = cash.write_fact(_filled(payload))
    return Recorded(
        document_id=str(document.id), fact_id=str(fact_id),
        action=action, landing=landing,
    )


def _sign_of(document) -> int:
    """Знак сумм этого документа: как у его первой строки."""
    first = invoice_fact(document)
    return -1 if first is not None and first.amount < 0 else 1


def _ledger_of(document) -> str:
    """Регистр позиции — тот же, что у первой строки документа.

    Спрашивать его отдельно значило бы позволить одной бумаге лечь в два
    регистра: половина в официальный, половина в дополнительный. Такое бывает,
    но это не позиция, а два разных документа, и заводятся они отдельно.
    """
    first = invoice_fact(document)
    return first.ledger if first is not None else "official"


def positions_balance(document) -> tuple[Decimal, Decimal, Decimal]:
    """Сумма по бумаге, сумма позиций и их разница.

    Сумма по бумаге — `total_amount` шапки: то, что напечатано в документе.
    Сумма позиций — сложение строк. Пока их две, они обязаны сходиться; когда
    расходятся, значит позицию потеряли или сумму в шапке набрали неверно, — и
    заметить это можно только вычитанием.
    """
    stated = document.total_amount
    got = invoice_amount(document)
    if stated is None:
        return got, got, Decimal("0")
    # Знак: расходы хранятся отрицательными, а в шапке человек пишет модуль
    # суммы. Сравниваем по модулю, иначе «сходится» никогда не наступит.
    stated = abs(stated)
    return stated, abs(got), stated - abs(got)


def _article(item, counterparty: Counterparty) -> tuple[PnlItem, str]:
    """Строка P&L и название позиции. Статьи нет — служебная «Не разобрано».

    Название позиции — снимок на момент записи, и язык у него исходный, а не
    язык страницы: иначе одна и та же статья попадала бы в данные то «Вода», то
    «Voda» в зависимости от того, кто вносил счёт, и в отчёте это выглядело бы
    двумя разными строками. Читателю название показывается на его языке — из
    самой статьи.

    У строки без статьи снимком становится название контрагента: «Не разобрано»
    в списке из двадцати строк не отличает одну от другой, а разбирать их
    человеку.
    """
    from django.conf import settings

    if item is None:
        return line(UNCLASSIFIED), counterparty.title
    return item.pnl_item, cash.item_title(item.titles, language=settings.LANGUAGE_CODE)


def _write_document(who, *, entry_key: str, counterparty: Counterparty,
                    doc_date: date, period: date, number: str, amount) -> str:
    """Шапка счёта через `upsert_document`: тот же ключ — тот же документ.

    Ключ идемпотентности документа — `(тенант, источник, внешний id)`, и внешним
    id служит ключ записи формы. Значит повторная отправка той же формы не
    заводит второго счёта, а правка обновляет тот же — ровно как строка.

    **Вид документа здесь пишется только при первой записи.** `upsert_document`
    (`0230`) в `on conflict do update` вид не перечисляет намеренно: он свойство
    самой бумаги, а не того, кто её разбирает, — и принесённый с точки чек
    (T174) остаётся чеком, хотя разбирают его формой счёта. Правило проверяется
    `tests/test_papers_screen.py`: добавьте `kind` в список обновляемых колонок,
    и проверка чека покраснеет.
    """
    payload = {
        "tenant_id": str(who.tenant_id),
        "counterparty_id": str(counterparty.id),
        "kind": INVOICE_KIND,
        "source": MANUAL_SOURCE,
        "external_id": entry_key,
        "doc_number": number or None,
        "doc_date": doc_date.isoformat(),
        "period": period.isoformat(),
        "total_amount": str(amount),
    }
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "select upsert_document(%s::jsonb)", [json.dumps(_filled(payload))]
            )
            written = cursor.fetchone()[0]
            # Ключи Django отложенные, то есть проверяются на коммите — за
            # пределами этой точки сохранения. Тот же довод, что в `write_fact`,
            # и точно так же режим возвращается обратно: он действует до конца
            # транзакции, то есть до конца запроса, а строку счёта продукт пишет
            # уже после документа.
            cursor.execute("set constraints all immediate")
            cursor.execute("set constraints all deferred")
            return str(written)
    except DatabaseError as refusal:
        state = getattr(getattr(refusal, "__cause__", None), "sqlstate", "") or ""
        if state in ("42501", "23503"):
            # Чужой контрагент и несуществующий отвечают одинаково: по ответу
            # нельзя понять, что строка существует у соседа (D023).
            raise SupplierRefused(_("Контрагент не найден.")) from refusal
        raise


def _filled(payload: dict) -> dict:
    """Пустые ключи не отправляем: `jsonb_populate_record` поставил бы null
    поверх значения по умолчанию — например, обнулил бы валюту партнёра."""
    return {name: value for name, value in payload.items() if value is not None}


def revise_invoice(who, document, fact, **entered) -> Recorded:
    """Правка счёта. Открытый месяц — заменой версии, закрытый — сторно и новая.

    Ровно тот же приём, что у расхода из кассы (`cash.revise_expense`), и по тем
    же двум доводам. Строку закрытого месяца не переписать физически:
    `facts_guard` отвергает и `update`, и `delete`, включая суперпользователя.
    Отказать вместо этого значило бы оставить бухгалтера с неверным числом
    навсегда, а тихо переписать закрытое — сделать июнь сегодня и июнь через
    полгода разными числами (D020).

    Пара «минус старое, плюс новое», а не одна дельта: у счёта могут поменяться
    статья, точка и регистр, и одна строка на разницу была бы верной только при
    правке суммы, а в остальных случаях ставила бы деньги не в ту статью молча.
    """
    key = entry_key_of(fact)
    if not cash.month_is_closed(who.tenant_id, fact.period):
        return record_invoice(who, entry_key=key, **entered)

    storno_line(who, fact)
    return record_invoice(
        who, entry_key=key + cash.FIX_SUFFIX, document_key=key, **entered,
    )


def entry_key_of(fact) -> str:
    """Ключ записи, из которого сделан `dedup_key` строки счёта."""
    key = fact.dedup_key
    return key[len(INVOICE_PREFIX):] if key.startswith(INVOICE_PREFIX) else key


def storno_line(who, fact) -> str:
    """Сторно строки счёта: та же запись с обратным знаком в текущем месяце.

    Копируется сама строка, а не собирается заново: сторно обязано отменять
    ровно то, что записано, — включая название позиции, снятое в день внесения.
    Ставка НДС копируется, а сумма налога нет: её база посчитает от
    отрицательной суммы сама, а копия положительной **добавила** бы налог
    вместо того, чтобы его отменить (тот же довод, что в `cash.storno_expense`).
    """
    # Месяц сторно считается от ПЕРИОДА УЧЁТА исходной строки, а не от даты
    # документа. У счёта они разные по замыслу: счёт за июнь приходит в июле, и
    # если считать по дате документа, сторно легло бы в июль, а исправленная
    # запись — в текущий месяц. Пара «минус старое, плюс новое» разъехалась бы
    # по разным месяцам, и в июле появился бы расход −24 000 из ниоткуда.
    landing = cash.landing_for(who.tenant_id, fact.period)
    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        # Дата документа остаётся исходной: деньги относятся к тому событию.
        "doc_date": (fact.doc_date or fact.period).isoformat(),
        "unit_id": str(fact.unit_id) if fact.unit_id else None,
        "pnl_item_id": str(fact.pnl_item_id),
        "expense_item_id": str(fact.expense_item_id) if fact.expense_item_id else None,
        "counterparty_id": str(fact.counterparty_id) if fact.counterparty_id else None,
        "ledger": fact.ledger,
        "amount": str(-fact.amount),
        "vat_rate": str(fact.vat_rate) if fact.vat_rate is not None else None,
        "title": fact.title,
        "note": fact.note or None,
        "source": MANUAL_SOURCE,
        "document_id": str(fact.document_id) if fact.document_id else None,
        "dedup_key": fact.dedup_key + cash.STORNO_SUFFIX,
        "allocation": "direct" if fact.unit_id else "pending",
    }
    fact_id, _action = cash.write_fact(_filled(payload))
    return str(fact_id)


# --- платёж -------------------------------------------------------------------


def pay(who, document, *, entry_key: str, on: date, amount, till_id, ledger: str,
        note: str = "") -> Recorded:
    """Отметить оплату счёта отдельным событием с собственной датой.

    Период платежа — месяц **денег**, а не месяц счёта: движение денег
    случилось тогда, когда оно случилось, и переносить его в период учёта счёта
    значило бы стереть разницу, ради которой события и разведены.

    Точка платежа берётся у самого счёта: платёж — это деньги за него, и
    выбирать ей второй ответ было бы нечем. Счёт на всю сеть даёт платёж без
    точки — такой же, как он сам.
    """
    row = invoice_fact(document)
    if row is None:
        raise SupplierRefused(_("У счёта нет ни одной строки: оплачивать нечего."))

    landing = cash.landing_for(who.tenant_id, on)
    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        "doc_date": on.isoformat(),
        "unit_id": str(row.unit_id) if row.unit_id else None,
        "pnl_item_id": str(line(SUPPLIER_PAYMENT).id),
        "counterparty_id": str(document.counterparty_id) if document.counterparty_id else None,
        "till_id": str(till_id) if till_id else None,
        "ledger": ledger,
        "amount": str(amount),
        "title": _payment_title(document),
        "note": note or None,
        # Канал денег: из кассы — наличные, иначе банк. Это не про P&L (платёж
        # в него не входит вовсе), а про сверку кассы — ради неё колонка и есть.
        "channel": "cash" if till_id else "bank",
        "source": MANUAL_SOURCE,
        "document_id": str(document.id),
        "dedup_key": PAYMENT_PREFIX + entry_key,
        "allocation": "direct" if row.unit_id else "pending",
    }
    fact_id, action = cash.write_fact(_filled(payload))
    return Recorded(
        document_id=str(document.id), fact_id=str(fact_id), action=action, landing=landing,
    )


def _payment_title(document) -> str:
    """Название строки платежа: по счёту такому-то. Номера нет — по контрагенту."""
    if document.doc_number:
        return _("Оплата счёта %(number)s") % {"number": document.doc_number}
    return _("Оплата поставщику")


def record_purchase(
    who,
    *,
    entry_key: str,
    on: date,
    counterparty: Counterparty,
    item,
    unit_id,
    ledger: str,
    amount,
    vat_rate,
    note: str = "",
) -> Recorded:
    """Оплата без счёта: расход признаётся в момент оплаты.

    Документа нет, значит и признавать расход раньше нечем — строка и есть
    событие целиком. Отсюда и `doc_date` = дата денег, и канал `bank`.

    **Наличной такая оплата не бывает.** Мелкая покупка за наличные вносится
    расходом из кассы (третья очередь): там регистр учёта приезжает из самой
    кассы (D039), а здесь его выбирают руками — и два способа завести одну и ту
    же трату разошлись бы в регистре молча.
    """
    landing = cash.landing_for(who.tenant_id, on)
    pnl_item, title = _article(item, counterparty)
    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        "doc_date": on.isoformat(),
        "unit_id": str(unit_id) if unit_id else None,
        "pnl_item_id": str(pnl_item.id),
        "expense_item_id": str(item.id) if item is not None else None,
        "counterparty_id": str(counterparty.id),
        "ledger": ledger,
        "amount": str(amount),
        "vat_rate": str(vat_rate) if vat_rate is not None else None,
        "title": title,
        "note": note or None,
        "channel": "bank",
        "source": MANUAL_SOURCE,
        "dedup_key": PURCHASE_PREFIX + entry_key,
        "allocation": "direct" if unit_id else "pending",
    }
    fact_id, action = cash.write_fact(_filled(payload))
    return Recorded(
        document_id="", fact_id=str(fact_id), action=action, landing=landing,
    )


# --- чтение -------------------------------------------------------------------


@dataclass(frozen=True)
class Listed:
    """Счёт в списке: шапка, действующая строка и его СЕГОДНЯШНЯЯ сумма.

    Сумма считается по всем действующим строкам документа, а не берётся у
    первой. Разница появляется после правки счёта закрытого месяца: там строк
    становится три — исходная в закрытом месяце, сторно и исправленная (D020), —
    и сумма счёта это их сложение. Показать вместо неё исходную значило бы
    показать число, которое человек уже исправил.
    """

    document: SourceDocument
    fact: Fact                 # действующая исходная строка: по ней правят и платят
    amount: Decimal            # сумма счёта сегодня
    corrections: int           # сколько строк-исправлений лежит рядом


def invoices(who, chosen: dict) -> list[Listed]:
    """Счета партнёра по отбору. Срез делает база, здесь только условия и порядок.

    Ни `filter(unit_id__in=...)`, ни проверок регистра здесь нет: строки счетов —
    обычные факты, и лишнее отсекают политики `facts` (D014). Документ при этом
    закрыт только партнёром, поэтому отбор идёт **от строк**: счёт, строку
    которого роль не видит, не должен появляться в списке даже названием.
    """
    rows = (
        Fact.objects.select_related("document", "document__counterparty", "unit",
                                    "expense_item", "pnl_item")
        .filter(
            dedup_key__startswith=INVOICE_PREFIX,
            superseded_at__isnull=True,
            document__isnull=False,
        )
        .exclude(allocation="allocated")
        .order_by("-doc_date", "created_at")
    )
    if chosen.get("from"):
        rows = rows.filter(doc_date__gte=chosen["from"])
    if chosen.get("to"):
        rows = rows.filter(doc_date__lte=chosen["to"])
    if chosen.get("counterparty"):
        rows = rows.filter(counterparty_id=chosen["counterparty"])
    cut = chosen.get("ledger") or ""
    if cut:
        from .directory_views import LEDGER_CODES
        if cut not in LEDGER_CODES:
            # Неизвестное слово отвечает тем же, чем невидимый регистр, —
            # пустотой (D023). Отказ сделал бы выдуманное слово отличимым от
            # скрытого, то есть превратил бы перебор в способ узнать состав
            # регистров партнёра.
            return []
        rows = rows.filter(ledger=cut)

    return _fold(rows)


def _fold(rows) -> list[Listed]:
    """Строки — в счета: одна строка списка на документ, сумма сложением.

    Свернуть обязательно: после правки закрытого месяца строк у счёта три, и
    тремя строками в списке они читались бы как три разных счёта с одним
    номером — а оплата у них при этом одна на всех.
    """
    order: list[str] = []
    grouped: dict[str, list[Fact]] = {}
    for row in rows:
        key = str(row.document_id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    listed = []
    for key in order:
        lines = grouped[key]
        primary = _primary(lines)
        if primary is None:
            # Исходную строку роль не видит, а исправление видит — при исправных
            # политиках так не бывает (у строк одна точка и один регистр). Если
            # случится, счёт пропускается целиком: показать исправление без того,
            # что оно исправляет, значит показать число без смысла.
            continue
        listed.append(Listed(
            document=primary.document,
            fact=primary,
            amount=sum((row.amount for row in lines), Decimal("0")),
            corrections=len(lines) - 1,
        ))
    return listed


def _primary(lines: list[Fact]) -> Fact | None:
    """Исходная строка счёта: та, у ключа которой нет приставки исправления."""
    for row in lines:
        if not _is_correction(row):
            return row
    return None


def _is_correction(fact) -> bool:
    return fact.dedup_key.endswith(cash.STORNO_SUFFIX) or fact.dedup_key.endswith(
        cash.FIX_SUFFIX
    )


def invoice_fact(document) -> Fact | None:
    """Действующая ИСХОДНАЯ строка счёта: по ней правят и по ней платят.

    Строки-исправления сюда не попадают намеренно: править надо то, что человек
    завёл, а сторно и исправление — следствие этой правки, у них своя жизнь и
    свой месяц.
    """
    lines = list(
        Fact.objects.select_related("expense_item", "unit", "pnl_item")
        .filter(document_id=document.id, dedup_key__startswith=INVOICE_PREFIX,
                superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .order_by("line_no", "created_at")
    )
    return _primary(lines)


def invoice_lines(document) -> list[Fact]:
    """Все действующие строки счёта: исходная и исправления рядом с ней."""
    return list(
        Fact.objects.select_related("unit")
        .filter(document_id=document.id, dedup_key__startswith=INVOICE_PREFIX,
                superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .order_by("period", "created_at")
    )


def invoice_amount(document) -> Decimal:
    """Сегодняшняя сумма счёта: сложение всех действующих строк."""
    return sum((row.amount for row in invoice_lines(document)), Decimal("0"))


def payments_of(document) -> list[Fact]:
    """Платежи по счёту — действующие, без детей разнесения."""
    return list(
        Fact.objects.select_related("till", "unit")
        .filter(document_id=document.id, dedup_key__startswith=PAYMENT_PREFIX,
                superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .order_by("doc_date", "created_at")
    )


def paid_by_document(document_ids) -> dict:
    """Сколько заплачено по каждому счёту. Одним запросом на список, не по строке.

    Выборка идёт под теми же политиками, что и сам список: платёж, которого роль
    не видит, не должен доставать её ни числом, ни остатком (D023).
    """
    if not document_ids:
        return {}
    found = (
        Fact.objects.filter(
            document_id__in=list(document_ids),
            dedup_key__startswith=PAYMENT_PREFIX,
            superseded_at__isnull=True,
        )
        .exclude(allocation="allocated")
        .values_list("document_id")
        .annotate(total=_sum("amount"))
    )
    return {str(document_id): total for document_id, total in found}


def _sum(field):
    from django.db.models import Sum

    return Sum(field)


def outstanding(amount, paid) -> Decimal:
    """Сколько осталось заплатить. Переплата даёт ноль, а не минус.

    Минус в колонке «Остаток» читается как долг поставщика нам, а это другое
    событие — и оно должно быть видно платежом, а не отрицательным остатком.
    """
    left = Decimal(amount or 0) - Decimal(paid or 0)
    return left if left > 0 else Decimal("0")


def document_or_none(document_id):
    """Счёт по номеру — под политиками базы, и только внесённый руками.

    Виды два: счёт и чек. Чек попал сюда с приходом бумаг с точек (T174) —
    разобранный чек живёт в списке счетов такой же строкой, и не открывать его
    карточку значило бы оставить в списке ссылку, ведущую в 404.

    Строка банковской выписки и расчёт зарплаты сюда не попадают: править их
    экраном счетов нечем, а 404 на них — тот же ответ, что на чужой (D023).
    """
    return SourceDocument.objects.filter(
        pk=document_id, kind__in=DOCUMENT_KINDS, source=MANUAL_SOURCE,
    ).select_related("counterparty").first()


# --- инбокс классификации (T152) ------------------------------------------------


def waiting_for_an_article(who):
    """Строки без статьи: те, что стоят в инбоксе.

    Отбор идёт по служебной строке P&L, а не по пустому `expense_item_id`: пусто
    у половины продукта — у зарплаты, у выручки коннектора, у переводов, — и
    отбор по нему втащил бы в инбокс всё, что статьи не имеет по своей природе.
    Служебная статья означает ровно одно: «человек ещё не сказал, что это».

    Срез делает база: строку чужой точки и невидимого регистра политики `facts`
    отсекут раньше (D014), а строку без точки увидит только тот, кто ведёт все
    точки партнёра (`app_network_row_is_visible`, `0236`).
    """
    return list(
        Fact.objects.select_related("counterparty", "unit", "document")
        .filter(pnl_item_id=line(UNCLASSIFIED).id, superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .order_by("-doc_date", "created_at")
    )


def classify(who, fact, *, item, unit_id) -> Recorded:
    """Назначить строке статью (и точку) — прямо из инбокса.

    Открытый месяц — заменой версии: тот же ключ уходит в `upsert_fact`, старая
    строка помечается заменённой, новая встаёт рядом, и сумма не удваивается.

    Закрытый месяц — сторно и новая строка в текущем (D020). Разбор
    неразобранного — это правка задним числом, и переписать ею закрытый месяц
    нельзя: июнь сегодня и июнь через полгода обязаны давать одно число.
    """
    remember(who, fact.counterparty_id, item)
    if not cash.month_is_closed(who.tenant_id, fact.period):
        return _reclassified(who, fact, item=item, unit_id=unit_id,
                             dedup_key=fact.dedup_key)
    storno_line(who, fact)
    return _reclassified(who, fact, item=item, unit_id=unit_id,
                         dedup_key=fact.dedup_key + cash.FIX_SUFFIX)


def _reclassified(who, fact, *, item, unit_id, dedup_key: str) -> Recorded:
    """Та же строка с назначенной статьёй. Всё остальное копируется как есть.

    Копируется, а не собирается заново: разбор меняет ровно две вещи — статью и
    точку, — и любое поле, забытое при пересборке, потерялось бы молча. Так уже
    терялась касса у детей разнесения (`0239`).
    """
    from django.conf import settings

    # Разбор НЕ двигает период учёта: человек назвал статью, а не переставил
    # месяц. Считается он от периода исходной строки — открыт, значит строка
    # остаётся в нём; закрыт, значит разбор ложится в текущий вместе со сторно
    # (D020). Взять сюда дату документа значило бы молча переносить июньский
    # счёт в июль просто потому, что бумага пришла в июле.
    landing = cash.landing_for(who.tenant_id, fact.period)
    payload = {
        "tenant_id": str(who.tenant_id),
        "period": landing.period.isoformat(),
        "doc_date": (fact.doc_date or fact.period).isoformat(),
        "unit_id": str(unit_id) if unit_id else None,
        "pnl_item_id": str(item.pnl_item_id),
        "expense_item_id": str(item.id),
        "counterparty_id": str(fact.counterparty_id) if fact.counterparty_id else None,
        "till_id": str(fact.till_id) if fact.till_id else None,
        "ledger": fact.ledger,
        "amount": str(fact.amount),
        "vat_rate": str(fact.vat_rate) if fact.vat_rate is not None else None,
        # Название позиции становится названием статьи: до разбора там стояло имя
        # контрагента (`_article`), и оставить его значило бы, что в отчёте
        # строка называется поставщиком, а не тем, за что заплатили.
        "title": cash.item_title(item.titles, language=settings.LANGUAGE_CODE),
        "note": fact.note or None,
        "channel": fact.channel,
        "source": fact.source,
        "document_id": str(fact.document_id) if fact.document_id else None,
        "line_no": fact.line_no,
        "dedup_key": dedup_key,
        "allocation": "direct" if unit_id else "pending",
    }
    fact_id, action = cash.write_fact(_filled(payload))
    return Recorded(
        document_id=str(fact.document_id or ""), fact_id=str(fact_id),
        action=action, landing=landing,
    )


def remember(who, counterparty_id, item) -> None:
    """Запомнить, какую статью человек дал этому поставщику (issue #173).

    Память нужна ради одной цифры: половина инбокса каждый месяц — те же
    поставщики, и без неё бухгалтер ищет ту же статью в списке сорок раз подряд.

    **Последнее решение побеждает.** Версий у памяти нет намеренно: она ничего
    не считает и в закрытый месяц не входит, поэтому переучить её должно быть
    так же дёшево, как разобрать одну строку. `hits` растёт — по нему видно,
    какая подсказка заслужена практикой, а какая поставлена одним разбором.

    Строка без контрагента памяти не оставляет: помнить «безымянному — статью»
    значило бы предлагать её всему, что пришло без имени.

    **Права спрашиваются заранее, а не ловятся отказом базы.** Политика
    `classify_insert` требует `suppliers.classify`; роль, которая строки
    разбирать может, а память вести — нет, получила бы отказ базы посреди
    запроса, и вместе с памятью развалился бы сам разбор: при `ATOMIC_REQUESTS`
    упавшая вставка ломает транзакцию целиком. Это не гипотеза — ровно такая
    роль стояла на стенде (у бухгалтера права не было). Поэтому память —
    удобство, которое **пропускается**, когда права на неё нет, а не ошибка.
    """
    if counterparty_id is None:
        return
    if not permissions.has(who, "suppliers.classify"):
        return
    rule = ClassificationRule.objects.filter(
        tenant_id=who.tenant_id, counterparty_id=counterparty_id,
    ).first()
    if rule is None:
        ClassificationRule.objects.create(
            tenant_id=who.tenant_id, counterparty_id=counterparty_id,
            expense_item=item, decided_by=who.user_id,
        )
        return
    same = rule.expense_item_id == item.id
    rule.expense_item = item
    # Переучили — счётчик начинается заново: он отвечает на вопрос «сколько раз
    # подтвердили ЭТО соответствие», а не «сколько раз тут вообще разбирали».
    rule.hits = rule.hits + 1 if same else 1
    rule.decided_at = timezone.now()
    rule.decided_by = who.user_id
    rule.save(update_fields=["expense_item", "hits", "decided_at", "decided_by"])


def suggestions(who) -> dict[str, ExpenseItem]:
    """Что предложить по каждому поставщику: id контрагента → статья.

    Одной выборкой на весь список, а не по строке: инбокс в сорок строк дал бы
    сорок запросов ровно за тем, чтобы нарисовать подсказку.
    """
    rules = (
        ClassificationRule.objects
        .filter(tenant_id=who.tenant_id)
        .select_related("expense_item")
    )
    return {str(rule.counterparty_id): rule.expense_item for rule in rules}


def unclassified_fact(fact_id) -> Fact | None:
    """Строка инбокса по номеру — под политиками базы, и только неразобранная.

    Разобранная строка сюда не попадает намеренно: разбирать её нечего, а 404 на
    неё — тот же ответ, что на чужую (D023).
    """
    return (
        Fact.objects.select_related("counterparty", "unit")
        .filter(pk=fact_id, pnl_item_id=line(UNCLASSIFIED).id, superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .first()
    )
