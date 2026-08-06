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
"""
from django.db import migrations

ROLE = "app_user"

FORWARD = f"""
do $$
begin
    if not exists (select 1 from pg_roles where rolname = '{ROLE}') then
        create role {ROLE} nologin;
    end if;
end $$;

-- Явно снимаем всё, что могло бы обойти политики: роль заводится один раз,
-- а живёт долго.
alter role {ROLE} nosuperuser nobypassrls nocreatedb nocreaterole;

grant usage on schema public to {ROLE};
grant select, insert, update, delete on all tables in schema public to {ROLE};
grant usage, select on all sequences in schema public to {ROLE};

-- Будущие таблицы (следующие миграции) тоже должны быть доступны роли.
alter default privileges in schema public
    grant select, insert, update, delete on tables to {ROLE};
alter default privileges in schema public
    grant usage, select on sequences to {ROLE};

-- Чтобы подключившийся мог переключиться на роль приложения. Владелец схемы
-- обычно и есть тот, кто ходит в базу; отдельный логин-пользователь получает
-- членство при развёртывании тем же способом.
grant {ROLE} to current_user;
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
