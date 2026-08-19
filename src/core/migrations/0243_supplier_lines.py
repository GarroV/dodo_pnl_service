"""Две служебные строки P&L, на которых стоит четвёртая очередь (T151, T152).

Обе — **общие** (`tenant_id is null`), как и остальной единый справочник статей:
они не настройка партнёра, а часть устройства продукта. Заводятся миграцией, а
не сидом, ровно поэтому: сид наполняет разработку и демо, а платежи поставщикам
работают у каждого партнёра, включая того, у кого сид никогда не гонялся.

**`supplier_payment` — «Оплата поставщику», вид `transfer`.**

Платёж по счёту — это движение денег, а не расход: расход уже признан самим
счётом в его периоде учёта. Посчитать оба значило бы удвоить траты в P&L. Вид
`transfer` для этого уже есть и уже везде исключён из отчётов
(`pnl_by_unit`, `pnl_by_network`, `pnl_report`, выгрузка `collect_expenses`) —
одним словом `kind`, а не проверкой в каждом потребителе.

Почему платёж вообще факт, а не своя таблица. У фактов уже есть всё, что
платежу нужно и что своей таблице пришлось бы написать заново: идемпотентность
по `dedup_key`, версионирование заменой, запрет правки закрытого месяца
(`facts_guard`), изоляция партнёров и видимость по точке, регистру и кассе. Своя
таблица означала бы второй путь записи денег — и он обошёл бы всё перечисленное
разом.

**`unclassified` — «Не разобрано», вид `expense`.**

Строка, для которой статья ещё не выбрана, обязана быть **видна числом**, а не
пропадать (DoD очереди). Пустой `pnl_item_id` для этого не годится физически:
колонка `not null`, а `pnl_lines` соединяется со статьёй внутренним соединением
— строка без статьи исчезла бы из P&L молча, то есть ровно так, как нельзя.

Служебная статья вида «расход» держит сумму в итоге по сети и в выгрузке, а
инбокс собирается по ней же. Уходит она оттуда единственным способом — когда
человек назначит настоящую статью.

**Нераспределённое перестаёт считать переводы.** `facts_unallocated` отвечает на
вопрос «что мешает закрыть месяц»: это суммы, которые есть в P&L по сети и
которых нет ни на одной точке. Перевода в P&L нет вовсе — ни по сети, ни по
точке, — поэтому платёж без точки там не мешает ничему, и его строка была бы
неправдой в списке, который читают ради закрытия месяца. Условие ставится по
виду статьи, а не по источнику: так же, как оно уже стоит во всех отчётах.
"""
from django.db import migrations

# Ключи фиксированные: на них ссылается код продукта (`web/suppliers.py`), и
# вычислять их на каждой базе заново означало бы искать статью по названию.
UNCLASSIFIED = "9d2f0000-0000-4000-8000-0000000000c1"
SUPPLIER_PAYMENT = "9d2f0000-0000-4000-8000-0000000000c2"

# Общие строки справочника пишутся при **выключенных** политиках, и это не
# обход разграничения, а единственный способ их завести. На `pnl_items` стоит
# `force row level security` (`0004_rls`), а писать общие строки
# (`tenant_id is null`) не вправе никто из приложения — по замыслу: иначе
# пользователь одного партнёра менял бы справочник всей сети. Миграции при этом
# накатываются ролью-владельцем БЕЗ `bypassrls` (T058, на площадке другой не
# бывает), то есть политики действуют и на неё.
#
# Найдено проверкой `tests/test_plain_owner.py`: без этих двух строк миграция
# падала с `new row violates row-level security policy for table "pnl_items"` —
# локально, где владелец суперпользователь, всё выглядело исправным.
#
# Выключение живёт внутри транзакции самой миграции (DDL в Postgres
# транзакционный), поэтому наружу состояние «политики сняты» не выходит.
# `force` при этом не трогается: он хранится отдельным признаком и переживает
# `disable`/`enable`.
LINES = f"""
alter table pnl_items disable row level security;

insert into pnl_items (id, tenant_id, code, title, kind, sort_order) values
    ('{UNCLASSIFIED}', null, 'unclassified', 'Не разобрано', 'expense', 88),
    ('{SUPPLIER_PAYMENT}', null, 'supplier_payment', 'Оплата поставщику', 'transfer', 96)
on conflict (tenant_id, code) do nothing;

-- Отложенные ключи проверяются на коммите, а до тех пор висят «незавершёнными
-- событиями триггеров» — и `alter table` при них не выполняется вовсе. Гоним их
-- сейчас же и возвращаем режим обратно: он действует до конца транзакции, а
-- транзакция здесь общая на всю миграцию.
set constraints all immediate;
set constraints all deferred;

alter table pnl_items enable row level security;

comment on table pnl_items is
    'Статьи P&L. Общие строки (tenant_id is null) — часть устройства продукта: у каждого партнёра они одни и те же';
"""

DROP_LINES = f"""
alter table pnl_items disable row level security;
delete from pnl_items where id in ('{UNCLASSIFIED}', '{SUPPLIER_PAYMENT}');
alter table pnl_items enable row level security;
"""

# Представление пересоздаётся целиком: `create or replace view` не умеет менять
# состав колонок, а условие меняется вместе с соединением.
UNALLOCATED = """
drop view if exists facts_unallocated;

create view facts_unallocated with (security_invoker = true) as
select f.tenant_id, f.period, f.id as fact_id, f.title, f.amount, f.currency,
       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source,
       f.expense_item_id
  from facts f
  join pnl_items i on i.id = f.pnl_item_id
  left join counterparties c on c.id = f.counterparty_id
 where f.superseded_at is null
   and f.allocation = 'pending'
   -- Перевод денег в P&L не входит ни по сети, ни по точке, поэтому его
   -- отсутствие на точке ничего не задерживает. Оставленный здесь, он выглядел
   -- бы препятствием к закрытию месяца, которым не является.
   and i.kind <> 'transfer';

comment on view facts_unallocated is
    'Суммы без точки: что мешает закрыть месяц. Факт без правила обязан быть видимым, а не исчезать. Переводы не считаются: в P&L их нет вовсе';

grant select on facts_unallocated to app_user;
"""

UNALLOCATED_BACK = """
drop view if exists facts_unallocated;

create view facts_unallocated with (security_invoker = true) as
select f.tenant_id, f.period, f.id as fact_id, f.title, f.amount, f.currency,
       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source,
       f.expense_item_id
  from facts f
  left join counterparties c on c.id = f.counterparty_id
 where f.superseded_at is null and f.allocation = 'pending';

grant select on facts_unallocated to app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0242_counterparties"),
    ]

    operations = [
        migrations.RunSQL(LINES, DROP_LINES),
        migrations.RunSQL(UNALLOCATED, UNALLOCATED_BACK),
    ]
