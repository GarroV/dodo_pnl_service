"""Центральная таблица фактов и сборка P&L (T107).

Схема **перенесена**, а не написана заново: источник — `db/migrations/0004_facts.sql`,
1159 строк SQL, проверенных на живом Postgres до переноса (накат с нуля,
разнесение до копейки, идемпотентность повторной загрузки, сборка дерева
статей). Разбор состояния — `docs/backlog-facts.md`. Каталог `db/` после
переноса удалён: схему ведёт Django (D019), а две копии одной схемы разъезжаются
молча.

**Зачем таблица.** Единственное место, куда стекаются все первичные финансовые
события: выручка из Dodo IS, сырьё и списания, фактуры поставщиков, зарплата,
наличные из кассы, переводы, налоги. Каждый следующий модуль пишет сюда, а не
придумывает своё хранилище — иначе P&L собирался бы из нескольких правд.

Это НЕ двойная запись. Управленческий учёт: одна строка = одно событие с
разрезами. Требование к прослеживаемости ровно одно — от строки P&L дойти до
первичного документа и понять, откуда взялась сумма.

**Пять решений, которые стоит понимать до чтения схемы.**

1. *Строка факта = позиция документа, не документ.* Накладная из Метро содержит
   и продукты, и канцелярию — это два факта с разными статьями. Отдельной
   таблицы позиций нет: факт и есть позиция, а связь с документом даёт
   `document_id` + `line_no`.
2. *Дата документа и период учёта — разные поля.* Счёт за электричество за июнь
   приходит в июле: `doc_date` = июль, `period` = июнь. Отчёт всегда по периоду.
3. *Разнесение по точкам выражено в данных, а не в комментарии к сумме.*
   Фактура приходит на юрлицо (`unit_id` пустой, `allocation = 'pending'`),
   правило порождает дочерние факты по точкам (`allocated`, видно правило, долю
   и родителя), родитель получает `split` и в P&L больше не считается — иначе
   двойной счёт.
4. *Пересчёт задним числом — заменой версии, а не правкой на месте.* История
   остаётся, закрытый период защищён триггером, отчёт смотрит только на строки
   без `superseded_at`.
5. *Идемпотентность импорта — на детерминированном `dedup_key`.* Уникальность
   только среди действующих строк, поэтому история версий не мешает.

---

**Что пришлось привести к нынешней схеме, а не перенести как есть.** Исходник
писался до второй очереди и до сегодняшней модели доступа:

| в исходнике | здесь | почему |
|---|---|---|
| `app_visible_layers()` | `app_visible_ledgers()` (`0004_rls`) | функция уже есть, второй копии правила видимости быть не должно (D014) |
| `app_visible_units()` своя | `app_unit_is_visible()` (`0011`) | там же записано правило «строка без точки видна всем в тенанте»; своя копия разъехалась бы молча |
| `app_tenant_ids()` создаётся заново | берётся готовая | она уже стоит в `0004_rls` и на ней держится вся изоляция |
| правки политик `0003` (permissive → restrictive, общий справочник статей) | не переносятся | оба дефекта уже исправлены в `0004_rls`, повтор был бы вторым правилом об одном |
| политика `layer_visibility` | `ledger_visibility` | регистры называются нейтрально (D009), и на это есть сторож имён |
| правило разнесения ищется по контрагенту | по контрагенту **и регистру** | см. ниже, это единственная правка смысла |

**Единственная правка смысла — поиск правила разнесения.** Исходник искал
правило по паре «тенант + контрагент» и брал `limit 1` по дате. С тех пор у
`allocation_rules` появилось ограничение `allocation_rules_no_overlap`, куда
входит **регистр**: у одного поставщика законно есть два действующих правила —
официальное и внутреннее, потому что один и тот же поставщик может оплачиваться
и по счёту, и из кассы, и это разные строки P&L. Без фильтра по регистру
`limit 1` выбирал бы из них произвольное — то есть разносил бы фактуру по
чужому правилу и молчал об этом. Правка внесена явно и записана в отчёт блока,
а не сделана тихо.

**Чего здесь нет намеренно.** Проводки зарплаты в факты
(`post_payroll_facts` из исходника) — она читала `payslips.tax` и
`payslips.contributions`, а этих колонок больше нет: итоги вынесены в
`payslip_totals` (T050) и видны только роли, которой видны все регистры (T071).
Перенести её «как есть» значило бы получить функцию, которая молча проводит
зарплату без налогов. Место этой работы — T113, где расходы встают в выгрузку
рядом с зарплатными строками; исходник лежит в истории git.

---

**Разграничение доступа — три ограничения, все по образцу зарплатных таблиц.**

- `tenant_isolation` — пермиссивная, как везде: свой партнёр.
- `ledger_visibility` — `as restrictive`: пермиссивные политики Postgres
  объединяет через OR, поэтому «регистр видим» не сужало бы выборку вообще,
  строку своего тенанта пропустила бы изоляция. `for all`, а не `for select`:
  иначе невидимый регистр нельзя было бы прочитать, но можно было бы в него
  вписать.
- `unit_visibility` — `as restrictive`, через `app_unit_is_visible()`.
  Управляющий видит свою точку; факт **без** точки видят все в тенанте — он не
  принадлежит чужой точке, он ждёт разнесения, и молча терять его хуже.

`force row level security` обязателен: без него политики не действуют на
владельца таблиц, а миграции и обслуживание ходят как раз им.

На `source_documents` и `fact_batches` стоит только изоляция тенанта: ни
регистра, ни точки у этих строк нет. Документ — контейнер, суммы и разрезы живут
в фактах, и они закрыты.

**Представления — `security_invoker = true`.** Иначе представление читает данные
правами владельца, и RLS перестаёт работать вовсе: потекут и регистры, и
тенанты. Отсюда же берётся требование D023 «ни строк, ни следа в итогах» —
суммы `pnl_by_unit` и `pnl_by_network` считаются по видимому срезу, потому что
невидимых строк в `pnl_lines` просто нет.

**Проверки схемы — в `RunSQL`, а не в `Meta.constraints`.** Две из них
(`period = date_trunc(...)`, `char_length(currency) = 3`) на языке Django не
выражаются, и разносить один набор инвариантов по двум местам хуже, чем держать
его целиком в одном. Индексы и уникальность, наоборот, выражаются полностью и
объявлены в модели.

**Нумерация с 0230** — стройка идёт блоками в разных копиях репозитория, номера
разведены заранее. Зависимость объявлена от текущего листа (`0220`) явно, чтобы
лист остался один: разведённые номера сами по себе от второго листа не спасают.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.fields

# --- словари домена -----------------------------------------------------------
# Нативные enum, как остальные словари продукта: чужое значение обязана отвергать
# база, а не приложение (см. шапку `core/fields.py`).
TYPES = """
create type fact_source as enum (
    'dodo_is',    -- коннектор операционной системы: выручка, сырьё, списания, часы
    'bank',       -- импорт банковской выписки
    'einvoice',   -- электронные фактуры
    'payroll',    -- проводка зарплатного движка
    'cash',       -- касса точки: наличные расходы, переводы, пополнения
    'manual',     -- ручной ввод через веб или Telegram
    'import'      -- разовая загрузка таблицы партнёра
);

