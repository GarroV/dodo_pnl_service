"""Закрытие часов по точке: «я всё ввёл, больше не правьте» (T022).

**Что появляется.** Таблица закрытий и запрет записи в закрытые часы. Закрытие
относится к паре «точка + месяц», поэтому одна точка закрывается независимо от
соседних — ровно то, что требует спека («хочу закрывать свою точку независимо
от других, чтобы не ждать всю сеть») и чего не даёт закрытие периода целиком.

**Почему политики, а не триггер.** У заморозки утверждённого расчёта (миграция
`0041`) выбран триггер — там гарантия обязана действовать и на суперпользователя,
потому что утверждённая ведомость уже выдана людям. Здесь другое: закрытие — это
операционная отсечка внутри партнёра, её соблюдают пользователи приложения, а
обслуживание (сид, восстановление из дампа, миграции) обязано мочь писать.
Поэтому механизм тот же, что у видимости точек (T044) и прав роли (T064):
приложение объясняет словами, база гарантирует политиками.

Пересечения с заморозкой расчёта нет: `0041` табеля **не трогает** вовсе (правка
входных данных задним числом — T026). Один и тот же отказ двумя разными словами
поэтому не объясняется — на пути записи в табель запрет ровно один.

**Форма политик — как в `0022`.** `for insert/update with check` даёт громкий
отказ (`new row violates row-level security policy`). Тихое «изменено 0 строк»
здесь недопустимо: точку могли закрыть между открытием страницы и досылкой
ячейки, и приложение приняло бы такой ответ за удачную запись.

`using` у правки намеренно нет — по той же причине. Следствие названо честно:
`update`, **переносящий** строку табеля в другую, незакрытую точку, политикой не
ловится. Сегодня такого пути нет ни в интерфейсе, ни в импорте (точка строки
берётся из условий найма и не меняется), а разнесение при переводе внутри месяца
делается разрезанием дней — это отдельная работа, и запрет для неё надо будет
писать вместе с ней, а не угадывать сейчас.

**Право `unit.close`.** Оно объявлено в ролях с самого начала и до сих пор
ничего не решало — `0022` называет это прямо: политика без экрана есть догадка.
Теперь у права появился экран, поэтому появляется и политика. Право не
подразумевается из других: у директора сида его нет, и база его не пускает —
как и интерфейс, который в этом случае не показывает кнопки.

**Чего эта миграция не делает.**

- Не требует закрытия точек перед расчётом периода. Закрытие — сигнал
  управляющего бухгалтеру, а не предусловие расчёта; делать его обязательным
  значило бы решить за партнёра, как устроен его месяц.
- Не запрещает закрывать точку с незаполненными часами. Построчная блокировка и
  «спорный сотрудник не держит остальных» — T027.
- Не пишет журнала закрытий сверх самой таблицы: строка закрытия и есть запись
  в истории, а открытие заново её не удаляет, а помечает.
"""
import django.db.models.deletion
from django.db import migrations, models

COMMENTS = """
comment on table timesheet_closures is
    'Закрытие часов точки за месяц. Действующее закрытие — строка с пустым reopened_at';
comment on column timesheet_closures.period is
    'Месяц, часы которого закрыты. Первое число месяца, как во всех периодах';
comment on column timesheet_closures.closed_by is
    'Кто закрыл: контекст приложения (app_user_id()). Пусто — закрыто обслуживанием мимо приложения';
comment on column timesheet_closures.reopened_at is
    'Когда точку открыли заново. Пусто — закрытие действует, часы не правятся';
"""

RULES = """
-- Закрыты ли часы этой точки за этот месяц. security definer обязателен: на
-- самой таблице закрытий висит RLS, и без него политика запрета читала бы
-- закрытия глазами того, кого она ограничивает. Невидимое закрытие означало бы
-- «не закрыто», то есть запрет снимался бы сам собой у того, кто его не видит.
create or replace function timesheet_closed(p_tenant uuid, p_unit uuid, p_period date)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1 from timesheet_closures
         where tenant_id = p_tenant
           and unit_id = p_unit
           and period = p_period
           and reopened_at is null
    )
$$;

comment on function timesheet_closed(uuid, uuid, date) is
    'Закрыты ли часы точки за месяц. Строка табеля без точки не закрывается никогда';

-- То же самое, но по строке табеля: у подневных данных своей точки нет, она у
-- родителя. Отдельная функция, а не подзапрос в каждой политике: разъехавшиеся
-- копии одного правила и есть тот способ, которым доступ ломается незаметно.
create or replace function timesheet_row_closed(p_timesheet uuid)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1 from timesheets t
         where t.id = p_timesheet
           and timesheet_closed(t.tenant_id, t.unit_id, t.period)
    )
$$;

comment on function timesheet_row_closed(uuid) is
    'Закрыты ли часы строки табеля. На этом стоит запрет записи в подневные данные';
"""

