"""Первичное наполнение справочника статей файлом бухгалтера (T147, D041).

Ответ владельца на Q015: «Берём за основу её справочник, но должна быть
возможность редактировать и дополнять». Правка с экрана уже есть (T108) —
недостаёт того, с чего справочник начинается: списка, который бухгалтер ведёт у
себя. Вводить его руками по одной статье означало бы переписать чужую таблицу
вручную и ошибиться в паре названий, а расхождение вылезет только при первой
сборке P&L.

**Настоящего образца файла у нас нет.** Он у бухгалтера Сербии, и до ответа
неизвестно ни как называются её колонки, ни в каком они порядке. Поэтому разбор
терпимый: колонки ищутся по заголовку среди известных названий на трёх языках,
порядок и регистр значения не имеют, лишние колонки не мешают. Обязательна одна
— название статьи; всё остальное либо в файле, либо задаётся на форме загрузки
один раз для всего файла.

Три правила, каждое проверено отдельно:

**1. Повторная загрузка того же файла не плодит дублей.** Сходятся строки по
коду — тому самому, про который в форме статьи написано «по нему статья сходится
с файлом бухгалтера при загрузке». Кода в файле нет — он берётся из названия
устойчиво, а не считается заново от порядка строк.

**2. Строки, которых в файле нет, не удаляются молча.** Их судьбу решает
человек: файл бухгалтера — не полная правда о справочнике, в нём может не быть
статей, заведённых у нас позже. Продукт называет такие статьи вслух и оставляет.

**3. Файл, который разобрать нельзя, отвергается словами.** Не пятисоткой и не
половиной загруженного: наполовину загруженный справочник хуже пустого, потому
что незаметен.
"""
from __future__ import annotations

import io

import openpyxl
import pytest

from conftest import body, login_as
from test_directory import sql  # noqa: F401
from test_expense_items_screen import items_removed  # noqa: F401

UPLOAD_URL = "/directory/expense-items/upload/"
LIST_URL = "/directory/expense-items/"