comment on type fact_source is
    'Откуда пришли данные. Нужно для сверок «что дал Dodo IS, а что вбили руками» и для повторного импорта: у каждого источника свой ключ идемпотентности';

create type document_kind as enum (
    'invoice',        -- фактура, накладная
    'receipt',        -- чек, товарный чек
    'bank_line',      -- строка банковской выписки
    'payroll_run',    -- расчёт зарплаты за период
    'cash_expense',   -- расход из кассы
    'transfer',       -- перевод, пополнение
    'tax',            -- налог, такса
    'dodo_is_report', -- выгрузка из операционной системы
    'manual'          -- внесено человеком без документа
);

comment on type document_kind is 'Вид первичного документа';

create type fact_allocation as enum (
    'direct',     -- точка известна из источника, разнесения не было
    'pending',    -- точка неизвестна: ждёт правила или решения человека
    'split',      -- родитель, заменённый дочерними фактами; в P&L не считается
    'allocated'   -- результат разнесения: видно правило, долю и родителя
);

comment on type fact_allocation is
    'Состояние разнесения по точкам: лежит ли сумма на точке и если да — сама пришла или её разнесли';

create type batch_status as enum ('running', 'done', 'failed');

comment on type batch_status is 'Состояние партии загрузки';
"""

DROP_TYPES = """
drop type if exists batch_status;
drop type if exists fact_allocation;
drop type if exists document_kind;
drop type if exists fact_source;
"""

# --- инварианты строки ---------------------------------------------------------
# В SQL, а не в `Meta.constraints`: две проверки на языке Django не выражаются,
# а разносить один набор инвариантов по двум местам хуже, чем держать целиком.
CONSTRAINTS = """
alter table facts
    -- Период учёта — всегда месяц. Дата в середине месяца дала бы тихо неверный
    -- период: отчёт строится по нему, и расхождение всплыло бы в суммах.
    add constraint facts_period_is_month
        check (period = date_trunc('month', period)::date),
    add constraint facts_currency_is_code
        check (char_length(currency) = 3),
    -- 'direct' обязан знать точку, иначе сумма потеряется между «по точкам» и
    -- «по сети». Расход на юрлицо целиком — это 'pending' плюс правило.
    add constraint facts_direct_knows_its_unit
        check (allocation <> 'direct' or unit_id is not null),
    add constraint facts_pending_has_no_unit
        check (allocation <> 'pending' or unit_id is null),
    add constraint facts_split_has_no_unit
        check (allocation <> 'split' or unit_id is null),
    add constraint facts_allocated_is_complete
        check (allocation <> 'allocated'
               or (unit_id is not null and parent_fact_id is not null
                   and allocation_rule_id is not null)),
    -- Заменённый и заменивший: ссылка может быть пустой (строку убрали из
    -- повторной выгрузки), но без даты замены её быть не может.
    add constraint facts_superseded_has_a_date
        check (superseded_by is null or superseded_at is not null);

-- Дети живут только вместе с родителем — каскадом **в самой базе**: Django
-- исполняет каскад в Python и до чистого SQL не дотягивается, а осиротевшие
-- дети остались бы в отчёте суммами от строки, которой больше нет.
alter table facts
    add constraint facts_parent_fact_fkey
    foreign key (parent_fact_id) references facts (id) on delete cascade;

-- Ссылка на заменившую строку отложенная: она ставится ДО вставки новой версии
-- (иначе не обойти уникальность dedup_key), а проверяется на коммите.
alter table facts
    add constraint facts_superseded_by_fkey
    foreign key (superseded_by) references facts (id)
    deferrable initially deferred;

alter table source_documents
    add constraint source_documents_period_is_month
        check (period is null or period = date_trunc('month', period)::date);
"""

DROP_CONSTRAINTS = """
alter table source_documents drop constraint if exists source_documents_period_is_month;
alter table facts drop constraint if exists facts_superseded_by_fkey;
alter table facts drop constraint if exists facts_parent_fact_fkey;
alter table facts
    drop constraint if exists facts_superseded_has_a_date,
    drop constraint if exists facts_allocated_is_complete,
    drop constraint if exists facts_split_has_no_unit,
    drop constraint if exists facts_pending_has_no_unit,
    drop constraint if exists facts_direct_knows_its_unit,
    drop constraint if exists facts_currency_is_code,
    drop constraint if exists facts_period_is_month;
"""

COMMENTS = """
comment on table facts is
    'Первичные финансовые события всех источников: строка = позиция документа. Единственное место, откуда собирается P&L';
comment on table source_documents is
    'Первичные документы: прослеживаемость от строки P&L через факт до файла';
comment on table fact_batches is
    'Партия загрузки: одна выгрузка = одна строка. Нужна, чтобы понимать, откуда взялись эти триста строк';

comment on column facts.period is
    'Период учёта, всегда первое число месяца. Отчёт строится по нему, а не по дате документа';
comment on column facts.doc_date is
    'Дата документа. Может быть в другом месяце: счёт за июнь приходит в июле';
comment on column facts.amount is
    'Сумма в валюте учёта. Знак обычно положительный, отрицательный = возврат или исправление; доход это или расход, задаёт статья';
comment on column facts.channel is
    'Канал денег для сверки кассы, не для P&L. Пусто = движения денег не было (начисление)';
comment on column facts.dedup_key is
    'Детерминированный ключ события в источнике. Повторная загрузка того же ключа не создаёт новую строку';
comment on column facts.allocation is
    'Состояние разнесения. split — родитель, заменённый детьми: в P&L он не считается, иначе двойной счёт';
comment on column facts.allocation_share is
    'Доля родительской суммы. Хранится для сверки: сумма детей обязана совпадать с родителем до копейки';
