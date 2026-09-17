"""Рамка страны перестаёт быть словом на схеме (T192, issue #182).

**Что было.** Значение уровня партнёра, группы или человека могло быть мягче
страны, и никто не останавливал: процент больничного опускался с 0,65 до 0,10
обычной формой на экране правил. Продукт при этом обещал обратное — эталон
модуля 17 говорит «страна задаёт значение и рамку, юрлицо доопределяет внутри
рамки», а журнал блока `rules` записал за T165 прямо: «рамка НЕ реализована…
партнёр всегда вправе переопределить у себя любое значение в любую сторону».

**Что появляется.** Сама рамка живёт в теле пресета страны разделом `frames` —
это данные, а не схема, и миграции они не требуют (`payroll/frames.py`,
`presets/serbia-2026.yaml`). Схема нужна второй половине эталона: «Попытка выйти
за рамку — 24.07, Nikola Perić… Отказ, который никуда не записан, повторят
завтра».

**Почему таблица, а не колонка в `rule_overrides`.** Отвергнутая правка
переопределением не становится: строки правила от неё не возникает вовсе. Класть
её туда же значило бы завести в таблице действующих правил строки, которые не
действуют, и каждому читателю (сборка пресета, история версий, счётчик «задано
отдельно») пришлось бы помнить про них и отфильтровывать. Один забывший — и
отвергнутое значение уезжает в расчёт.

**Из чего следует форма политик.** Вставляет тот, кто вправе вести правила
(`rules.manage`): попытка возникает ровно там, где человек эту правку и делает.
Читает — он же: журнал отказов лежит на карточке правила рядом с историей
версий. А `update` и `delete` не разрешены никому: журнал, из которого можно
вычеркнуть строку, журналом не является. Двумя замками, как у `platform_admins`
(`0248`): политик на запись нет И права роли отозваны явно — политика, которую
однажды добавят «чтобы починить экран», без отозванного права открыла бы дверь
молча.

**Чего эта миграция намеренно не делает.** Проверки рамки в базе нет, и это
решение, а не пропуск. Рамка — правило продукта, а не граница доступа: она не
защищает одного партнёра от другого (это делает `tenant_isolation`) и не
защищает закрытый месяц (это делает `payrun_frozen_guard`). Записать её вторым
разом на PL/pgSQL значило бы завести два ответа на вопрос «что мягче» — на
Python и на SQL, — и разошлись бы они молча, как разошёлся бы второй порядок
наложения слоёв. Запись идёт одной дорогой (`web/rules.save_override`, через
неё же пишет форма группы в справочниках), и проверка стоит на ней.
"""

import django.db.models.deletion
from django.db import migrations, models

import core.fields

COMMENTS = """
comment on table rule_frame_attempts is
    'След отказа: попытка поставить значение мягче правил страны. Строки не правятся и не удаляются — это журнал аудита, а не черновик (T192)';
comment on column rule_frame_attempts.wanted is
    'Что пытались поставить. Рядом лежит значение страны на ту же дату: рамка со временем едет, и через год строка обязана читаться без пересборки пресета';
comment on column rule_frame_attempts.frame is
    'Снимок рамки на момент отказа: режим, границы и источник (статья закона)';
comment on column rule_frame_attempts.created_by_name is
    'Снимок имени автора: право вести правила не даёт читать чужие строки users, а след аудита обязан пережить переименование учётки';
"""

POLICIES = """
alter table rule_frame_attempts enable row level security;
alter table rule_frame_attempts force  row level security;

create policy tenant_isolation on rule_frame_attempts
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Ведёт правила — видит и отказы по ним: журнал лежит на карточке правила
-- рядом с историей версий, и разводить их по разным правам было бы враньём о
-- том, что одно из двух кому-то нужно отдельно.
create policy rules_manage_read on rule_frame_attempts
    as restrictive for select
    using (app_has_permission(tenant_id, 'rules.manage'));

create policy rules_manage_insert on rule_frame_attempts
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'rules.manage'));
"""

DROP_POLICIES = """
drop policy if exists rules_manage_insert on rule_frame_attempts;
drop policy if exists rules_manage_read on rule_frame_attempts;
drop policy if exists tenant_isolation on rule_frame_attempts;
alter table rule_frame_attempts no force row level security;
alter table rule_frame_attempts disable row level security;
"""

# Второй замок: политик на update и delete нет, и права на них отозваны. Одного
# замка мало — политику однажды добавят «чтобы починить экран», и отсутствие
# права окажется единственным, что удержало журнал от правки.
PRIVILEGES = """
grant select, insert on rule_frame_attempts to app_user;
revoke update, delete on rule_frame_attempts from app_user;
"""
DROP_PRIVILEGES = "revoke all on rule_frame_attempts from app_user;"


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0267_employee_units_screen'),
    ]

    operations = [
        migrations.CreateModel(
            name='RuleFrameAttempt',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('path', models.TextField()),
                ('scope_type', core.fields.EnumField(db_type_name='rule_scope')),
                ('scope_id', models.UUIDField(blank=True, null=True)),
                ('wanted', models.JSONField()),
                ('country_value', models.JSONField(blank=True, null=True)),
                ('frame', models.JSONField()),
                ('valid_from', models.DateField()),
                ('created_by', models.UUIDField(blank=True, null=True)),
                ('created_by_name', models.TextField(db_default='')),
                ('created_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
            ],
            options={
                'db_table': 'rule_frame_attempts',
                'indexes': [models.Index(models.F('tenant'), models.F('path'), models.OrderBy(models.F('created_at'), descending=True), name='rule_frame_attempts_lookup')],
            },
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
