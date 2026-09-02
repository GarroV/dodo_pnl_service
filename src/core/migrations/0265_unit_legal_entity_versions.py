"""Точка переезжает в другое юрлицо — новой версией с даты (T189, issue #179).

Эталон (модуль 11) говорит это прямо: «Точка меняет юрлицо так же, как сотрудник
меняет ставку: новой версией с даты. Прошлые месяцы остаются за старым юрлицом,
иначе разъедется отчётность обоих».

До этой миграции связь была одна на всю жизнь точки, и перенос делался правкой
поля — задним числом и без следа: закрытые месяцы молча переезжали в другое
юрлицо вместе с точкой. Это тот же класс, что D020 запрещает для расчёта, и
здесь он дороже: отчётность расходится сразу у двух компаний.

**Колонка `units.legal_entity_id` остаётся, но перестаёт быть правдой об
истории.** Она — снимок «где точка сейчас», и держат его два триггера:

* правка версий обновляет колонку по сегодняшнему дню;
* правка колонки заводит версию — с даты открытия точки, если версий ещё не
  было, иначе с сегодня.

Второй нужен не ради красоты, а потому что точку заводят и правят пять разных
мест (форма справочника, сид разработки, демо-сид, платформенная админка,
тесты). Требовать от каждого помнить про историю значит однажды получить точку
без единой версии — и молча, потому что колонка при этом заполнена.

Зацикливания не выходит: второй триггер пишет версию только когда она
расходится с колонкой, а первый ставит колонку ровно в то, что стоит в
версиях, — на втором витке расхождения уже нет.

Границы полуоткрытые (`[)`), как у всех версий проекта: «по 1 июля» и «с 1
июля» — стык, а не пересечение.
"""

import core.models
import django.contrib.postgres.constraints
import django.db.models.deletion
from django.db import migrations, models


COMMENTS = """
comment on table unit_legal_entities is
    'Под каким юрлицом точка стояла в тот или иной период. Перенос — новая версия с даты, прошлые месяцы остаются за прежним юрлицом (T189, issue #179)';
comment on column unit_legal_entities.valid_to is
    'Конец периода НЕ входит: «по 1 июля» и «с 1 июля» — стык, а не пересечение. Пусто — версия действует до сих пор';
comment on column units.legal_entity_id is
    'Снимок текущей связи, а не история. Правду о том, чьей точка была в мае, знает unit_legal_entities; колонку держат триггеры (T189)';
"""

RESOLVER = """
-- Чьей была точка на эту дату. Без security definer намеренно: таблица под
-- своей политикой, и функция обязана отвечать ровно то, что человеку видно.
create or replace function unit_legal_entity_at(p_unit uuid, p_on date)
returns uuid
language sql stable
set search_path = public
as $$
    select legal_entity_id
      from unit_legal_entities
     where unit_id = p_unit
       and valid_from <= p_on
       and (valid_to is null or valid_to > p_on)
     limit 1
$$;

comment on function unit_legal_entity_at(uuid, date) is
    'Юрлицо точки на указанную дату. Отчёт за прошлый период обязан спрашивать её, а не колонку units.legal_entity_id';
"""

DROP_RESOLVER = "drop function if exists unit_legal_entity_at(uuid, date);"

# Первая версия каждой точки. Дата — открытие точки: связь обязана покрывать
# всю её историю, иначе отчёт за первый месяц ответит «юрлица не было».
# Точка без даты открытия получает заведомо раннюю: выдуманная дата здесь
# честнее дыры, потому что дыра читается как «в мае точка была ничьей».
BACKFILL = """
insert into unit_legal_entities (tenant_id, unit_id, legal_entity_id, valid_from)
select u.tenant_id, u.id, u.legal_entity_id, coalesce(u.opened_at, date '1900-01-01')
  from units u
 where u.legal_entity_id is not null;
"""

