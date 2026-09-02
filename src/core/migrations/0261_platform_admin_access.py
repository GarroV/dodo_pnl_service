"""Администратор платформы ведёт пространства: узкая дверь, а не снос стены (D065, issue #221).

Владелец просит платформенную админку: список пространств, внутрь пространства —
его сотрудники, выдача ролей. Сегодня ни одно из этих действий невозможно, и не
из-за отсутствия экрана: **база запрещает их сама**. На `tenants` политика только
на чтение своих, на `users` роли приложения не выдан `insert` вовсе, чужие
членства видит лишь тот, у кого есть `roles.manage` в том же тенанте.

Запрет этот не лишний — на нём стоит изоляция партнёров. Поэтому дверь делается
узкой и с двумя ограничениями, каждое из которых важнее удобства.

## Ограничение первое: только четыре таблицы

Право платформенного администратора открывает `tenants`, `users`, `memberships`
и справочник `roles` — ровно то, из чего состоит управление доступом. Политики
остальных таблиц **не трогаются ни одной строкой**, и это значит буквально
следующее: администратор платформы не видит ни зарплат, ни табелей, ни фактов
партнёров. Он управляет доступом, а не смотрит чужие деньги.

Соблазн сделать проще был: достаточно расширить `app_tenant_ids()`, чтобы она
возвращала платформенному администратору все тенанты, — и одна правка открыла бы
ему весь продукт целиком. Ровно поэтому так и не сделано.

## Ограничение второе: право читается из таблицы, куда продукт не пишет

`app_is_platform_admin()` смотрит в `platform_admins` — таблицу, у которой нет ни
одной политики на запись, а права роли приложения отозваны (`0248`). Выдать себе
это право изнутри продукта нельзя никак: первая и всякая следующая строка
кладётся администратором базы (`manage.py platform_admin`, T165).

Без этого условия дверь открывала бы сама себя: получив право писать в
`memberships`, администратор партнёра выдал бы себе платформенное право и вышел
за пределы своего пространства.

## Почему политика, а не отдельная роль базы

Второй рассмотренный путь (issue #221, вариант A) — завести четвёртую роль
Postgres рядом с `MIGRATION_DB_USER`, `APP_DB_USER` и `QUEUE_DB_USER`, дать ей
право писать и ходить ею на платформенных экранах. Он отвергнут: такая роль
обходит RLS целиком, то есть внутри неё изоляции партнёров не существует вовсе, и
единственной защитой остаётся аккуратность кода приложения. Это второй Django
admin, против которого возражало само устройство продукта.

Здесь же изоляция остаётся там, где была, — в базе. Ошибка в коде экрана не
покажет чужие зарплаты, потому что политики на них не менялись.

## Что проверяет тест

Обязательно ролью `app_user`: владелец таблиц RLS обходит, и при неверно
написанной политике прогон был бы зелёным. На этом проекте так уже терялся дефект
видимости регистров. Проверяются обе стороны — что платформенный администратор
может, и что обычный пользователь по-прежнему не может ничего из этого.
"""
from django.db import migrations

FUNCTION = """
create or replace function app_is_platform_admin()
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1 from platform_admins where user_id = app_user_id()
    )
$$;

comment on function app_is_platform_admin() is
    'Ведёт ли текущий пользователь платформу. Право лежит в platform_admins — таблице, куда продукт не пишет: иначе дверь открывала бы сама себя';
"""

DROP_FUNCTION = """
drop function if exists app_is_platform_admin();
"""

POLICIES = """
-- ── tenants: видеть все пространства и заводить новые ──────────────────────
-- Политики объединяются через OR, поэтому обычный пользователь сохраняет
-- прежнюю видимость (`own_tenants`), а платформенный получает сверх неё все.
create policy tenants_platform_read on tenants
    for select using (app_is_platform_admin());

create policy tenants_platform_write on tenants
    for all
    using (app_is_platform_admin())
    with check (app_is_platform_admin());

-- ── users: завести первого человека пространства и видеть людей ────────────
-- `select` нужен не ради любопытства: без него экран не покажет, кому выдана
-- роль, и список сотрудников пространства был бы набором идентификаторов.
create policy users_platform_read on users
    for select using (app_is_platform_admin());

create policy users_platform_write on users
    for all
    using (app_is_platform_admin())
    with check (app_is_platform_admin());

-- ── memberships: выдать роль в любом пространстве ──────────────────────────
create policy memberships_platform_read on memberships
    for select using (app_is_platform_admin());

create policy memberships_platform_write on memberships
    for all
    using (app_is_platform_admin())
    with check (app_is_platform_admin());

-- ── roles: справочник ролей пространства ───────────────────────────────────
-- Чтение — чтобы знать, что выдавать. Вставка — потому что роли нового
-- пространства заводятся вместе с ним (`core.spaces.create_space`): партнёр без
-- ролей это оболочка, в которую некого пустить, ведь членство ссылается на роль.
--
-- Правки и удаления здесь намеренно нет: менять уже заведённые роли — дело
-- партнёра, у него для этого право `roles.manage` (`0242`). Платформа заводит
-- партнёра, а не ведёт его хозяйство.
--
-- Найдено тестом, а не рассуждением: первая версия миграции давала только
-- `select`, и заведение пространства падало на вставке ролей.
create policy roles_platform_read on roles
    for select using (app_is_platform_admin());

create policy roles_platform_create on roles
    for insert with check (app_is_platform_admin());

-- Ограничивающая политика `0242` перекрывает разрешающую: она СУЖАЕТ, и никакая
-- новая permissive-политика её не обходит. Поэтому условие расширяется здесь, а
-- не дублируется рядом — иначе роли нового пространства не завелись бы, и это
-- выглядело бы как необъяснимый отказ на ровном месте.
--
-- Форма остаётся прежней: право партнёра `roles.manage` ИЛИ право платформы.
-- Второе не заменяет первое и не расширяет его: партнёр по-прежнему заводит
-- роли только у себя.
drop policy roles_manage_insert on roles;
create policy roles_manage_insert on roles
    as restrictive for insert
    with check (
        app_has_permission(tenant_id, 'roles.manage')
        or app_is_platform_admin()
    );
"""

DROP_POLICIES = """
drop policy if exists roles_manage_insert on roles;
create policy roles_manage_insert on roles
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'roles.manage'));

drop policy if exists roles_platform_create on roles;
drop policy if exists roles_platform_read on roles;
drop policy if exists memberships_platform_write on memberships;
drop policy if exists memberships_platform_read on memberships;
drop policy if exists users_platform_write on users;
drop policy if exists users_platform_read on users;
drop policy if exists tenants_platform_write on tenants;
drop policy if exists tenants_platform_read on tenants;
"""

# Права выписываются явно. `insert` на `users` был отозван в `0010` — вернуть его
# без политики выше означало бы разрешить заводить учётки кому угодно; вместе с
# политикой это умеет только платформенный администратор.
PRIVILEGES = """
grant select, insert, update on tenants to app_user;
grant insert on users to app_user;
grant select, insert, update, delete on memberships to app_user;
grant select, insert on roles to app_user;
"""

DROP_PRIVILEGES = """
revoke insert on users from app_user;
revoke insert, update on tenants from app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0260_data_feeds"),
    ]

    operations = [
        migrations.RunSQL(FUNCTION, DROP_FUNCTION),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
