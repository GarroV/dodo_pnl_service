"""Справочник статей расходов и статья у факта (T108).

**Что появляется.** Таблица `expense_items` — то, что человек выбирает, внося
трату, — и колонка `facts.expense_item_id`, которой выбранное запоминается.

**Почему статья не то же самое, что строка P&L.** `pnl_items` — это отчёт: то,
что бухгалтер увидит строкой в P&L («Коммунальные»). Статья — то, чем он
оперирует при внесении: «вода», «электричество», «вывоз мусора». Их несколько на
одну строку отчёта. Свести их в один справочник значило бы выбирать между
раздутым до сотни строк P&L и потерянной подробностью внесения. Поэтому статья
ссылается на строку отчёта, а не заменяет её.

**Справочник заводится пустым.** Список статей придёт с файла бухгалтера Сербии
(вопрос Q015 владельцу отправлен, ответа нет). Выдуманный список означал бы, что
одна и та же трата у нас и у неё называется по-разному, и вскрылось бы это на
первой сборке P&L — когда сходиться уже поздно. Пустой справочник честнее: он
виден пустым и требует наполнения, а не притворяется готовым.

**Названия хранятся словарём по языкам интерфейса.** Это исключение из правила
«данные не переводятся» (`web/i18n.py`): у названия статьи есть читатели на
разных языках — сербский бухгалтер вносит, русскоязычный оперативный директор
читает. Ключи словаря — коды языков как есть (`ru`, `en`, `sr-latn`), без своего
списка соответствий: второй список языков рядом с `settings.LANGUAGES` разошёлся
бы с ним молча.

**`facts.expense_item_id` пустой у всего, что статьи не имеет** — у зарплатных
строк, выручки коннектора, переводов между кассой и банком. Строка P&L при этом
у факта заполнена всегда и остаётся своей колонкой: она копируется из статьи в
момент записи, чтобы переименование или перепривязка статьи не двигали уже
собранный отчёт закрытого месяца.

**`facts_same` переписывается вместе с колонкой.** Функция отвечает на вопрос
«одно ли это событие по существу», и от её ответа зависит идемпотентность записи
факта. Не добавить в неё новую колонку значило бы, что смена **только** статьи
не считается изменением: повторная запись вернула бы `unchanged`, и правка молча
не применилась бы.

**Право на ведение — то же `directory.manage`**, что у остальных справочников
(`0130`), и той же формой политик: `as restrictive` только на запись, чтение не
трогаем. Статьи обязаны видеть все, кто вносит расходы, — а вести их вправе один
администратор сети.
"""

import django.db.models.deletion
from django.db import migrations, models

# --- комментарии в самой базе -------------------------------------------------
# Схема должна объясняться тому, кто пришёл в неё psql-ом, а не только тому, кто
# читает этот файл (соглашение проекта, `0003_comments`).
COMMENTS = """
comment on table expense_items is
    'Статьи расходов партнёра: то, что человек выбирает, внося трату. Ссылаются на строку P&L, не заменяют её';
comment on column expense_items.code is
    'Код статьи, уникальный у партнёра. По нему статья сходится с файлом бухгалтера при загрузке';
comment on column expense_items.titles is
    'Названия по языкам интерфейса: {"ru": …, "en": …, "sr-latn": …}. Ключи — коды языков из настроек';
comment on column expense_items.pnl_item_id is
    'Строка P&L, в которую статья попадает в отчёте. Нескольким статьям законно соответствует одна строка';
comment on column expense_items.valid_from is
    'С какой даты статьёй можно пользоваться';
comment on column expense_items.valid_to is
    'Статья закрывается датой, а не удалением: закрытые месяцы на неё ссылаются';
comment on column facts.expense_item_id is
    'Статья расходов, выбранная при внесении. Пусто у фактов без статьи: зарплата, выручка, переводы';
"""

# --- «одно ли это событие» ------------------------------------------------------
# Переписывается целиком, а не правится: список колонок в этой функции — и есть
# определение того, что считается изменением факта.
FACTS_SAME = """
create or replace function facts_same(a facts, b facts)
returns boolean
language sql immutable
as $$
    select (a.period, a.doc_date, a.unit_id, a.legal_entity_id, a.pnl_item_id,
            a.expense_item_id,
            a.ledger, a.counterparty_id, a.amount, a.currency, a.amount_report,
            a.report_currency, a.quantity, a.uom, a.title, a.note, a.channel,
            a.source, a.source_ref, a.document_id, a.line_no,
            a.allocation, a.allocation_rule_id, a.allocation_share, a.parent_fact_id)
        is not distinct from
           (b.period, b.doc_date, b.unit_id, b.legal_entity_id, b.pnl_item_id,
            b.expense_item_id,
            b.ledger, b.counterparty_id, b.amount, b.currency, b.amount_report,
            b.report_currency, b.quantity, b.uom, b.title, b.note, b.channel,
            b.source, b.source_ref, b.document_id, b.line_no,
            b.allocation, b.allocation_rule_id, b.allocation_share, b.parent_fact_id)
$$;

comment on function facts_same(facts, facts) is
    'Одно ли это событие по существу. Служебные поля не считаются изменением, иначе идемпотентности бы не было';
"""