comment on column facts.superseded_at is
    'Заполнено = строка больше не действует. Отчёты фильтруют по superseded_at is null';
comment on column facts.superseded_by is
    'Строка, заменившая эту. Отложенный внешний ключ: ссылка ставится до вставки новой версии';
comment on column facts.parent_fact_id is
    'Родительская фактура, из которой разнесён этот факт';
comment on column source_documents.external_id is
    'Идентификатор документа в системе-источнике. С ним же идёт идемпотентность повторной загрузки';
comment on column source_documents.period is
    'Период учёта по умолчанию для позиций документа';
comment on column source_documents.content_hash is
    'Не поменялся — разбирать документ заново нечего';
comment on column fact_batches.stats is
    'Итог загрузки: сколько вставлено, изменено, пропущено';
"""

# --- защита закрытого периода --------------------------------------------------
GUARD = """
-- Закрытый период не меняется никаким способом: ни импортом, ни пересчётом
-- разнесения, ни удалением. Открыть заново = откатить расчёт периода (`0190`),
-- то есть осознанное действие человека, а не побочный эффект загрузки.
--
-- Триггер, а не политика: политики не действуют на суперпользователя, а закрытый
-- месяц обязан быть закрыт для всех путей записи.
create or replace function facts_guard()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
    v_status period_status;
    v_kind   text;
begin
    if tg_op in ('UPDATE', 'DELETE') then
        select status into v_status
          from periods where tenant_id = old.tenant_id and period = old.period;
        if v_status = 'closed' then
            raise exception 'период % закрыт: факты изменять нельзя',
                to_char(old.period, 'YYYY-MM');
        end if;
    end if;

    if tg_op in ('INSERT', 'UPDATE') then
        select status into v_status
          from periods where tenant_id = new.tenant_id and period = new.period;
        if v_status = 'closed' then
            raise exception 'период % закрыт: факты добавлять нельзя',
                to_char(new.period, 'YYYY-MM');
        end if;

        -- Подытог считается из детей. Писать в него факт — значит получить
        -- сумму, которая не сходится ни с чем.
        select kind into v_kind from pnl_items where id = new.pnl_item_id;
        if v_kind = 'subtotal' then
            raise exception 'статья % — подытог, факты в неё писать нельзя', new.pnl_item_id;
        end if;
    end if;

    return coalesce(new, old);
end $$;

comment on function facts_guard() is
    'Закрытый месяц не принимает и не отдаёт правок ни одним путём записи; в статью-подытог факты не пишутся';

create trigger facts_guard_trg
    before insert or update or delete on facts
    for each row execute function facts_guard();
"""

DROP_GUARD = """
drop trigger if exists facts_guard_trg on facts;
drop function if exists facts_guard();
"""

# --- запись факта --------------------------------------------------------------
WRITE = """
-- Сравниваем по существу: служебные поля (id, revision, время создания) не
-- должны считаться изменением, иначе идемпотентность превратится в фикцию.
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

comment on function facts_same(facts, facts) is
    'Одно ли это событие по существу. Служебные поля не считаются изменением, иначе идемпотентности бы не было';

-- Дети живут только вместе с родителем: заменили родителя — заменяются и дети,
-- иначе в отчёте останутся суммы от исчезнувшей строки.
create or replace function supersede_fact(p_fact_id uuid, p_superseded_by uuid default null)
returns void
language plpgsql
as $$
declare
    c uuid;
begin
    update facts
       set superseded_at = now(), superseded_by = p_superseded_by
     where id = p_fact_id and superseded_at is null;

    for c in select id from facts where parent_fact_id = p_fact_id and superseded_at is null
    loop
        perform supersede_fact(c);
    end loop;
end $$;

comment on function supersede_fact(uuid, uuid) is
    'Пометить факт заменённым вместе с его детьми. История остаётся, отчёты её не видят';

-- Единственная точка записи факта. Любой источник — коннектор, импорт выписки,
-- зарплата, ручной ввод — идёт сюда, поэтому идемпотентность и версионирование
-- реализованы один раз.
--
-- Принимает jsonb, а не двадцать аргументов: разрезов много, часть всегда
-- пустая, и позиционный вызов на двадцати параметрах читается как ребус.
--
-- Прав владельца у функции нет намеренно: она исполняется правами вызвавшего,
-- то есть политики действуют. Функция, обходящая RLS, была бы дырой размером со
-- всю таблицу — писать в неё будут и экран, и API, и импорт.
create or replace function upsert_fact(p_payload jsonb, out fact_id uuid, out action text)
language plpgsql
as $$
declare
    v_new facts;
    v_cmp facts;
    v_old facts;
begin
    v_new := jsonb_populate_record(null::facts, p_payload);

    if v_new.tenant_id is null or v_new.period is null or v_new.pnl_item_id is null
       or v_new.amount is null or v_new.source is null or v_new.dedup_key is null
       or v_new.title is null then
        raise exception 'upsert_fact: обязательны tenant_id, period, pnl_item_id, amount, source, dedup_key, title';
    end if;

    v_new.allocation := coalesce(v_new.allocation, 'direct');
    v_new.ledger     := coalesce(v_new.ledger, 'official');
    v_new.currency   := coalesce(v_new.currency,
                                 (select base_currency from tenants where id = v_new.tenant_id));
    v_new.report_currency := coalesce(v_new.report_currency,
                                 (select report_currency from tenants where id = v_new.tenant_id));

    select * into v_old
      from facts
     where tenant_id = v_new.tenant_id and dedup_key = v_new.dedup_key
       and superseded_at is null;

    if found then
        -- Состояние разнесения ведёт система, а не источник. Коннектор всегда
        -- присылает «точка неизвестна», а факт к этому моменту мог быть уже
        -- разнесён. Без этой поправки каждая повторная выгрузка сбрасывала бы
        -- разнесение и заново плодила версии — то есть идемпотентности бы не было.
        v_cmp := v_new;
        if v_old.allocation = 'split' and v_new.allocation = 'pending' then
            v_cmp.allocation := 'split';
        end if;

        if facts_same(v_old, v_cmp) then
            -- Повторная загрузка того же события. Ничего не пишем, чтобы
            -- не плодить версии и не трогать время изменения.
            fact_id := v_old.id;
            action  := 'unchanged';
            return;
        end if;

        -- Событие изменилось по существу. Новая версия встаёт как её описал
        -- источник — то есть снова ожидающей разнесения, а дети старой версии
        -- снимаются. Сумма при этом из P&L не исчезает: 'pending' считается.
        v_new.id       := gen_random_uuid();
        v_new.revision := v_old.revision + 1;
        -- Заменяем каскадом: дети старой версии обязаны уйти вместе с ней,
        -- иначе в отчёте будут и они, и новая версия родителя — двойной счёт.
        -- Ссылку на новую строку ставим до её вставки: ключ отложенный,
        -- проверится на коммите. Иначе не обойти уникальность dedup_key.
        perform supersede_fact(v_old.id, v_new.id);
        action := 'updated';
    else
        v_new.id       := coalesce(v_new.id, gen_random_uuid());
        v_new.revision := 1;
        action := 'inserted';
    end if;

    v_new.created_at    := coalesce(v_new.created_at, now());
    v_new.created_by    := coalesce(v_new.created_by, app_user_id());
    v_new.superseded_at := null;
    v_new.superseded_by := null;

    insert into facts select (v_new).*;
    fact_id := v_new.id;
