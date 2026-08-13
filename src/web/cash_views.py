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
    on = _date(request, "date", _("Дата расхода"), required=True)
    amount = _number(request, "amount", _("Сумма"))
    if amount == 0:
        raise BadInput(_("«%(label)s»: расход на ноль не вносится.") % {"label": _("Сумма")})

    item = _item(request, who, on)
    ledger = _ledger(request, who)
    note = _text(request, "note", _("Комментарий"), required=False)
    entry_key = cash.parse_entry_key(request.POST.get("entry_key", ""))

    recorded = cash.record_expense(
        who, on=on, amount=amount, item=item, unit_id=_unit(request, who),
        ledger=ledger, note=note, entry_key=entry_key,
    )
    landed = reverse("expense-new") + f"?saved={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&from={recorded.landing.moved_from:%Y-%m}"
    return landed


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
    today = date.today()
    on = _entered_date(entered) or today
    return {
        "error": error,
        "notice": _notice(request),
        "entry_key": cash.new_entry_key(),
        "fields": [
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
        ],
    }


def _entered_date(entered) -> date | None:
    from datetime import datetime

    try:
        return datetime.strptime(entered.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def _unit_field(who, entered) -> list[dict]:
    """Точка: выбор — тому, у кого их несколько; надпись — тому, у кого одна.

    Надпись, а не поле с одним вариантом: поле приглашает выбрать, а выбирать
    нечего. И не скрытое поле — скрытое выглядело бы как защита, которой оно не
    является: точку всё равно отвергает база.
    """
    if len(who.unit_ids) == 1:
        code = Unit.objects.filter(pk=who.unit_ids[0]).values_list("code", flat=True).first()
        return [{
            "kind": "note", "name": "unit", "label": _("Точка"), "value": code or "",
            "help": _("Ваша точка. Расход пойдёт на неё."),
        }]
    units = Unit.objects.order_by("code")
    if who.unit_ids:
        units = units.filter(pk__in=who.unit_ids)
    return [_select(
        "unit", _("Точка"), units.values_list("id", "code"),
        entered.get("unit"), required=True,
    )]


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
        return _("Расход записан в месяц %(month)s.") % {"month": month_title(saved)}
    return _(
        "Месяц %(closed)s закрыт, поэтому расход записан в %(month)s — "
        "с исходной датой и не сдвинув закрытый месяц."
    ) % {"closed": month_title(moved), "month": month_title(saved)}


def _month_or_none(raw: str | None) -> date | None:
    from datetime import datetime

    try:
        return datetime.strptime(raw or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        return None
