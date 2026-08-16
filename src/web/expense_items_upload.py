"""Наполнение справочника статей файлом бухгалтера (T147, D041).

Правка статей с экрана уже есть (T108); недостаёт того, с чего справочник
начинается, — списка, который бухгалтер ведёт у себя. Ответ владельца на Q015:
«Берём за основу её справочник, но должна быть возможность редактировать и
дополнять».

**Настоящего образца файла у нас нет.** Он у бухгалтера Сербии, и до ответа
неизвестно ни как называются её колонки, ни в каком они порядке, ни на каком
языке. Поэтому разбор устроен терпимо:

* колонки ищутся **по заголовку** среди известных названий на трёх языках,
  регистр и лишние пробелы значения не имеют, порядок — тоже;
* лишние колонки не мешают: чужой файл не обязан состоять только из нужного нам;
* обязательна ровно одна колонка — **название**. Всё остальное либо есть в
  файле, либо задаётся один раз на форме загрузки для всего файла.

**Идемпотентность — по коду статьи.** Тому самому, про который в форме статьи
написано «по нему статья сходится с файлом бухгалтера при загрузке». Кода в
файле нет — он выводится **из названия**, устойчиво: тот же файл даст те же
коды и во второй раз. Считать код от номера строки было бы идемпотентностью на
один день — до первой вставленной сверху строки.

**Ничего не удаляется.** Строки справочника, которых в файле нет, остаются и
называются человеку вслух. Файл бухгалтера — не полная правда о справочнике: у
нас могли завести статью позже, и на неё уже ссылаются расходы. Молчаливое
удаление означало бы, что эти расходы остались без статьи, а заметили бы это
через месяц, собирая P&L.

**Разбор отделён от записи.** `read_rows` только читает книгу и ничего не знает
про базу, `apply_rows` только пишет. Так разбор чужого формата проверяется без
базы, а правило «не плодить дублей» — без файла.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from django.utils.translation import gettext as _

from core.models import ExpenseItem, PnlItem

# Файл человека читается только как книга Excel: в продукте уже есть загрузка
# табеля и таблицы бухгалтера, и обе принимают xlsx. Второй формат — это второй
# разбор, который однажды разойдётся с первым.
MAX_UPLOAD = 5 * 1024 * 1024


class FileRefused(Exception):
    """Файл не принят целиком. Половина загруженного справочника хуже пустого."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# --- какие заголовки мы узнаём ---------------------------------------------------
#
# Списки намеренно щедрые: угадать, как бухгалтер назвала колонку, нельзя, а
# лишнее слово в списке не стоит ничего. Сравнение идёт по нормализованному
# заголовку (см. `_norm`), поэтому регистр, пробелы и знаки препинания не важны.
COLUMNS = {
    "code": ("code", "sifra", "sifra artikla", "kod", "код", "шифра", "артикул"),
    "title": ("title", "name", "naziv", "stavka", "trosak", "название",
              "наименование", "статья", "статьярасхода", "расход"),
    "pnl": ("pnl", "p&l", "pnl line", "pnl item", "kategorija", "grupa",
            "строка p&l", "строка pnl", "строка", "категория", "группа"),
    "from": ("validfrom", "from", "od", "vazi od", "действуетс", "с"),
}


def _norm(value) -> str:
    """Заголовок без того, что не несёт смысла: регистра, пробелов и диакритики.

    Диакритика снимается намеренно: сербская латиница пишет «Šifra», а список
    известных заголовков держать в двух написаниях каждого слова — верный
    способ однажды добавить одно и забыть второе. Так же уравниваются «ё» и «е».
    """
    text = unicodedata.normalize("NFKD", str(value or "")).strip().lower()
    text = "".join(sign for sign in text if not unicodedata.combining(sign))
    # Остаются только буквы и цифры: «Строка P&L», «строка p&l» и «Строка-PL» —
    # один и тот же заголовок, и разбирать их порознь означало бы завести три
    # варианта одного слова и однажды забыть четвёртый.
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def code_from_title(title: str) -> str:
    """Код из названия — устойчиво, а не по номеру строки.

    Номер строки перестал бы совпадать при первой же строке, вставленной сверху,
    и повторная загрузка того же файла завела бы весь справочник заново. Название
    же меняется редко, а когда изменится — статья заведётся новой, и старая
    останется видимой в списке, а не исчезнет молча.
    """
    text = unicodedata.normalize("NFKC", title).strip().lower()
    text = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-")
    return text or "bez-naziva"