end $$;

comment on function upsert_fact(jsonb) is
    'Единственная точка записи факта: идемпотентность по dedup_key и версионирование заменой. Возвращает inserted | updated | unchanged';

-- Документ тоже приходит повторно (перезагрузка выписки, повторная выгрузка).
create or replace function upsert_document(p_payload jsonb)
returns uuid
language plpgsql
as $$
declare
    v_new source_documents;
    v_id  uuid;
begin
    v_new := jsonb_populate_record(null::source_documents, p_payload);

    -- Значения по умолчанию проставляются здесь, а не колонками таблицы.
    -- `insert ... select (v_new).*` подставляет **явный null** в каждую колонку,
    -- которой не было в jsonb, и умолчание базы при явном null не срабатывает:
    -- строка падала на `not null` первичного ключа. В исходнике
    -- (`db/migrations/0004_facts.sql`) это сделано для фактов и забыто для
    -- документов, то есть завести новый документ функция не могла вовсе —
    -- дефект найден переносом, см. журнал блока `facts`.
    v_new.id         := coalesce(v_new.id, gen_random_uuid());
    v_new.payload    := coalesce(v_new.payload, '{}'::jsonb);
    v_new.created_at := coalesce(v_new.created_at, now());
    v_new.created_by := coalesce(v_new.created_by, app_user_id());

    insert into source_documents as d select (v_new).*
    on conflict (tenant_id, source, external_id) do update
       set legal_entity_id = coalesce(excluded.legal_entity_id, d.legal_entity_id),
           counterparty_id = coalesce(excluded.counterparty_id, d.counterparty_id),
           doc_number      = coalesce(excluded.doc_number, d.doc_number),
           doc_date        = excluded.doc_date,
           period          = coalesce(excluded.period, d.period),
           currency        = coalesce(excluded.currency, d.currency),
           total_amount    = coalesce(excluded.total_amount, d.total_amount),
           content_hash    = coalesce(excluded.content_hash, d.content_hash),
           file_url        = coalesce(excluded.file_url, d.file_url),
           payload         = case when excluded.payload = '{}'::jsonb then d.payload else excluded.payload end,
           batch_id        = coalesce(excluded.batch_id, d.batch_id)
    returning d.id into v_id;

    return v_id;
end $$;

comment on function upsert_document(jsonb) is
    'Запись первичного документа с идемпотентностью по (тенант, источник, внешний id)';
"""

DROP_WRITE = """
drop function if exists upsert_document(jsonb);
drop function if exists upsert_fact(jsonb);
drop function if exists supersede_fact(uuid, uuid);
drop function if exists facts_same(facts, facts);
"""

# --- разнесение по точкам ------------------------------------------------------
ALLOCATION = """
-- План разнесения: чистый расчёт без записи. Отдельно от применения, чтобы
-- пересчёт мог сравнить «что должно быть» с «что есть» и не переписывать факты,
-- когда результат не изменился.
create or replace function allocation_plan(p_fact_id uuid)
returns table (unit_id uuid, share numeric, amount numeric, amount_report numeric, rule_id uuid)
language plpgsql stable
as $$
declare
    f facts;
    r allocation_rules;
    v_period_end date;
begin
    select * into f from facts where id = p_fact_id;
    if not found then
        raise exception 'факт % не найден', p_fact_id;
    end if;
    if f.counterparty_id is null then
        return;    -- без контрагента правило искать негде
    end if;

    v_period_end := (f.period + interval '1 month - 1 day')::date;

    -- Правило действует на период учёта, а не на дату документа: отчёт строится
    -- по периоду, разнесение должно жить в той же логике.
    --
    -- Регистр входит в поиск наравне с контрагентом. Так устроено само правило:
    -- ограничение `allocation_rules_no_overlap` разрешает одному поставщику два
    -- действующих правила в разных регистрах, потому что один и тот же
    -- поставщик может оплачиваться и по счёту, и из кассы. Без этого условия
    -- `limit 1` выбирал бы из двух произвольное — то есть разносил бы фактуру
    -- по чужому правилу и молчал об этом.
    select * into r
      from allocation_rules ar
     where ar.tenant_id = f.tenant_id
       and ar.counterparty_id = f.counterparty_id
       and ar.ledger = f.ledger
       and ar.valid_from <= f.period
       and (ar.valid_to is null or ar.valid_to > f.period)
     order by ar.valid_from desc
     limit 1;

    if not found or r.method = 'ask' then
        -- Правила нет или оно требует человека — плана нет, факт ждёт.
        return;
    end if;

    return query
    with base as (
        select u.id as unit_id,
               u.code as unit_code,
               case r.method
                   when 'fixed_unit' then 1::numeric
                   when 'even'       then 1::numeric
                   when 'by_revenue' then coalesce(rev.amount, 0)
               end as weight
          from units u
          left join lateral (
              -- Выручка точки за тот же период учёта, по действующим фактам
              select sum(x.amount) as amount
                from facts x
                join pnl_items pi on pi.id = x.pnl_item_id
               where x.tenant_id = f.tenant_id
                 and x.unit_id = u.id
                 and x.period = f.period
                 and x.superseded_at is null
                 and x.allocation <> 'split'
                 and pi.kind = 'revenue'
          ) rev on r.method = 'by_revenue'
         where u.tenant_id = f.tenant_id
           and (r.method <> 'fixed_unit' or u.id = r.unit_id)
           -- Фактура пришла на юрлицо — разносим только на его точки
           and (f.legal_entity_id is null or u.legal_entity_id = f.legal_entity_id)
           -- Точка должна работать в этом периоде
           and (u.opened_at is null or u.opened_at <= v_period_end)
           and (u.closed_at is null or u.closed_at >= f.period)
    ),
    kept as (
        -- Точка без выручки не получает долю при 'by_revenue'
        select * from base where weight > 0
    ),
    ordered as (
        select k.unit_id,
               k.weight,
               sum(k.weight) over (order by k.unit_code
                                   rows between unbounded preceding and current row) as cum,
               sum(k.weight) over () as total
          from kept k
    )
    -- Копейки. Округляем накопленную сумму и берём разность с предыдущей: так
    -- сумма детей всегда равна родителю до копейки, а распределение остатка
    -- детерминировано (по коду точки), а не зависит от порядка строк.
    select o.unit_id,
           round(o.weight / o.total, 6),
           round(f.amount * o.cum / o.total, 2)
               - round(f.amount * (o.cum - o.weight) / o.total, 2),
           case when f.amount_report is null then null
                else round(f.amount_report * o.cum / o.total, 2)
                     - round(f.amount_report * (o.cum - o.weight) / o.total, 2)
           end,
           r.id
      from ordered o
     where o.total > 0;
