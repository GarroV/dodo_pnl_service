"""Расходы по HTTP: та же дверь, что у экрана, только другая ручка (T112).

Отдельной поверхностью API в первой версии нет (`docs/forge/spec.md`, секция
«API и будущая MCP-обёртка»): продукт живёт страницами. Эти маршруты — заготовка
для двух будущих поверхностей, Telegram и MCP, и написаны они здесь ровно
потому, что переписать их потом было бы дороже, чем соблюсти пять условий сразу.

**Модуль намеренно не содержит ни одного собственного правила о деньгах и
доступе.** Разбор формы — `cash_views.parse_expense`, запись — `cash.*`, выборка
и строки списка — `expenses_views`. Если бы здесь появилась своя копия хоть
одного из них, у продукта стало бы два ответа на вопрос «что сейчас видно и что
можно записать», и разошлись бы они молча: экран проверяется смоуком, а вызов —
нет. Отсюда и способ приёмки: каждый сценарий гоняется дважды, экраном и
запросом, и ответы сравниваются (`tests/test_expenses_api.py`).

Пять условий спеки и где они здесь:

1. **Роль и тенант приезжают контекстом базы.** Вызов идёт тем же
   `DbContextMiddleware`, что страницы: `set local role app_user` плюс
   `app.user_id` вошедшего. Ни тенанта, ни роли в параметрах вызова нет и быть
   не может — обёртка не имеет собственного доступа к данным и не может его
   расширить. Здесь это выражено отсутствием кода: срез никто не собирает.
2. **Срез по регистру — параметр запроса** (`?ledger=`), а не отдельный
   маршрут. Сужает и никогда не расширяет: видимость по-прежнему решает база.
3. **Записывающие вызовы только POST.** GET на них — 405 с `Allow`, а не тихий
   показ. Причина уезжает в саму строку тем же полем, что у формы
   (комментарий), автора проставляет база (`created_by = app_user_id()`).
4. **Отказ по невидимому неотличим от несуществующего** (D014, D023). Это не
   «поставили одинаковый текст»: чужая точка, чужая статья и чужой расход
   отсекаются политиками ещё до представления, поэтому им физически нечем
   отличаться от выдуманных.
5. **Долгих операций у расходов нет.** Список ограничен окном и сам говорит,
   что есть ещё; пересчёт разнесения принимает **один** месяц за вызов. Кнопка
   на экране обходит месяцы сама — она отвечает человеку, который смотрит на
   страницу; вызову держать соединение неограниченным обходом нельзя.

**Формат ответа — JSON, деньги строками.** Числом их отдавать нельзя: двоичная
дробь превращает 80756.32 в 80756.32000000001 на первом же потребителе, а
локализация — в «80 756,32». Строка одинаково читается и ботом, и человеком.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from django.http import Http404, JsonResponse, QueryDict
from django.utils.translation import gettext as _

from . import cash, expenses_views
from .cash_views import parse_expense
from .directory_views import BadInput
from .principal import get_current_principal

__all__ = [
    "allocate",
    "expense",
    "expense_delete",
    "expenses",
    "unallocated",
]

# Окно списка. Не про безопасность, а про то, что вызов не должен отдавать
# неограниченную выборку: у страницы есть человек, который перестанет ждать, а
# у бота — нет.
DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _json(payload: dict, status: int = 200) -> JsonResponse:
    # ensure_ascii=False: ответ читают глазами при разборе, и `ра` в
    # сообщении об отказе — это отказ, который никто не прочитает.
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def endpoint(*methods: str):
    """Обвязка вызова: метод, кто вошёл, тело JSON и отказы словами.

    Одной обвязкой на все маршруты, а не пятью копиями: порядок проверок здесь
    — часть контракта (метод раньше данных, вход раньше отбора), и разъехавшийся
    порядок означал бы, что один маршрут рассказывает о существовании строки
    тому, кого вообще не пустили бы.
    """
    def decorate(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            if request.method not in methods:
                # Запись по ссылке из истории браузера случиться не должна.
                return _refusal(
                    _("Этот вызов принимает только %(methods)s.")
                    % {"methods": ", ".join(methods)},
                    405, allow=methods,
                )
            who = get_current_principal(request)
            if who is None:
                return _refusal(_("Сначала войдите."), 401)
            if who.tenant_id is None:
                # Учётка без членства: политики без членства пусты, показывать
                # нечего и писать некуда.
                return _refusal(
                    _("Вас ещё не завели ни к одному партнёру."), 403
                )
            try:
                _absorb_json(request)
                return view(request, who, *args, **kwargs)
            except BadInput as bad:
                return _refusal(bad.message, bad.http_status)
            except cash.UnitRefused as refusal:
                return _refusal(refusal.message, refusal.http_status)
            except cash.CashRefused as refusal:
                return _refusal(refusal.message, refusal.http_status)
            except Http404:
                # Чужой расход и выдуманный номер отвечают одинаково (D023):
                # текст один, потому что причина отказа наружу не выносится.
                return _refusal(_("Расход не найден."), 404)

        return guarded

    return decorate


def _refusal(message: str, status: int, *, allow=()) -> JsonResponse:
    response = _json({"error": message}, status)
    if allow:
        response.headers["Allow"] = ", ".join(allow)
    return response


def _absorb_json(request) -> None:
    """Тело JSON кладётся туда, откуда его читает разбор формы.

    Так у продукта остаётся **один** разбор расхода на оба способа отправки:
    второй, «для API», разошёлся бы с первым на первой же правке правил — и
    разошёлся бы молча, потому что каждый по отдельности остался бы верным.
    """
    kind = (request.content_type or "").split(";")[0].strip()
    if kind != "application/json":
        return
    try:
        parsed = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise BadInput(_("Тело запроса не разобрано как JSON.")) from None
    if not isinstance(parsed, dict):
        raise BadInput(_("Тело запроса должно быть объектом JSON."))

    fields = QueryDict(mutable=True)
    for name, value in parsed.items():
        fields[name] = "" if value is None else str(value)
    request.POST = fields


# --- список -------------------------------------------------------------------


@endpoint("GET", "POST")
def expenses(request, who):
    """Список расходов (GET) и внесение расхода (POST)."""
    if request.method == "POST":
        return _record(request, who)
    return _listing(request, who)


def _listing(request, who) -> JsonResponse:
    """Тот же отбор и те же строки, что на экране, плюс срез и окно.

    Выборка одна: из неё же считается итог — как в списке (T110). Второй запрос
    `sum(amount)` был бы вторым источником истины и разошёлся бы с показанным
    молча.
    """
    # Отбор разбирается тем же `filters_from`, что у экрана, целиком — включая
    # `?ledger=`. Своя строка разбора здесь стояла до T133, и ровно из-за неё
    # один и тот же адрес отвечал экраном и вызовом по-разному: экран параметр
    # не читал вовсе. Второй разбор рядом с первым расходится молча.
    chosen = expenses_views.filters_from(request)
    limit, offset = _window(request)

    rows = expenses_views.rows_for(who, chosen, window=(offset, limit + 1))
    has_more = len(rows) > limit
    rows = rows[:limit]
    shown = [_row(row) for row in rows]
    total = sum(
        (row["amount"] for row in rows if row["state"] == expenses_views.ACTIVE),
        Decimal("0"),
    )
    return _json({
        "rows": shown,
        "count": len(shown),
        "total": f"{total}",
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    })


def _window(request) -> tuple[int, int]:
    """Сколько строк и с какой. Испорченное значение — отказ, а не умолчание.

    Тихое умолчание означало бы, что вызывающий листает не то, что думает, и
    узнает об этом по недостающим деньгам.
    """
    limit = _positive(request, "limit", DEFAULT_LIMIT)
    offset = _positive(request, "offset", 0)
    if limit > MAX_LIMIT:
        raise BadInput(
            _("«limit» больше %(max)s не бывает: спросите страницами.")
            % {"max": MAX_LIMIT}
        )
    if limit == 0:
        raise BadInput(_("«limit» должен быть больше нуля."))
    return limit, offset


def _positive(request, name: str, default: int) -> int:
    raw = (request.GET.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise BadInput(
            _("«%(name)s»: это целое число, а не «%(value)s».")
            % {"name": name, "value": raw}
        )
    return int(raw)


def _row(row: dict) -> dict:
    """Строка списка для вызывающего: та же, что уходит в разметку.

    Сумма отдаётся строкой и ровно та же, по которой экран считает итог
    (`data-amount`), — то есть сверить ответ с экраном можно посимвольно.
    `amount_text` остаётся рядом: он отформатирован на языке запроса и нужен
    тому, кто показывает ответ человеку.
    """
    out = {name: value for name, value in row.items() if name not in ("amount", "amount_raw")}
    out["amount"] = row["amount_raw"]
    return out


# --- запись -------------------------------------------------------------------


def _record(request, who) -> JsonResponse:
    """Внести расход. Разбор и запись — те же, что у формы (T109)."""
    entered = parse_expense(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))
    recorded = cash.record_expense(who, entry_key=entry_key, **entered)

    answer = _recorded(recorded)
    if entered["unit_id"] is None:
        # Расход на всю сеть разносится сразу, как и с экрана: узнать через
        # месяц, что сумма висела нераспределённой, — худший из ответов.
        outcome = cash.spread_now(recorded.fact_id)
        # `reason` — разбираемый машиной код причины ожидания (T132), а не
        # переведённая фраза: бот обязан отличать «правила нет» от «правило есть,
        # но выручки в продукте пока нет», а по словам этого не сделать.
        answer["allocation"] = {
            "state": outcome.state, "rows": outcome.rows, "reason": outcome.reason,
        }
    return _json(answer)


def _recorded(recorded) -> dict:
    """Что случилось с деньгами: куда легли и завели ли новую версию."""
    return {
        "fact_id": recorded.fact_id,
        "action": recorded.action,
        "period": f"{recorded.landing.period:%Y-%m}",
        "moved_from": (
            f"{recorded.landing.moved_from:%Y-%m}"
            if recorded.landing.moved_from else None
        ),
    }


@endpoint("GET", "POST")
def expense(request, who, fact_id):
    """Карточка расхода (GET) и правка (POST)."""
    fact = expenses_views.expense_or_404(fact_id)
    if request.method == "GET":
        return _json({
            "expense": _row(expenses_views.row_of(fact)),
            "editable": expenses_views.editable(fact),
            "notice": expenses_views.closed_notice(who, fact),
        })

    if not expenses_views.editable(fact):
        raise BadInput(_("Эта строка уже заменена: правьте ту, что действует."))
    entered = parse_expense(request, who)
    return _json(_recorded(cash.revise_expense(who, fact, **entered)))


@endpoint("POST")
def expense_delete(request, who, fact_id):
    """Удаление расхода. Только POST: это запись, а не просмотр.

    Открытый месяц — пометка (строка остаётся видимой как удалённая), закрытый
    — сторно в текущем: тронуть строку закрытого месяца нельзя физически.

    **Состояние называется своим словом, включая «удалять было нечего» (T154).**
    Раньше повторное удаление расхода, который уже правили в закрытом месяце,
    отвечало тем же `storno`, что и первое, — при том что не менялось ничего, а
    исправленная строка оставалась в P&L. Бот по такому ответу не отличил бы
    сделанное от несделанного.
    """
    fact = expenses_views.expense_or_404(fact_id)
    if not expenses_views.editable(fact):
        # Заменённую строку удалять нечего: она уже вышла из счёта.
        return _json({"fact_id": str(fact.id), "state": "replaced", "storno": None})

    removal = cash.remove_expense(who, fact)
    return _json({
        "fact_id": str(fact.id),
        "state": {"marked": "removed", "stornoed": "storno", "already": "already"}[
            removal.state
        ],
        "storno": _landed(removal.landing) if removal.landing is not None else None,
        # Снята ли исправленная строка прежней правки: по ней и оставались деньги
        # в P&L, поэтому ответ о ней разбираемый машиной, а не только словами.
        "correction_withdrawn": removal.withdrew_correction,
    })


def _landed(landing) -> dict:
    """Куда легло сторно: месяц учёта и месяц, из которого его перенесли."""
    return {
        "period": f"{landing.period:%Y-%m}",
        "moved_from": f"{landing.moved_from:%Y-%m}" if landing.moved_from else None,
    }


# --- нераспределённое и разнесение --------------------------------------------


@endpoint("GET")
def unallocated(request, who):
    """Суммы без точки: что мешает закрыть месяц. Та же выборка, что на экране."""
    rows = expenses_views.waiting_rows(who)
    total = sum((row["amount"] for row in rows), Decimal("0"))
    return _json({
        "rows": [_row(row) for row in rows],
        "count": len(rows),
        "total": f"{total}",
    })


@endpoint("POST")
def allocate(request, who):
    """Пересчитать разнесение за **один** месяц.

    Месяц обязателен намеренно (условие 5 спеки). Экранная кнопка обходит все
    ждущие месяцы сама, и это её право: она отвечает человеку, который смотрит
    на страницу и видит, что работа идёт. Вызов, обходящий неограниченный список
    месяцев, держал бы соединение неизвестно сколько — а на такую операцию спека
    требует идентификатор работы, которого у расходов нет и заводить его не за
    чем: пересчёт месяца — один оператор в базе.

    Закрытые месяцы пересчёт пропускает и **называет** их: молчаливый пропуск
    читается как «пересчитано» (D020).
    """
    period = _month(request)
    spread = cash.reallocate(who.tenant_id, [period])
    return _json({
        "period": f"{period:%Y-%m}",
        "changed": spread.changed,
        "skipped": [f"{month:%Y-%m}" for month in spread.skipped],
        "refused": [f"{month:%Y-%m}" for month in spread.refused],
    })


def _month(request) -> date:
    raw = (request.POST.get("period") or "").strip()
    if not raw:
        raise BadInput(
            _("Укажите месяц пересчёта: «period» в виде 2026-08, по одному за вызов.")
        )
    try:
        return datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise BadInput(
            _("«period»: месяц пишется как 2026-08, а не «%(value)s».") % {"value": raw}
        ) from None
