"""Экран внесения расхода из кассы (T109).

Форма одна и короткая: дата, сумма, статья, комментарий. Всё, что делает её
непростой, живёт не здесь, а в `web/cash.py` — там правила и объяснения, почему
они такие. Здесь только разбор ввода и показ.

**Точка не фильтруется представлением, и это главное решение экрана.** У
управляющего она подставляется своя, но пришедшая в запросе **не отбрасывается
и не проверяется списком** — она уходит в базу, и отвергают её политики (D014).
Список вариантов в форме — удобство для того, у кого точек много, а не защита:
защита, написанная в двух местах, однажды разойдётся, и разойдётся молча.

**Отказ по точке одинаков для всех причин.** Чужая, несуществующая, чужого
партнёра — один текст и один код. По ответу нельзя понять, что точка вообще
есть (D023).

**Регистр учёта у расхода есть, умолчание — официальный.** Вопрос Q013 владельцу
отправлен, ответа нет; поддерживаются оба варианта: механизм на месте, но
человеку, который про регистры не думает, ничего делать не нужно. Список —
только видимые роли: предложить завести расход в регистр, которого человек не
видит, значило бы дать ему записать данные, которые он потом не найдёт.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import Unit

from . import cash
from .directory_views import LEDGER_CODES, BadInput, _choice, _date, _number, _select, _text
from .format import ledger_title
from .i18n import month_title
from .principal import get_current_principal


@login_required
def cash_expense(request):
    """Форма внесения расхода. GET — пустая, POST — запись и возврат сюда же."""
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        # Учётка без членства: данных она не видит вовсе (политики без членства
        # пусты), и внести расход ей некуда. Говорим об этом словами, а не
        # показываем форму, которая ответит непонятной ошибкой базы.
        return render(request, "web/directory/denied.html", {
            "message": _(
                "Вас ещё не завели ни к одному партнёру, поэтому вносить расход некуда. "
                "Попросите администратора сети добавить вас."
            ),
        }, status=403)

    error, status = "", 200
    entered = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        try:
            return redirect(_save(request, who))
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/cash/expense.html", _context(
        request, who, entered, error=error,
    ), status=status)


def _save(request, who) -> str:
    """Записать расход и вернуть адрес, на который уводим после записи.

    Возврат адресом, а не страницей: обновление после записи не должно вносить
    расход второй раз. Что именно случилось, уезжает в адрес месяцами, а не
    готовой фразой, — фразу в адресе не перевести и подставить в неё можно что
    угодно.
    """
    entered = parse_expense(request, who)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))

    recorded = cash.record_expense(who, entry_key=entry_key, **entered)
    landed = reverse("expense-new") + f"?saved={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&from={recorded.landing.moved_from:%Y-%m}"
    if entered["unit_id"] is None:
        # Расход на всю сеть разносится сразу, а не ждёт кнопки: человек,
        # внёсший аренду офиса, должен увидеть ответ «разошлось на три точки»
        # в тот же миг, а не узнать через месяц, что сумма висела нераспределённой.
        state, spread = cash.spread_now(recorded.fact_id)
        landed += f"&{state}={spread}"
    return landed


def parse_expense(request, who) -> dict:
    """Разобрать форму расхода — одинаково для внесения и для правки.

    Одной функцией, а не двумя похожими: правила «статья обязана действовать на
    дату», «ноль не вносится» и «точку отвергает база» одни и те же, а две копии
    разъехались бы на первой правке — и разъехались бы молча, потому что каждая
    по отдельности осталась бы верной.
    """
    on = _date(request, "date", _("Дата расхода"), required=True)
    amount = _number(request, "amount", _("Сумма"))
    if amount == 0:
        raise BadInput(_("«%(label)s»: расход на ноль не вносится.") % {"label": _("Сумма")})

    return {
        "on": on,
        "amount": amount,
        "item": _item(request, who, on),
        "unit_id": _unit(request, who),
        "ledger": _ledger(request, who),
        "note": _text(request, "note", _("Комментарий"), required=False),
    }


def _item(request, who, on: date):
    """Статья расхода. Без неё расход не сохраняется — ему негде быть в P&L.

    Проверяется и то, что статья действует на дату расхода: статья, заведённая
    в августе, не объясняет июньскую трату, и молча принять её значило бы
    поставить в отчёт правило, которого в тот месяц не было.
    """
    raw = (request.POST.get("item") or "").strip()
    if not raw:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": _("Статья расхода")})

    from core.models import ExpenseItem

    item = ExpenseItem.objects.filter(pk=_uuid_or_refuse(raw, _("Статья расхода"))).first()
    if item is None:
        raise BadInput(_("Статья не найдена."))
    if item.valid_from > on or (item.valid_to is not None and item.valid_to <= on):
        raise BadInput(
            _(
                "Статья «%(title)s» на %(date)s не действует: выберите другую "
                "или поправьте её в справочнике."
            )
            % {"title": cash.item_title(item.titles), "date": on.isoformat()}
        )
    return item


def _uuid_or_refuse(raw: str, label: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise BadInput(_("«%(label)s»: такого варианта нет.") % {"label": label}) from None


def _unit(request, who):
    """Точка расхода — как её прислал запрос, без проверки списком.

    Пусто и человек ограничен одной точкой — подставляем её: выбирать ему не из
    чего, и требовать выбор было бы лишним шагом. Всё остальное уходит в базу
    как есть: чужую точку отвергнет политика, а не эта функция (D014).
    """
    raw = (request.POST.get("unit") or "").strip()
    if raw == cash.NETWORK_UNIT:
        # Расход юрлица целиком: аренда офиса, реклама на сеть. Точки у него нет
        # не по недосмотру — её выбирает правило разнесения (T111).
        return None
    if not raw and len(who.unit_ids) == 1:
        return who.unit_ids[0]
    if not raw:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": _("Точка")})
    try:
        return UUID(raw)
    except ValueError:
        # Тот же отказ, что у чужой точки: по ответу нельзя понять, существует
        # ли она (D023). Поэтому здесь не «неверный формат», а «не найдена».
        raise cash.UnitRefused() from None


def _ledger(request, who) -> str:
    """Регистр учёта. Умолчание — официальный (Q013), выбор — из видимых роли.

    Невидимый регистр отвергается здесь, а не базой, и это не обход правила про
    точку: список регистров — не справочник, а три значения, и отказ по ним
    выглядит для человека одинаково («такого варианта нет») независимо от того,
    видит он регистр или его не бывает. Что база тоже не пропустит такую
    запись, проверено отдельно (`tests/test_facts_access.py`).
    """
    raw = (request.POST.get("ledger") or "").strip()
    if not raw:
        return "official"
    return _choice(
        request, "ledger", _("Регистр учёта"),
        [code for code in LEDGER_CODES if code in who.visible_ledgers],
    )


# --- показ --------------------------------------------------------------------


def _context(request, who, entered, *, error: str) -> dict:
    return {
        "error": error,
        "notice": _notice(request),
        "entry_key": cash.new_entry_key(),
        "fields": expense_fields(who, entered),
    }


def expense_fields(who, entered) -> list[dict]:
    """Поля формы расхода — одни и те же на внесении и на правке (T110).

    Список статей берётся на дату расхода: статья, заведённая в августе, не
    объясняет июньскую трату, и предлагать её было бы обещанием, которое
    отвергнет разбор ввода.
    """
    today = date.today()
    on = _entered_date(entered) or today
    return [
        {"kind": "date", "name": "date", "label": _("Дата расхода"), "required": True,
         "value": entered.get("date") or today.isoformat(),
         "help": _("Когда деньги вышли из кассы. Месяц уже закрыт — расход "
                   "ляжет в текущий, а эта дата останется при нём.")},
        {"kind": "number", "name": "amount", "label": _("Сумма"), "required": True,
         "value": entered.get("amount", "")},
        _select(
            "item", _("Статья расхода"),
            [
                (item.id, cash.item_title(item.titles))
                for item in cash.items_on(who.tenant_id, on)
            ],
            entered.get("item"), required=True,
            help=_("Статьи ведёт администратор сети. Нужной нет — попросите завести: "
                   "выдумывать название на месте нельзя, иначе одна трата "
                   "назовётся по-разному."),
        ),
        *_unit_field(who, entered),
        _select(
            "ledger", _("Регистр учёта"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            entered.get("ledger") or "official", required=True,
        ),
        {"kind": "text", "name": "note", "label": _("Комментарий"),
         "value": entered.get("note", ""),
         "help": _("Зачем потратили. Через месяц это единственное, по чему "
                   "строку узнают.")},
    ]


def _entered_date(entered) -> date | None:
    from datetime import datetime

    try:
        return datetime.strptime(entered.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def _unit_field(who, entered) -> list[dict]:
    """Точка: свои точки плюс «вся сеть» — тому, кто ведёт все точки.

    Список — удобство, а не защита: точку всё равно отвергает база (D014).
    Раньше вариант «вся сеть» предлагался всем, и это оказалось не безобидным:
    управляющий одной точки вносил им расход юрлица на 500 000, который потом
    любой директор разносил на три чужие точки (T130). Теперь запись без точки
    отвергает политика `unit_visibility` на `facts`, а список её решение
    повторяет — чтобы форма не предлагала того, что база не примет.
    """
    units = Unit.objects.order_by("code")
    if who.unit_ids:
        units = units.filter(pk__in=who.unit_ids)
    rows = list(units.values_list("id", "code"))
    # Подсказка живёт вместе со своим вариантом: объяснять «всю сеть» тому, у
    # кого её в списке нет, значит обещать несуществующее поле.
    hint = ""
    if not who.unit_ids:
        rows.append((cash.NETWORK_UNIT, _("Вся сеть")))
        hint = _("«Вся сеть» — расход юрлица целиком (аренда офиса, реклама): "
                 "он разойдётся по точкам правилом статьи.")
    # Тому, у кого точка одна, она же и подставляется: выбирать ему почти не из
    # чего, а требовать выбор было бы лишним шагом.
    chosen = entered.get("unit") or (who.unit_ids[0] if len(who.unit_ids) == 1 else "")
    return [_select("unit", _("Точка"), rows, chosen, required=True, help=hint)]


def _notice(request) -> str:
    """Что случилось с расходом — словами и с месяцем, в который он лёг.

    Месяц называется всегда, а не только при переносе: человек, вводивший
    июньскую дату в августе, обязан узнать, где искать свою строку, — и узнать
    это сразу, а не при первом расхождении в отчёте.
    """
    saved = _month_or_none(request.GET.get("saved"))
    if saved is None:
        return ""
    moved = _month_or_none(request.GET.get("from"))
    if moved is None:
        landed = _("Расход записан в месяц %(month)s.") % {"month": month_title(saved)}
        return landed + _spread(request)
    return _(
        "Месяц %(closed)s закрыт, поэтому расход записан в %(month)s — "
        "с исходной датой и не сдвинув закрытый месяц."
    ) % {"closed": month_title(moved), "month": month_title(saved)} + _spread(request)


def _spread(request) -> str:
    """Что стало с расходом на всю сеть: разошёлся, ждёт правила или не нам разносить.

    Три ответа, и ни один не молчит. «Ждёт» — самый важный: сумма без точки не
    попадает в разрез по точкам, и человек обязан узнать об этом сразу, а не при
    сборке P&L, когда сходиться уже поздно.
    """
    from django.urls import reverse

    waiting = _(
        " Точки у расхода нет, поэтому он ждёт разнесения — и виден в списке "
        "нераспределённых: %(url)s"
    ) % {"url": reverse("expenses-unallocated")}

    if request.GET.get("split"):
        return _(
            " Расход разнесён по точкам правилом статьи: строк — %(count)s."
        ) % {"count": request.GET["split"]}
    if "refused" in request.GET:
        return _(
            " Разнести его по точкам может тот, кто ведёт все точки: строки "
            "разнесения ложатся и на чужие точки."
        ) + waiting
    if "waiting" in request.GET:
        return _(" Правила разнесения у статьи нет.") + waiting
    return ""


def _month_or_none(raw: str | None) -> date | None:
    from datetime import datetime

    try:
        return datetime.strptime(raw or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        return None