end $$;

comment on function allocation_plan(uuid) is
    'Каким должно быть разнесение факта по точкам. Ничего не пишет: пересчёт сравнивает план с тем, что есть';

-- Применить план: превратить ожидающий факт в набор фактов по точкам.
create or replace function allocate_fact(p_fact_id uuid)
returns int
language plpgsql
as $$
declare
    f facts;
    p record;
    v_unit_code text;
    n int := 0;
begin
    select * into f from facts where id = p_fact_id and superseded_at is null;
    if not found then
        raise exception 'факт % не найден или уже заменён', p_fact_id;
    end if;
    if f.allocation <> 'pending' then
        return 0;    -- уже разнесён или разносить нечего
    end if;

    for p in select * from allocation_plan(f.id) loop
        select code into v_unit_code from units where id = p.unit_id;

        perform upsert_fact(jsonb_build_object(
            'tenant_id',          f.tenant_id,
            'period',             f.period,
            'doc_date',           f.doc_date,
            'unit_id',            p.unit_id,
            'legal_entity_id',    f.legal_entity_id,
            'pnl_item_id',        f.pnl_item_id,
            'ledger',             f.ledger,
            'counterparty_id',    f.counterparty_id,
            'amount',             p.amount,
            'currency',           f.currency,
            'amount_report',      p.amount_report,
            'report_currency',    f.report_currency,
            'fx_rate',            f.fx_rate,
            'fx_rate_date',       f.fx_rate_date,
            'title',              f.title,
            'channel',            f.channel,
            'source',             f.source,
            'source_ref',         f.source_ref,
            'document_id',        f.document_id,
            'line_no',            f.line_no,
            'batch_id',           f.batch_id,
            -- Ключ ребёнка выводится из ключа родителя: устойчив между
            -- пересчётами, поэтому повторный расчёт не плодит строки.
            'dedup_key',          f.dedup_key || '#' || v_unit_code,
            'allocation',         'allocated',
            'allocation_rule_id', p.rule_id,
            'allocation_share',   p.share,
            'parent_fact_id',     f.id
        ));
        n := n + 1;
    end loop;

    if n > 0 then
        update facts set allocation = 'split' where id = f.id;
    end if;

    return n;
end $$;

comment on function allocate_fact(uuid) is
    'Разнести ожидающий факт по точкам правилом. Родитель становится split и в P&L больше не считается';

-- Пересчёт разнесения за период: правило поменялось задним числом. Закрытый
-- период не пересчитываем — и говорим об этом вслух, а не молча пропускаем:
-- молчаливый пропуск читается как «пересчитано».
create or replace function reallocate_period(p_tenant uuid, p_period date)
returns int
language plpgsql
as $$
declare
    v_status period_status;
    f       facts;
    c       facts;
    p       record;
    v_action text;
    v_unit_code text;
    v_plan_count int;
    n int := 0;
begin
    select status into v_status from periods where tenant_id = p_tenant and period = p_period;
    if v_status = 'closed' then
        raise exception 'период % закрыт: пересчёт разнесения невозможен',
            to_char(p_period, 'YYYY-MM');
    end if;

    for f in
        select * from facts
         where tenant_id = p_tenant and period = p_period
           and superseded_at is null
           and allocation in ('pending', 'split')
         order by created_at, id
    loop
        select count(*) into v_plan_count from allocation_plan(f.id);

        if v_plan_count = 0 then
            -- Правило исчезло или снова требует человека: снимаем детей и
            -- возвращаем факт в ожидание, чтобы он не потерялся.
            if f.allocation = 'split' then
                for c in select * from facts
                          where parent_fact_id = f.id and superseded_at is null loop
                    perform supersede_fact(c.id);
                    n := n + 1;
                end loop;
                update facts set allocation = 'pending' where id = f.id;
            end if;
            continue;
        end if;

        -- Точки, которых в новом плане нет
        for c in
            select * from facts ch
             where ch.parent_fact_id = f.id and ch.superseded_at is null
               and not exists (select 1 from allocation_plan(f.id) pl where pl.unit_id = ch.unit_id)
        loop
            perform supersede_fact(c.id);
            n := n + 1;
        end loop;

        -- Остальные: upsert сам решит, менять или оставить как есть
        for p in select * from allocation_plan(f.id) loop
            select code into v_unit_code from units where id = p.unit_id;

            select action into v_action from upsert_fact(jsonb_build_object(
                'tenant_id',          f.tenant_id,
                'period',             f.period,
                'doc_date',           f.doc_date,
                'unit_id',            p.unit_id,
                'legal_entity_id',    f.legal_entity_id,
                'pnl_item_id',        f.pnl_item_id,
                'ledger',             f.ledger,
                'counterparty_id',    f.counterparty_id,
                'amount',             p.amount,
                'currency',           f.currency,
                'amount_report',      p.amount_report,
                'report_currency',    f.report_currency,
                'fx_rate',            f.fx_rate,
                'fx_rate_date',       f.fx_rate_date,
                'title',              f.title,
                'channel',            f.channel,
                'source',             f.source,
                'source_ref',         f.source_ref,
                'document_id',        f.document_id,
                'line_no',            f.line_no,
                'batch_id',           f.batch_id,
                'dedup_key',          f.dedup_key || '#' || v_unit_code,
                'allocation',         'allocated',
                'allocation_rule_id', p.rule_id,
                'allocation_share',   p.share,
                'parent_fact_id',     f.id
            ));

            if v_action <> 'unchanged' then
                n := n + 1;
            end if;
        end loop;

        if f.allocation = 'pending' then
            update facts set allocation = 'split' where id = f.id;
        end if;
    end loop;

    return n;
