"""Управляющий приносит бумагу с точки: накладную и чек (T174, D047).

Что решает эта миграция. У управляющего точки в сборе первички роль одна —
**донести бумагу**, а не считать: накладную поставщика и чек с точки он
фотографирует и скидывает, а поставщика, статью и период учёта назначает потом
бухгалтер. До этой миграции такого состояния в схеме не было вовсе: документ без
строк ничем не отличался от документа, у которого строки не записались, а точки
у документа не было ни одной — то есть чужую бумагу нечем было отсечь.

**Точка у документа — это чья бумага, а не куда лёг расход.** Разница не
формальная: накладную приносят с NS1, а расход по ней может лечь на всю сеть
(аренда, реклама) или на другую точку — точку расхода назначает бухгалтер при
разборе, и живёт она у факта, где и была. Здесь же — тот, кто бумагу принёс, и
ради этого поля политика: управляющему видна и доступна на запись только своя
точка, и отсекает это база, а не форма (D014).

**Правило зовётся, а не переписывается** — `app_unit_is_visible`, та же функция,
что закрывает восемь таблиц с первой волны. Взято именно оно, а не
`app_network_row_is_visible`: у документов без точки (все счета, заведённые до
сегодня, и вся банковская выписка завтра) отсутствие точки означает «точка живёт
у строки», и спрятать их от управляющего значило бы отобрать у него список его же
счетов. Бумага с точки при этом обязана точку назвать — за это отвечает
ограничение `source_documents_paper_names_its_unit`, а не форма: без него
управляющий сдавал бы бумагу «на всю сеть», и она была бы видна всем.

**`handed_over_at` — отметка «бумагу принесли», и она несёт смысл.** Инбокс
собирает по ней бумаги, ждущие разбора, и по ней же они отличаются от документа,
у которого строки не записались из-за отказа. Это ровно тот молчаливый сбой,
который в проекте-предшественнике стоил дорого: документ без строк выглядел
разобранным. Здесь наоборот — бумага без строк громко стоит в инбоксе, а её
сумма (со слов управляющего) в P&L не входит, потому что в P&L входят факты, а
их ещё нет.

**Файл живёт отдельной таблицей, а не колонкой в документе.** Django выбирает
все колонки модели, и фотография накладной уезжала бы в каждый список счетов —
десятки мегабайт на экран. Отдельная таблица ещё и отвечает на вопрос
«кто видит файл» одним правилом: файл виден ровно тогда, когда виден его
документ, — политика зовёт документ, а не повторяет его условия.

Функцию `upsert_document` (`0230`) здесь не трогаем намеренно: её
`on conflict do update` перечисляет колонки, новых в списке нет, и точка бумаги
при разборе остаётся прежней. Это и требуется — разбор назначает статью, а не
переставляет точку, с которой бумагу принесли.
"""
from django.db import migrations, models

import core.models

COMMENTS = """
comment on column source_documents.unit_id is
    'Чья это бумага: точка, с которой её принесли. Не точка расхода — та живёт у факта и назначается при разборе';
comment on column source_documents.handed_over_at is
    'Когда бумагу принесли с точки. Заполнено — документ ждёт разбора бухгалтером; пусто — обычный документ (счёт, выписка)';

comment on table document_files is
    'Фотография или файл принесённой бумаги. Отдельно от документа: Django выбирает все колонки модели, и файл уезжал бы в каждый список счетов';
comment on column document_files.media_type is
    'Тип содержимого, определённый по самим байтам, а не по слову из браузера';
comment on column document_files.sha256 is
    'Отпечаток файла. По нему видно, что принесли ту же бумагу второй раз';
"""

POLICIES = """
-- Точка бумаги. `as restrictive` — иначе политика объединялась бы через OR и не
-- сужала бы выборку вовсе (тот же довод, что у `facts`). `for all`, а не
-- `for select`: без `with check` управляющий не видел бы чужую точку, но мог бы
-- в неё писать — то есть сдавать бумагу за соседа.
--
-- Документ без точки виден всем в тенанте: у счёта и у строки выписки точка
-- живёт у факта, и спрятать их значило бы отобрать у управляющего его же счета.
create policy unit_visibility on source_documents
    as restrictive for all
    using (app_unit_is_visible(tenant_id, unit_id))
    with check (app_unit_is_visible(tenant_id, unit_id));

alter table document_files enable row level security;
alter table document_files force  row level security;

create policy tenant_isolation on document_files
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Кому виден файл, решает его документ. Правило не переписывается, а зовётся
-- (D014): повтори здесь условия по точке — и однажды они разойдутся с теми, что
-- стоят на документе, причём молча. Подзапрос идёт под политиками смотрящего:
-- документ, которого он не видит, не найдётся, и файл не отдастся.
create policy follows_its_document on document_files
    as restrictive for all
    using (exists (
        select 1 from source_documents d where d.id = document_files.document_id
    ))
    with check (exists (
        select 1 from source_documents d where d.id = document_files.document_id
    ));
"""

DROP_POLICIES = """
drop policy if exists follows_its_document on document_files;
drop policy if exists tenant_isolation on document_files;
alter table document_files no force row level security;
alter table document_files disable row level security;
drop policy if exists unit_visibility on source_documents;
"""

# Права роли приложения выписываются явно, а не надеются на `alter default
# privileges` из `0005_app_role`: оно покрывает новые таблицы только пока
# миграции накатывает та же роль, что их выдавала. Отказ по привилегии выглядел
# бы как работающее разграничение, а продукт не читал бы ни одной бумаги.
PRIVILEGES = """
grant select, insert, update, delete on document_files to app_user;
"""

DROP_PRIVILEGES = """
revoke all on document_files from app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа: разведённые номера сами по себе от второго
        # листа миграций не спасают, а на этом проекте он расходился трижды.
        ("core", "0245_roles_shipped_shape"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcedocument",
            name="unit",
            field=models.ForeignKey(
                blank=True, db_column="unit_id", null=True,
                on_delete=models.deletion.SET_NULL, to="core.unit",
            ),
        ),
        migrations.AddField(
            model_name="sourcedocument",
            name="handed_over_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="sourcedocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("handed_over_at__isnull", True))
                | models.Q(("unit__isnull", False)),
                name="source_documents_paper_names_its_unit",
            ),
        ),
        migrations.AddIndex(
            model_name="sourcedocument",
            index=models.Index(
                models.F("tenant"), models.F("handed_over_at").desc(),
                condition=models.Q(("handed_over_at__isnull", False)),
                name="source_docs_handed_over_idx",
            ),
        ),
        migrations.CreateModel(
            name="DocumentFile",
            fields=[
                ("document", models.OneToOneField(
                    db_column="document_id", on_delete=models.deletion.CASCADE,
                    primary_key=True, serialize=False, to="core.sourcedocument",
                )),
                ("tenant", models.ForeignKey(
                    db_column="tenant_id", on_delete=models.deletion.CASCADE,
                    to="core.tenant",
                )),
                ("media_type", models.TextField()),
                ("byte_size", models.IntegerField()),
                ("content", models.BinaryField()),
                ("sha256", models.TextField()),
                ("created_at", models.DateTimeField(
                    db_default=core.models.now_default())),
                ("created_by", models.UUIDField(blank=True, null=True)),
            ],
            options={"db_table": "document_files"},
        ),
        migrations.AddConstraint(
            model_name="documentfile",
            constraint=models.CheckConstraint(
                condition=models.Q(("byte_size__gt", 0)),
                name="document_files_size_is_positive",
            ),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
