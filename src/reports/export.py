"""Три выгрузки периода в xlsx (T032): к выплате, для P&L, в виде бухгалтера.

**Все три берут готовый срез** (`reports.sheet.SheetSlice`) — тот самый, что
показан на экране, с тем же значением разреза. Собирать данные заново было бы
проще ровно один раз: дальше две выборки одного и того же расходятся молча, и
человек получает файл, не совпадающий с тем, что он видел. Отсюда же следует
главное свойство безопасности: регистр, которого роль не видит, физически не
доезжает до файла — суммы отобраны политиками базы ещё до среза, а разрез умеет
только сужать.

**Файл опаснее экрана.** Он уходит из продукта и живёт своей жизнью, поэтому
D023 («ни строк, ни следа») здесь читается буквально: в книге не должно быть ни
строк чужого регистра, ни его названия, ни его вклада в итог. Итоги в файле
считаются по его же строкам — не переносятся из полной ведомости.

**Налоги в разрезе регистра не существуют.** Налог и взносы посчитаны по строке
ведомости целиком и живут в `payslip_totals`, закрытой своей политикой (T050).
Поэтому блок налогов есть только в срезе «все видимые регистры»: приписать
общий налог одному регистру значило бы выдумать число.

Заголовки листов и колонок живут здесь, а не в `web`: выгрузка — это документ,
и его шапка часть формата файла, а не оформления экрана. Единственное, что
приезжает снаружи, — подписи регистров: они уже есть в `web/format.py` и
обязаны совпадать с экраном слово в слово.
"""
from __future__ import annotations

import io
from collections import namedtuple
from datetime import date
from decimal import Decimal
from uuid import UUID

import openpyxl
from django.utils.translation import gettext as _
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from reports.sheet import ALL, SheetSlice

# Строка блока налогов: статья, точка и два числа. Кортеж, а не словарь: у него
# фиксированный порядок полей, и перепутать местами налог и взносы нельзя.
TaxLine = namedtuple("TaxLine", "article unit tax contributions")

# Человек, у которого не заведена статья P&L, обязан быть виден в файле, а не
# пропасть из него: пропавшая строка — это не найденная позже недостача.
#
# Константы с этим текстом больше нет: строка переводится в местах, где
# используется (`gettext("Без статьи")`), потому что перевод возможен только
# когда Django уже настроен, а тело модуля выполняется раньше.

# Колонки таблицы партнёра, под которыми лежит **то же самое**, что у нас.
# Заголовков часов здесь нет намеренно: у партнёра «SATI RADA» — это часы, а у
# нас начисление за них, и деньги под этим заголовком были бы ложью.
PARTNER_HEADERS = {
    "meal_and_vacation_bonus": "TOPLI OBROK I REGRES",
    "minimum_guarantee": "KOREKCIJA DO MINIMALCA",
    "manual_correction": "KOREKCIJA (RUCNO)",
    "deduction": "OBUSTAVA",
}
PARTNER_TOTAL = "UKUPNO ZA ISPLATU"

MONEY = "#,##0.00"


def _stamp(period: date | None) -> str:
    return f"{period:%Y-%m}" if period else "period"


def _file_name(kind: str, period: date | None, cut: str) -> str:
    """Имя файла латиницей: кириллица в заголовке ответа доезжает мусором.

    Разрез входит в имя, когда он выбран: два файла одного месяца в одной папке
    иначе неразличимы, и человек отправит бухгалтеру не тот.
    """
    tail = f"-{cut}" if cut and cut != ALL else ""
    return f"{kind}-{_stamp(period)}{tail}.xlsx"


def _head(ws, text: str, width: int) -> None:
    ws.append([text])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    del width


def _headers(ws, row: list[str]) -> None:
    ws.append(row)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top", wrap_text=True)


