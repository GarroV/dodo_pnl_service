"""Коннекторы включаются по одному и по умолчанию выключены (D063).

**Что решается.** Dodo IS умеет отдавать многое — часы, выручку, расход сырья,
списания, справочники. Брать всё это в работу разом нельзя: часть чужих цифр
может считаться не так, как нужно партнёру. Владелец сказал прямо про
себестоимость: «я не уверен, что та же себестоимость считается нормально в додо
ис». Проверить это можно только на живых данных за реальный месяц — но проверять
на живых данных, которые уже попали в расчёт, поздно.

Отсюда таблица: **каждый поток данных включается отдельно и имеет три
состояния**, а не булев флаг «коннектор включён».

| Состояние | Тянем? | Идёт в расчёт и в P&L? |
|---|---|---|
| `off` | нет | нет |
| `trial` | да | **нет** |
| `on` | да | да |

`trial` — тот самый режим, ради которого всё и заводится: цифра приезжает,
ложится рядом с нашей, её видно и можно сравнить за месяц-другой, но ни на один
итог она не влияет. Без него выбор был бы между «не знаем, сходится ли» и
«узнаем, когда уже посчитали неправильно».

**Почему состояние в базе, а не в настройках приложения.** Оно у каждого
партнёра своё: одному себестоимость Dodo IS подходит, другому нет. Настройка в
конфиге означала бы одно значение на весь инстанс, то есть либо чужой риск
всем, либо отказ всем.

**Почему `off` — умолчание для всего.** Поток, о котором никто не знает, не
должен молча попадать в отчёт. Новый поток появляется выключенным, и включает
его человек, а не выкладка.

**Почему перечисление, а не свободный текст.** Список потоков — словарь домена,
как регистры учёта и статусы периода: опечатка в названии потока означала бы
включённым не то, что думает человек, и увидеть это было бы негде. Значение
отвергает база (см. шапку `core/fields.py`).

**Чего здесь нет.** Ни адреса API, ни токена, ни расписания. Секреты живут в
окружении площадки (D043), а не в базе партнёра; расписание — дело коннектора.
Здесь только ответ на вопрос «берём ли мы это в работу», и он один и тот же
независимо от того, как именно данные приедут.
"""
from django.db import migrations, models

import core.fields
from core.models import now_default, uuid_pk

TYPES = """
create type data_feed as enum (
    'hours',       -- отработанные часы; единственное, что берётся из Dodo IS по D062
    'revenue',     -- выручка по точкам с разбивкой
    'cogs',        -- расход сырья, себестоимость
    'write_offs',  -- списания, брак, питание персонала
    'staff',       -- справочник сотрудников
    'units'        -- справочник точек
);

comment on type data_feed is
    'Поток данных из внешней системы. Включается по одному: чужие цифры проверяются порознь, а не пакетом';

create type data_feed_state as enum (
    'off',    -- не тянем вовсе
    'trial',  -- тянем и показываем рядом, но в расчёт и в P&L не берём
    'on'      -- берём в работу
);

comment on type data_feed_state is
    'Насколько доверяем потоку. trial — режим сверки: цифра видна, но ни на один итог не влияет';
"""

DROP_TYPES = """
drop type if exists data_feed_state;
drop type if exists data_feed;
"""

COMMENTS = """
comment on table data_feeds is
    'Какие потоки внешних данных партнёр берёт в работу. Строки нет — поток выключен: умолчание всегда off';
comment on column data_feeds.state is
    'off | trial | on. trial кладёт данные рядом для сравнения и не пускает их в расчёт';
comment on column data_feeds.changed_by is
    'Кто последним менял состояние. Включение чужих цифр в расчёт — решение, у которого должен быть автор';
comment on column data_feeds.last_pulled_at is
    'Когда коннектор последний раз забирал этот поток. Пусто — не забирал ни разу';
comment on column data_feeds.note is
    'Зачем состояние такое. Место для «сверяем с ноября, расходится на 2%» — иначе через полгода причину не вспомнить';
"""

POLICIES = """
alter table data_feeds enable row level security;
alter table data_feeds force  row level security;

create policy tenant_isolation on data_feeds
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));
"""

DROP_POLICIES = """
drop policy if exists tenant_isolation on data_feeds;
alter table data_feeds no force row level security;
alter table data_feeds disable row level security;
"""

# Права роли приложения выписываются явно, а не надеются на `alter default
# privileges` из `0005_app_role`: оно покрывает новые таблицы только пока
# миграции накатывает та же роль, что их выдавала.
PRIVILEGES = """
grant select, insert, update, delete on data_feeds to app_user;
"""

DROP_PRIVILEGES = """
revoke all on data_feeds from app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа: разведённые номера сами по себе от второго
        # листа миграций не спасают, а на этом проекте он расходился трижды.
        ("core", "0259_receipts_and_surfaces"),
    ]

    operations = [
        migrations.RunSQL(TYPES, DROP_TYPES),
        migrations.CreateModel(
            name="DataFeed",
            fields=[
                ("id", uuid_pk()),
                ("feed", core.fields.EnumField(db_type_name="data_feed")),
                ("state", core.fields.EnumField(db_type_name="data_feed_state", db_default="off")),
                ("changed_by", models.UUIDField(blank=True, null=True)),
                ("changed_at", models.DateTimeField(blank=True, null=True)),
                ("last_pulled_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=now_default())),
                (
                    "tenant",
                    models.ForeignKey(
                        db_column="tenant_id",
                        on_delete=models.deletion.CASCADE,
                        to="core.tenant",
                    ),
                ),
            ],
            options={"db_table": "data_feeds"},
        ),
        migrations.AddConstraint(
            model_name="datafeed",
            constraint=models.UniqueConstraint(
                fields=["tenant", "feed"], name="data_feeds_tenant_feed_uniq"
            ),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
