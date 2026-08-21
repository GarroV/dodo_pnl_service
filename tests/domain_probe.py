"""Пробная строка в каждой таблице продукта — материал схемной проверки.

**Зачем это вообще есть.** Проверка «без контекста пользователя ни одна таблица
не отдаёт строк» (`test_no_domain_table_returns_rows_without_context`) считает
отданные строки. Таблица, в которой строк нет вовсе, отдаёт ноль при любой
политике — и с настоящей, и с `using (true)`, и без политики совсем. То есть
самые новые таблицы, которых ещё нет в наполнении фикстуры, попадали в перебор,
выглядели проверенными и не были проверены ничем (issue #63: ослабленную
политику на `payrun_jobs` не поймал ни один тест).

**Почему наполнитель, а не список исключений.** Список «эти таблицы разрешено
пропускать» пополняется молча и возвращает ту же слепоту через месяц. Здесь
наоборот: строка кладётся в **каждую** таблицу перебора, а таблица, куда
положить не удалось, называется по имени и валит проверку — то есть новая
таблица без политики роняет прогон сама, без того чтобы кто-то вспомнил о ней.

**Почему строка собирается из схемы, а не пишется руками для каждой таблицы.**
Таблиц в продукте больше тридцати, и растут они каждой очередью стройки. Список
инструкций «как заполнить вот эту» отставал бы от схемы ровно так же, как
отставало наполнение фикстуры, — и отставал бы молча. Поэтому берутся
обязательные колонки прямо из `information_schema`, ссылки — из существующих
строк, а руками задаются только те значения, которые схема отвергает
по смыслу (`MEANINGFUL_VALUES` ниже).

Строки живут внутри транзакции теста и исчезают вместе с ней: фикстура `db`
откатывает всё, что тест написал.
"""
from __future__ import annotations

# Обязательные колонки таблицы: без умолчания и без права быть пустыми.
COLUMNS = """
select column_name, is_nullable, column_default, data_type, udt_name
  from information_schema.columns
 where table_schema = 'public' and table_name = %s
 order by ordinal_position
"""

# Внешние ключи: чем заполнять колонки-ссылки. Без этого пробная строка
# упиралась бы в «нет такой строки в родительской таблице».
FOREIGN_KEYS = """
select kcu.column_name, ccu.table_name
  from information_schema.table_constraints tc
  join information_schema.key_column_usage kcu
    on kcu.constraint_name = tc.constraint_name
  join information_schema.constraint_column_usage ccu
    on ccu.constraint_name = tc.constraint_name
 where tc.constraint_type = 'FOREIGN KEY' and tc.table_schema = 'public'
   and tc.table_name = %s
"""

# Значения, которые схема отвергает по смыслу, а не по типу. Каждое — с
# причиной: список без причин однажды станет местом, куда дописывают, чтобы
# «прошло», и наполнитель перестанет быть проверкой.
MEANINGFUL_VALUES: dict[str, dict[str, str]] = {
    # Название статьи расхода не бывает пустым (`expense_items_titles_not_empty`):
    # статья без подписи не показывается ни на одном экране.
    "expense_items": {"titles": """'{"ru": "Проба"}'::jsonb"""},
    # Валюта факта — код из трёх букв (`facts_currency_is_code`).
    "facts": {"currency": "'RSD'"},
    # Заморозка строки ведомости кладётся сразу снятой. Живая заморозка
    # запрещает запись в `payslip_totals` и `pay_components` (T027) — то есть
    # пробная строка одной таблицы не пустила бы пробную строку соседней, и
    # проверка молча потеряла бы две таблицы.
    "payslip_freezes": {"released_at": "now()"},
    # Содержимое принесённой бумаги — `bytea`, и пустым оно быть не может
    # (`document_files_size_is_positive`): файл нулевой длины — это не файл, а
    # неудавшаяся загрузка, и хранить его как принятую бумагу нельзя. Типового
    # значения для `bytea` у пробника нет намеренно: двоичных колонок в схеме
    # одна, и придумывать под неё правило дороже, чем назвать значение здесь.
    "document_files": {"content": r"'\x00'::bytea", "byte_size": "1"},
}