def _fit(ws, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width


def _money_format(ws, first_column: int) -> None:
    for row in ws.iter_rows(min_col=first_column):
        for cell in row:
            if isinstance(cell.value, (int, float, Decimal)):
                cell.number_format = MONEY


def _save(book) -> bytes:
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _named(column, component_title) -> str:
    """Заголовок колонки на языке файла (T092).

    Тот же вопрос, что на экране: в `pay_components.title` лежит подпись,
    замороженная расчётом, а файл выгружает человек, который читает на своём
    языке. Подставляет её вызывающий — здесь, как и с регистрами, только показ.
    """
    return component_title(column.code, column.title) if component_title else column.title


def _titled(title: str, view: SheetSlice, ledger_title) -> str:
    """Шапка документа. Разрез назван, если он выбран, — файл обязан сказать,
    что он не про весь расчёт, иначе его прочитают как полный."""
    if view.cut and view.cut != ALL:
        return f"{title} · {ledger_title(view.cut)}"
    return title


# --- ведомость к выплате ------------------------------------------------------


def payout(view: SheetSlice, *, tenant_id=None, period=None, title="",
           ledger_title=str, component_title=None) -> tuple[bytes, str]:
    """Ведомость к выплате: ровно то, что человек видит на экране, файлом."""
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = "K isplati"

    columns = view.sheet.columns
    _head(ws, _("Ведомость к выплате · %(sub)s") % {"sub": _titled(title, view, ledger_title)}, 4)
    _headers(ws, (
        [_("№"), _("Сотрудник"), _("Точка"), _("Регистр")]
        + [_named(column, component_title) for column in columns]
        + [_("Итого"), _("Примечание")]
    ))

    for number, row in enumerate(view.sheet.rows, start=1):
        notes = []
        if row.is_retro:
            # Разница за закрытый месяц обязана объяснить себя: без источника
            # это непонятная сумма в чужом месяце (T026).
            notes.append(_("разница за %(date)s") % {"date": f"{row.retro_source:%m.%Y}"})
        if row.frozen:
            notes.append(
                _("строка заморожена: %(reason)s") % {"reason": row.freeze_reason}
                if row.freeze_reason else _("строка заморожена")
            )
        ws.append(
            [number, row.employee, row.unit, ledger_title(row.ledger)]
            + [row.amounts.get(column.code) for column in columns]
            + [row.total, "; ".join(notes)]
        )

    # Подвал считается по строкам этого файла, а не переносится из полной
    # ведомости: итог, больший суммы показанного, выдаёт скрытое вычитанием.
    _headers(ws, (
        [_("Итого"), "", "", ""]
        + [view.sheet.column_totals.get(column.code) for column in columns]
        + [view.sheet.total, _("человек: %(count)s") % {"count": view.sheet.employees}]
    ))

    _fit(ws, [5, 28, 8, 15] + [14] * len(columns) + [14, 30])
    _money_format(ws, 5)
    return _save(book), _file_name("payout", period, view.cut)


# --- строки для P&L -----------------------------------------------------------


def collect_articles(tenant_id: UUID) -> dict[str, str]:
    """Ключ сотрудника → статья P&L его группы.

    Через условия найма, а не через имя листа: соответствие «лист → точка»
    приблизительное, а группа у человека одна и версионируется вместе с наймом.
    """
    from core.models import EmploymentTerm

    out: dict[str, str] = {}
    for term in EmploymentTerm.objects.filter(
        tenant_id=tenant_id
    ).select_related("employee", "group__pnl_item").order_by("valid_from"):
        if term.group.pnl_item_id:
            out[term.employee.external_id] = term.group.pnl_item.title
    return out


def collect_taxes(tenant_id: UUID, period: date, articles: dict[str, str]) -> list[TaxLine]:
    """Налог и взносы по статье и точке — из итогов строк ведомости.

    Выборка идёт от `payslip_totals`, и это и есть граница видимости: итоги
    видны роли, только если ей видна вся строка (T050). Приложение здесь ничего
    не маскирует и не досчитывает — что база отдала, то и сложено.
    """
    from core.models import PayslipTotals

    grouped: dict[tuple[str, str], list[Decimal]] = {}
    for row in PayslipTotals.objects.filter(
        tenant_id=tenant_id, payslip__payrun__period=period
    ).select_related("payslip__employee", "payslip__unit"):
        key = (
            articles.get(row.payslip.employee.external_id, _("Без статьи")),
            row.payslip.unit.code if row.payslip.unit_id else "",
        )
        bucket = grouped.setdefault(key, [Decimal(0), Decimal(0)])
        bucket[0] += row.tax
        bucket[1] += row.contributions

    return [
        TaxLine(article, unit, tax, contributions)
        for (article, unit), (tax, contributions) in sorted(grouped.items())
        if tax or contributions
    ]


def pnl(view: SheetSlice, *, tenant_id=None, period=None, title="",
        ledger_title=str, articles=None, taxes=None,
        component_title=None) -> tuple[bytes, str]:
    """Строки для P&L: начисления и налоги раздельно, по статье и точке.

    Итоговой строки в файле нет намеренно: это заготовка строк для сборки P&L,
    и «Итого» в ней загрузчик посчитал бы такой же строкой данных.
    """
    if articles is None:
        articles = collect_articles(tenant_id)
    # Разрез — это один регистр, а налог посчитан по строке ведомости целиком.
    # Приписать его регистру нельзя, поэтому в разрезе налогов нет вовсе — и это
    # решается здесь, а не тем, что вызывающий их не передал.
    if view.cut != ALL:
        taxes = []
    elif taxes is None:
        taxes = collect_taxes(tenant_id, period, articles)

    book = openpyxl.Workbook()
    ws = book.active
    ws.title = "PnL"

    _head(ws, _("Строки для P&L · %(sub)s") % {"sub": _titled(title, view, ledger_title)}, 4)
    _headers(ws, [
        _("Статья P&L"), _("Точка"), _("Регистр"), _("Тип строки"), _("Компонент"), _("Сумма"),
    ])

    accruals: dict[tuple[str, str, str, str, str], Decimal] = {}
    for row in view.sheet.rows:
        article = articles.get(row.employee, _("Без статьи"))
        for column in view.sheet.columns:
            amount = row.amounts.get(column.code)
            if not amount:
                continue
            key = (article, row.unit, row.ledger, column.code,
                   _named(column, component_title))
            accruals[key] = accruals.get(key, Decimal(0)) + amount

    for (article, unit, ledger, _code, column_title), amount in sorted(accruals.items()):
        ws.append([article, unit, ledger_title(ledger), _("Начисление"), column_title, amount])

    for line in taxes:
        # Регистра у налога нет, и прочерк здесь честнее пустой ячейки: пустая
        # читается как «забыли заполнить».
        ws.append([line.article, line.unit, "—", _("Налог"), _("Налог на доход"), line.tax])
        ws.append([line.article, line.unit, "—", _("Взносы"), _("Взносы"), line.contributions])

    _fit(ws, [26, 8, 15, 14, 26, 14])
    _money_format(ws, 6)
    return _save(book), _file_name("pnl", period, view.cut)


# --- вид, привычный бухгалтеру ------------------------------------------------


def partner(view: SheetSlice, *, tenant_id=None, period=None, title="",
            ledger_title=str, component_title=None) -> tuple[bytes, str]:
    """Тот же расчёт, разложенный так, как привык читать бухгалтер партнёра.

    Привычное здесь — две вещи: **лист на точку** (в его таблице лист на точку
    и схему) и **его собственные заголовки** там, где под ними лежит то же
    самое. Где не то же самое, остаётся наше название: заголовок «SATI RADA»
    над начислением за часы был бы не привычным видом, а неправдой.
    """
    book = openpyxl.Workbook()
    book.remove(book.active)

    columns = view.sheet.columns
    headers = [
        PARTNER_HEADERS.get(column.code) or _named(column, component_title)
        for column in columns
    ]

    units = sorted({row.unit for row in view.sheet.rows})
    for unit in units or [""]:
        ws = book.create_sheet(unit or "Bez objekta")
        _head(ws, f"{_titled(title, view, ledger_title)} · {unit}", 4)
        _headers(ws, ["R.br.", "IME I PREZIME", _("Регистр")] + headers + [PARTNER_TOTAL])

        rows = [row for row in view.sheet.rows if row.unit == unit]
        for number, row in enumerate(rows, start=1):
            ws.append(
                [number, row.employee, ledger_title(row.ledger)]
                + [row.amounts.get(column.code) for column in columns]
                + [row.total]
            )

        # Подвал листа — по строкам этого листа. Складывать сюда весь период
        # значило бы показать на листе точки чужие деньги.
        _headers(ws, (
            ["UKUPNO", "", ""]
            + [
                sum((row.amounts.get(column.code) or Decimal(0) for row in rows), Decimal(0))
                or None
                for column in columns
            ]
            + [sum((row.total for row in rows), Decimal(0))]
        ))
        _fit(ws, [6, 28, 15] + [16] * len(columns) + [16])
        _money_format(ws, 4)

    return _save(book), _file_name("partner", period, view.cut)
