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

from core.models import CASH, Unit

from . import cash, receipts
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
        except receipts.ReceiptRefused as refusal:
            # Чек не принят — расход при этом уже записан: деньги ушли, и
            # отменять их запись из-за неудачного снимка нельзя. Человеку
            # говорится про чек, а расход он найдёт в списке.
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
    # Чек прикладывается тем же движением, что и запись (T184). Управляющий
    # снимает бумажку, стоя у печи, и второй заход «а теперь откройте расход и
    # приложите фотографию» не случится никогда. Сам чек при этом
    # необязателен: деньги уже потрачены, и отказать в записи расхода из-за
    # отсутствия снимка значило бы потерять сам расход.
    #
    # Порядок именно такой: сначала расход, потом чек. Политика чека зовёт факт
    # — до его записи чек класть некуда.
    sent = request.FILES.get("receipt")
    if sent is not None:
        receipts.attach(who, entry_key, data=sent.read(), file_name=sent.name)
    landed = reverse("expense-new") + f"?saved={recorded.landing.period:%Y-%m}"
    if recorded.landing.moved_from is not None:
        landed += f"&from={recorded.landing.moved_from:%Y-%m}"
    if entered["unit_id"] is None:
        # Расход на всю сеть разносится сразу, а не ждёт кнопки: человек,
        # внёсший аренду офиса, должен увидеть ответ «разошлось на три точки»
        # в тот же миг, а не узнать через месяц, что сумма висела нераспределённой.
        outcome = cash.spread_now(recorded.fact_id)
        # У ожидания в адрес уезжает КОД причины, а не число строк: причин
        # четыре, и они требуют разных слов (T132). Готовую фразу в адрес
        # положить нельзя — её не перевести и подставить в неё можно что угодно.
        landed += f"&{outcome.state}=" + (
            outcome.reason if outcome.state == "waiting" else str(outcome.rows)
        )
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

    till_id, till = _till(request)
    return {
        "on": on,
        "amount": amount,
        "item": _item(request, who, on),
        "unit_id": _unit(request, who, till),
        "till_id": till_id,
        "ledger": _ledger(request, who, till),
        "vat_rate": _vat_rate(request),
        "note": _text(request, "note", _("Комментарий"), required=False),
    }


def _vat_rate(request):
    """Ставка НДС из формы (T146, D042). Пусто — налога нет вовсе, а не ноль.

    Разница существенная: ноль означал бы «налог есть и он нулевой», и в отчёте
    появилась бы строка с налогом 0,00 там, где налога не было в природе. Всё,
    что вносилось до этой задачи, и всё, где НДС не при чём, обязано остаться
    пустым.

    Верхний предел проверяется здесь **и** в базе (`facts_vat_rate_range`), и
    это не дублирование правила: база защищает данные от дефекта в любом пути
    записи, а форма объясняет человеку, что он опечатался. Сумму налога здесь
    никто не считает — её считает база, одним местом на продукт.
    """
    rate = _number(request, "vat_rate", _("Ставка НДС"), required=False)
    if rate is not None and rate > 100:
        raise BadInput(
            _("«%(label)s»: ставки больше 100%% не бывает.") % {"label": _("Ставка НДС")}
        )
    return rate


def _till(request):
    """Касса из формы: её номер и сама строка, если она роли видна (T145).

    Номер возвращается **всегда**, а строка — только когда касса видна. Разница
    существенная: номер уходит в базу как есть и отвергается там политикой
    `till_visibility` на `facts` (D014), а строка нужна лишь затем, чтобы взять
    из неё точку и регистр. То есть невидимая касса не превращается здесь в
    отказ — она превращается в отказ базы, и по нему нельзя понять, существует
    ли касса (D023).

    Единственное, что отвергается на месте, — значение, которое номером не
    является вовсе: такой кассы не может быть ни у кого, и говорить о ней
    отдельными словами нечего. Тот же приём, что у точки.
    """
    from core.models import Till

    raw = (request.POST.get("till") or "").strip()
    if not raw:
        return None, None
    try:
        till_id = UUID(raw)
    except ValueError:
        raise cash.TillRefused() from None
    return till_id, Till.objects.filter(pk=till_id).first()


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


