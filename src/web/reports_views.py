"""Экраны отчётов: сверка с таблицей бухгалтера (T031) и выгрузки (T032).

Отдельным модулем от `web/views.py` не по вкусу, а потому что оформление здесь
своё и объёмное: подписи полей сверки, названия причин, заголовки листов
выгрузки. В общем модуле страниц периода они бы утонули.

Границу, проведённую T028, этот модуль не двигает: считает и сверяет `reports`,
а здесь только слова и адреса. Ключевое следствие — **выгрузка берёт тот же
срез с тем же разрезом, что показан на экране** (`reports.sheet.build_slice`),
а не собирает данные заново: две выборки одного и того же расходятся молча, и
человек получает файл, не совпадающий с тем, что он видел.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from reports import export as exports
from reports.reconcile import reconcile
from reports.sheet import build_slice

from . import runslice
from .cash import item_title
from .format import ledger_title, money
from .labels import labeller
from .views import find_period, month_title

__all__ = ["period_export", "period_reconcile"]

# Больше этого зарплатная таблица не бывает: восемь листов на несколько сотен
# человек — сотни килобайт. Ограничение не про безопасность, а про внятный
# отказ вместо съеденной памяти на случайно выбранном видеофайле.
MAX_UPLOAD = 20 * 1024 * 1024

# Файл разобран и понят, не принято именно его содержимое — 422, а не 400.
REFUSED = 422

# Как называются сверяемые числа в таблице бухгалтера. Порядок — тот, в котором
# их читают: сначала то, что человек получит на руки.
FIELD_TITLES = {
    "net": gettext_noop("К выплате (нето)"),
    "gross": gettext_noop("Бруто"),
    "contributions": gettext_noop("Взносы"),
    "total_cost": gettext_noop("Полная стоимость"),
}

# Названия причин расхождения. Причина — это вход расчёта, а не сумма: если
# сошлись все входы, а итог разошёлся, значит разошлось правило, и это тоже
# ответ, который человек должен прочитать.
CAUSE_TITLES = {
    "insured": gettext_noop("Часы для взносов"),
    # «Базовая ставка», а не «ставка за час» (issue #75). Сверяется
    # `employees.base_rate`, и у сдельной группы это цена доставки, а не часа —
    # ровно та же неправда, что след уже пережил на своём экране (T081).
    # Признака `pay_per_unit` у причины под рукой нет: она строится по паре «что
    # в файле / что в расчёте», без правил группы. Название, верное при любом
    # способе оплаты, — единственное, которое здесь можно написать честно.
    "rate": gettext_noop("Базовая ставка"),
    "coefficient": gettext_noop("Коэффициент"),
    "meal": gettext_noop("Топли оброк и регрес"),
}

EXPORTS = {
    "payout": exports.payout,
    "pnl": exports.pnl,
    "partner": exports.partner,
}


def _hour_titles(period) -> dict[str, str]:
    """Названия видов часов — из правил страны, а не из списка в коде.

    Тот же источник, из которого берёт колонки табель: новая страна не должна
    требовать правки интерфейса. Правил на месяц нет — показываем код вида
    часов, а не выдумываем название.
    """
    try:
        from core.rules import load_rules_at

        rules = load_rules_at(
            period.tenant_id, period.tenant.country_code, period.period
        )
        return {
            code: (body or {}).get("title") or code
            for code, body in (rules.base.get("hour_types") or {}).items()
        }
    except Exception:  # noqa: BLE001 — правил может не быть, это не поломка страницы
        return {}


def _cause_text(cause, hour_titles: dict[str, str]) -> str:
    """Причина словами: что и на что разошлось. Оба числа, а не только разница."""
    # Число подставляется уже готовой строкой, а не числом: у формы предложения
    # и у формата числа разные хозяева, и склеивать их в одном ключе перевода
    # значило бы отдать переводчику ещё и разделитель дробной части (T017).
    if cause.kind == "hours":
        # Вид часов — данные партнёра (название приезжает из правил страны), а
        # «Часы» перед ним — слово продукта. Поэтому переводится только оно.
        what = _("Часы · %(kind)s") % {"kind": hour_titles.get(cause.code, cause.code)}
        return _("%(what)s: в таблице %(expected)s, в расчёте %(actual)s") % {
            "what": what,
            "expected": f"{cause.expected:g}",
            "actual": f"{cause.actual:g}",
        }
    known = CAUSE_TITLES.get(cause.kind)
    what = _(known) if known is not None else cause.kind
    return _("%(what)s: в таблице %(expected)s, в расчёте %(actual)s") % {
        "what": what,
        "expected": money(cause.expected),
        "actual": money(cause.actual),
    }


def _amount_view(amount) -> dict:
    """Одно сравниваемое число для показа.

    `state` — не украшение: «сошлось», «копейки» и «разошлось» читаются глазами
    по-разному, а состояния, отличающиеся только цветом, не читаются вовсе.
    """
    if not amount.comparable:
        state = "unknown"
    elif amount.matches:
        state = "match"
    elif amount.rounding:
        state = "rounding"
    else:
        state = "off"
    return {
        "title": _(FIELD_TITLES[amount.code]) if amount.code in FIELD_TITLES else amount.code,
        "expected": money(amount.expected) if amount.expected is not None else "—",
        "actual": money(amount.actual) if amount.actual is not None else "—",
        "diff": money(amount.diff) if amount.diff is not None else "—",
        "state": state,
    }


def _line_view(line, hour_titles) -> dict:
    amounts = [_amount_view(a) for a in line.amounts]
    return {
        "name": line.name,
        "sheet": line.sheet,
        # `compared` — не то же, что `matched`, и разделять их обязательно.
        # Строка, деньги которой роли не отданы (T071), не сошлась и не
        # разошлась: сравнивать было нечего. Без этого различения она попадала
        # бы в «Разошлось», где у неё не показывается ни одного числа — то есть
        # без имени и с подписью про расхождение правила расчёта.
        "compared": line.compared,
        # Сказал ли файл хоть что-то о входах (T119). У нашей собственной
        # выгрузки входов нет вовсе, и подпись «входы сошлись» под её строками
        # была бы утверждением о том, чего никто не сравнивал.
        "stated_inputs": line.stated_inputs,
        "matched": line.matched,
        "rounding_only": line.rounding_only,
        "amounts": amounts,
        # У разошедшейся строки показываются только разошедшиеся числа: три
        # сошедшихся поля рядом с одним разошедшимся прячут именно то, что
        # человек ищет.
        "off": [a for a in amounts if a["state"] in ("off", "rounding")],
        "causes": [_cause_text(cause, hour_titles) for cause in line.causes],
    }


def _report(request, period, *, result=None, error=None, status=200):
    hour_titles = _hour_titles(period) if result is not None else {}
    lines = [_line_view(line, hour_titles) for line in result.lines] if result else []
    return render(
        request,
        "web/reconcile.html",
        {
            "period": period,
            "title": month_title(period.period),
            "error": error,
            "result": result,
            # Разделено на четыре списка здесь, а не условиями в разметке: у
            # разошедшегося, копеечного, несравнённого и сошедшегося разный
            # вес, и читают их в разном порядке. Смешанная таблица заставляла
            # бы искать глазами то единственное, ради чего сверку и открыли.
            "off_lines": [line for line in lines if line["compared"]
                          and not line["matched"] and not line["rounding_only"]],
            "rounding_lines": [line for line in lines if line["rounding_only"]],
            "inputs_lines": [line for line in lines if not line["compared"]],
            # Говорил ли файл о входах хоть по одной такой строке: от этого
            # зависит, вправе ли раздел обещать, что входы сверены (T119).
            "inputs_stated": any(
                line["stated_inputs"] for line in lines if not line["compared"]
            ),
            "matched_lines": [line for line in lines if line["matched"]],
            "totals": {
                "expected": money(result.total_expected) if result else "",
                "actual": money(result.total_actual) if result else "",
                "diff": money(result.total_diff) if result else "",
                "off": bool(result and result.total_diff != 0),
                # Итог по нулю сверенных строк — это не «сошлось в ноль».
                # Подвал «в таблице 0.00, в расчёте 0.00, разница 0.00»
                # читается как совпадение, хотя не сравнивалось ничего, и
                # именно так сверка соврала бы роли со скрытыми итогами.
                "any": bool(result and any(line.compared for line in result.lines)),
                # Почему не сравнивалось (T120). Код называет ядро: разметка
                # знает только, что сумм нет, и одна фраза на все случаи
                # объясняла ноль доступом, которого хватало.
                "reason": result.nothing_compared if result else "",
            },
        },
        status=status,
    )


@login_required
def period_reconcile(request, period_id):
    """Сверка расчёта с таблицей бухгалтера (T031).

    Своего права у сверки нет и не заводится: она ничего не пишет, а видит
    ровно то, что база отдаёт роли открывшего. Отдельное право означало бы
    вторую правду о видимости рядом с политиками базы.

    Файл нигде не сохраняется — ни на диск, ни в базу (D028): в таблице
    партнёра ФИО и суммы живых людей, и сверка не повод заводить им ещё одно
    место жительства.
    """
    period = find_period(period_id)
    if request.method != "POST":
        return _report(request, period)

    upload = request.FILES.get("table")
    if upload is None:
        return _report(request, period, error=_("Файл не выбран."), status=400)
    if upload.size > MAX_UPLOAD:
        return _report(
            request, period, status=400,
            error=_("Файл больше %(limit)s МБ — это не зарплатная таблица.")
                  % {"limit": MAX_UPLOAD // 1024 // 1024},
        )

    try:
        result = reconcile(upload, tenant_id=period.tenant_id, period=period.period)
    except Exception as broken:  # noqa: BLE001 — чужой файл бывает чем угодно
        # Разбирать исключения openpyxl по одному значило бы гадать за
        # библиотеку. А вот молчаливая 500-я недопустима: человек должен
        # прочитать, что файл не разобран, и почему.
        return _report(
            request, period, status=REFUSED,
            error=_("Файл не удалось прочитать как книгу Excel: %(reason)s")
                  % {"reason": broken},
        )

    return _report(request, period, result=result)


@login_required
def period_export(request, period_id, kind):
    """Выгрузка периода в xlsx (T032): ведомость, строки P&L, вид бухгалтера.

    Все три берут **тот же срез с тем же разрезом**, что показан на экране
    (`build_slice`), и потому не могут разойтись с тем, что человек видел.
    Регистр, которого роль не видит, сюда не приезжает физически: суммы уже
    отобраны политиками базы, а разрез умеет только сужать.
    """
    build = EXPORTS.get(kind)
    if build is None:
        raise Http404("такой выгрузки нет")

    period = find_period(period_id)
    view = build_slice(period.tenant_id, period.period, request.GET.get("ledger", ""))
    book, name = build(
        view, tenant_id=period.tenant_id, period=period.period,
        title=month_title(period.period), ledger_title=ledger_title,
        # Отдан ли смотрящему весь расчёт партнёра (T141). Нужно файлу P&L:
        # без налоговой части он обязан сказать, почему её нет, а «итоги вашей
        # роли не отданы» — утверждение о правах, и спрашивается оно у тех же
        # функций, на которых стоят политики базы (D014). Передаётся всем трём
        # выгрузкам одним набором аргументов — по тому же доводу, что и
        # `item_title`: развилка «этому виду передаём, тому нет» и есть место,
        # где однажды передадут не туда.
        whole_run=runslice.sees_whole_run(period.tenant_id),
        # Колонки файла названы на языке страницы — тем же способом, что на
        # экране (T092): выгрузка обязана читаться так же, как то, из чего её
        # сделали.
        component_title=labeller(
            period.tenant_id, period.tenant.country_code, period.period
        ),
        # Название статьи расхода — на языке страницы, тем же правилом, что в
        # списке расходов (T108). Правило приезжает **параметром**, а не
        # импортом внутри `reports`: отчёты не знают про интерфейс, и подписи
        # регистров уже устроены так же.
        item_title=item_title,
    )

    response = HttpResponse(
        book,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # Имя файла латиницей: кириллица в заголовке ответа требует кодирования, а
    # половина почтовых клиентов и файловых менеджеров показывает её как мусор.
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response
