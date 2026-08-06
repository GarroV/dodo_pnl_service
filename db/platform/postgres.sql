-- =============================================================================
-- Платформа: чистый Postgres (локальная разработка, тесты, self-hosted)
-- =============================================================================
-- Схема и миграции не знают, откуда берётся личность пользователя. Весь
-- контракт — одна функция app_user_id(). Здесь она читает GUC-переменную
-- app.user_id, которую приложение выставляет на соединении:
--
--     set local app.user_id = '00000000-0000-0000-0000-000000000001';
--
-- Применять ДО миграций: 0004 полагается на app_user_id(), а 0003 —
-- на auth.uid() (см. шим ниже).
-- =============================================================================

create or replace function app_user_id()
returns uuid
language sql stable
as $$
    -- true во втором аргументе = не падать, если переменная не выставлена
    select nullif(current_setting('app.user_id', true), '')::uuid
$$;

comment on function app_user_id() is
    'Единственная точка привязки к платформе аутентификации. На чистом Postgres — GUC app.user_id, на Supabase — auth.uid()';


-- --- Шим auth.uid() ----------------------------------------------------------
-- Миграции 0001–0003 писались под Supabase и вызывают auth.uid() напрямую.
-- Postgres проверяет тело SQL-функции при создании, поэтому без этого шима
-- 0003 не применится на чистом Postgres вообще. Новый код (0004 и дальше)
-- зависит только от app_user_id(); шим существует ради обратной совместимости
-- и уйдёт, когда 0003 перепишут на app_user_id().

create schema if not exists auth;

create or replace function auth.uid()
returns uuid
language sql stable
set search_path = public
as $$
    select app_user_id()
$$;


-- --- Роль приложения ---------------------------------------------------------
-- RLS не действует на владельца таблиц и на суперпользователя, поэтому
-- приложение обязано подключаться отдельной непривилегированной ролью.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'pnl_app') then
        create role pnl_app nologin;
    end if;
end $$;

grant usage on schema public to pnl_app;

-- Права выдаём заранее, на всё, что создадут следующие миграции: default
-- privileges действуют на объекты, созданные после этой команды текущей ролью.
alter default privileges in schema public
    grant select, insert, update, delete on tables to pnl_app;
alter default privileges in schema public
    grant usage, select on sequences to pnl_app;