@dataclass(frozen=True)
class Row:
    """Строка файла, уже разобранная: что человек написал, без наших домыслов."""

    title: str
    code: str
    pnl: str
    valid_from: str
    line_no: int


def read_rows(handle) -> list[Row]:
    """Прочитать книгу и вернуть строки. Про базу здесь не знают ничего.

    Отказ бывает ровно двух видов, и оба — про файл целиком: «это не книга» и
    «в книге нет колонки с названием». Всё остальное решается построчно, потому
    что одна негодная строка не повод отвергнуть чужой справочник целиком.
    """
    import openpyxl

    try:
        book = openpyxl.load_workbook(handle, read_only=True, data_only=True)
    except Exception as broken:  # noqa: BLE001 — чужой файл бывает чем угодно
        # Разбирать исключения openpyxl по одному значило бы гадать за
        # библиотеку. Молчаливая пятисотка при этом недопустима: человек должен
        # прочитать, что именно не так с его файлом.
        raise FileRefused(
            _("Файл не удалось прочитать как книгу Excel: %(reason)s")
            % {"reason": broken}
        ) from broken

    sheet = book.active
    rows = list(sheet.iter_rows(values_only=True))
    header_at, columns = _find_header(rows)
    if "title" not in columns:
        raise FileRefused(_(
            "В файле не нашлась колонка с названием статьи. Назовите её "
            "«Название», «Naziv» или «Name» — по остальным колонкам продукт "
            "разберётся сам."
        ))

    found = []
    for offset, raw in enumerate(rows[header_at + 1:], start=header_at + 2):
        title = _cell(raw, columns.get("title"))
        if not title:
            continue    # пустая строка-разделитель: в чужих таблицах их много
        found.append(Row(
            title=title,
            code=_cell(raw, columns.get("code")) or code_from_title(title),
            pnl=_cell(raw, columns.get("pnl")),
            valid_from=_cell(raw, columns.get("from")),
            line_no=offset,
        ))
    return found


def _find_header(rows) -> tuple[int, dict[str, int]]:
    """Найти строку заголовков: первая, где узнаётся хоть одна наша колонка.

    Первая строка книги не обязана быть заголовком — над таблицей у людей часто
    стоит название документа или дата выгрузки. Поэтому ищется не «строка 1», а
    первая узнаваемая.
    """
    for index, raw in enumerate(rows[:20]):
        columns = {}
        for position, value in enumerate(raw or ()):
            key = _norm(value)
            for name, variants in COLUMNS.items():
                if key and key in {_norm(variant) for variant in variants}:
                    columns.setdefault(name, position)
        if columns:
            return index, columns
    return 0, {}


def _cell(raw, position) -> str:
    if position is None or raw is None or position >= len(raw):
        return ""
    value = raw[position]
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


# --- запись ---------------------------------------------------------------------