def book(rows: list[list], sheet_title: str = "Troškovi") -> bytes:
    """Собрать книгу Excel из строк — то, что приносит человек."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def upload(client, raw: bytes, name: str = "troskovi.xlsx", **extra):
    payload = {"file": io.BytesIO(raw)}
    payload["file"].name = name
    payload.update(extra)
    # `follow=True`: после успешной загрузки продукт уводит человека обратно на
    # справочник (POST → redirect → GET), и итог загрузки написан уже там.
    # Без перехода тест читал бы пустое тело редиректа и проверял бы не то.
    return client.post(UPLOAD_URL, payload, follow=True)


def codes(sql) -> list[str]:  # noqa: F811
    return [
        row[0] for row in sql.execute(
            "select code from expense_items order by code"
        ).fetchall()
    ]


# Образец: три колонки, названия по-сербски, порядок «как у человека».
SAMPLE = [
    ["Šifra", "Naziv", "P&L"],
    ["voda", "Voda", "food_cost"],
    ["struja", "Struja", "food_cost"],
    ["popravka", "Popravka opreme", "food_cost"],
]


# --- право --------------------------------------------------------------------


@pytest.mark.parametrize("role", ["director", "accountant", "manager"])
def test_a_role_without_the_right_cannot_load_the_directory(client, sql, role, items_removed):  # noqa: F811
    """Загрузка — ведение справочника, и право у неё то же, что у формы."""
    login_as(client, role)
    try:
        answer = upload(client, book(SAMPLE))
        assert answer.status_code == 403, answer.status_code
        assert codes(sql) == [], "справочник наполнился без права"
    finally:
        client.post("/logout/")


# --- 1. идемпотентность по коду -------------------------------------------------


def test_the_file_fills_the_empty_directory(client, sql, items_removed):  # noqa: F811
    """Три строки файла — три статьи, и человек читает, что именно завелось."""
    login_as(client, "admin")
    try:
        answer = upload(client, book(SAMPLE))
        assert answer.status_code in (200, 302), body(answer)
        assert codes(sql) == ["popravka", "struja", "voda"], codes(sql)
    finally:
        client.post("/logout/")


def test_the_same_file_loaded_twice_makes_no_duplicates(client, sql, items_removed):  # noqa: F811
    """Второй раз тот же файл — ни одной новой статьи (D041).

    Сломайте сходимость по коду — и справочник удвоится на первой же повторной
    загрузке, а бухгалтер увидит каждую статью дважды и не поймёт, какая из них
    настоящая.
    """
    login_as(client, "admin")
    try:
        upload(client, book(SAMPLE))
        first = codes(sql)
        page = body(upload(client, book(SAMPLE)))
        assert codes(sql) == first, codes(sql)
        assert "3" in page, "не сказано, что все три строки уже были"
    finally:
        client.post("/logout/")


def test_a_changed_title_updates_the_item_instead_of_adding_one(
    client, sql, items_removed,  # noqa: F811
):
    """Правка в файле — правка статьи, а не вторая статья с тем же смыслом."""
    login_as(client, "admin")
    try:
        upload(client, book(SAMPLE))
        changed = [row[:] for row in SAMPLE]
        changed[1][1] = "Voda i piće"
        upload(client, book(changed))

        assert codes(sql) == ["popravka", "struja", "voda"], codes(sql)
        titles = sql.execute(
            "select titles from expense_items where code = 'voda'"
        ).fetchone()[0]
        assert "Voda i piće" in titles.values(), titles
    finally:
        client.post("/logout/")


def test_a_file_without_a_code_column_still_lands_and_stays_idempotent(
    client, sql, items_removed,  # noqa: F811
):
    """Кода в файле нет — он берётся из названия, и повтор всё равно не плодит.

    Иначе загрузка файла с одними названиями была бы одноразовой: второй раз она
    завела бы те же статьи ещё раз, потому что сходиться было бы не по чему.
    """
    login_as(client, "admin")
    try:
        rows = [["Naziv"], ["Voda"], ["Struja"]]
        upload(client, book(rows), pnl_item=_line(sql))
        first = codes(sql)
        assert len(first) == 2, first

        upload(client, book(rows), pnl_item=_line(sql))
        assert codes(sql) == first, codes(sql)
    finally:
        client.post("/logout/")


def _line(sql) -> str:  # noqa: F811
    return str(sql.execute(
        "select id from pnl_items where code = 'food_cost'"
    ).fetchone()[0])


# --- терпимость разбора -----------------------------------------------------------


def test_the_order_and_the_case_of_the_columns_do_not_matter(
    client, sql, items_removed,  # noqa: F811
):
    """Колонки ищутся по заголовку, а не по номеру: чужой файл не обязан быть нашим."""
    login_as(client, "admin")
    try:
        answer = upload(client, book([
            ["  НАЗВАНИЕ ", "Строка P&L", "код"],
            ["Вода", "food_cost", "voda"],
        ]))
        assert answer.status_code in (200, 302), body(answer)
        assert codes(sql) == ["voda"], codes(sql)
    finally:
        client.post("/logout/")


def test_an_unknown_pnl_line_skips_the_row_and_says_which(
    client, sql, items_removed,  # noqa: F811
):
    """Одна негодная строка не отменяет файл, но и не исчезает молча."""
    login_as(client, "admin")
    try:
        page = body(upload(client, book([
            ["Šifra", "Naziv", "P&L"],
            ["voda", "Voda", "food_cost"],
            ["neznano", "Nešto", "такой-строки-нет"],
        ])))
        assert codes(sql) == ["voda"], codes(sql)
        assert "neznano" in page, "пропущенная строка не названа"
    finally:
        client.post("/logout/")


def test_a_file_without_titles_is_refused_in_words(client, sql, items_removed):  # noqa: F811
    """Без колонки названия загружать нечего — и сказать об этом надо до записи."""
    login_as(client, "admin")
    try:
        answer = upload(client, book([["Šifra", "P&L"], ["voda", "food_cost"]]))
        assert answer.status_code == 400, answer.status_code
        assert "Название" in body(answer), body(answer)
        assert codes(sql) == [], "часть файла всё-таки записалась"
    finally:
        client.post("/logout/")


def test_something_that_is_not_a_workbook_is_refused_in_words(
    client, sql, items_removed,  # noqa: F811
):
    """Не книга Excel — отказ словами, а не белая страница с трассировкой."""
    login_as(client, "admin")
    try:
        answer = upload(client, b"this is not a workbook at all", name="spisak.xlsx")
        assert answer.status_code == 400, answer.status_code
        assert codes(sql) == []
    finally:
        client.post("/logout/")


def test_the_upload_without_a_file_says_so(client, sql, items_removed):  # noqa: F811
    login_as(client, "admin")
    try:
        answer = client.post(UPLOAD_URL, {})
        assert answer.status_code == 400, answer.status_code
    finally:
        client.post("/logout/")


# --- 2. лишнее в справочнике не пропадает ------------------------------------------


def test_items_missing_from_the_file_are_kept_and_named(client, sql, items_removed):  # noqa: F811
    """Статьи, которых в файле нет, остаются — и человек про них читает (D041).

    Файл бухгалтера не полная правда о справочнике: у нас могли завести статью
    позже. Молчаливое удаление означало бы, что расходы, на неё ссылающиеся,
    остались без статьи, — и заметили бы это через месяц.
    """
    login_as(client, "admin")
    try:
        upload(client, book(SAMPLE))
        page = body(upload(client, book([
            ["Šifra", "Naziv", "P&L"],
            ["voda", "Voda", "food_cost"],
        ])))
        assert codes(sql) == ["popravka", "struja", "voda"], codes(sql)
        assert "struja" in page and "popravka" in page, page
    finally:
        client.post("/logout/")


def test_the_screen_offers_the_upload(client, items_removed):  # noqa: F811
    """Кнопка загрузки живёт на самом справочнике, а не по прямому адресу."""
    login_as(client, "admin")
    try:
        assert UPLOAD_URL in body(client.get(LIST_URL))
    finally:
        client.post("/logout/")


# --- 3. дата «Действует с» из файла: разобрана или названа --------------------
#
# Находка Н5/Н1 восьмой сверки (T155). Дата в файле бухгалтера написана как
# `01.06.2026`, `date.fromisoformat` её не принимает — и продукт молча подставлял
# умолчание формы (`2020-01-01`), не сказав об этом ни слова: ни в сообщении, ни
# в списке пропущенных. Соседняя негодная ячейка того же файла (строка P&L)
# называется поимённо, то есть две ошибки одного класса обрабатывались
# противоположно.
#
# Цена молчания не косметическая: `cash.items_on` отбирает статьи по дате, и
# статья, заведённая бухгалтером с июня, после загрузки предлагалась и
# принималась для трат за любой прошлый месяц, включая закрытые. Отличить
# «дату взяли из файла» от «дату подставили за меня» человек не мог никак.


def dates(sql) -> dict:  # noqa: F811
    return {
        code: valid_from
        for code, valid_from in sql.execute(
            "select code, valid_from from expense_items order by code"
        ).fetchall()
    }


@pytest.mark.parametrize("written", ["01.06.2026", "2026-06-01", "1.6.2026."])
def test_the_date_from_the_file_is_read_not_replaced(
    client, sql, items_removed, written,  # noqa: F811
):
    """ГЛАВНАЯ ПРОВЕРКА: дата из файла доезжает до статьи, какой бы записью её ни вели.

    `дд.мм.гггг` — обычная запись даты и в Сербии, и в России, а не экзотика;
    сербский Excel пишет её ещё и с точкой на конце («01.06.2026.»).
    """
    from datetime import date

    login_as(client, "admin")
    try:
        upload(client, book([
            ["Šifra", "Naziv", "P&L", "Važi od"],
            ["voda", "Voda", "food_cost", written],
        ]), valid_from="2020-01-01")
        assert dates(sql).get("voda") == date(2026, 6, 1), (
            f"«{written}»: дата подменена умолчанием формы, а не прочитана из файла"
        )
    finally:
        client.post("/logout/")


def test_an_unreadable_date_is_named_and_the_row_is_skipped(
    client, sql, items_removed,  # noqa: F811
):
    """Нечитаемая дата называется человеку так же, как непонятая строка P&L.

    И статья с ней не заводится: тихо подставленное умолчание — это ошибка,
    которую нельзя увидеть никогда.
    """
    login_as(client, "admin")
    try:
        page = body(upload(client, book([
            ["Šifra", "Naziv", "P&L", "Važi od"],
            ["voda", "Voda", "food_cost", "когда-нибудь"],
            ["struja", "Struja", "food_cost", "01.07.2026"],
        ]), valid_from="2020-01-01"))

        assert "voda" in page, f"негодная дата не названа человеку:\n{page[:1500]}"
        assert "когда-нибудь" in page, "не сказано, что именно не разобрано"
        assert "voda" not in dates(sql), "статья с негодной датой всё-таки завелась"
        # Соседняя годная строка при этом загружена: одна негодная ячейка не
        # повод отвергнуть чужой справочник целиком.
        assert "struja" in dates(sql), "годная строка не загрузилась"
    finally:
        client.post("/logout/")


def test_an_empty_date_still_takes_the_default_from_the_form(
    client, sql, items_removed,  # noqa: F811
):
    """Пустая ячейка — не ошибка: человек ничего не написал, и умолчание законно.

    Разница между «не написал» и «написал, а его не поняли» и есть вся суть
    находки: раньше эти два случая обрабатывались одинаково.
    """
    from datetime import date

    login_as(client, "admin")
    try:
        upload(client, book([
            ["Šifra", "Naziv", "P&L", "Važi od"],
            ["voda", "Voda", "food_cost", ""],
        ]), valid_from="2021-03-01")
        assert dates(sql).get("voda") == date(2021, 3, 1), dates(sql)
    finally:
        client.post("/logout/")


def test_an_ambiguous_slash_date_is_named_rather_than_guessed(
    client, sql, items_removed,  # noqa: F811
):
    """`01/06/2026` — это 1 июня или 6 января: продукт не гадает, а спрашивает.

    Угадать здесь нельзя ничем, кроме предположения о стране автора файла, и
    угаданная не туда дата — та же молчаливая ошибка, ради которой задача и
    заведена, только на шаг хитрее: она выглядит как прочитанная из файла.
    """
    login_as(client, "admin")
    try:
        page = body(upload(client, book([
            ["Šifra", "Naziv", "P&L", "Važi od"],
            ["voda", "Voda", "food_cost", "01/06/2026"],
        ]), valid_from="2020-01-01"))
        assert "voda" in page and "01/06/2026" in page, page[:1500]
        assert "voda" not in dates(sql), "дата со слэшами всё-таки угадана"
    finally:
        client.post("/logout/")
