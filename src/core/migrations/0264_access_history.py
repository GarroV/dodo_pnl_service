"""Роль на срок и история доступов, которую не переписать (T188, issue #178).

Эталон (модуль 11 и «Роли и права») требует от раздела доступов трёх вещей,
которых у продукта не было: приглашать человека из интерфейса, выдавать роль
**на срок** и вести историю выдач, которая не удаляется. Здесь — база под две
последние.

**Срок учитывает сама база, и это главное решение миграции.** Все шесть функций
контекста читают `memberships` своим запросом; каждая — отдельная дверь. Срок,
записанный в колонку, но не проверяемый функцией, не срок, а надпись: доступ
остаётся, а на вопрос «кончился ли он» отвечает тот, кто вовремя посмотрел.
Поэтому условие дописано в **каждую** из шести, и на каждую есть своя проверка
ролью `app_user`.

Граница включающая (`expires_at >= current_date`): «выдал до 31.07» человек
читает как «31-го ещё можно», и база обязана понимать это так же.

**История доступов закрыта на запись правами, а не политикой.** У `app_user`
нет `update` и `delete` на `access_log` вовсе. Политику можно написать неверно
и не заметить — на этом проект уже обжигался (`0242`, «сам себе роль»);
отсутствующее право отказывает всегда и одинаково. Единственная политика записи
разрешает вставку тому, кто ведёт роли, и **только под своим именем**: запись
«Х выдал роль» ценна ровно тем, что её физически не мог сделать не Х.

Читать историю вправе тот же, кто ведёт роли. Это имена людей партнёра и то,
кто кому что открывал, — тому, кто доступами не распоряжается, знать это
незачем.
"""

import django.db.models.deletion
from django.db import migrations, models

# --- срок членства в каждой двери --------------------------------------------
#
# Условие одно и то же и написано шесть раз намеренно: общая «функция живых
# членств» вернула бы нас к тому, от чего `0004` предостерегает прямо — политика
# на `memberships`, которой нужна политика на `memberships`. Проверено на живой
# базе в прошлой волне: рекурсия там ловится не ошибкой типа, а `stack depth
# limit exceeded`, и вылезает уже на площадке.
TERM = """
create or replace function app_tenant_ids()
returns setof uuid
language sql stable security definer
set search_path = public
as $$
    select m.tenant_id from memberships m
     where m.user_id = app_user_id()
       and (m.expires_at is null or m.expires_at >= current_date)
$$;

create or replace function app_visible_ledgers(p_tenant uuid)
returns ledger[]
language sql stable security definer
set search_path = public
as $$
    select coalesce(
        (select array_agg(distinct l)
           from memberships m
           join roles r on r.id = m.role_id
           cross join unnest(r.visible_ledgers) as l
          where m.user_id = app_user_id() and m.tenant_id = p_tenant
            and (m.expires_at is null or m.expires_at >= current_date)),
        '{}'::ledger[]
    )
$$;

create or replace function app_unit_ids(p_tenant uuid)
returns uuid[]
language sql stable security definer
set search_path = public
as $$
    with mine as (
        select m.unit_ids
          from memberships m
         where m.user_id = app_user_id() and m.tenant_id = p_tenant
           and (m.expires_at is null or m.expires_at >= current_date)
    )
    select case
        -- Членства нет — ни одной точки. Разведено явно: агрегат над нулём
        -- строк вернул бы null, то есть «все точки», и функция открыла бы
        -- чужого тенанта, если бы его когда-нибудь пропустила изоляция.
        when not exists (select 1 from mine) then '{}'::uuid[]
        -- Хотя бы одно членство без списка точек — доступ ко всем точкам.
        -- Просроченное таким членством уже не считается, иначе управляющий
        -- одной точки увидел бы все — самая тихая из здешних утечек.
        when exists (select 1 from mine where unit_ids is null) then null
        else (
            select coalesce(array_agg(distinct value), '{}'::uuid[])
              from mine, unnest(mine.unit_ids) as value
        )
    end
$$;

create or replace function app_has_permission(p_tenant uuid, p_code text)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1
          from memberships m
          join roles r on r.id = m.role_id
         where m.user_id = app_user_id()
           and m.tenant_id = p_tenant
           and r.permissions ? p_code
           and (m.expires_at is null or m.expires_at >= current_date)
    )
$$;

create or replace function app_manages_calendar(p_country text)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1
          from memberships m
          join roles r on r.id = m.role_id
          join tenants t on t.id = m.tenant_id
         where m.user_id = app_user_id()
           and r.permissions ? 'directory.manage'
           and t.country_code = p_country
           and (m.expires_at is null or m.expires_at >= current_date)
    )
$$;

create or replace function app_user_display_name(p_user uuid)
returns text
language sql stable security definer
set search_path = public
as $$
    select coalesce(nullif(u.full_name, ''), u.username)
      from users u
     where u.id = p_user
       and exists (
           select 1
             from memberships mine
             join memberships theirs on theirs.tenant_id = mine.tenant_id
            where mine.user_id = app_user_id()
              and theirs.user_id = p_user
              and (mine.expires_at is null or mine.expires_at >= current_date)
              and (theirs.expires_at is null or theirs.expires_at >= current_date)
       )
$$;

comment on column memberships.expires_at is
    'По какое число действует роль включительно. Пусто = навсегда. Учитывают все функции контекста: срок, который знает только экран, сроком не является (T188)';
"""