@dataclass
class Outcome:
    """Что случилось с файлом — числами и списками, а не одним словом «готово»."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def apply_rows(who, rows: list[Row], *, language: str,
               default_pnl_id=None, default_from: date) -> Outcome:
    """Завести или обновить статьи по строкам файла. Ничего не удаляет.

    `default_pnl_id` и `default_from` — то, чего в чужом файле может не быть
    вовсе: строка P&L и дата начала действия задаются один раз на форме и
    применяются ко всем строкам, где своих значений нет. Это не догадка за
    человека: он видит оба поля на форме и меняет их до загрузки.
    """
    outcome = Outcome()
    lines = _pnl_lines()
    existing = {item.code: item for item in ExpenseItem.objects.all()}
    seen = set()

    for row in rows:
        pnl_id = lines.get(_norm(row.pnl)) if row.pnl else default_pnl_id
        if pnl_id is None:
            # Строка P&L не опознана и умолчания нет: положить статью некуда, а
            # выбрать за человека — значит спрятать его же ошибку в данные.
            outcome.skipped.append((row.code, _("строка P&L не опознана: «%(value)s»")
                                    % {"value": row.pnl or "—"}))
            continue

        starts = default_from
        if row.valid_from:
            # Дата в файле есть — значит она и решает, а не умолчание формы.
            # Не разобрали — строка пропускается и называется, ровно как строка
            # с неопознанной строкой P&L выше: две ошибки одного класса в одном
            # файле обязаны вести себя одинаково (T155, находка Н1).
            starts = read_date(row.valid_from)
            if starts is None:
                outcome.skipped.append((row.code, _(
                    "дата «Действует с» не разобрана: «%(value)s». Пишите её как "
                    "01.06.2026 или 2026-06-01"
                ) % {"value": row.valid_from}))
                continue

        item = existing.get(row.code)
        if item is None:
            item = ExpenseItem(
                tenant_id=who.tenant_id, code=row.code,
                titles={language: row.title},
                pnl_item_id=pnl_id, valid_from=starts,
            )
            item.save()
            existing[row.code] = item
            outcome.created.append(row.code)
            seen.add(row.code)
            continue

        seen.add(row.code)
        titles = dict(item.titles or {})
        # Названия на других языках не трогаются: файл говорит на одном языке, а
        # статью у нас могли перевести. Затирать перевод строкой из чужого файла
        # значило бы терять работу, которую никто не просил отменять.
        if titles.get(language) == row.title and item.pnl_item_id == pnl_id:
            outcome.unchanged.append(row.code)
            continue
        titles[language] = row.title
        item.titles = titles
        # Строка P&L меняется только у статьи, которую ещё не утверждали месяцем:
        # эта привязка не версионируется, и её правка задевает закрытый месяц
        # (T108). Здесь загрузка ведёт себя осторожнее формы — она массовая, и
        # тихо переставить сотню статей нельзя.
        if item.pnl_item_id != pnl_id:
            outcome.skipped.append((row.code, _(
                "строка P&L у статьи уже другая — поменяйте её на карточке статьи"
            )))
            continue
        item.save()
        outcome.updated.append(row.code)

    outcome.kept = sorted(set(existing) - seen)
    return outcome


def _pnl_lines() -> dict:
    """Строки P&L, узнаваемые и по коду, и по названию.

    По названию — потому что в файле бухгалтера кода нашей строки P&L взяться
    неоткуда: она пишет «Коммунальные», а не `utilities`.
    """
    found = {}
    for line in PnlItem.objects.filter(kind__in=("expense", "transfer")):
        found[_norm(line.code)] = line.id
        found[_norm(line.title)] = line.id
    return found


# Запись даты, которую человек ведёт руками. ISO разбирается отдельно
# (`date.fromisoformat`), здесь — привычная запись Сербии и России: «01.06.2026»,
# «1.6.2026» и она же с точкой на конце, как её печатает сербский Excel.
#
# Слэшей в списке нет намеренно: «01/06/2026» — это и 1 июня, и 6 января, и
# выбрать между ними можно только предположением о стране автора файла.
# Угаданная не туда дата выглядит как прочитанная из файла, то есть это та же
# молчаливая подмена, ради которой задача заведена, — поэтому такая ячейка
# называется человеку, а не разбирается.
DOTTED = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\.?$")


def read_date(raw: str) -> date | None:
    """Дата из ячейки файла. `None` — «написано, но разобрать нечем».

    Пустую ячейку сюда не приносят: пусто и нечитаемо — **разные** события, и
    именно их одинаковая обработка была находкой Н1 восьмой сверки. В первом
    случае человек ничего не написал, и умолчание формы законно; во втором он
    написал, его не поняли — и промолчать об этом нельзя.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    dotted = DOTTED.match(text)
    if dotted is None:
        return None
    day, month, year = (int(part) for part in dotted.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None