TRIGGERS = """
-- Версии → колонка. Колонка обязана показывать сегодняшнее состояние, что бы
-- ни сделали с историей: закрыли версию, передвинули дату, удалили запись.
create or replace function app_unit_entity_snapshot()
returns trigger
language plpgsql
set search_path = public
as $$
declare
    target uuid := coalesce(NEW.unit_id, OLD.unit_id);
    today  uuid := unit_legal_entity_at(coalesce(NEW.unit_id, OLD.unit_id), current_date);
begin
    update units set legal_entity_id = today
     where id = target and legal_entity_id is distinct from today;
    return null;
end
$$;

create trigger unit_entity_snapshot
    after insert or update or delete on unit_legal_entities
    for each row execute function app_unit_entity_snapshot();

-- Колонка → версии. Точку заводят и правят из пяти мест; требовать от каждого
-- помнить про историю значит однажды получить точку без единой версии — и
-- молча, потому что колонка при этом заполнена.
create or replace function app_unit_entity_version()
returns trigger
language plpgsql
set search_path = public
as $$
declare
    today  uuid := unit_legal_entity_at(NEW.id, current_date);
    since  date;
begin
    if NEW.legal_entity_id is not distinct from today then
        -- Колонку поставил снимок выше: истории эта правка ничего не сообщает.
        return null;
    end if;

    if not exists (select 1 from unit_legal_entities where unit_id = NEW.id) then
        -- Первая версия точки идёт от её открытия, а не от сегодня: иначе
        -- отчёт за прошлый месяц у только что заведённой точки ответил бы
        -- «юрлица не было», хотя оно было всегда.
        since := coalesce(NEW.opened_at, current_date);
    else
        since := current_date;
    end if;

    update unit_legal_entities
       set valid_to = since
     where unit_id = NEW.id
       and valid_from < since
       and (valid_to is null or valid_to > since);

    delete from unit_legal_entities
     where unit_id = NEW.id and valid_from >= since;

    if NEW.legal_entity_id is not null then
        insert into unit_legal_entities
                    (tenant_id, unit_id, legal_entity_id, valid_from)
             values (NEW.tenant_id, NEW.id, NEW.legal_entity_id, since);
    end if;
    return null;
end
$$;

create trigger unit_entity_version
    after insert or update of legal_entity_id on units
    for each row execute function app_unit_entity_version();
"""

DROP_TRIGGERS = """
drop trigger if exists unit_entity_version on units;
drop trigger if exists unit_entity_snapshot on unit_legal_entities;
drop function if exists app_unit_entity_version();
drop function if exists app_unit_entity_snapshot();
"""

# Политики те же, что у остальных справочников (`0130`): перенос точки — это
# правка справочника, и правом закрыт как справочник.
POLICIES = """
alter table unit_legal_entities enable row level security;
alter table unit_legal_entities force  row level security;

create policy tenant_isolation on unit_legal_entities
    for select
    using (tenant_id in (select app_tenant_ids()));

create policy directory_manage_insert on unit_legal_entities
    as restrictive for insert
    with check (
        tenant_id in (select app_tenant_ids())
        and app_has_permission(tenant_id, 'directory.manage')
    );

create policy directory_manage_update on unit_legal_entities
    as restrictive for update
    using (
        tenant_id in (select app_tenant_ids())
        and app_has_permission(tenant_id, 'directory.manage')
    );

create policy directory_manage_delete on unit_legal_entities
    as restrictive for delete
    using (
        tenant_id in (select app_tenant_ids())
        and app_has_permission(tenant_id, 'directory.manage')
    );

-- Разрешающая пара к ограничивающим: без неё запись не пройдёт ни у кого,
-- потому что `restrictive` только сужает уже разрешённое.
create policy directory_write on unit_legal_entities
    for insert with check (tenant_id in (select app_tenant_ids()));
create policy directory_change on unit_legal_entities
    for update using (tenant_id in (select app_tenant_ids()));
create policy directory_remove on unit_legal_entities
    for delete using (tenant_id in (select app_tenant_ids()));
"""

DROP_POLICIES = """
drop policy if exists directory_remove on unit_legal_entities;
drop policy if exists directory_change on unit_legal_entities;
drop policy if exists directory_write on unit_legal_entities;
drop policy if exists directory_manage_delete on unit_legal_entities;
drop policy if exists directory_manage_update on unit_legal_entities;
drop policy if exists directory_manage_insert on unit_legal_entities;
drop policy if exists tenant_isolation on unit_legal_entities;
alter table unit_legal_entities no force row level security;
alter table unit_legal_entities disable row level security;
"""

PRIVILEGES = "grant select, insert, update, delete on unit_legal_entities to app_user;"
DROP_PRIVILEGES = "revoke all on unit_legal_entities from app_user;"


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0264_access_history'),
    ]

    operations = [
        migrations.CreateModel(
            name='UnitLegalEntity',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('valid_from', models.DateField()),
                ('valid_to', models.DateField(blank=True, null=True)),
                ('legal_entity', models.ForeignKey(db_column='legal_entity_id', on_delete=django.db.models.deletion.PROTECT, to='core.legalentity')),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
                ('unit', models.ForeignKey(db_column='unit_id', on_delete=django.db.models.deletion.CASCADE, related_name='entity_versions', to='core.unit')),
            ],
            options={
                'db_table': 'unit_legal_entities',
                'constraints': [django.contrib.postgres.constraints.ExclusionConstraint(expressions=[('unit', '='), (core.models.DateRange('valid_from', 'valid_to', models.Value('[)')), '&&')], name='unit_legal_entities_no_overlap'), models.CheckConstraint(condition=models.Q(('valid_to__isnull', True), ('valid_to__gt', models.F('valid_from')), _connector='OR'), name='unit_legal_entities_dates_in_order')],
            },
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(RESOLVER, DROP_RESOLVER),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.RunSQL(TRIGGERS, DROP_TRIGGERS),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
