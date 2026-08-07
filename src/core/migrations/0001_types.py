"""
Расширения и словарные типы домена.

Django нативными enum-типами управлять не умеет, поэтому они создаются руками
и до таблиц: колонки на них ссылаются. Выбор в пользу enum, а не текста
с проверкой, сознательный — чужое значение регистра учёта должна отвергать
база, а не надежда на приложение.
"""
from django.db import migrations

FORWARD = """
create extension if not exists "pgcrypto";
-- btree_gist нужен ограничениям EXCLUDE на непересечение периодов действия
-- правил: обычный btree в GiST-ограничение не годится.
create extension if not exists "btree_gist";

-- Регистры учёта. Свойство сотрудника, группы или операции — не компании.
create type ledger as enum ('official', 'supplementary', 'internal');

-- Куда уходит выплата. Надбавка наличными — не особый случай, а канал.
create type payout_channel as enum ('bank', 'cash');

-- Как разносить расход по точкам.
create type allocation_method as enum (
    'fixed_unit',      -- всегда конкретная точка
    'ask',             -- спрашивать каждый раз
    'even',            -- поровну между точками
    'by_revenue'       -- пропорционально выручке
);

create type period_status as enum ('open', 'review', 'closed');
create type payrun_status as enum ('draft', 'calculated', 'approved', 'paid');

-- Уровень, на котором задано переопределение правила.
create type rule_scope as enum ('country', 'tenant', 'group', 'employee');
"""

BACKWARD = """
drop type if exists rule_scope;
drop type if exists payrun_status;
drop type if exists period_status;
drop type if exists allocation_method;
drop type if exists payout_channel;
drop type if exists ledger;
"""


class Migration(migrations.Migration):
    initial = True

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
