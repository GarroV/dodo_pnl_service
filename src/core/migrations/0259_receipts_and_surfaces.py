"""Чек к расходу наличными и «где выбирается» у статьи расходов (T184, T191).

Две задачи в одной миграции, потому что обе про одно и то же место продукта —
внесение траты с телефона управляющего, — и накатывать их врозь незачем.

## Чек к расходу (T184)

Модуль 6 эталона стоит на мысли «наличный расход — два независимых факта»:
деньги ушли из кассы, когда управляющий их отдал, а в P&L расход входит тогда,
когда бухгалтер увидит бумагу. До этой миграции второй половины в схеме не
существовало вовсе: сумма из чека вводилась руками, а самого чека в продукте не
оставалось — то есть подтвердить трату через месяц было нечем.

**Почему отдельная таблица, а не `document_files`.** Файл бумаги с точки виден
тогда, когда виден его документ (`follows_its_document`, `0246`), а документ
режется только по точке. Расход наличными режется ещё и по регистру учёта и по
кассе — значит чек внутреннего расхода лежал бы в таблице, политика которой про
регистры ничего не знает, и был бы виден тому, кому сам расход не виден.
Фотография чека показывает и сумму, и за что заплачено, так что это утечка
ровно того, ради чего политики на `facts` и написаны.

Поэтому таблица своя, а её политика **зовёт сам факт** (D014). Условия по точке,
регистру и кассе здесь не повторяются ни одной строкой: повторённое правило
однажды разойдётся с оригиналом, и разойдётся молча.

**Ключ — запись расхода, а не строка факта.** Правка расхода идёт заменой
версии, у новой версии другой `id`. Привязка чека к `id` осиротела бы на первой
же правке суммы. Ключ записи (`entry_key`, из которого собран `facts.dedup_key`)
живёт через все версии, поэтому чек переживает и правку, и удаление, и сторно.

**Из этого следует, что приложить чек можно и к расходу закрытого месяца.** Ни
одной колонки в `facts` запись чека не трогает, `facts_guard` её не видит и
видеть не должен: деньги от появления фотографии не меняются. Обратное решение
означало бы, что расход, у которого чек нашёлся в июле, останется без бумаги
навсегда.

## Где выбирается статья (T191)

Модуль 14 эталона: «список длиной в сорок строк никто не читает». Управляющий на
телефоне выбирает из шести статей с пометкой «расходы наличными», а полный список
нужен бухгалтеру и операционному директору.

**Это не право видеть статью.** Выбрать её может любая роль, и никакой роли
статьи не запрещены — поэтому поверхность живёт колонкой в справочнике, а не
политикой. Смешать эти два смысла значило бы однажды спрятать статью от
бухгалтера, поправив форму управляющего.

**Умолчание — все три поверхности.** Существующие статьи получают полный набор,
то есть ведут себя ровно как до миграции. Обратное умолчание («нигде») вычистило
бы списки выбора у всех сразу и молча: форма показала бы пустой список, а
причина лежала бы в колонке, о которой никто не знает.

**Пустой набор запрещён базой.** Статья, не предлагаемая нигде, — не «закрытая»
(у закрытой есть `valid_to`, и она остаётся в прошлых записях), а строка, которую
нельзя выбрать ни в одной форме продукта. Запрет стоит в базе, а не в форме:
писать сюда будут и загрузка файла бухгалтера, и будущий API — тот же довод, что
у ограничения на пустое название.
"""
import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models

COMMENTS = """
comment on column expense_items.surfaces is
    'Где статья предлагается: cash — расход наличными, invoice — разнесение накладной, bank — разбор выписки. Это не право видеть статью, а место её предложения';

comment on table cash_receipts is
    'Чек к расходу наличными. Отдельно от document_files: политика зовёт сам факт, поэтому чек режется по точке, регистру и кассе так же, как расход';
comment on column cash_receipts.entry_key is
    'Ключ записи расхода (facts.dedup_key без приставки manual:cash:). Не id факта: правка расхода заводит новую версию, а чек — та же бумажка';
comment on column cash_receipts.media_type is
    'Тип содержимого, определённый по самим байтам, а не по слову из браузера';
comment on column cash_receipts.sha256 is
    'Отпечаток файла. По нему видно, что переснимали тот же чек';
"""