# Обратный ход возвращает функции к виду без срока. Колонка при этом остаётся —
# её убирает сам Django, — и это тот случай, когда откат ОБЯЗАН быть полным:
# функция со сроком поверх снесённой колонки не выполнилась бы вовсе.
TERM_BACK = """
create or replace function app_tenant_ids()
returns setof uuid
language sql stable security definer
set search_path = public
as $$
    select tenant_id from memberships where user_id = app_user_id()
$$;

create or replace function app_visible_ledgers(p_tenant uuid)
returns ledger[]
language sql stable security definer
set search_path = public
as $$
    select coalesce(
        (select array_agg(distinct l)
           from memberships m
           join roles r on r.id = m.role_id
           cross join unnest(r.visible_ledgers) as l
          where m.user_id = app_user_id() and m.tenant_id = p_tenant),
        '{}'::ledger[]
    )
$$;

create or replace function app_unit_ids(p_tenant uuid)
returns uuid[]
language sql stable security definer
set search_path = public
as $$
    with mine as (
        select unit_ids
          from memberships
         where user_id = app_user_id() and tenant_id = p_tenant
    )
    select case
        when not exists (select 1 from mine) then '{}'::uuid[]
        when exists (select 1 from mine where unit_ids is null) then null
        else (
            select coalesce(array_agg(distinct value), '{}'::uuid[])
              from mine, unnest(mine.unit_ids) as value
        )
    end
$$;

create or replace function app_has_permission(p_tenant uuid, p_code text)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1
          from memberships m
          join roles r on r.id = m.role_id
         where m.user_id = app_user_id()
           and m.tenant_id = p_tenant
           and r.permissions ? p_code
    )
$$;

create or replace function app_manages_calendar(p_country text)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1
          from memberships m
          join roles r on r.id = m.role_id
          join tenants t on t.id = m.tenant_id
         where m.user_id = app_user_id()
           and r.permissions ? 'directory.manage'
           and t.country_code = p_country
    )
$$;

create or replace function app_user_display_name(p_user uuid)
returns text
language sql stable security definer
set search_path = public
as $$
    select coalesce(nullif(u.full_name, ''), u.username)
      from users u
     where u.id = p_user
       and exists (
           select 1
             from memberships mine
             join memberships theirs on theirs.tenant_id = mine.tenant_id
            where mine.user_id = app_user_id()
              and theirs.user_id = p_user
       )
$$;
"""