def _unit(request, who, till=None):
    """Точка расхода — как её прислал запрос, без проверки списком.

    Пусто и человек ограничен одной точкой — подставляем её: выбирать ему не из
    чего, и требовать выбор было бы лишним шагом. Всё остальное уходит в базу
    как есть: чужую точку отвергнет политика, а не эта функция (D014).

    **Касса задаёт точку** (T145). Касса стоит на точке, поэтому расход из неё —
    расход этой точки, и второго ответа на этот вопрос быть не может. Если в
    форме пришла другая точка, продукт отказывает словами, а не выбирает за
    человека одно из двух: молчаливый выбор означал бы, что расход лёг не туда,
    куда он думал. Это не проверка доступа — оба поля человек видит на экране,
    и сравниваются они между собой, а не с правами.
    """
    raw = (request.POST.get("unit") or "").strip()
    if till is not None:
        if raw == cash.NETWORK_UNIT:
            raise BadInput(_(
                "Расход на всю сеть из кассы не оплачивается: у кассы есть точка. "
                "Уберите кассу или выберите её точку."
            ))
        if raw and raw != str(till.unit_id):
            raise BadInput(
                _("Касса «%(till)s» стоит на точке %(unit)s — выберите её или уберите кассу.")
                % {"till": till.title, "unit": till.unit.code}
            )
        return till.unit_id
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


def _ledger(request, who, till=None) -> str:
    """Регистр учёта: из кассы умолчанием, руками — по желанию (T145, D039).

    Пусто в форме означает «из кассы»: официальная касса — официальный регистр,
    внутренняя — внутренний. Кассы нет — остаётся прежнее умолчание,
    официальный. Явно выбранное значение побеждает всегда: ручная правка
    регистра остаётся возможной, она лишь перестаёт быть главным способом.

    Невидимый регистр отвергается здесь, а не базой, и это не обход правила про
    точку: список регистров — не справочник, а три значения, и отказ по ним
    выглядит для человека одинаково («такого варианта нет») независимо от того,
    видит он регистр или его не бывает. Что база тоже не пропустит такую
    запись, проверено отдельно (`tests/test_facts_access.py`).
    """
    raw = (request.POST.get("ledger") or "").strip()
    if not raw:
        return till.ledger if till is not None else "official"
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
        # Чек снимается тем же движением, что вносится расход (T184).
        "receipt_accept": receipts.ACCEPT,
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
         "help": _("Когда деньги вышли из кассы.") + _closed_hint(who, on)},
        {"kind": "number", "name": "amount", "label": _("Сумма"), "required": True,
         "value": entered.get("amount", ""),
         "help": _("Сумма из чека — с налогом, если он в ней есть.")},
        # Ставка, а не сумма налога (T146, D042): в чеке напечатана ставка, а
        # сумму из неё считает база. Спрашивать обе означало бы просить человека
        # проверить работу продукта за него — и однажды получить пару, которая
        # не сходится.
        {"kind": "number", "name": "vat_rate", "label": _("Ставка НДС, %"),
         "value": entered.get("vat_rate", ""),
         "help": _("В Сербии 20 или 10. Пусто — налог не выделяем: в P&L такая "
                   "трата пойдёт полной суммой. С заполненной ставкой в P&L по "
                   "умолчанию поедет сумма без НДС.")},
        _select(
            "item", _("Статья расхода"),
            [
                (item.id, cash.item_title(item.titles))
                # Только статьи с пометкой «расходы наличными» (T191): сорок
                # строк управляющий на телефоне не читает, а выбирает первую
                # похожую. Уже выбранная статья остаётся в списке всегда — иначе
                # правка комментария у старого расхода молча переназначила бы
                # ему статью, а с ней и строку P&L.
                for item in cash.items_on(
                    who.tenant_id, on, surface=CASH, keep=entered.get("item") or None,
                )
            ],
            entered.get("item"), required=True,
            help=_("Статьи ведёт администратор сети. Нужной нет — попросите завести: "
                   "выдумывать название на месте нельзя, иначе одна трата "
                   "назовётся по-разному."),
        ),
        *_till_field(entered),
        *_unit_field(who, entered),
        _select(
            "ledger", _("Регистр учёта"),
            [(code, ledger_title(code)) for code in LEDGER_CODES
             if code in who.visible_ledgers],
            entered.get("ledger") or "", required=False,
            empty_label=_("Из кассы"),
            help=_("Пусто — регистр берётся из выбранной кассы; без кассы это "
                   "официальный. Выбранное здесь значение сильнее кассы."),
        ),
        {"kind": "text", "name": "note", "label": _("Комментарий"),
         "value": entered.get("note", ""),
         "help": _("Зачем потратили. Через месяц это единственное, по чему "
                   "строку узнают.")},
    ]