DROP_RULES = """
drop function if exists timesheet_row_closed(uuid);
drop function if exists timesheet_closed(uuid, uuid, date);
"""

# --- сама таблица закрытий ---------------------------------------------------
# Три слоя, ровно как у остальных таблиц продукта: тенант, точка, право.
CLOSURE_POLICIES = """
alter table timesheet_closures enable row level security;
-- force обязателен: без него политики не действуют на владельца таблиц, а
-- миграции и обслуживание ходят как раз им.
alter table timesheet_closures force row level security;

create policy tenant_isolation on timesheet_closures
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Управляющий закрывает и видит только свою точку. `for all`, а не `for select`:
-- иначе чужое закрытие было бы не видно, но его можно было бы снять.
create policy unit_visibility on timesheet_closures
    as restrictive for all
    using (app_unit_is_visible(tenant_id, unit_id))
    with check (app_unit_is_visible(tenant_id, unit_id));

create policy unit_close_insert on timesheet_closures
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'unit.close'));

create policy unit_close_update on timesheet_closures
    as restrictive for update
    with check (app_has_permission(tenant_id, 'unit.close'));

create policy unit_close_delete on timesheet_closures
    as restrictive for delete
    using (app_has_permission(tenant_id, 'unit.close'));
"""

DROP_CLOSURE_POLICIES = """
drop policy if exists unit_close_delete on timesheet_closures;
drop policy if exists unit_close_update on timesheet_closures;
drop policy if exists unit_close_insert on timesheet_closures;
drop policy if exists unit_visibility on timesheet_closures;
drop policy if exists tenant_isolation on timesheet_closures;
alter table timesheet_closures no force row level security;
alter table timesheet_closures disable row level security;
"""

# --- запрет записи в закрытые часы -------------------------------------------
# Обе таблицы табеля, потому что месячный итог собирается из дней: закрыть одну
# и оставить другую значило бы запретить парадный вход и оставить чёрный ход.
HOURS_POLICIES = """
create policy hours_open_insert on timesheets
    as restrictive for insert
    with check (not timesheet_closed(tenant_id, unit_id, period));

create policy hours_open_update on timesheets
    as restrictive for update
    with check (not timesheet_closed(tenant_id, unit_id, period));

-- У удаления `with check` не бывает, и тихий отказ здесь безвреден: данные
-- остаются на месте. Тот же довод, что в `0022`.
create policy hours_open_delete on timesheets
    as restrictive for delete
    using (not timesheet_closed(tenant_id, unit_id, period));

create policy hours_open_insert on timesheet_days
    as restrictive for insert
    with check (not timesheet_row_closed(timesheet_id));

create policy hours_open_update on timesheet_days
    as restrictive for update
    with check (not timesheet_row_closed(timesheet_id));

create policy hours_open_delete on timesheet_days
    as restrictive for delete
    using (not timesheet_row_closed(timesheet_id));
"""

DROP_HOURS_POLICIES = "\n".join(
    f"drop policy if exists hours_open_{action} on {table};"
    for table in ("timesheets", "timesheet_days")
    for action in ("insert", "update", "delete")
)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_timesheet_day_units'),
    ]

    operations = [
        migrations.CreateModel(
            name='TimesheetClosure',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('period', models.DateField()),
                ('closed_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('closed_by', models.UUIDField(blank=True, null=True)),
                ('reopened_at', models.DateTimeField(blank=True, null=True)),
                ('reopened_by', models.UUIDField(blank=True, null=True)),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
                ('unit', models.ForeignKey(db_column='unit_id', on_delete=django.db.models.deletion.CASCADE, to='core.unit')),
            ],
            options={
                'db_table': 'timesheet_closures',
                'indexes': [models.Index(models.F('tenant'), models.F('period'), name='timesheet_closures_period_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('reopened_at__isnull', True)), fields=('tenant', 'unit', 'period'), name='timesheet_closures_active_uniq')],
            },
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(RULES, DROP_RULES),
        migrations.RunSQL(CLOSURE_POLICIES, DROP_CLOSURE_POLICIES),
        migrations.RunSQL(HOURS_POLICIES, DROP_HOURS_POLICIES),
    ]
