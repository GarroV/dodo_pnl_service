"""
Комментарии к колонкам ручной корректировки и выплаты наличными.

Отдельной миграцией, а не дописыванием в `0003_comments`: колонок на тот момент
ещё нет, и комментарий не на что вешать. Правило то же — неочевидная колонка без
комментария в схеме не живёт, читать её будут через `psql \\d+`, а не через
модели.
"""
from django.db import migrations

COLUMNS = {
    "timesheets.cash_payout": (
        "Выплата наличными за период. Отсюда payslips.to_cash; "
        "в таблице партнёра — столбец ISPLATA U KES"
    ),
    "timesheets.manual_correction": (
        "Ручная правка начисления. Пусто и ноль — разные вещи: пусто значит "
        "«правки не было» и движок считает доплату до минимума сам"
    ),
    "timesheets.correction_reason": (
        "Почему поправили. Обязательна при непустой правке — держится "
        "ограничением timesheets_correction_trace_check (D025)"
    ),
    "timesheets.corrected_by": "Кто поправил. Обязателен при непустой правке (D025)",
    "timesheets.corrected_at": "Когда поправили",
}


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


FORWARD = "\n".join(
    f"comment on column {column} is {_literal(text)};" for column, text in COLUMNS.items()
)

BACKWARD = "\n".join(f"comment on column {column} is null;" for column in COLUMNS)


class Migration(migrations.Migration):
    dependencies = [("core", "0007_timesheet_corrections")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
