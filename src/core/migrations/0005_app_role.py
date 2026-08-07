"""
Роль приложения `app_user`.

Зачем отдельная роль: RLS не действует на владельца таблиц (без force) и никогда
не действует на суперпользователя. В типовом образе Postgres всё ходит ролью
`postgres` — с ней политики можно написать как угодно неправильно и получить
зелёные тесты. Поэтому приложение обязано ходить ролью, которая:

- не суперпользователь и без `bypassrls`;
- не владеет ни одной таблицей;
- имеет ровно права на данные, без DDL.

Роль групповая (`nologin`) и выдаётся тому, кто подключается: сервис делает
`set local role app_user` в транзакции запроса вместе с контекстом пользователя.
Так одна и та же роль работает и локально, и в контейнере, и в тестах, а пароль
логина остаётся делом развёртывания и в миграции не появляется.

Почему атрибуты роли проверяются, а не выставляются (T052). `alter role ...
nosuperuser` может выполнить только суперпользователь — на управляемом Postgres
такой роли не дают вовсе. Прежняя версия миграции делала `alter` безусловно и
поэтому не накатывалась нигде, кроме кластера, где мы сами суперпользователь.
Теперь: роли нет — заводим (атрибуты по умолчанию как раз нужные); роль есть —
сверяем и **останавливаем миграцию с внятным текстом**, если она умеет обходить
политики. Молча продолжать нельзя: роль приложения с `bypassrls` обнуляет всю
изоляцию, и заметить это по работе продукта невозможно.
"""
from django.db import migrations

ROLE = "app_user"

FORWARD = f"""
do $$
declare
    attrs record;
begin
    if not exists (select 1 from pg_roles where rolname = '{ROLE}') then
        -- Умолчания create role: nosuperuser, nobypassrls, nocreatedb,
        -- nocreaterole. Пишем их явно, чтобы намерение читалось из кода.
        create role {ROLE} nologin nosuperuser nobypassrls nocreatedb nocreaterole;
    end if;

    select rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
      into attrs from pg_roles where rolname = '{ROLE}';

    if attrs.rolsuper or attrs.rolbypassrls then
        raise exception
            'роль {ROLE} обходит RLS (superuser=%, bypassrls=%) — разграничение доступа не работает',
            attrs.rolsuper, attrs.rolbypassrls
            using hint = 'выполните суперпользователем: alter role {ROLE} nosuperuser nobypassrls';
    end if;

    if attrs.rolcreatedb or attrs.rolcreaterole then
        raise exception
            'роль {ROLE} имеет права на DDL (createdb=%, createrole=%) — приложению они не нужны',
            attrs.rolcreatedb, attrs.rolcreaterole
            using hint = 'выполните: alter role {ROLE} nocreatedb nocreaterole';
    end if;
end $$;

-- Право переключиться на роль приложения тому, кто накатывает миграции: обычно
-- он же ходит в базу. Даём только если он ещё не член — повторный grant требует
-- прав администратора роли, которых у обычного владельца может не быть.
do $$
begin
    if not pg_has_role(current_user, '{ROLE}', 'member') then
        execute format('grant {ROLE} to %I', current_user);
    end if;
exception when insufficient_privilege then
    raise exception
        'некому выдать роль {ROLE} пользователю % — он не член и не администратор роли', current_user
        using hint = 'выполните суперпользователем: grant {ROLE} to <владелец> with admin option';
end $$;

grant usage on schema public to {ROLE};
grant select, insert, update, delete on all tables in schema public to {ROLE};
grant usage, select on all sequences in schema public to {ROLE};

-- Будущие таблицы (следующие миграции) тоже должны быть доступны роли.
alter default privileges in schema public
    grant select, insert, update, delete on tables to {ROLE};
alter default privileges in schema public
    grant usage, select on sequences to {ROLE};
"""

BACKWARD = f"""
alter default privileges in schema public
    revoke select, insert, update, delete on tables from {ROLE};
alter default privileges in schema public
    revoke usage, select on sequences from {ROLE};
revoke all on all tables in schema public from {ROLE};
revoke all on all sequences in schema public from {ROLE};
revoke usage on schema public from {ROLE};
-- Саму роль не удаляем: она может быть выдана другим пользователям кластера,
-- и drop role оборвал бы им доступ молча.
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0004_rls")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
