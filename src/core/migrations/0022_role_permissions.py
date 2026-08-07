"""Права роли перестают быть надписью (T064).

**Что было.** У роли есть `permissions`, сид их заполняет, `Principal` их
загружает — и на этом всё. Ни одна проверка на них не смотрела: грепом по
`src/web`, `src/timesheets`, `src/payrun` нет ни одного обращения, кроме
загрузки. Фактически администратор сети, у которого нет ни `timesheet.edit`, ни
`payrun.calculate`, **отредактировал ячейку табеля**. Отказ на «Посчитать» он
получал не по праву, а по случайности: ему в сиде выдан только официальный
регистр, и срабатывала проверка регистров — та же плашка появилась бы и у роли,
у которой право есть.

**Почему в базе, а не только в представлении.** Тот же довод, что у D014:
проверка в коде — это обещание, которое каждый следующий экран должен не забыть
повторить. Забытая проверка видимости даёт пустоту, а забытая проверка права
даёт **запись**, и её уже не отменишь. Поэтому право на запись стоит там же, где
всё остальное разграничение, а внятный отказ словами — в приложении
(`web/permissions.py`): база отвечает кодом ошибки, из которого человеку ничего
не понятно.

**Форма политик.** Ограничивающие (`as restrictive`), и только на запись:

* `for insert with check (...)` и `for update with check (...)` — отказ громкий:
  `new row violates row-level security policy`. `using` у правки намеренно нет:
  с ним строка просто не нашлась бы и `update` вернул бы «изменено 0 строк» —
  тихий успех, худший из возможных ответов;
* `for delete using (...)` — здесь `with check` не бывает, и тихий отказ
  безвреден: данные остаются на месте;
* чтение не трогаем вовсе. Право править табель — не право его видеть:
  администратор сети обязан видеть данные, которые не правит.

**Какие права и на каких таблицах.**

| право | таблицы |
|---|---|
| `timesheet.edit` | `timesheets`, `timesheet_days` |
| `payrun.calculate` | `payruns`, `payslips`, `pay_components`, `payslip_totals` |

Обе таблицы табеля закрыты одним правом сознательно: подневное хранение (D011) —
это тот же табель, только другой таблицей, и разводить их правами значило бы
завести две разные правды об одном действии.

**Чего эта миграция не делает.** Прав `directory.manage`, `rules.manage`,
`roles.manage`, `period.approve`, `period.reopen`, `unit.close` она не
проверяет: экранов справочников и цикла периода ещё нет, а политика, стоящая
там, где никто не пишет, — это не защита, а догадка о будущем устройстве.
Заводить их надо вместе с экранами, той же парой «политика + внятный отказ».
"""
from django.db import migrations

FUNCTION = """
-- Есть ли у пользователя право в этом тенанте. Права лежат в роли (jsonb-массив
-- кодов), роль приезжает через членство — то же место, откуда берутся регистры
-- и точки. Отдельного справочника прав нарочно нет: он был бы вторым источником
-- истины о доступе рядом с ролью, ровно тем, против чего D014.
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

comment on function app_has_permission(uuid, text) is
    'Есть ли у текущего пользователя это право в тенанте. Контекста нет — false, то есть запись запрещена';
"""

DROP_FUNCTION = "drop function if exists app_has_permission(uuid, text);"

WRITE_RIGHTS = {
    "timesheets": ("timesheet_edit", "timesheet.edit"),
    "timesheet_days": ("timesheet_edit", "timesheet.edit"),
    "payruns": ("payrun_calculate", "payrun.calculate"),
    "payslips": ("payrun_calculate", "payrun.calculate"),
    "pay_components": ("payrun_calculate", "payrun.calculate"),
    "payslip_totals": ("payrun_calculate", "payrun.calculate"),
}

POLICIES = "\n".join(
    f"""
create policy {policy}_insert on {table}
    as restrictive for insert
    with check (app_has_permission(tenant_id, '{code}'));

create policy {policy}_update on {table}
    as restrictive for update
    with check (app_has_permission(tenant_id, '{code}'));

create policy {policy}_delete on {table}
    as restrictive for delete
    using (app_has_permission(tenant_id, '{code}'));
"""
    for table, (policy, code) in WRITE_RIGHTS.items()
)

DROP_POLICIES = "\n".join(
    f"""
drop policy if exists {policy}_insert on {table};
drop policy if exists {policy}_update on {table};
drop policy if exists {policy}_delete on {table};
"""
    for table, (policy, _) in WRITE_RIGHTS.items()
)


class Migration(migrations.Migration):
    dependencies = [("core", "0021_payslip_ledgers_hidden")]

    operations = [
        migrations.RunSQL(FUNCTION, DROP_FUNCTION),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