end $$;

comment on function reallocate_period(uuid, date) is
    'Пересчёт разнесения за период после правки правил. На неизменившихся правилах не меняет ничего; в закрытом месяце отказывает вслух';
"""

DROP_ALLOCATION = """
drop function if exists reallocate_period(uuid, date);
drop function if exists allocate_fact(uuid);
drop function if exists allocation_plan(uuid);
"""

# --- валюта консолидации -------------------------------------------------------
FX = """
-- Последний известный курс на дату. Курс на конец месяца может ещё не приехать —
-- тогда берём предыдущий, а не падаем.
create or replace function fx_rate_on(p_base text, p_quote text, p_on date)
returns numeric
language sql stable
as $$
    select case
             when p_base = p_quote then 1::numeric
             else (
                 select rate from fx_rates
                  where base_currency = p_base and quote_currency = p_quote
                    and rate_date <= p_on
                  order by rate_date desc
                  limit 1
             )
           end
$$;

comment on function fx_rate_on(text, text, date) is
    'Последний известный курс на дату. Курса на конец месяца может ещё не быть — берётся предыдущий';

-- Фиксация суммы в валюте консолидации. Курс приколачиваем к факту, чтобы
-- закрытый период не поехал при обновлении справочника курсов.
create or replace function fill_report_amounts(p_tenant uuid, p_period date)
returns int
language plpgsql
as $$
declare
    v_report text;
    v_base   text;
    v_date   date;
    v_count  int;
begin
    select base_currency, report_currency into v_base, v_report
      from tenants where id = p_tenant;
    v_date := (p_period + interval '1 month - 1 day')::date;   -- курс на конец месяца

    update facts f
       set amount_report   = round(f.amount * fx_rate_on(f.currency, v_report, v_date), 2),
           report_currency = v_report,
           fx_rate         = fx_rate_on(f.currency, v_report, v_date),
           fx_rate_date    = v_date
     where f.tenant_id = p_tenant and f.period = p_period
       and f.superseded_at is null
       and f.amount_report is null
       and fx_rate_on(f.currency, v_report, v_date) is not null;

    get diagnostics v_count = row_count;
    return v_count;
end $$;

comment on function fill_report_amounts(uuid, date) is
    'Приколотить к фактам периода курс и сумму в валюте консолидации: закрытый месяц не должен ехать при обновлении курсов';
"""

DROP_FX = """
drop function if exists fill_report_amounts(uuid, date);
drop function if exists fx_rate_on(text, text, date);
"""

# --- сборка P&L ----------------------------------------------------------------
# security_invoker = true обязателен на каждом представлении: иначе оно читает
# данные правами владельца и RLS перестаёт работать — потекут и регистры, и
# тенанты.
VIEWS = """
-- Действующие факты, годные к счёту. 'split' исключён: его заменили дети.
create view pnl_lines with (security_invoker = true) as
select f.id           as fact_id,
       f.tenant_id,
       f.period,
       f.doc_date,
       f.unit_id,
       u.code         as unit_code,
       u.title        as unit_title,
       f.legal_entity_id,
       f.pnl_item_id,
       i.code         as pnl_code,
       i.title        as pnl_title,
       i.kind,
       f.ledger,
       f.counterparty_id,
       f.allocation,
       f.allocation_rule_id,
       f.source,
       f.channel,
       f.amount,
       f.currency,
       -- Если курс к факту ещё не приколочен, считаем на лету по курсу на конец
       -- периода. null = курса нет вообще, и это видно в отчёте.
       coalesce(
           f.amount_report,
           round(f.amount * fx_rate_on(f.currency, t.report_currency,
                                       (f.period + interval '1 month - 1 day')::date), 2)
       )              as amount_report,
       t.report_currency,
       f.document_id,
       f.title
  from facts f
  join tenants t   on t.id = f.tenant_id
  join pnl_items i on i.id = f.pnl_item_id
  left join units u on u.id = f.unit_id
 where f.superseded_at is null
   and f.allocation <> 'split';

comment on view pnl_lines is
    'Действующие факты с раскрытыми справочниками. Основа всех отчётов: RLS работает через security_invoker';

-- P&L по точке
create view pnl_by_unit with (security_invoker = true) as
select tenant_id, period, unit_id, unit_code, unit_title,
       pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(*)           as fact_count
  from pnl_lines
 where kind <> 'transfer'    -- переводы не расход и не выручка
 group by 1, 2, 3, 4, 5, 6, 7, 8, 9;

comment on view pnl_by_unit is 'P&L по точке и статье за период';

-- P&L по сети: то же, без разреза по точкам. Суммы по точкам и по сети сходятся,
-- потому что нераспределённые факты ('pending') считаются в обоих случаях —
-- просто без точки.
create view pnl_by_network with (security_invoker = true) as
select tenant_id, period, pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(distinct unit_id) as unit_count
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6;

comment on view pnl_by_network is 'P&L по сети целиком: суммы сходятся с разрезом по точкам';

-- Что мешает закрыть период: суммы без точки
create view facts_unallocated with (security_invoker = true) as
select f.tenant_id, f.period, f.id as fact_id, f.title, f.amount, f.currency,
       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source
  from facts f
  left join counterparties c on c.id = f.counterparty_id
 where f.superseded_at is null and f.allocation = 'pending';

comment on view facts_unallocated is
    'Суммы без точки: что мешает закрыть месяц. Факт без правила обязан быть видимым, а не исчезать';

