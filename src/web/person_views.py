"""Экран «Выплаты по человеку»: месяц за месяцем и из чего сложилось (T166).

**Почему отдельный модуль, а не ещё один раздел карточки сотрудника.** Карточка
— справочник: имя, ставка, история условий найма. Здесь данные расчёта, и разница
не косметическая. Карточку читает управляющий точки (T173): ставок в Dodo IS нет
вовсе, и проверить их можно только у нас — а сумм расчёта он не видит (это прямое
требование к роли). Держать оба вида данных на одном экране значит каждый раз
решать заново, что из него скрыть, — и однажды решить неверно.

**Кому открыт.** Тому, у кого есть `payrun.calculate`: этот экран отвечает на
вопрос «почему человеку столько начислено», а задаёт его тот, кто ведёт расчёт
месяца. Управляющему точки не открыт (сумм расчёта он не видит), администратору
сети — тоже: у него нет ни одного права на данные расчёта, и это намеренно
(`core.roles`). Отказ — словами, теми же, которыми отвечает и всё остальное:
исчезнувший без объяснения экран читается как поломка продукта.

**Порядок проверок.** Человек ищется ДО проверки права — как на карточке (T173).
Иначе чужой сотрудник отвечал бы читателю 403, а не 404, то есть по коду ответа
было бы видно, что он существует.

**Чего здесь нет.** Ни одного фильтра по регистру и по точке: срез делает база.
Числа, посчитанные по всем регистрам сразу (бруто, налог, взносы, каналы
выплаты), приходят только из `payslip_totals` и только целиком — разбор в
`payrun.person`. Итог по месяцу и по всей истории равен сумме показанных строк,
поэтому у роли с урезанным набором регистров он меньше — ровно на то, чего ей не
отдали, и без следа того, сколько это было.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from core.models import Period, Tenant
from payrun import person as history
from web import permissions
from web.directory_views import _employee_or_404, _reader, _refusal
from web.format import EMPTY, hours, ledger_title, money
from web.i18n import month_title
from web.labels import labeller

# Почему объяснение стоит всегда, а не когда есть что прятать. Оно свойство
# роли, а не данных: появляясь только при скрытых строках, оно само рассказывало
# бы об их существовании — та же утечка на один бит, что в `web.runslice`.
#
# `gettext_noop`, а не `gettext` (T017). Значение константы модуля вычисляется
# **один раз при импорте**, а язык страницы — у каждого запроса свой: переведённая
# на импорте строка осталась бы русской навсегда. Поймано в браузере: на
# английской странице половина текста была русской при полностью зелёном
# каталоге — сравнивать было не с чем. Перевод делается в месте использования.
#
# И вызывается пометка **своим полным именем**: извлечение строк смотрит на имя
# функции в тексте файла, а не на то, что она делает, — под псевдонимом строка
# молча не попала бы в каталог вовсе.
DERIVED_NOTE = gettext_noop(
    "«Начислено» — сумма показанных строк. Бруто, налог, взносы и каналы "
    "выплаты посчитаны по всем регистрам учёта сразу, поэтому показываются "
    "тому, кому видны все регистры, и по месяцам не складываются."
)


# Чем выплачено. Обе половины называются всегда, в том числе нулевые: «в кассу
# 0,00» — это ответ «наличными не платили», а пропавшая половина строки читалась
# бы как «про наличные ничего не известно». `gettext_noop` — по той же причине,
# что у объяснения выше.
CHANNELS = gettext_noop("на карту %(bank)s · в кассу %(cash)s")


def _derived(month) -> dict:
    """Производные числа месяца — или прочерки, если итоги роли не отданы.

    Прочерки ставятся **всей группой сразу**: итоги приходят и исчезают вместе,
    и половина группы читалась бы как «этих чисел нет», а не как «они не для
    вас». По той же причине вместе с ними исчезает и подпись о каналах выплаты.

    Отсутствие здесь никого не называет: политика `ledger_visibility` (миграция
    `0023`) решает вопрос по роли, а не по строке, поэтому у роли с неполным
    набором регистров прочерк стоит во всех месяцах подряд.
    """
    if month.totals is None:
        return {
            "gross": EMPTY, "tax": EMPTY, "contributions": EMPTY,
            "net": EMPTY, "channels": "",
        }
    return {
        "gross": money(month.totals.gross),
        "tax": money(month.totals.tax),
        "contributions": money(month.totals.contributions),
        "net": money(month.totals.net),
        "channels": _(CHANNELS) % {
            "bank": money(month.totals.to_bank),
            "cash": money(month.totals.to_cash),
        },
    }


def _period_urls(tenant_id, months) -> dict:
    """Адрес ведомости каждого месяца — одним запросом, а не по месяцу.

    Месяц в таблице — ссылка: человек, увидевший странную сумму, идёт смотреть
    её в ведомости целиком, рядом с остальными людьми.
    """
    wanted = [month.period for month in months]
    return {
        row.period: reverse("period", args=[row.id])
        for row in Period.objects.filter(tenant_id=tenant_id, period__in=wanted)
    }


def _trace_url(month) -> str:
    """След расчёта месяца. Пусто — объяснять нечего, и обещать ссылку нельзя.

    Разрез не приписывается: человек пришёл сюда за всей своей историей, а не за
    одним регистром, и след обязан объяснить то же самое, что показано в строке.
    """
    if month.payslip_id is None:
        return ""
    return reverse("payslip-trace", args=[month.payslip_id])


@login_required
def employee_pay(request, employee_id):
    """Выплаты одного человека по месяцам."""
    who, denied = _reader(request)
    if denied is not None:
        return denied
    person = _employee_or_404(who, employee_id)
    try:
        permissions.check(who, permissions.PAYRUN_CALCULATE)
    except permissions.PermissionRefused as refusal:
        return _refusal(request, refusal)

    found = history.build_history(who.tenant_id, person.id)
    periods = _period_urls(who.tenant_id, found.months)

    # Подписи компонентов — на языке страницы (T092). Правила берутся по
    # **самому свежему** месяцу истории: колонка одна на всю таблицу, а месяцев
    # в ней много, и назвать её сразу всеми именами, которые код носил за год,
    # нельзя. Имя, под которым компонент известен сегодня, — единственное, что
    # читателю поможет; имя, замороженное в момент расчёта, никуда не пропало и
    # стоит на следе своего месяца.
    country = Tenant.objects.filter(id=who.tenant_id).values_list(
        "country_code", flat=True
    ).first() or ""
    label = (
        labeller(who.tenant_id, country, found.months[0].period)
        if found.months
        # Месяцев нет — колонок тоже нет, и спрашивать правила незачем. Заглушка
        # оставлена, чтобы у контекста был один и тот же вид: пустой список
        # колонок не должен требовать отдельной ветки в разметке.
        else (lambda code, stored="": stored or code)
    )

    return render(request, "web/person_pay.html", {
        "person": person,
        "back_url": reverse("directory-employee", args=[person.id]),
        "derived_note": _(DERIVED_NOTE),
        "cards": [
            {
                "label": _("Начислено за всё время"),
                "value": money(found.accrued),
                "about": _("Сумма показанных строк по всем месяцам"),
            },
            {
                "label": _("Месяцев с выплатами"),
                "value": str(len(found.months)) if found.months else EMPTY,
                "about": _("Считаются только месяцы, строки которых вам видно"),
            },
            {
                "label": _("В среднем за месяц"),
                "value": (
                    money(found.accrued / len(found.months)) if found.months else EMPTY
                ),
                "about": _("Начислено, поделённое на число этих месяцев"),
            },
            {
                "label": _("Часов отработано"),
                "value": hours(found.hours),
                "about": _("По табелю за те же месяцы; часы регистром не делятся"),
            },
        ],
        # Динамика — от старого месяца к новому: время читается слева направо.
        # Таблица идёт наоборот (свежий месяц первым), и это не разнобой: в
        # таблице ищут последний месяц, на полосе — ход по годам.
        "bars": [
            {
                "title": month_title(month.period),
                "amount": money(month.accrued),
                "share": month.share,
            }
            for month in reversed(found.months)
        ],
        "months": [
            {
                "title": month_title(month.period),
                "url": periods.get(month.period, ""),
                "unit": month.unit or EMPTY,
                "ledgers": [
                    {"title": ledger_title(name), "amount": money(amount)}
                    for name, amount in month.by_ledger
                ],
                "hours": hours(month.hours),
                "norm": hours(month.norm_hours),
                "accrued": money(month.accrued),
                **_derived(month),
                "frozen": month.frozen,
                "freeze_reason": month.freeze_reason,
                "trace_url": _trace_url(month),
            }
            for month in found.months
        ],
        "columns": [label(column.code, column.title) for column in found.columns],
        "pieces": [
            {
                "title": month_title(piece.period),
                "ledger": ledger_title(piece.ledger),
                "cells": [
                    money(piece.amounts.get(column.code)) for column in found.columns
                ],
                "total": money(piece.total),
                # Строка разницы обязана объяснить себя словами: без
                # месяца-источника это непонятная сумма в чужом месяце (T026).
                "is_retro": piece.is_retro,
                "retro_title": (
                    month_title(piece.retro_source) if piece.is_retro else ""
                ),
            }
            for piece in found.pieces
        ],
        "column_totals": [
            money(found.column_totals.get(column.code)) for column in found.columns
        ],
        "accrued": money(found.accrued),
        "total_hours": hours(found.hours),
        # Прочерк в подвале — тот же самый прочерк, что во всех числовых
        # ячейках продукта. Приезжает из `web.format`, а не набирается в
        # разметке: второй его источник разошёлся бы с первым молча, и `numclass`
        # перестал бы отличать пустоту от значения.
        "dash": EMPTY,
    })