POLICIES = """
alter table cash_receipts enable row level security;
alter table cash_receipts force  row level security;

create policy tenant_isolation on cash_receipts
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Кому виден чек, решает его расход. Правило не переписывается, а зовётся
-- (D014): повтори здесь условия по точке, регистру и кассе — и однажды они
-- разойдутся с теми, что стоят на `facts`, причём молча. Подзапрос идёт под
-- политиками смотрящего: расход, которого он не видит, не найдётся, и чек не
-- отдастся.
--
-- `as restrictive` обязательно: обычные политики объединяются через OR, и рядом
-- с `tenant_isolation` эта не сужала бы выборку вовсе.
--
-- Про `superseded_at` здесь намеренно ничего не сказано. Удалённый и заменённый
-- расход остаются видимыми в реестре (состоянием «удалён»/«заменён»), и его чек
-- обязан оставаться видимым вместе с ним: иначе строка на экране есть, а бумаги
-- к ней нет — и объяснить это человеку нечем.
create policy follows_its_expense on cash_receipts
    as restrictive for all
    using (exists (
        select 1 from facts f
         where f.tenant_id = cash_receipts.tenant_id
           and f.dedup_key = 'manual:cash:' || cash_receipts.entry_key
    ))
    with check (exists (
        select 1 from facts f
         where f.tenant_id = cash_receipts.tenant_id
           and f.dedup_key = 'manual:cash:' || cash_receipts.entry_key
    ));
"""

DROP_POLICIES = """
drop policy if exists follows_its_expense on cash_receipts;
drop policy if exists tenant_isolation on cash_receipts;
alter table cash_receipts no force row level security;
alter table cash_receipts disable row level security;
"""

# Права роли приложения выписываются явно, а не надеются на `alter default
# privileges` из `0005_app_role`: оно покрывает новые таблицы только пока
# миграции накатывает та же роль, что их выдавала. Отказ по привилегии выглядел
# бы как работающее разграничение, а продукт не показал бы ни одного чека.
PRIVILEGES = """
grant select, insert, update, delete on cash_receipts to app_user;
"""

DROP_PRIVILEGES = """
revoke all on cash_receipts from app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа: разведённые номера сами по себе от второго
        # листа миграций не спасают, а на этом проекте он расходился трижды.
        ("core", "0258_employee_units"),
    ]

    operations = [
        migrations.CreateModel(
            name="CashReceipt",
            fields=[
                ("id", models.UUIDField(
                    db_default=models.Func(function="gen_random_uuid"),
                    primary_key=True, serialize=False,
                )),
                ("entry_key", models.TextField()),
                ("media_type", models.TextField()),
                ("byte_size", models.IntegerField()),
                ("content", models.BinaryField()),
                ("sha256", models.TextField()),
                ("file_name", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=models.Func(function="now"))),
                ("created_by", models.UUIDField(blank=True, null=True)),
            ],
            options={"db_table": "cash_receipts"},
        ),
        migrations.AddField(
            model_name="expenseitem",
            name="surfaces",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.TextField(), db_default=["cash", "invoice", "bank"],
            ),
        ),
        migrations.AddConstraint(
            model_name="expenseitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("surfaces__len__gt", 0),
                    ("surfaces__contained_by", ["cash", "invoice", "bank"]),
                ),
                name="expense_items_surfaces_known",
            ),
        ),
        migrations.AddField(
            model_name="cashreceipt",
            name="tenant",
            field=models.ForeignKey(
                db_column="tenant_id", on_delete=django.db.models.deletion.CASCADE,
                to="core.tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashreceipt",
            constraint=models.UniqueConstraint(
                fields=("tenant", "entry_key"), name="cash_receipts_entry_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashreceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(("byte_size__gt", 0)),
                name="cash_receipts_size_is_positive",
            ),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
