"""
Комментарии к таблицам и неочевидным колонкам.

Отдельной миграцией, потому что Django переносит в базу только `db_comment`,
а объяснять надо не «что это», а «почему так». Через полгода никто не вспомнит,
откуда взялось разделение регистров или зачем у сотрудника внешний ключ.
"""
from django.db import migrations

TABLES = {
    "tenants": "Партнёр. Единица изоляции данных: всё остальное ссылается сюда",
    "legal_entities": "Юрлицо партнёра. Бухгалтерия работает с ним, пиццерий для неё нет",
    "units": "Точка = пиццерия. Расходы разносятся на неё, а не на юрлицо",
    "roles": "Роль. Она же решает, какие регистры учёта человек видит",
    "memberships": "Кто, в каком тенанте, с какой ролью и по каким точкам",
    "calendars": "Производственный календарь страны на месяц",
    "pnl_items": "Статьи P&L. Единый справочник по сети — цель проекта",
    "counterparties": "Контрагенты. «Как его пишут в разных системах» — это данные, не память",
    "allocation_rules": "Правила разнесения расхода по точкам, версионируются по датам",
    "fx_rates": "Курсы валют на дату",
    "periods": "Учётный месяц тенанта и его состояние",
    "rule_presets": "Готовый набор правил страны. Новая страна = новый пресет, не новый код",
    "rule_overrides": "Переопределения поверх пресета: страна → партнёр → группа → человек",
    "employee_groups": "Группы сотрудников: схема расчёта и регистр учёта по умолчанию",
    "employees": "Сотрудники партнёра",
    "employment_terms": "Условия найма на период: ставка, коэффициент, группа, точка",
    "timesheets": "Часы за период по сотруднику",
    "payruns": "Расчёт зарплаты за месяц целиком",
    "payslips": "Строка ведомости по сотруднику",
    "pay_components": "Атом расчёта: из компонентов собирается и ведомость, и строки P&L",
}

COLUMNS = {
    "tenants.report_currency": "Консолидация по сети идёт в этой валюте, по курсу на конец месяца",
    "units.external_ids": "Идентификаторы точки в чужих системах: Dodo IS и прочие",
    "roles.tenant_id": "null = системная роль, общая для всех тенантов",
    "roles.visible_ledgers": "Регистры учёта, доступные роли. На этом держится RLS видимости",
    "memberships.user_id": "Пользователь блока auth. Голый uuid: схема не зависит от вида учётки",
    "memberships.unit_ids": "null = все точки тенанта",
    "pnl_items.tenant_id": "null = общий справочник статей, одинаковый для всей сети",
    "pnl_items.parent_id": "Дерево: подытог собирается из детей",
    "counterparties.aliases": "Написания названия в разных системах — по ним идёт сопоставление",
    "allocation_rules.unit_id": "Обязателен только для метода fixed_unit",
    "allocation_rules.ledger": "Регистр учёта правила: видимость закрыта ограничивающей политикой",
    "employees.external_id": "Сквозной ключ (в Сербии JMBG). ФИО между системами не совпадают",
    "employment_terms.scheme": "Переопределяет схему расчёта группы",
    "employment_terms.ledger": "Переопределяет регистр учёта группы",
    "timesheets.insured_hours": "База для взносов, может отличаться от суммы отработанных часов",
    "timesheets.hours": "Часы по типам: {regular: 176, sick: 20, ...}",
    "timesheets.source": "Откуда приехали часы: manual | dodo_is | import",
    "rule_presets.body": "Тело пресета — то, что сейчас лежит в YAML",
    "rule_overrides.path": "Путь в пресете, напр. hour_types.night.pay_percent",
    "pay_components.channel": "Банк или касса. Надбавка наличными — не особый случай, а канал",
    "pay_components.ledger": "Регистр учёта компонента: видимость закрыта ограничивающей политикой",
}


def _literal(text: str) -> str:
    """Строковый литерал SQL. Комментарии — DDL, параметры туда не подставить."""
    return "'" + text.replace("'", "''") + "'"


def _sql() -> str:
    lines = [
        f"comment on table {table} is {_literal(comment)};" for table, comment in TABLES.items()
    ]
    lines += [
        f"comment on column {column} is {_literal(comment)};"
        for column, comment in COLUMNS.items()
    ]
    return "\n".join(lines)


def _drop_sql() -> str:
    lines = [f"comment on table {table} is null;" for table in TABLES]
    lines += [f"comment on column {column} is null;" for column in COLUMNS]
    return "\n".join(lines)


class Migration(migrations.Migration):
    dependencies = [("core", "0002_initial")]

    operations = [migrations.RunSQL(_sql(), _drop_sql())]
