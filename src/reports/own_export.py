"""Чтение нашей же выгрузки «Вид бухгалтера» (T119).

Зачем отдельный разбор. Сверка знала один формат — таблицу партнёра
(`payroll.importers.plata_xlsx`): свои имена листов, заголовки в первой строке,
имя и фамилия в двух колонках. Наша выгрузка устроена иначе — лист на точку,
шапка документа сверху, полное имя одной ячейкой, колонка регистра. Поэтому
файл, только что скачанный со страницы периода, при загрузке обратно не
читался вовсе: «лист не входит в формат PLATA» восемь раз и ноль сошедшихся
строк.

**Почему не привести выгрузку к формату партнёра.** Имя листа у него несёт
схему расчёта и группу («NS 1 Bulevar», «NS privremeni poslovi»), а наши листы
— точки; у нас есть колонка регистра, которой там нет и быть не может. Втиснуть
своё в чужую форму значило бы соврать про то, что в файле лежит, — а «привычный
бухгалтеру вид» перестал бы быть видом нашего расчёта.

**Как формат узнаётся.** По шапке таблицы: первые две колонки `R.br.` и
`IME I PREZIME` плюс колонка итога `UKUPNO ZA ISPLATU`. Заголовки берутся из
самой выгрузки (`reports.export`), а не переписаны сюда: разъехавшись, они
превратили бы «своя выгрузка не читается» из починенного дефекта в
воспроизведённый. Опознание не зависит ни от языка, на котором файл выгрузили,
ни от свойств документа: человек мог открыть книгу в Excel и сохранить заново.

**Что читается — только имя, лист и итог.** Ни часов, ни ставки в файле нет, и
выдумывать их нельзя: сверка обязана сравнивать то, что в файле написано, и
молчать об остальном (`reports.reconcile.FileLine`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext as _

from payroll.importers import Finding
from reports.export import PARTNER_NAME, PARTNER_NUMBER, PARTNER_TOTAL

# Докуда искать шапку таблицы. Над ней стоит заголовок документа и пустая
# строка; запас — на случай, если в шапку добавят строку.
HEADER_ROWS = 6


@dataclass(frozen=True)
class OwnRow:
    """Одна строка нашей выгрузки: человек, лист и итог к выплате."""

    sheet: str
    name: str
    total: Decimal


@dataclass
class OwnFile:
    """Результат разбора: что прочитано и что не прочитано."""

    rows: list[OwnRow] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip().upper()


def _layout(ws) -> tuple[int, int] | None:
    """Строка шапки и колонка итога — или ничего, если лист не наш."""
    for row in range(1, HEADER_ROWS + 1):
        if _norm(ws.cell(row, 1).value) != _norm(PARTNER_NUMBER):
            continue
        if _norm(ws.cell(row, 2).value) != _norm(PARTNER_NAME):
            continue
        for column in range(3, (ws.max_column or 3) + 1):
            if _norm(ws.cell(row, column).value) == _norm(PARTNER_TOTAL):
                return row, column
    return None


def looks_like_own_export(book) -> bool:
    """Наша ли это книга. Достаточно одного узнанного листа: остальные назовёт разбор."""
    return any(_layout(ws) is not None for ws in book.worksheets)


def _amount(raw) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw).strip().replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None


def read_own_export(book) -> OwnFile:
    """Разобрать книгу: строки и всё, что разобрать не удалось.

    Молчание здесь так же опасно, как в разборе таблицы партнёра (T021):
    пропущенный лист или строка — это человек, который тихо не попал в сверку.
    """
    out = OwnFile()
    for ws in book.worksheets:
        layout = _layout(ws)
        if layout is None:
            out.findings.append(Finding(
                "sheet", ws.title,
                _("лист не похож на выгрузку «Вид бухгалтера» — "
                  "ни одна его строка не загружена"),
            ))
            continue

        header, total_column = layout
        for row in range(header + 1, (ws.max_row or header) + 1):
            # Место в файле теми же словами, какими его видит человек: имя
            # листа и номер строки. Одной строкой перевода, а не склейкой из
            # слов: у порядка слов в языках свои правила.
            where = _("%(sheet)s, строка %(row)s") % {"sheet": ws.title, "row": row}
            number = ws.cell(row, 1).value
            name = str(ws.cell(row, 2).value or "").strip()
            # Подвал листа («UKUPNO») номера не имеет — он уходит сюда же и
            # молча, как и должен: это не строка человека.
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                if name:
                    out.findings.append(Finding(
                        "row", where,
                        _("строка не пронумерована и не загружена: «%(name)s»")
                        % {"name": name},
                    ))
                continue
            if not name:
                out.findings.append(Finding(
                    "row", where,
                    _("у строки нет имени — она не загружена"),
                ))
                continue

            total = _amount(ws.cell(row, total_column).value)
            if total is None:
                out.findings.append(Finding(
                    "value", where,
                    _("«%(value)s» в колонке «%(column)s» — не число, "
                      "строка не загружена")
                    % {"value": ws.cell(row, total_column).value,
                       "column": PARTNER_TOTAL},
                ))
                continue
            out.rows.append(OwnRow(sheet=ws.title, name=name, total=total))
    return out