-- Готовый отчёт: дерево статей с подытогами. p_unit_id = null — по всей сети.
--
-- amount — сумма по статье в её собственном смысле (выручка положительна, расход
-- положителен). signed_amount — с учётом знака, чтобы подытог вида «результат»
-- считался простым сложением детей.
create or replace function pnl_report(p_tenant uuid, p_period date, p_unit_id uuid default null)
returns table (
    pnl_item_id   uuid,
    code          text,
    title         text,
    kind          text,
    level         int,
    sort_path     text,
    amount        numeric,
    amount_report numeric,
    signed_amount numeric
)
language sql stable
as $$
    with recursive tree as (
        select i.id, i.parent_id, i.code, i.title, i.kind, 1 as level,
               lpad(i.sort_order::text, 6, '0') || '.' || i.code as sort_path,
               array[i.id] as path
          from pnl_items i
         where i.parent_id is null
           and (i.tenant_id = p_tenant or i.tenant_id is null)
        union all
        select c.id, c.parent_id, c.code, c.title, c.kind, t.level + 1,
               t.sort_path || '/' || lpad(c.sort_order::text, 6, '0') || '.' || c.code,
               t.path || c.id
          from pnl_items c
          join tree t on c.parent_id = t.id
         where (c.tenant_id = p_tenant or c.tenant_id is null)
    ),
    own as (
        select l.pnl_item_id,
               sum(l.amount)        as amount,
               sum(l.amount_report) as amount_report,
               -- Знак задаёт статья: расход уменьшает результат
               sum(case when l.kind = 'revenue' then l.amount else -l.amount end) as signed_amount
          from pnl_lines l
         where l.tenant_id = p_tenant
           and l.period = p_period
           and l.kind <> 'transfer'
           and (p_unit_id is null or l.unit_id = p_unit_id)
         group by 1
    )
    -- Подытог = сумма всех своих потомков (включая себя): подъём по path
    select t.id, t.code, t.title, t.kind, t.level, t.sort_path,
           coalesce(sum(o.amount), 0),
           sum(o.amount_report),
           coalesce(sum(o.signed_amount), 0)
      from tree t
      left join tree d on t.id = any (d.path)
      left join own o on o.pnl_item_id = d.id
     group by t.id, t.code, t.title, t.kind, t.level, t.sort_path
     order by t.sort_path
$$;

comment on function pnl_report(uuid, date, uuid) is
    'P&L за период: дерево статей с подытогами. p_unit_id = null — по всей сети';
"""

DROP_VIEWS = """
drop function if exists pnl_report(uuid, date, uuid);
drop view if exists facts_unallocated;
drop view if exists pnl_by_network;
drop view if exists pnl_by_unit;
drop view if exists pnl_lines;
"""

# --- изоляция данных -----------------------------------------------------------
POLICIES = """
alter table facts            enable row level security;
alter table facts            force  row level security;
alter table source_documents enable row level security;
alter table source_documents force  row level security;
alter table fact_batches     enable row level security;
alter table fact_batches     force  row level security;

create policy tenant_isolation on facts
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

create policy tenant_isolation on source_documents
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

create policy tenant_isolation on fact_batches
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Регистр учёта. `as restrictive` — иначе политика объединялась бы через OR и не
-- сужала бы выборку вообще. `for all`, а не `for select`: без `with check`
-- ограничение обходится вставкой — не вижу, но записать могу.
create policy ledger_visibility on facts
    as restrictive for all
    using (ledger = any (app_visible_ledgers(tenant_id)))
    with check (ledger = any (app_visible_ledgers(tenant_id)));

-- Точка. Правило не переписывается, а зовётся: разъехавшиеся копии одного
-- правила и есть тот способ, которым доступ ломается незаметно (D014).
-- Факт без точки (ещё не разнесённый) виден всем, у кого есть доступ к тенанту:
-- он не принадлежит чужой точке, он ждёт разнесения, и терять его нельзя.
create policy unit_visibility on facts
    as restrictive for all
    using (app_unit_is_visible(tenant_id, unit_id))
    with check (app_unit_is_visible(tenant_id, unit_id));
