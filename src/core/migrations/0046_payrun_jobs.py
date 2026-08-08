"""Задание на расчёт периода: фоновая задача, её ход и её результат (T024).

Зачем отдельная таблица, а не поля в `payruns`:

1. Задание существует и тогда, когда расчёта ещё нет (нажали кнопку на пустом
   периоде), и тогда, когда расчёт отказался считаться. Вешать это на строку
   расчёта значило бы заводить расчёт ради сообщения о том, что расчёта не будет.
2. Решающее: отметки о ходе работы пишутся по **отдельному соединению**. Расчёт
   идёт одной транзакцией, и всё, записанное внутри неё, снаружи не видно до
   конца — то есть прогресс появился бы на экране ровно тогда, когда он уже не
   нужен. Автономных транзакций в Postgres нет, поэтому таблица прогресса не
   должна попадать в транзакцию расчёта вовсе. Если она туда попадёт, канал
   прогресса встанет на её же блокировке, и снаружи это будет выглядеть как
   зависший расчёт — то есть ровно как то, от чего мы уходим.

**Идемпотентность запуска держит база.** `payrun_jobs_active_uniq` — частичный
уникальный индекс: незавершённое задание на период ровно одно. Второе нажатие
кнопки не заводит второго расчёта, а получает отказ словами. Второй контур —
перевод задания `queued → running` условным `update` в коде задачи: задача, не
сумевшая перевести строку, не делает ничего. Третий, прежний, остаётся на месте:
ведомости пересобираются целиком, а `payslips_payrun_employee_uniq` не даёт
появиться второму комплекту.

**Статус задания — нативный enum**, как остальные словари домена: чужое значение
отвергает база, а не приложение. Тип регистрируется в драйвере
(`core/db_types.py`) — иначе psycopg отдавал бы массивы таких значений строкой.

**Политика — обычная изоляция по тенанту.** Своей видимости по регистрам учёта у
задания нет и быть не должно: в нём нет сумм, только этап и счётчик людей.
Отказ, попавший в `error`, — тот же текст, что человек увидел бы синхронно.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.fields

TYPE = """
create type payrun_job_status as enum ('queued', 'running', 'done', 'failed');

comment on type payrun_job_status is
    'Состояние фоновой задачи расчёта: в очереди, выполняется, завершена, отказ';
"""

DROP_TYPE = "drop type if exists payrun_job_status;"

SCHEMA = """
comment on table payrun_jobs is
    'Задание на расчёт периода: кто запустил, чем занято сейчас, чем кончилось. Прогресс пишется отдельным соединением, поэтому таблица не участвует в транзакции расчёта';

comment on column payrun_jobs.requested_by is
    'Кто нажал кнопку. Его же контекстом задача ходит в базу: работать «от имени системы» фоновый расчёт не должен';
comment on column payrun_jobs.background is
    'true — задача ушла в очередь; false — посчитано прямо в запросе. Различие видно человеку: подменять одно другим молча нельзя';
comment on column payrun_jobs.task_id is
    'Идентификатор задачи в django_q — чтобы задание можно было найти в очереди, когда что-то пошло не так';
comment on column payrun_jobs.stage is
    'Чем задача занята сейчас, словами для человека';
comment on column payrun_jobs.done is
    'Сколько единиц этапа сделано; вместе с total даёт полосу прогресса';
comment on column payrun_jobs.error is
    'Отказ или поломка — тем же текстом, каким расчёт ответил бы синхронно';

-- Незавершённое задание на период ровно одно. Это и есть идемпотентность
-- запуска: второе нажатие кнопки упирается сюда, а не порождает второй расчёт.
-- Частичный индекс, а не обычный: завершённых заданий за период накапливается
-- сколько угодно, и они и есть история запусков.
create unique index payrun_jobs_active_uniq
    on payrun_jobs (tenant_id, period)
    where status in ('queued', 'running');

comment on index payrun_jobs_active_uniq is
    'Один незавершённый расчёт на период: повторный запуск не плодит ведомости';

alter table payrun_jobs enable row level security;
alter table payrun_jobs force row level security;

create policy tenant_isolation on payrun_jobs
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));
"""

DROP_SCHEMA = """
drop policy if exists tenant_isolation on payrun_jobs;
drop index if exists payrun_jobs_active_uniq;
"""


class Migration(migrations.Migration):
    dependencies = [
        # Лист миграций на момент вливания master: без этой зависимости граф
        # разошёлся бы на два листа, и схема не накатилась бы вовсе.
        ("core", "0044_merge_20260808_1149"),
    ]

    operations = [
        # Тип раньше таблицы: колонка status на нём и стоит.
        migrations.RunSQL(TYPE, DROP_TYPE),
        migrations.CreateModel(
            name="PayrunJob",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="gen_random_uuid"),
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("period", models.DateField()),
                (
                    "status",
                    core.fields.EnumField(
                        db_default="queued", db_type_name="payrun_job_status"
                    ),
                ),
                ("requested_by", models.UUIDField(blank=True, null=True)),
                ("background", models.BooleanField(db_default=True)),
                ("task_id", models.TextField(blank=True, null=True)),
                ("stage", models.TextField(db_default="")),
                ("done", models.IntegerField(db_default=0)),
                ("total", models.IntegerField(db_default=0)),
                ("error", models.TextField(db_default="")),
                ("details", models.JSONField(db_default=[])),
                ("created_at", models.DateTimeField(db_default=models.Func(function="now"))),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        db_column="tenant_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "payrun_jobs",
                "indexes": [
                    models.Index(
                        models.F("tenant"),
                        models.F("period"),
                        models.OrderBy(models.F("created_at"), descending=True),
                        name="payrun_jobs_period_idx",
                    )
                ],
            },
        ),
        migrations.RunSQL(SCHEMA, DROP_SCHEMA),
    ]