HISTORY = """
comment on table access_log is
    'История доступов: кто, кому, когда и зачем. Не переписывается и не удаляется — у app_user нет на неё update и delete (T188, issue #178)';
comment on column access_log.role_title is
    'Снимок названия роли на момент выдачи: роль переименуют, а запись обязана читаться прежней';
comment on column access_log.until is
    'Снимок срока ТОЙ выдачи, а не того, что стоит в членстве сейчас';

alter table access_log enable row level security;
alter table access_log force  row level security;

-- Читает тот, кто ведёт роли. Это имена людей партнёра и то, кто кому что
-- открывал; тому, кто доступами не распоряжается, знать это незачем.
create policy roles_manager_read on access_log
    for select
    using (app_has_permission(tenant_id, 'roles.manage'));

-- Пишет тот же — и ТОЛЬКО под своим именем. Иначе запись «Х выдал роль»
-- перестаёт что-либо доказывать, а вся история существует ради доказательства.
create policy roles_manager_write on access_log
    for insert
    with check (
        app_has_permission(tenant_id, 'roles.manage')
        and actor_user_id = app_user_id()
    );
"""

# ── пригласить человека ───────────────────────────────────────────────────────
#
# `insert` на `users` роли приложения уже выдан (`0261`), но политики под него
# была ровно одна — платформенного администратора. Значит партнёр не мог завести
# своего сотрудника ни одним способом, кроме сида или рук в базе: ровно то, с
# чего начинается issue #178.
#
# Условие — «этот человек где-нибудь ведёт роли». Тенанта у `users` нет и быть
# не может: учётка одна на продукт, а членства у неё разные. Опасности в этом
# меньше, чем кажется: заведённая строка сама по себе не даёт ничего, полезной
# её делает членство, а членство закрыто своей политикой (`0242`) и своим
# тенантом.
#
# `app_tenant_ids()` вместо запроса к `memberships` намеренно: она security
# definer и уже отсекает просроченные членства, то есть срок роли действует и
# здесь — иначе вчерашний администратор продолжал бы заводить людей.
INVITE = """
create policy users_role_manager_invite on users
    for insert
    with check (exists (
        select 1 from app_tenant_ids() as t where app_has_permission(t, 'roles.manage')
    ));
"""

DROP_INVITE = "drop policy if exists users_role_manager_invite on users;"

DROP_HISTORY = """
drop policy if exists roles_manager_write on access_log;
drop policy if exists roles_manager_read on access_log;
alter table access_log no force row level security;
alter table access_log disable row level security;
"""

# Только чтение и вставка — и `revoke` обязателен, а не «просто не выдали».
# `0005_app_role` объявила `alter default privileges … grant select, insert,
# update, delete on tables`, то есть КАЖДАЯ новая таблица приезжает роли
# приложения с полным набором. Молчаливо рассчитывать на «мы это не выдавали»
# здесь нельзя: сама попытка так и прошла — правка истории меняла ноль строк
# без единого слова (политики нет, а право есть), то есть выглядела удавшейся.
PRIVILEGES = """
grant select, insert on access_log to app_user;
revoke update, delete on access_log from app_user;
"""
DROP_PRIVILEGES = "revoke all on access_log from app_user;"


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0263_permission_matrix'),
    ]

    operations = [
        migrations.AddField(
            model_name='membership',
            name='expires_at',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='AccessLogEntry',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('actor_user_id', models.UUIDField()),
                ('subject_user_id', models.UUIDField()),
                ('action', models.TextField()),
                ('role_id', models.UUIDField(blank=True, null=True)),
                ('role_title', models.TextField(db_default='')),
                ('until', models.DateField(blank=True, null=True)),
                ('reason', models.TextField(db_default='')),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
            ],
            options={
                'db_table': 'access_log',
                'indexes': [models.Index(models.F('tenant'), models.F('at'), name='access_log_recent')],
            },
        ),
        migrations.RunSQL(TERM, TERM_BACK),
        migrations.RunSQL(HISTORY, DROP_HISTORY),
        migrations.RunSQL(INVITE, DROP_INVITE),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
