"""
Подневное хранение табеля (T019, решение D011).

Часы вводятся числом за месяц, но хранятся по дням. Причина не в отчётности:
точка учёта стоит на строке `timesheets`, и когда сотрудника переводят между
точками посреди месяца, месяц нужно разрезать по дате. Без подневных данных это
означало бы угадывание и миграцию данных задним числом.

Что здесь руками, а не автогенерацией Django:

1. **Внешний ключ с `on delete cascade`.** Django исполняет каскад в Python,
   поэтому в SQL его нет вовсе. Удаление табеля мимо ORM — сидом, обслуживанием,
   обычным `delete from` — оставило бы дни сиротами. Модель объявлена с
   `db_constraint=False` именно ради того, чтобы ключ был создан здесь.
2. **Политики RLS.** Список таблиц в `0004_rls` — снимок на момент той миграции;
   новая таблица закрывается там, где заводится, иначе она молча оказалась бы
   единственной незакрытой.
3. **Комментарии к таблице и колонкам** — тем же коммитом, что и схема.
"""

import django.db.models.deletion
from django.db import migrations, models

FOREIGN_KEY = """
alter table timesheet_days
    add constraint timesheet_days_timesheet_fk
    foreign key (timesheet_id) references timesheets (id) on delete cascade;
"""

DROP_FOREIGN_KEY = """
alter table timesheet_days drop constraint if exists timesheet_days_timesheet_fk;
"""

RLS = """
alter table timesheet_days enable row level security;
-- force обязателен: без него политики не действуют на владельца таблиц, а
-- миграции и обслуживание ходят как раз им — правила были бы украшением.
alter table timesheet_days force row level security;
create policy tenant_isolation on timesheet_days
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));
"""

DROP_RLS = """
drop policy if exists tenant_isolation on timesheet_days;
alter table timesheet_days no force row level security;
alter table timesheet_days disable row level security;
"""

COMMENTS = """
comment on table timesheet_days is
    'Из каких дней сложился месячный итог табеля. Сумма часов по типу равна timesheets.hours того же типа';
comment on column timesheet_days.timesheet_id is
    'Строка табеля-родитель: от неё день берёт сотрудника, период и точку';
comment on column timesheet_days.hour_type is
    'Ключ типа часа из hour_types пресета страны: regular, sick, vacation, …';
comment on column timesheet_days.source is
    'spread — ровная раскладка месячного числа по рабочим дням, настоящих дат за ней нет; manual — число введено человеком в сетке табеля; dodo_is — из учётной системы';
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_users'),
    ]

    operations = [
        migrations.CreateModel(
            name='TimesheetDay',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('work_date', models.DateField()),
                ('hour_type', models.TextField()),
                ('hours', models.DecimalField(decimal_places=2, max_digits=6)),
                ('source', models.TextField(db_default='spread')),
                ('created_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
                ('timesheet', models.ForeignKey(db_column='timesheet_id', db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name='days', to='core.timesheet')),
            ],
            options={
                'db_table': 'timesheet_days',
                'indexes': [models.Index(models.F('tenant'), models.F('timesheet'), name='timesheet_days_lookup_idx')],
                'constraints': [models.UniqueConstraint(fields=('tenant', 'timesheet', 'work_date', 'hour_type'), name='timesheet_days_uniq'), models.CheckConstraint(condition=models.Q(('hours__gte', 0)), name='timesheet_days_hours_check')],
            },
        ),
        migrations.RunSQL(FOREIGN_KEY, DROP_FOREIGN_KEY),
        migrations.RunSQL(RLS, DROP_RLS),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
    ]