def _till_field(entered) -> list[dict]:
    """Касса, из которой платили (T145). Список — только видимые роли кассы.

    Срез делает база: политики `unit_visibility` и `ledger_visibility` на
    `tills` (D014). Закрытые кассы в список не идут — предлагать к новой трате
    коробку, которой больше нет, незачем, — но та, что уже выбрана у правимой
    записи, остаётся: иначе правка старого расхода молча снимала бы с него кассу.

    Поля нет вовсе, когда касс не заведено ни одной: пустой выбор из ничего
    только мешает, а расход без кассы — законное состояние (так внесены все
    расходы до этой задачи).
    """
    from .tills_views import visible_tills

    chosen = str(entered.get("till") or "")
    rows = [
        (till.id, f"{till.code} · {till.unit.code} · {ledger_title(till.ledger)}")
        for till in visible_tills()
        if till.closed_at is None or str(till.id) == chosen
    ]
    if not rows:
        return []
    return [_select(
        "till", _("Касса"), rows, chosen, required=False,
        empty_label=_("Без кассы"),
        help=_("Откуда взяли деньги. Регистр учёта расхода приезжает из кассы, "
               "а точка — та, на которой она стоит."),
    )]


def _closed_hint(who, on: date) -> str:
    """Оговорка про закрытый месяц — только когда он и правда закрыт (T134).

    Раньше эта фраза стояла в подписи поля **константой**: продукт словами
    сообщал состояние периода и сообщал его неверно — на свежем сиде, где не
    закрыт ни один месяц, человек читал «месяц уже закрыт». Английский перевод
    той же строки был условным («If the month is already closed»), то есть два
    языка одного продукта говорили про одно и то же разное.

    Месяц берётся тот, что стоит в поле даты. Сменив дату в браузере, человек
    подписи не обновит — но подпись, которая права в момент показа и молчит в
    остальных случаях, честнее подписи, которая утверждает закрытие всегда. Что
    именно случилось, продукт всё равно скажет после записи (`_notice`).
    """
    if not cash.month_is_closed(who.tenant_id, on.replace(day=1)):
        return ""
    try:
        landing = cash.landing_for(who.tenant_id, on)
    except cash.CashRefused as refusal:
        return " " + refusal.message
    return " " + _(
        "Месяц %(closed)s закрыт: расход ляжет в %(month)s, а эта дата останется "
        "при нём."
    ) % {"closed": month_title(on.replace(day=1)), "month": month_title(landing.period)}


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
    """Что стало с расходом на всю сеть: разошёлся, ждёт (и почему) или не нам.

    Три ответа, и ни один не молчит. «Ждёт» — самый важный: сумма без точки не
    попадает в разрез по точкам, и человек обязан узнать об этом сразу, а не при
    сборке P&L, когда сходиться уже поздно.

    **Причина ожидания называется настоящая (T132).** Раньше здесь стояла одна
    фраза на все случаи — «правила разнесения у статьи нет», — и она приходила в
    том числе тогда, когда правило есть и лежит в `allocation_rules`: пустой
    план бывает и у `by_revenue` (выручки в продукте нет вовсе), и у `ask`
    (точку выбирает человек). Слова берутся из `allocation.waiting_title`, то
    есть те же самые, что в колонке списка нераспределённых: два экрана про одну
    строку обязаны говорить одно и то же.
    """
    from django.urls import reverse

    from .allocation import waiting_title

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
        return " " + waiting_title(request.GET["waiting"] or "no_rule") + waiting
    return ""


def _month_or_none(raw: str | None) -> date | None:
    from datetime import datetime

    try:
        return datetime.strptime(raw or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        return None
