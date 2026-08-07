"""Управляющий точки видит только свою точку (T044).

**Что было.** У членства есть колонка `unit_ids` с комментарием «null = все точки
тенанта», у `Principal` — заполненный список точек, на странице входа управляющему
обещана точка NS1. А фильтра по точкам не было нигде: ни в базе, ни в приложении.
Ведомость показывала управляющему все три точки партнёра.

**Почему чинится здесь.** D014: разграничение доступа целиком в RLS, приложение
не фильтрует вручную. Фильтр в представлении дал бы второй источник истины о
доступе — и первый же новый отчёт, забывший его повторить, отдал бы чужую точку
молча. Цепочка обрывалась ровно на этом месте: `memberships.unit_ids` в базе
есть, а функции контекста, которая бы его читала, не было. Заводится по образцу
`app_tenant_ids()` и `app_visible_ledgers()` — там же, где стоит вся остальная
изоляция.

**Два решения, зафиксированные тестами (`tests/test_unit_visibility.py`).**

1. *`unit_ids is null` у членства = все точки тенанта.* Так уже написано в
   комментарии к колонке (миграция `0003_comments`), и функция обязана этому
   следовать, иначе директор перестал бы видеть что-либо. Отдельно разведён
   случай «членства в тенанте нет вовсе»: он даёт пустой список, а не «все» —
   иначе агрегат над нулём строк вернул бы null и открыл всё.
2. *Строка без точки (`unit_id is null`) видна всем, у кого есть доступ к
   тенанту.* Она не принадлежит чужой точке — она не принадлежит никакой.
   `unit_id` обнуляется при удалении точки (`on delete set null`), и молча
   терять такие строки хуже, чем показать их тому, кто и так работает внутри
   этого партнёра. Правило живёт в одном месте — `app_unit_is_visible()`, — а не
   повторяется условием на каждой таблице.

**Почему `as restrictive`.** Пермиссивные политики Postgres объединяет через OR:
«точка видна» не сужало бы выборку вообще, строку своего тенанта пропустила бы
политика изоляции. Тот же довод, что у видимости регистров в `0004_rls`.

**Почему `for all`, а не только `for select`.** Иначе управляющий не видел бы
чужую точку, но мог бы в неё **писать** — сетка ввода часов как раз появляется.
Ролям, которые считают период целиком (директор, администратор), точки не
ограничены, поэтому расчёту это ничего не ломает.

**Чего эта миграция не делает.**

- `allocation_rules.unit_id` не трогается: там точка — это параметр правила
  разнесения («отнести на точку X»), а не принадлежность строки. Правила
  разнесения — настройка тенанта, и прятать их по точкам было бы неверно.
- `employees` остаются видны целиком: точка у сотрудника не хранится, она в
  условиях найма. Закрывать имена пришлось бы присоединением, а это уже другая
  задача.
- Сотрудник, переведённый в середине месяца, попадает в ведомость одной строкой
  с одной точкой (`payslips_payrun_employee_uniq`), поэтому управляющий видит её
  целиком или не видит вовсе. Разнесение по точкам при переводе — известная
  открытая работа, здесь она не решается.

**Побочное следствие, о котором стоит знать.** Политика `ledger_visibility` на
`payslip_totals` (миграция `0009`) проверяет строку через подзапрос к `payslips`.
Теперь на `payslips` есть и ограничение по точкам, поэтому итоги чужой точки для
управляющего тоже пропадают. Это желаемое поведение, но комментарий в `0009`
(«на самой ведомости политики видимости регистров нет») с тех пор описывает
только регистры.
"""
from django.db import migrations

# Таблицы, где точка — принадлежность строки. `units` в списке особая: там роль
# точки играет собственный ключ строки.
UNIT_TABLES = {
    "units": "id",
    "employment_terms": "unit_id",
    "timesheets": "unit_id",
    "payslips": "unit_id",
}

FUNCTIONS = """
-- Точки пользователя в тенанте. null = ограничения нет (все точки), '{}' =
-- ни одной. security definer обязателен по той же причине, что у
-- app_tenant_ids(): на memberships висит своя RLS, иначе политика звала бы
-- функцию, которой нужна та же политика.
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
        -- Членства нет — ни одной точки. Разведено явно: агрегат над нулём
        -- строк вернул бы null, то есть «все точки», и функция открыла бы
        -- чужого тенанта, если бы его когда-нибудь пропустила изоляция.
        when not exists (select 1 from mine) then '{}'::uuid[]
        -- Хотя бы одно членство без списка точек — доступ ко всем точкам.
        when exists (select 1 from mine where unit_ids is null) then null
        else (
            select coalesce(array_agg(distinct value), '{}'::uuid[])
              from mine, unnest(mine.unit_ids) as value
        )
    end
$$;

comment on function app_unit_ids(uuid) is
    'Точки пользователя в тенанте. null = все точки, пустой массив = ни одной';

-- Единственное место, где записано правило видимости точки. Условие не
-- повторяется на каждой таблице нарочно: разъехавшиеся копии одного правила и
-- есть тот способ, которым доступ ломается незаметно.
create or replace function app_unit_is_visible(p_tenant uuid, p_unit uuid)
returns boolean
language sql stable
as $$
    select p_unit is null                       -- строка без точки: ничья, видна всем
        or app_unit_ids(p_tenant) is null       -- у пользователя все точки тенанта
        or p_unit = any (app_unit_ids(p_tenant))
$$;

comment on function app_unit_is_visible(uuid, uuid) is
    'Видна ли строка этой точки текущему пользователю. Строка без точки видна всем в тенанте';
"""

DROP_FUNCTIONS = """
drop function if exists app_unit_is_visible(uuid, uuid);
drop function if exists app_unit_ids(uuid);
"""

POLICIES = "\n".join(
    f"""
create policy unit_visibility on {table}
    as restrictive for all
    using (app_unit_is_visible(tenant_id, {column}))
    with check (app_unit_is_visible(tenant_id, {column}));
"""
    for table, column in UNIT_TABLES.items()
) + """
-- У компонента выплаты своей точки нет — он живёт при строке ведомости.
-- Без этой политики ведомость на экране была бы срезана правильно (она
-- собирается присоединением к `payslips`), а прямой запрос «сумма компонентов»
-- в следующем отчёте вернул бы все точки. То есть защита держалась бы на том,
-- что автор отчёта не забыл присоединить нужную таблицу.
create policy unit_visibility on pay_components
    as restrictive for all
    using (exists (select 1 from payslips p where p.id = pay_components.payslip_id))
    with check (exists (select 1 from payslips p where p.id = pay_components.payslip_id));
"""

DROP_POLICIES = "\n".join(
    f"drop policy if exists unit_visibility on {table};"
    for table in [*UNIT_TABLES, "pay_components"]
)


class Migration(migrations.Migration):
    dependencies = [("core", "0010_users")]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP_FUNCTIONS),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
