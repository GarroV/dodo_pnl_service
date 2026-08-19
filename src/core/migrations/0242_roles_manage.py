"""Право `roles.manage` наконец что-то значит в базе (T171, issue #77).

Экран ролей строится, и вместе с ним закрываются две дыры, которые до сих пор
никто не проверял, потому что проверять было нечего: экрана не было, а `0130`
честно написала, что права на роли не трогает — «политика под ненаписанный
экран это догадка».

**Дыра первая: сам себе роль.** Политика `tenant_isolation` на `memberships`
из `0004` писалась как изоляция чтения — «вижу свои членства» — и звучала
`for all using (user_id = app_user_id()) with check (user_id = app_user_id())`.
Но `for all` это и запись тоже. То есть управляющий точки мог вписать **себе**
строку с ролью администратора и получить справочники, правила и роли, не
спрашивая никого. Проверено ролью `app_user` до этой миграции: вставка
проходила.

**Дыра вторая: правка собственной роли.** У `roles` не было ни одной политики
записи — только изоляция по тенанту, и та `for all`. Любой сотрудник партнёра
мог дописать своей роли `payrun.calculate`. Тоже проверено: проходило.

**Что здесь сделано.** Чтение своих членств отделено от записи: своё членство
больше не разрешение его менять. Запись членств и правка ролей требуют
`roles.manage` — и это проверяет база, а не форма. Тому же праву открыто чтение
чужих членств: без него экран ролей показывал бы одного себя.

**Про рекурсию, из-за которой это не сделали раньше.** `memberships` — корневая
таблица контекста: политика на ней не может звать функции, которым нужна та же
политика (`0004`, пункт 4). Здесь она их зовёт — и это безопасно ровно при одном
условии: внутренний запрос функции отфильтрован по `m.user_id = app_user_id()`.
Так написана `app_has_permission` (`0022`), и так проверено на живой базе:
функция без этого фильтра даёт `stack depth limit exceeded`, а прямая ссылка на
`memberships` из политики — `infinite recursion detected in policy`. Условие
записано здесь, чтобы следующая правка не сняла фильтр молча.

**Почему не `security definer`-функция «в обход политик».** `0004` обещала, что
экран управления людьми получит данные именно так. Обещание не работает в нашем
развёртывании: `security definer` **не** обходит `force row level security`,
если владелец таблиц не суперпользователь, — а он у нас не суперпользователь.
Проверено на Postgres 17 отдельным стендом.

**Общие роли (`tenant_id is null`) не правятся никем.** Они видны всем
партнёрам, и правка одним из них меняла бы то, что видят остальные — тот же
довод, что у общего справочника статей в `0004`. Ограничивающая политика даёт
это даром: `app_has_permission(null, …)` — false.
"""
from django.db import migrations

POLICIES = """
-- ── roles: запись только с `roles.manage` ───────────────────────────────────
-- Политики ограничивающие: они СУЖАЮТ уже разрешённое изоляцией по тенанту, а
-- не открывают новое. Форма та же, что у справочников в `0130`, чтобы правило
-- «право проверяется там же, где всё остальное разграничение» читалось одинаково.
create policy roles_manage_insert on roles
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'roles.manage'));

create policy roles_manage_update on roles
    as restrictive for update
    with check (app_has_permission(tenant_id, 'roles.manage'));

create policy roles_manage_delete on roles
    as restrictive for delete
    using (app_has_permission(tenant_id, 'roles.manage'));

-- ── memberships: своё членство больше не разрешение его менять ──────────────
drop policy tenant_isolation on memberships;

create policy own_membership_read on memberships
    for select
    using (user_id = app_user_id());

-- Чужие членства видит только тот, кто их ведёт. Остальным по-прежнему не видно,
-- кто ещё работает у партнёра: из ведомости это не следует.
create policy memberships_manage_read on memberships
    for select
    using (app_has_permission(tenant_id, 'roles.manage'));

-- Запись — целиком под правом. `using` нужен и на запись: без него `update` и
-- `delete` не нашли бы строку вовсе, и отказ стал бы тихим «изменено 0 строк»
-- (тот же разбор, что у календаря в `0130`).
create policy memberships_manage_write on memberships
    for all
    using (app_has_permission(tenant_id, 'roles.manage'))
    with check (app_has_permission(tenant_id, 'roles.manage'));
"""

REVERT = """
drop policy if exists memberships_manage_write on memberships;
drop policy if exists memberships_manage_read on memberships;
drop policy if exists own_membership_read on memberships;

create policy tenant_isolation on memberships
    for all
    using (user_id = app_user_id())
    with check (user_id = app_user_id());

drop policy if exists roles_manage_delete on roles;
drop policy if exists roles_manage_update on roles;
drop policy if exists roles_manage_insert on roles;
"""


class Migration(migrations.Migration):

    dependencies = [("core", "0241_insured_author")]

    operations = [migrations.RunSQL(POLICIES, REVERT)]
