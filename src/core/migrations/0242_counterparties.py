"""Контрагент становится справочником: одно название, ключ Dodo IS, даты (T150).

До этой миграции `counterparties` была таблицей-заготовкой из первой волны: три
поля и ни одного ограничения. Четвёртая очередь опирается на неё всерьёз, и без
трёх правил справочник не выполняет того, ради чего заведён.

**Одно название на партнёра.** Смысл справочника — чтобы траты одного поставщика
складывались. Пока «EPS» и «EPS Elektro» — две строки, они не складываются, и
сверить акт поставщика не с чем; поймать это глазами нельзя, потому что обе
строки выглядят правильными. Другие написания живут в `aliases` — данными, а не
второй карточкой.

**Ключ Dodo IS заведён пустым и заранее.** В шестой очереди строки придётся
сводить со справочником поставщиков Dodo IS (`Accounting → Vendor List`,
`docs/dodo-is-api.md`). Свести их потом по названию — ручная работа на каждый
импорт, поэтому поле стоит с самого начала. Уникальность частичная: пустой ключ
означает «ещё не сопоставлен», и таких строк сколько угодно — обычный уникальный
ключ разрешил бы сегодня ровно одного контрагента на партнёра.

**Даты действия.** Контрагент заканчивается датой, а не удалением: на него
ссылаются факты закрытых месяцев. Существующим строкам дата начала
проставляется из времени их создания — выдумывать им «действует с 1970» значило
бы записать в данные неправду.

**Кто ведёт справочник.** Ограничивающие политики на запись — те же три и по
тому же образцу, что у касс (`0239`): заводит, правит и закрывает контрагента
тот, у кого есть `directory.manage`. Чтение не трогаем вовсе: контрагента обязан
видеть каждый, кто вносит счёт, — иначе внести его будет не на кого.
"""
from django.db import migrations, models

COMMENTS = """
comment on column counterparties.external_id is
    'Идентификатор поставщика в Dodo IS. Пусто — ещё не сопоставлен; по нему пойдёт сведение в шестой очереди';
comment on column counterparties.valid_from is
    'С какой даты работаем с этим контрагентом';
comment on column counterparties.valid_to is
    'С какой даты больше не работаем. Контрагент закрывается датой, а не удалением: на него ссылаются факты закрытых месяцев';
comment on column counterparties.created_by is
    'Кто завёл строку. Проставляется базой из контекста приложения';
"""

# Дата начала существующим строкам — из времени их создания. `coalesce` на
# случай строк, заведённых мимо умолчания (в тестовой фикстуре так и есть).
BACKFILL = """
update counterparties
   set valid_from = coalesce(created_at::date, current_date)
 where valid_from is null;
"""

POLICIES = """
-- Ведёт справочник тот же, кто ведёт остальные семь (`0130`, `0239`).
-- Ограничивающие политики только на запись: контрагента обязан ЧИТАТЬ каждый,
-- кто вносит счёт, иначе счёт не на кого выписать.
create policy directory_manage_insert on counterparties
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_update on counterparties
    as restrictive for update
    using (app_has_permission(tenant_id, 'directory.manage'))
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_delete on counterparties
    as restrictive for delete
    using (app_has_permission(tenant_id, 'directory.manage'));
"""

DROP_POLICIES = """
drop policy if exists directory_manage_delete on counterparties;
drop policy if exists directory_manage_update on counterparties;
drop policy if exists directory_manage_insert on counterparties;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа: разведённые номера сами по себе от второго
        # листа миграций не спасают, а на этом проекте он расходился уже трижды.
        ("core", "0241_insured_author"),
    ]

    operations = [
        migrations.AddField(
            model_name="counterparty",
            name="external_id",
            field=models.TextField(blank=True, null=True),
        ),
        # Пустой, затем заполняется, затем становится обязательным: строки в
        # таблице уже есть, и `not null` сразу отверг бы саму миграцию.
        migrations.AddField(
            model_name="counterparty",
            name="valid_from",
            field=models.DateField(null=True),
        ),
        migrations.AddField(
            model_name="counterparty",
            name="valid_to",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="counterparty",
            name="created_by",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="counterparty",
            name="valid_from",
            field=models.DateField(),
        ),
        migrations.AddConstraint(
            model_name="counterparty",
            constraint=models.UniqueConstraint(
                fields=("tenant", "title"), name="counterparties_tenant_title_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="counterparty",
            constraint=models.UniqueConstraint(
                condition=models.Q(("external_id__isnull", False)),
                fields=("tenant", "external_id"),
                name="counterparties_tenant_external_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="counterparty",
            constraint=models.CheckConstraint(
                condition=models.Q(("valid_to__isnull", True))
                | models.Q(("valid_to__gt", models.F("valid_from"))),
                name="counterparties_validity",
            ),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
