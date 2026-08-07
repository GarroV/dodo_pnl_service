"""
Разграничение доступа: функции контекста и политики RLS.

Единственный источник истины о том, кто что видит. Движок политик в коде дал бы
два расходящихся ответа на один вопрос; при RLS забытый фильтр в новом отчёте
даёт пустой результат, а не утечку.

Две вещи, без которых всё это — украшение:

1. `force row level security`. Политики по умолчанию НЕ действуют на владельца
   таблиц, а миграции и фон ходят как раз владельцем. Без force можно написать
   любые правила и получить зелёные тесты.
2. Политики видимости регистров — `as restrictive`. Пермиссивные политики
   Postgres объединяет через OR, поэтому «регистр видим» не сужало бы выборку
   вообще: строку своего тенанта пропустила бы политика изоляции. Дефект
   подтверждён экспериментом на живой базе 2026-08-06 — здесь он не повторён.

Суперпользователь обходит RLS всегда, включая force. Это осознанно: сид,
восстановление из дампа и обслуживание идут именно им.

3. Общие строки (`tenant_id is null`) — системные роли и общий справочник
   статей — читаются всеми, у кого есть контекст, но пишутся только в обход
   политик (миграцией или сидом). `null in (select ...)` даёт null, поэтому без
   отдельной ветки такие строки не видел никто; без проверки на `app_user_id()`
   они стали бы видны и без контекста, а это уже публикация справочника.
"""
from django.db import migrations

# Таблицы с tenant_id. Изоляция включается на всех разом: список в одном месте,
# чтобы новая таблица не оказалась незакрытой по недосмотру.
TENANT_TABLES = [
    "legal_entities", "units", "roles", "memberships", "pnl_items",
    "counterparties", "allocation_rules", "periods", "rule_overrides",
    "employee_groups", "employees", "employment_terms", "timesheets",
    "payruns", "payslips", "pay_components",
]

# Таблицы, где у строки есть собственный регистр учёта.
LEDGER_TABLES = ["pay_components", "allocation_rules"]

# Таблицы, где `tenant_id is null` — это не «ничьё», а «общее для всех»:
# системные роли и общий справочник статей P&L. Единый справочник — цель
# проекта, поэтому такие строки обязаны быть видны каждому, у кого есть
# контекст. Правило читается только на чтение: править общее из приложения
# нельзя, иначе пользователь одного партнёра менял бы справочник всей сети.
SHARED_ROW_TABLES = ["roles", "pnl_items"]

FUNCTIONS = """
-- Единственная точка привязки к системе входа. Приложение выставляет GUC
-- внутри транзакции запроса: set_config('app.user_id', ..., true). Настройка
-- живёт до конца транзакции, поэтому не может протечь между запросами в пуле.
create or replace function app_user_id()
returns uuid
language sql stable
as $$
    -- true во втором аргументе = не падать, если переменная не выставлена
    select nullif(current_setting('app.user_id', true), '')::uuid
$$;

comment on function app_user_id() is
    'Текущий пользователь приложения. Контекст не выставлен — null, и все политики дают пустоту';

-- Тенанты, где пользователь состоит.
-- security definer обязателен: на memberships висит своя RLS, иначе политика
-- звала бы функцию, которой нужна та же политика.
create or replace function app_tenant_ids()
returns setof uuid
language sql stable security definer
set search_path = public
as $$
    select tenant_id from memberships where user_id = app_user_id()
$$;

comment on function app_tenant_ids() is
    'Тенанты текущего пользователя. На этом стоит изоляция данных партнёров';

-- Регистры учёта, видимые пользователю в этом тенанте. Для чужого тенанта
-- членства нет, поэтому возвращается пустой массив — и ни одна строка не пройдёт.
create or replace function app_visible_ledgers(p_tenant uuid)
returns accounting_layer[]
language sql stable security definer
set search_path = public
as $$
    select coalesce(
        (select array_agg(distinct l)
           from memberships m
           join roles r on r.id = m.role_id
           cross join unnest(r.visible_layers) as l
          where m.user_id = app_user_id() and m.tenant_id = p_tenant),
        '{}'::accounting_layer[]
    )
$$;

comment on function app_visible_ledgers(uuid) is
    'Регистры учёта, видимые пользователю в тенанте. Тип переименуется вместе с регистрами (T004)';
"""

DROP_FUNCTIONS = """
drop function if exists app_visible_ledgers(uuid);
drop function if exists app_tenant_ids();
drop function if exists app_user_id();
"""


def _tenant_policies() -> str:
    parts = []
    for table in TENANT_TABLES:
        # `null in (select ...)` даёт null, то есть «не проходит»: без этой
        # добавки общие строки не видны никому, включая того, для кого они
        # заведены. Проверка на app_user_id() обязательна — «общее» не значит
        # «публичное», без контекста выборка обязана остаться пустой.
        shared = (
            " or (tenant_id is null and app_user_id() is not null)"
            if table in SHARED_ROW_TABLES
            else ""
        )
        parts.append(f"""
alter table {table} enable row level security;
alter table {table} force row level security;
create policy tenant_isolation on {table}
    for all
    using (tenant_id in (select app_tenant_ids()){shared})
    with check (tenant_id in (select app_tenant_ids()));
""")
    for table in LEDGER_TABLES:
        # as restrictive: объединяется с изоляцией через AND, то есть реально сужает.
        parts.append(f"""
create policy ledger_visibility on {table}
    as restrictive for select
    using (layer = any (app_visible_ledgers(tenant_id)));
""")
    return "\n".join(parts)


def _drop_tenant_policies() -> str:
    parts = []
    for table in LEDGER_TABLES:
        parts.append(f"drop policy if exists ledger_visibility on {table};")
    for table in TENANT_TABLES:
        parts.append(f"drop policy if exists tenant_isolation on {table};")
        parts.append(f"alter table {table} no force row level security;")
        parts.append(f"alter table {table} disable row level security;")
    return "\n".join(parts)


# Справочники без тенанта: пресеты, календарь и курсы одинаковы для всех,
# читать может любой, у кого есть контекст.
SHARED_TABLES = ["rule_presets", "calendars", "fx_rates"]

SHARED = "\n".join(
    f"""
alter table {table} enable row level security;
alter table {table} force row level security;
create policy read_all on {table} for select using (app_user_id() is not null);
"""
    for table in SHARED_TABLES
) + """
-- tenants: пользователь видит только те, где состоит
alter table tenants enable row level security;
alter table tenants force row level security;
create policy own_tenants on tenants
    for select using (id in (select app_tenant_ids()));
"""

DROP_SHARED = "\n".join(
    f"""
drop policy if exists read_all on {table};
alter table {table} no force row level security;
alter table {table} disable row level security;
"""
    for table in SHARED_TABLES
) + """
drop policy if exists own_tenants on tenants;
alter table tenants no force row level security;
alter table tenants disable row level security;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0003_comments")]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP_FUNCTIONS),
        migrations.RunSQL(_tenant_policies(), _drop_tenant_policies()),
        migrations.RunSQL(SHARED, DROP_SHARED),
    ]
