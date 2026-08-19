"""Экран ролей видит людей по именам, а не по идентификаторам (T171).

`0010` закрыла `users` наглухо: человек читает свою строку и ту, которой прямо
сейчас входит. Это верно по умолчанию — кто ещё работает у партнёра, из
ведомости не следует, — но экрану ролей нечего показывать: список членств без
имён это столбик UUID, по которому нельзя понять, кому ты выдаёшь право
считать деньги.

Открывается ровно столько, сколько нужно: тот, у кого `roles.manage`, читает
**строки людей своего тенанта** — тех, у кого есть членство там же. Ни чужих
партнёров, ни пароля это не открывает: `password` не выбирается экраном, а
запись в `users` по-прежнему только своя (`users_change_own_row`).

Почему условие написано подзапросом к `memberships`, а не функцией
`security definer`: функция не обошла бы `force row level security` — владелец
таблиц у нас не суперпользователь, это проверено на живой базе при постройке
`0242`. Подзапрос же безопасен по той же причине, что и там: политики
`memberships` разрешают строку либо по `user_id = app_user_id()` без вызова
функций, либо через `app_has_permission`, чья выборка отфильтрована по
`m.user_id = app_user_id()`.
"""
from django.db import migrations

POLICY = """
create policy users_role_manager_read on users
    for select
    using (exists (
        select 1
          from memberships m
         where m.user_id = users.id
           and app_has_permission(m.tenant_id, 'roles.manage')
    ));
"""

REVERT = "drop policy if exists users_role_manager_read on users;"


class Migration(migrations.Migration):

    dependencies = [("core", "0242_roles_manage")]

    operations = [migrations.RunSQL(POLICY, REVERT)]