def _value(conn, data_type: str, udt: str) -> str:
    """Значение по типу колонки. Смысла в нём нет и не нужно: проверка смотрит,
    видна ли строка без контекста пользователя, а не что в ней написано."""
    if data_type == "ARRAY":
        return "'{}'"
    if udt == "uuid":
        return "gen_random_uuid()"
    if data_type in ("text", "character varying"):
        return "'проба'"
    if data_type in ("numeric", "integer", "bigint", "smallint", "double precision"):
        return "0"
    if data_type == "date":
        return "date '2026-06-01'"
    if data_type.startswith("timestamp"):
        return "now()"
    if data_type == "jsonb":
        return "'{}'::jsonb"
    if data_type == "boolean":
        return "false"
    if data_type == "USER-DEFINED":
        # Свой enum-тип: берём первое значение — какое именно, для проверки
        # безразлично.
        label = conn.execute(
            "select enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid"
            " where t.typname = %s order by e.enumsortorder limit 1",
            (udt,),
        ).fetchone()
        if label:
            return f"'{label[0]}'::{udt}"
    return "null"


def _insert_statement(conn, table: str) -> str | None:
    """Собрать вставку одной строки. None — не из чего: не хватает ссылок."""
    keys = {row[0]: row[1] for row in conn.execute(FOREIGN_KEYS, (table,)).fetchall()}
    by_meaning = MEANINGFUL_VALUES.get(table, {})
    columns: list[str] = []
    values: list[str] = []

    for name, nullable, default, data_type, udt in conn.execute(COLUMNS, (table,)).fetchall():
        if name in by_meaning:
            columns.append(name)
            values.append(by_meaning[name])
            continue
        if name in keys:
            reference = keys[name]
            if conn.execute(f"select 1 from {reference} limit 1").fetchone() is None:
                # Родительская таблица ещё пуста — попробуем в следующий заход,
                # когда пробная строка появится и у неё.
                if nullable == "NO":
                    return None
                continue
            columns.append(name)
            values.append(f"(select id from {reference} limit 1)")
            continue
        if nullable == "YES" or default is not None:
            continue
        columns.append(name)
        values.append(_value(conn, data_type, udt))

    if not columns:
        # Таблица, где заполнять нечего: все колонки с умолчанием или пустые.
        # Строка из одних умолчаний — тоже строка, а проверке важно только её
        # наличие. Без этого такая таблица считалась бы «нечем наполнить» и
        # выпадала бы из проверки — то есть слепота вернулась бы с другой
        # стороны.
        return f"insert into {table} default values"
    return f"insert into {table} ({', '.join(columns)}) values ({', '.join(values)})"


def fill_empty_tables(conn, tables: list[str]) -> dict[str, str]:
    """Положить по строке в каждую пустую таблицу из списка.

    Возвращает причины отказа по тем, куда положить не удалось: их называет
    проверка, чтобы непроверенная таблица была видна по имени, а не пропущена.

    Заходов несколько: ссылки бывают на таблицы, которые сами наполняются здесь
    же (факт ссылается на партию, партия — на партнёра). Проще повторить обход,
    чем держать вручную выписанный порядок, который разъедется со схемой.
    """
    pending = [
        table for table in tables
        if conn.execute(f"select count(*) from {table}").fetchone()[0] == 0
    ]
    failures: dict[str, str] = {}

    while pending:
        rest: list[str] = []
        failures = {}
        for table in pending:
            statement = _insert_statement(conn, table)
            if statement is None:
                failures[table] = (
                    "не из чего собрать строку: пусты таблицы, на которые она ссылается"
                )
                rest.append(table)
                continue
            try:
                # Точка сохранения на каждую таблицу: отказ одной не должен
                # рвать транзакцию теста и уносить с собой уже вставленное.
                with conn.transaction():
                    conn.execute(statement)
            except Exception as exc:  # noqa: BLE001 — причина уходит в сообщение проверки
                failures[table] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                rest.append(table)
        if len(rest) == len(pending):
            # Заход не сдвинул дело с места — дальше повторять нечего.
            break
        pending = rest

    return failures