# Откат возвращает список колонок без статьи — тот, что был в `0230`.
FACTS_SAME_BACK = """
create or replace function facts_same(a facts, b facts)
returns boolean
language sql immutable
as $$
    select (a.period, a.doc_date, a.unit_id, a.legal_entity_id, a.pnl_item_id,
            a.ledger, a.counterparty_id, a.amount, a.currency, a.amount_report,
            a.report_currency, a.quantity, a.uom, a.title, a.note, a.channel,
            a.source, a.source_ref, a.document_id, a.line_no,
            a.allocation, a.allocation_rule_id, a.allocation_share, a.parent_fact_id)
        is not distinct from
           (b.period, b.doc_date, b.unit_id, b.legal_entity_id, b.pnl_item_id,
            b.ledger, b.counterparty_id, b.amount, b.currency, b.amount_report,
            b.report_currency, b.quantity, b.uom, b.title, b.note, b.channel,
            b.source, b.source_ref, b.document_id, b.line_no,
            b.allocation, b.allocation_rule_id, b.allocation_share, b.parent_fact_id)
$$;
"""

# --- разграничение доступа ------------------------------------------------------
# Изоляция тенанта — как у всех таблиц с `tenant_id`. Право вести справочник —
# как у остальных пяти (`0130`): ограничивающие политики только на запись.
POLICIES = """
alter table expense_items enable row level security;
alter table expense_items force  row level security;

create policy tenant_isolation on expense_items
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

create policy directory_manage_insert on expense_items
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_update on expense_items
    as restrictive for update
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_delete on expense_items
    as restrictive for delete
    using (app_has_permission(tenant_id, 'directory.manage'));
"""

DROP_POLICIES = """
drop policy if exists directory_manage_delete on expense_items;
drop policy if exists directory_manage_update on expense_items;
drop policy if exists directory_manage_insert on expense_items;
drop policy if exists tenant_isolation on expense_items;
alter table expense_items no force row level security;
alter table expense_items disable row level security;
"""

# Привилегии роли продукта. Без них разграничение выглядело бы работающим, а
# продукт не читал бы ничего: отказ по привилегии и отказ по политике — разные
# вещи, и путать их нельзя.
PRIVILEGES = "grant select, insert, update, delete on expense_items to app_user;"
DROP_PRIVILEGES = "revoke all on expense_items from app_user;"


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа — схема фактов, к которой добавляется колонка.
        ('core', '0230_facts'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExpenseItem',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('code', models.TextField()),
                ('titles', models.JSONField(db_default={})),
                ('valid_from', models.DateField()),
                ('valid_to', models.DateField(blank=True, null=True)),
                ('created_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('created_by', models.UUIDField(blank=True, null=True)),
                ('pnl_item', models.ForeignKey(db_column='pnl_item_id', on_delete=django.db.models.deletion.PROTECT, to='core.pnlitem')),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
            ],
            options={
                'db_table': 'expense_items',
            },
        ),
        migrations.AddField(
            model_name='fact',
            name='expense_item',
            field=models.ForeignKey(blank=True, db_column='expense_item_id', null=True, on_delete=django.db.models.deletion.PROTECT, to='core.expenseitem'),
        ),
        migrations.AddConstraint(
            model_name='expenseitem',
            constraint=models.UniqueConstraint(fields=('tenant', 'code'), name='expense_items_tenant_code_uniq'),
        ),
        migrations.AddConstraint(
            model_name='expenseitem',
            constraint=models.CheckConstraint(condition=models.Q(('titles', {}), _negated=True), name='expense_items_titles_not_empty'),
        ),
        migrations.AddConstraint(
            model_name='expenseitem',
            constraint=models.CheckConstraint(condition=models.Q(('valid_to__isnull', True), ('valid_to__gt', models.F('valid_from')), _connector='OR'), name='expense_items_validity'),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(FACTS_SAME, FACTS_SAME_BACK),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
