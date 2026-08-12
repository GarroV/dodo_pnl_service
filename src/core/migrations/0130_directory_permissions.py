"""Право `directory.manage` перестаёт быть надписью (T018).

**Долг, оставленный сознательно.** Миграция `0022_role_permissions` завела
проверку прав на запись для табеля и расчёта и там же записала, чего она
намеренно не делает: `directory.manage`, `rules.manage`, `roles.manage` не
проверяются, потому что экранов справочников ещё нет, а «политика, стоящая там,
где никто не пишет, — это не защита, а догадка о будущем устройстве». Экран
появился — платим долг той же парой «политика в базе + внятный отказ словами»
(`web/permissions.py`).

**Что было до этой миграции.** Изоляция тенанта (`0004_rls`) даёт `for all` с
проверкой `tenant_id in (select app_tenant_ids())`, то есть **любой** член
тенанта мог переписать справочник: управляющий — сменить ставку в условиях
найма, бухгалтер — переименовать точку. Никто этого не делал только потому, что
не было экрана.

**Какие таблицы и почему именно они.** Ровно те пять, что перечислены в задаче
как справочники, плюс условия найма — они и есть содержимое карточки сотрудника:

| таблица | что в ней ведут |
|---|---|
| `employees` | карточки людей |
| `employment_terms` | условия найма: группа, точка, ставка, коэффициент — **версионируются** |
| `employee_groups` | группы: схема расчёта и регистр учёта |
| `units` | точки |
| `legal_entities` | юрлица |

Форма политик — как в `0022`: `as restrictive`, только на запись, чтение не
трогаем. Право вести справочник — не право его видеть: и бухгалтер, и
управляющий обязаны видеть людей и точки, которых не правят.

**`calendars` — отдельный случай, и не по прихоти.** У календаря нет `tenant_id`:
производственный календарь общий для страны (`0004_rls`, `SHARED_TABLES`), и
`app_has_permission(tenant_id, …)` к нему не приставить. Сегодня на нём стоит
одна политика `read_all` — то есть записи нет вовсе ни у кого: под `force row
level security` отсутствие разрешающей политики на `insert` означает отказ. Не
«календарь защищён», а «календарь нельзя вести» — ровно та дыра, из-за которой
норму часов приходилось править руками в базе.

Правило для него: вести календарь страны вправе тот, у кого есть
`directory.manage` в тенанте **этой** страны. Двум партнёрам в одной стране
календарь общий — это свойство схемы (ключ `country_code + period`), а не
решение этой миграции.

**Почему у календаря `using` на `update`, хотя в `0022` его намеренно не было.**
Там политика ограничивающая: она сужает уже разрешённое, и `using` превратил бы
громкий отказ в тихое «изменено 0 строк». Здесь политика **разрешающая** —
единственная на запись, — и без `using` строка для `update` просто не нашлась бы
ни у кого, включая администратора. То есть выбора нет; тихий отказ закрыт
приложением: представление проверяет, что запись действительно изменилась, и
иначе говорит об этом словами (`web/directory_views.py`).

**Чего эта миграция по-прежнему не делает.** `rules.manage` и `roles.manage` не
проверяет: экранов правил и ролей нет, и заводить политику под ненаписанный
экран — та же догадка, против которой возражала `0022`.
"""
from django.db import migrations

# Функция для общего справочника без тенанта. Отдельная, а не условие внутри
# политики: правило «кто вправе вести календарь страны» должно читаться в одном
# месте — разъехавшиеся копии одного правила и есть способ, которым доступ
# ломается незаметно (тот же довод, что у `app_unit_is_visible`).
FUNCTION = """
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

comment on function app_manages_calendar(text) is
    'Вправе ли текущий пользователь вести производственный календарь этой страны. Контекста нет — false';
"""

DROP_FUNCTION = "drop function if exists app_manages_calendar(text);"

# Справочники с тенантом: право проверяется там же, где всё остальное
# разграничение. Имя политики одно на таблицу и говорит о праве, а не о задаче.
DIRECTORY_TABLES = [
    "employees",
    "employment_terms",
    "employee_groups",
    "units",
    "legal_entities",
]

POLICIES = "\n".join(
    f"""
create policy directory_manage_insert on {table}
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_update on {table}
    as restrictive for update
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_delete on {table}
    as restrictive for delete
    using (app_has_permission(tenant_id, 'directory.manage'));
"""
    for table in DIRECTORY_TABLES
) + """
create policy directory_manage_insert on calendars
    for insert
    with check (app_manages_calendar(country_code));

create policy directory_manage_update on calendars
    for update
    using (app_manages_calendar(country_code))
    with check (app_manages_calendar(country_code));

create policy directory_manage_delete on calendars
    for delete
    using (app_manages_calendar(country_code));
"""

DROP_POLICIES = "\n".join(
    f"""
drop policy if exists directory_manage_insert on {table};
drop policy if exists directory_manage_update on {table};
drop policy if exists directory_manage_delete on {table};
"""
    for table in [*DIRECTORY_TABLES, "calendars"]
)


class Migration(migrations.Migration):

    # Зависимость — от текущего листа, а не от того, что был листом в начале
    # работы: пока задача делалась, в основную ветку въехала `0140`. Две
    # миграции от одного родителя дают две головы, и `migrate` на чистой базе
    # отказывается выбирать между ними. Номер при этом меньше — Django смотрит
    # на зависимости, а не на имена, и переименовывать применённую миграцию
    # опаснее, чем оставить номер не по порядку.
    dependencies = [
        ("core", "0140_timesheet_piece_value"),
    ]

    operations = [
        migrations.RunSQL(FUNCTION, DROP_FUNCTION),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