"""

DROP_POLICIES = """
drop policy if exists unit_visibility on facts;
drop policy if exists ledger_visibility on facts;
drop policy if exists tenant_isolation on fact_batches;
drop policy if exists tenant_isolation on source_documents;
drop policy if exists tenant_isolation on facts;
alter table fact_batches     no force row level security;
alter table fact_batches     disable row level security;
alter table source_documents no force row level security;
alter table source_documents disable row level security;
alter table facts            no force row level security;
alter table facts            disable row level security;
"""

# Права роли приложения. `alter default privileges` из `0005_app_role` покрывает
# новые таблицы, но только пока миграции накатывает та же роль, что их выдавала.
# Выписываем явно: отказ по привилегии и отказ по политике — разные вещи, и
# первый выглядел бы как «разграничение работает», а продукт не читал бы ничего.
PRIVILEGES = """
grant select, insert, update, delete on facts, source_documents, fact_batches to app_user;
grant select on pnl_lines, pnl_by_unit, pnl_by_network, facts_unallocated to app_user;
"""

DROP_PRIVILEGES = """
revoke all on pnl_lines, pnl_by_unit, pnl_by_network, facts_unallocated from app_user;
revoke all on facts, source_documents, fact_batches from app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа: разведённые номера сами по себе от второго
        # листа миграций не спасают, а на этом проекте он расходился уже трижды.
        ("core", "0220_accountant_sees_every_ledger"),
    ]

    operations = [
        # Типы раньше колонок, которые на них ссылаются.
        migrations.RunSQL(TYPES, DROP_TYPES),

        migrations.CreateModel(
            name='Fact',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('period', models.DateField()),
                ('doc_date', models.DateField(blank=True, null=True)),
                ('ledger', core.fields.EnumField(db_default='official', db_type_name='ledger')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('currency', models.TextField()),
                ('amount_report', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ('report_currency', models.TextField(blank=True, null=True)),
                ('fx_rate', models.DecimalField(blank=True, decimal_places=8, max_digits=18, null=True)),
                ('fx_rate_date', models.DateField(blank=True, null=True)),
                ('quantity', models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True)),
                ('uom', models.TextField(blank=True, null=True)),
                ('title', models.TextField()),
                ('note', models.TextField(blank=True, null=True)),
                ('channel', core.fields.EnumField(blank=True, db_type_name='payout_channel', null=True)),
                ('source', core.fields.EnumField(db_type_name='fact_source')),
                ('source_ref', models.TextField(blank=True, null=True)),
                ('line_no', models.IntegerField(blank=True, null=True)),
                ('dedup_key', models.TextField()),
                ('allocation', core.fields.EnumField(db_default='direct', db_type_name='fact_allocation')),
                ('allocation_share', models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
                ('revision', models.IntegerField(db_default=1)),
                ('superseded_at', models.DateTimeField(blank=True, null=True)),
                ('superseded_by', models.UUIDField(blank=True, null=True)),
                ('created_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('created_by', models.UUIDField(blank=True, null=True)),
            ],
            options={
                'db_table': 'facts',
            },
        ),
        migrations.CreateModel(
            name='FactBatch',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('source', core.fields.EnumField(db_type_name='fact_source')),
                ('external_ref', models.TextField(blank=True, null=True)),
                ('status', core.fields.EnumField(db_default='running', db_type_name='batch_status')),
                ('started_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('stats', models.JSONField(db_default={})),
                ('created_by', models.UUIDField(blank=True, null=True)),
            ],
            options={
                'db_table': 'fact_batches',
            },
        ),
        migrations.CreateModel(
            name='SourceDocument',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('kind', core.fields.EnumField(db_type_name='document_kind')),
                ('source', core.fields.EnumField(db_type_name='fact_source')),
                ('external_id', models.TextField()),
                ('doc_number', models.TextField(blank=True, null=True)),
                ('doc_date', models.DateField()),
                ('period', models.DateField(blank=True, null=True)),
                ('currency', models.TextField(blank=True, null=True)),
                ('total_amount', models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ('content_hash', models.TextField(blank=True, null=True)),
                ('file_url', models.TextField(blank=True, null=True)),
                ('payload', models.JSONField(db_default={})),
                ('created_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('created_by', models.UUIDField(blank=True, null=True)),
            ],
            options={
                'db_table': 'source_documents',
            },
        ),
        migrations.RemoveConstraint(
            model_name='pnlitem',
            name='pnl_items_kind_check',
        ),
        migrations.AddConstraint(
            model_name='pnlitem',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ['revenue', 'expense', 'subtotal', 'transfer'])), name='pnl_items_kind_check'),
        ),
        migrations.AddField(
            model_name='fact',
            name='allocation_rule',
            field=models.ForeignKey(blank=True, db_column='allocation_rule_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.allocationrule'),
        ),
        migrations.AddField(
            model_name='fact',
            name='counterparty',
            field=models.ForeignKey(blank=True, db_column='counterparty_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.counterparty'),
        ),
        migrations.AddField(
            model_name='fact',
            name='legal_entity',
            field=models.ForeignKey(blank=True, db_column='legal_entity_id', null=True, on_delete=django.db.models.deletion.PROTECT, to='core.legalentity'),
        ),
        migrations.AddField(
            model_name='fact',
            name='parent_fact',
            field=models.ForeignKey(blank=True, db_column='parent_fact_id', db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='children', to='core.fact'),
        ),
        migrations.AddField(
            model_name='fact',
            name='pnl_item',
            field=models.ForeignKey(db_column='pnl_item_id', on_delete=django.db.models.deletion.PROTECT, to='core.pnlitem'),
        ),
        migrations.AddField(
            model_name='fact',
            name='tenant',
            field=models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant'),
        ),
        migrations.AddField(
            model_name='fact',
            name='unit',
            field=models.ForeignKey(blank=True, db_column='unit_id', null=True, on_delete=django.db.models.deletion.PROTECT, to='core.unit'),
        ),
        migrations.AddField(
            model_name='factbatch',
            name='tenant',
            field=models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant'),
        ),
        migrations.AddField(
            model_name='fact',
            name='batch',
            field=models.ForeignKey(blank=True, db_column='batch_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.factbatch'),
        ),
        migrations.AddField(
            model_name='sourcedocument',
            name='batch',
            field=models.ForeignKey(blank=True, db_column='batch_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.factbatch'),
        ),
        migrations.AddField(
            model_name='sourcedocument',
            name='counterparty',
            field=models.ForeignKey(blank=True, db_column='counterparty_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.counterparty'),
        ),
        migrations.AddField(
            model_name='sourcedocument',
            name='legal_entity',
            field=models.ForeignKey(blank=True, db_column='legal_entity_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.legalentity'),
        ),
        migrations.AddField(
            model_name='sourcedocument',
            name='tenant',
            field=models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant'),
        ),
        migrations.AddField(
            model_name='fact',
            name='document',
            field=models.ForeignKey(blank=True, db_column='document_id', null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.sourcedocument'),
        ),
        migrations.AddIndex(
            model_name='factbatch',
            index=models.Index(models.F('tenant'), models.F('source'), models.OrderBy(models.F('started_at'), descending=True), name='fact_batches_lookup_idx'),
        ),
        migrations.AddIndex(
            model_name='sourcedocument',
            index=models.Index(models.F('tenant'), models.OrderBy(models.F('doc_date'), descending=True), name='source_docs_date_idx'),
        ),
        migrations.AddIndex(
            model_name='sourcedocument',
            index=models.Index(models.F('tenant'), models.F('counterparty'), name='source_docs_counterparty_idx'),
        ),
        migrations.AddConstraint(
            model_name='sourcedocument',
            constraint=models.UniqueConstraint(fields=('tenant', 'source', 'external_id'), name='source_documents_external_uniq'),
        ),
        migrations.AddIndex(
            model_name='fact',
            index=models.Index(models.F('tenant'), models.F('period'), models.F('unit'), condition=models.Q(('superseded_at__isnull', True)), name='facts_period_unit'),
        ),
        migrations.AddIndex(
            model_name='fact',
            index=models.Index(models.F('tenant'), models.F('period'), models.F('pnl_item'), condition=models.Q(('superseded_at__isnull', True)), name='facts_period_item'),
        ),
        migrations.AddIndex(
            model_name='fact',
            index=models.Index(models.F('tenant'), models.F('document'), name='facts_document'),
        ),
        migrations.AddIndex(
            model_name='fact',
            index=models.Index(models.F('parent_fact'), condition=models.Q(('superseded_at__isnull', True)), name='facts_parent'),
        ),
        migrations.AddIndex(
            model_name='fact',
            index=models.Index(models.F('tenant'), models.F('period'), condition=models.Q(('allocation', 'pending'), ('superseded_at__isnull', True)), name='facts_pending'),
        ),
        migrations.AddConstraint(
            model_name='fact',
            constraint=models.UniqueConstraint(condition=models.Q(('superseded_at__isnull', True)), fields=('tenant', 'dedup_key'), name='facts_dedup_active'),
        ),
        migrations.RunSQL(CONSTRAINTS, DROP_CONSTRAINTS),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(GUARD, DROP_GUARD),
        migrations.RunSQL(WRITE, DROP_WRITE),
        migrations.RunSQL(ALLOCATION, DROP_ALLOCATION),
        migrations.RunSQL(FX, DROP_FX),
        migrations.RunSQL(VIEWS, DROP_VIEWS),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
