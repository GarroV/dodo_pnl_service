-- =============================================================================
-- 0004 — Центральная таблица фактов и сборка P&L
-- =============================================================================
-- Единственное место, куда стекаются все финансовые события: выручка из Dodo IS,
-- сырьё и списания, фактуры поставщиков, зарплата, наличные из кассы, переводы,
-- налоги. Каждый следующий модуль пишет сюда, а не придумывает своё хранилище.
--
-- Это НЕ двойная запись. Это управленческий учёт: одна строка = одно событие
-- с разрезами. Единственное требование к прослеживаемости — от строки P&L можно
-- дойти до первичного документа и понять, откуда взялась сумма.
--
-- Решения, которые стоит понимать до чтения схемы:
--
--   1. Строка факта = позиция документа, не документ. Накладная из Метро
--      содержит и продукты, и канцелярию — это два факта с разными статьями.
--      Отдельной таблицы позиций нет: факт и есть позиция. Меньше сущностей,
--      а связь с документом даёт facts.document_id + facts.line_no.
--
--   2. Дата документа и период учёта — разные поля. Счёт за электричество
--      за июнь приходит в июле: doc_date = июль, period = июнь. Отчёт всегда
--      строится по period.
--
--   3. Разнесение по точкам выражено в данных, а не в комментарии к сумме.
--      Фактура приходит на юрлицо (unit_id пустой, allocation = 'pending'),
--      правило из allocation_rules порождает дочерние факты по точкам
--      (allocation = 'allocated', видно правило, долю и родителя). Родитель
--      получает 'split' и в P&L больше не считается — иначе двойной счёт.
--
--   4. Пересчёт задним числом — через замену версии, а не правку на месте.
--      Изменилось правило разнесения — старые факты помечаются superseded_at
--      и рядом появляются новые. История остаётся, закрытый период защищён
--      триггером. Что видно в отчёте — только строки без superseded_at.
--
--   5. Идемпотентность импорта — на детерминированном ключе. Источник обязан
--      посчитать dedup_key, и повторная загрузка того же события ничего
--      не меняет (upsert_fact вернёт 'unchanged'). Уникальность — только среди
--      действующих строк, поэтому история версий не мешает.
-- =============================================================================


-- --- Правки существующей схемы -----------------------------------------------

-- Переводы между кассой и банком, пополнения кассы — тоже события, которые
-- нужно накапливать, но они не расход и не выручка. Держим их в фактах
-- (для сверки наличных), а из P&L исключаем по kind.
alter table pnl_items drop constraint pnl_items_kind_check;
alter table pnl_items add constraint pnl_items_kind_check
    check (kind in ('revenue', 'expense', 'subtotal', 'transfer'));


-- --- Личность пользователя без привязки к платформе --------------------------
-- app_user_id() создаётся миграцией Django core/0004_rls: каталог db/platform/
-- с привязкой к внешней платформе входа удалён (T003). Всё остальное ниже —
-- чистый Postgres. Функции security definer: они читают memberships и roles,
-- на которых сама же висит RLS, иначе получилась бы рекурсия политик.

create or replace function app_tenant_ids()
returns setof uuid
language sql stable security definer
set search_path = public
as $$
    select tenant_id from memberships where user_id = app_user_id()
$$;

create or replace function app_visible_layers(p_tenant uuid)
returns accounting_layer[]
language sql stable security definer
set search_path = public
as $$
    select coalesce(
        (select array_agg(distinct l)
           from memberships m
           join roles r on r.id = m.role_id
           cross join unnest(r.visible_layers) as l
          where m.user_id = app_user_id() and m.tenant_id = p_tenant),
        '{}'::accounting_layer[]
    )
$$;

-- null в результате = доступ ко всем точкам тенанта (так же, как null
-- в memberships.unit_ids). Управляющий точки видит только свою.
create or replace function app_visible_units(p_tenant uuid)
returns uuid[]
language sql stable security definer
set search_path = public
as $$
    with mine as (
        select unit_ids
          from memberships
         where user_id = app_user_id() and tenant_id = p_tenant
    )
    select case
             when exists (select 1 from mine where unit_ids is null) then null
             else coalesce(
                 (select array_agg(distinct u) from mine, unnest(unit_ids) as u),
                 '{}'::uuid[]
             )
           end
$$;


-- --- Исправление политик слоёв из 0003 ---------------------------------------
-- В 0003 политики layer_visibility созданы как permissive. Permissive-политики
-- в Postgres объединяются через OR, то есть они не сужали доступ, а расширяли:
-- строку чужого тенанта становилось видно, если совпал слой учёта. Сужать
-- умеет только restrictive. Миграции append-only, поэтому правим здесь.

drop policy if exists layer_visibility on pay_components;
create policy layer_visibility on pay_components
    as restrictive for select
    using (layer = any (app_visible_layers(tenant_id)));

drop policy if exists layer_visibility on allocation_rules;
create policy layer_visibility on allocation_rules
    as restrictive for select
    using (layer = any (app_visible_layers(tenant_id)));

-- Там же: единый справочник статей лежит с tenant_id = null, а политика
-- из 0003 проверяет `tenant_id in (...)`, что на null даёт null, то есть
-- «не видно». В итоге общие статьи были не видны никому, а на них держится
-- весь P&L. Разрешаем читать общий справочник; писать в него — по-прежнему
-- нельзя, это забота администратора сети.
create policy shared_catalogue on pnl_items
    for select
    using (tenant_id is null);


-- --- Справочные типы ---------------------------------------------------------

-- Откуда пришли данные. Нужно для сверок («что дал Dodo IS, а что вбили руками»)
-- и для повторного импорта: у каждого источника свой ключ идемпотентности.
create type fact_source as enum (
    'dodo_is',    -- коннектор операционной системы: выручка, сырьё, списания, часы
    'bank',       -- импорт банковской выписки
    'einvoice',   -- электронные фактуры
    'payroll',    -- проводка зарплатного движка
    'cash',       -- касса точки: наличные расходы, переводы, пополнения
    'manual',     -- ручной ввод через веб или Telegram
    'import'      -- разовая загрузка таблицы партнёра
);

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

-- Состояние разнесения по точкам. Отвечает на вопрос «эта сумма уже лежит
-- на точке, и если да — сама пришла или её разнесли».
create type fact_allocation as enum (
    'direct',     -- точка известна из источника, разнесения не было
    'pending',    -- точка неизвестна: ждёт правила или решения человека
    'split',      -- родитель, заменённый дочерними фактами; в P&L не считается
    'allocated'   -- результат разнесения: видно правило, долю и родителя
);


-- --- Партии загрузки ---------------------------------------------------------
-- Одна загрузка = одна партия. Нужна, чтобы понимать «откуда эти 300 строк»
-- и чтобы было за что взяться при разборе кривого импорта.

create type batch_status as enum ('running', 'done', 'failed');

create table fact_batches (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants on delete cascade,
    source       fact_source not null,
    external_ref text,                                  -- имя файла, период выгрузки, id запроса
    status       batch_status not null default 'running',
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    stats        jsonb not null default '{}'::jsonb,     -- сколько вставлено, изменено, пропущено
    created_by   uuid
);

create index on fact_batches (tenant_id, source, started_at desc);


-- --- Первичные документы -----------------------------------------------------
-- Прослеживаемость: от строки P&L через факт до документа и файла.
-- Идемпотентность на уровне документа: (tenant, источник, внешний id).

create table source_documents (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid not null references tenants on delete cascade,
    legal_entity_id uuid references legal_entities on delete set null,
    counterparty_id uuid references counterparties on delete set null,
    kind            document_kind not null,
    source          fact_source not null,
    external_id     text not null,                      -- id в системе-источнике
    doc_number      text,
    doc_date        date not null,
    period          date,                               -- период учёта по умолчанию для позиций
    currency        text,
    total_amount    numeric(18,2),
    content_hash    text,                               -- если не поменялся, разбирать заново нечего
    file_url        text,
    payload         jsonb not null default '{}'::jsonb,  -- сырой ответ источника, как пришёл
    batch_id        uuid references fact_batches on delete set null,
    created_at      timestamptz not null default now(),
    created_by      uuid,
    unique (tenant_id, source, external_id),
    check (period is null or period = date_trunc('month', period)::date)
);

create index on source_documents (tenant_id, doc_date desc);
create index on source_documents (tenant_id, counterparty_id);


-- --- Факты -------------------------------------------------------------------

create table facts (
    id               uuid primary key default gen_random_uuid(),
    tenant_id        uuid not null references tenants on delete cascade,

    -- Обязательные разрезы
    period           date not null,                     -- период учёта, первое число месяца
    doc_date         date,                              -- дата документа; может быть в другом месяце
    unit_id          uuid references units on delete restrict,
    legal_entity_id  uuid references legal_entities on delete restrict,
    pnl_item_id      uuid not null references pnl_items on delete restrict,
    layer            accounting_layer not null default 'white',
    counterparty_id  uuid references counterparties on delete set null,

    -- Суммы. Знак: обычно положительный, отрицательный = возврат или
    -- исправление. Смысл (доход/расход) задаёт статья, а не знак.
    amount           numeric(18,2) not null,            -- в валюте учёта
    currency         text not null,
    amount_report    numeric(18,2),                     -- в валюте консолидации
    report_currency  text,
    fx_rate          numeric(18,8),                     -- курс, по которому пересчитали
    fx_rate_date     date,

    -- Натуральные показатели: сырьё, списания, упаковка
    quantity         numeric(18,4),
    uom              text,

    title            text not null,                     -- что это: наименование позиции
    note             text,

    -- Канал денег. Нужен для сверки кассы, а не для P&L: «надбавка кешем» —
    -- обычный факт с channel = 'cash'. null = движения денег нет (начисление).
    channel          payout_channel,

    -- Откуда пришло
    source           fact_source not null,
    source_ref       text,                              -- id строки в источнике
    document_id      uuid references source_documents on delete set null,
    line_no          int,
    batch_id         uuid references fact_batches on delete set null,

    -- Идемпотентность. Ключ считает источник, он должен быть устойчивым
    -- между загрузками: не по времени и не по номеру строки в файле.
    dedup_key        text not null,

    -- Разнесение по точкам
    allocation         fact_allocation not null default 'direct',
    allocation_rule_id uuid references allocation_rules on delete set null,
    allocation_share   numeric(9,6),                    -- доля родителя, для проверки
    parent_fact_id     uuid references facts on delete cascade,

    -- Версионирование. Правку делаем заменой: старая строка помечается
    -- заменённой, новая встаёт рядом. Отчёт смотрит только на действующие.
    revision      int not null default 1,
    superseded_at timestamptz,
    superseded_by uuid,

    created_at timestamptz not null default now(),
    created_by uuid,

    -- Период учёта — всегда месяц
    check (period = date_trunc('month', period)::date),
    check (char_length(currency) = 3),

    -- 'direct' обязан знать точку, иначе сумма потеряется между «по точкам»
    -- и «по сети». Расход на юрлицо целиком — это 'pending' + правило 'even'.
    check (allocation <> 'direct' or unit_id is not null),
    check (allocation <> 'pending' or unit_id is null),
    check (allocation <> 'split'   or unit_id is null),
    check (allocation <> 'allocated'
           or (unit_id is not null and parent_fact_id is not null and allocation_rule_id is not null)),

    -- Заменённый и заменивший: ссылка может быть пустой (строку убрали
    -- из повторной выгрузки), но без даты замены её быть не может.
    check (superseded_by is null or superseded_at is not null),

    constraint facts_superseded_by_fkey foreign key (superseded_by) references facts (id)
        deferrable initially deferred
);

comment on column facts.dedup_key is
    'Детерминированный ключ события в источнике. Повторная загрузка того же ключа не создаёт новую строку';
comment on column facts.allocation_share is
    'Доля родительской суммы. Хранится для сверки: сумма детей обязана совпадать с родителем до копейки';
comment on column facts.superseded_at is
    'Заполнено = строка больше не действует. Отчёты фильтруют по superseded_at is null';

-- Идемпотентность: ключ уникален только среди действующих строк, поэтому
-- история версий не конфликтует сама с собой.
create unique index facts_dedup_active on facts (tenant_id, dedup_key)
    where superseded_at is null;

create index facts_period_unit on facts (tenant_id, period, unit_id)
    where superseded_at is null;
create index facts_period_item on facts (tenant_id, period, pnl_item_id)
    where superseded_at is null;
create index facts_document on facts (tenant_id, document_id);
create index facts_parent on facts (parent_fact_id) where superseded_at is null;
create index facts_pending on facts (tenant_id, period)
    where allocation = 'pending' and superseded_at is null;


-- --- Защита закрытого периода ------------------------------------------------
-- Закрытый период не меняется никаким способом: ни импортом, ни пересчётом
-- разнесения, ни удалением. Открыть заново = сменить статус в periods,
-- то есть осознанное действие человека, а не побочный эффект загрузки.

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

create trigger facts_guard_trg
    before insert or update or delete on facts
    for each row execute function facts_guard();


-- --- Запись фактов -----------------------------------------------------------

-- Сравниваем по существу: служебные поля (id, revision, время создания)
-- не должны считаться изменением, иначе идемпотентность превратится в фикцию.
create or replace function facts_same(a facts, b facts)
returns boolean
language sql immutable
as $$
    select (a.period, a.doc_date, a.unit_id, a.legal_entity_id, a.pnl_item_id,
            a.layer, a.counterparty_id, a.amount, a.currency, a.amount_report,
            a.report_currency, a.quantity, a.uom, a.title, a.note, a.channel,
            a.source, a.source_ref, a.document_id, a.line_no,
            a.allocation, a.allocation_rule_id, a.allocation_share, a.parent_fact_id)
        is not distinct from
           (b.period, b.doc_date, b.unit_id, b.legal_entity_id, b.pnl_item_id,
            b.layer, b.counterparty_id, b.amount, b.currency, b.amount_report,
            b.report_currency, b.quantity, b.uom, b.title, b.note, b.channel,
            b.source, b.source_ref, b.document_id, b.line_no,
            b.allocation, b.allocation_rule_id, b.allocation_share, b.parent_fact_id)
$$;

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

-- Единственная точка записи факта. Любой источник — коннектор, импорт выписки,
-- зарплата, ручной ввод — идёт сюда, поэтому идемпотентность и версионирование
-- реализованы один раз.
--
-- Принимает jsonb, а не двадцать аргументов: разрезов много, часть всегда
-- пустая, и позиционный вызов на двадцати параметрах читается как ребус.
--
-- action: inserted | updated | unchanged
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
    v_new.layer      := coalesce(v_new.layer, 'white');
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
        -- Ссылку на новую строку ставим до её вставки: FK отложенный,
        -- проверится на коммите. Иначе не обойти уникальность dedup_key.
        perform supersede_fact(v_old.id, v_new.id);
        action := 'updated';
    else
        v_new.id       := coalesce(v_new.id, gen_random_uuid());
        v_new.revision := 1;
        action := 'inserted';
    end if;

    v_new.created_at    := coalesce(v_new.created_at, now());
    v_new.superseded_at := null;
    v_new.superseded_by := null;

    insert into facts select (v_new).*;
    fact_id := v_new.id;
end $$;

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


-- --- Разнесение по точкам ----------------------------------------------------

-- План разнесения: чистый расчёт без записи. Отдельно от применения, чтобы
-- пересчёт мог сравнить «что должно быть» с «что есть» и не переписывать
-- факты, когда результат не изменился.
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

    -- Правило действует на период учёта, а не на дату документа: отчёт
    -- строится по периоду, разнесение должно жить в той же логике.
    select * into r
      from allocation_rules ar
     where ar.tenant_id = f.tenant_id
       and ar.counterparty_id = f.counterparty_id
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
    -- Копейки. Округляем накопленную сумму и берём разность с предыдущей:
    -- так сумма детей всегда равна родителю до копейки, а распределение
    -- остатка детерминировано (по коду точки), а не зависит от порядка строк.
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
            'layer',              f.layer,
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

-- Пересчёт разнесения за период: правило поменялось задним числом.
-- Закрытый период не пересчитываем — и говорим об этом вслух, а не молча
-- пропускаем: молчаливый пропуск читается как «пересчитано».
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
            -- Правило исчезло или снова требует человека: снимаем детей
            -- и возвращаем факт в ожидание, чтобы он не потерялся.
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
                'layer',              f.layer,
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


-- --- Зарплата: агрегат из pay_components ------------------------------------
-- В P&L идёт не ведомость, а суммы по точке, слою и статье. Детализация
-- остаётся в payslips и pay_components — оттуда и прослеживаемость.

create or replace function post_payroll_facts(
    p_payrun_id uuid,
    p_tax_pnl_code text default 'payroll_taxes'
)
returns int
language plpgsql
as $$
declare
    v_tenant     uuid;
    v_period     date;
    v_period_end date;
    v_status     payrun_status;
    v_doc        uuid;
    v_tax_item   uuid;
    v_missing    text;
    r            record;
    n            int := 0;
    v_action     text;
begin
    select tenant_id, period, status into v_tenant, v_period, v_status
      from payruns where id = p_payrun_id;
    if not found then
        raise exception 'расчёт % не найден', p_payrun_id;
    end if;
    if v_status = 'draft' then
        raise exception 'расчёт % ещё черновик: в P&L его проводить рано', p_payrun_id;
    end if;
    v_period_end := (v_period + interval '1 month - 1 day')::date;

    select id into v_tax_item
      from pnl_items
     where code = p_tax_pnl_code and (tenant_id = v_tenant or tenant_id is null)
     order by tenant_id nulls last
     limit 1;
    if v_tax_item is null then
        raise exception 'нет статьи P&L с кодом % для налогов и взносов', p_tax_pnl_code;
    end if;

    -- Группа сотрудника без статьи P&L — это не «ноль», а недонастроенный
    -- справочник. Молча пропустить = потерять зарплату в отчёте.
    select string_agg(distinct g.code, ', ') into v_missing
      from payslips ps
      join lateral (
          select et.* from employment_terms et
           where et.employee_id = ps.employee_id and et.valid_from <= v_period_end
           order by et.valid_from desc limit 1
      ) et on true
      join employee_groups g on g.id = et.group_id
     where ps.payrun_id = p_payrun_id and g.pnl_item_id is null;
    if v_missing is not null then
        raise exception 'у групп % не задана статья P&L', v_missing;
    end if;

    v_doc := upsert_document(jsonb_build_object(
        'tenant_id',   v_tenant,
        'kind',        'payroll_run',
        'source',      'payroll',
        'external_id', p_payrun_id::text,
        'doc_date',    v_period_end,
        'period',      v_period
    ));

    -- Начисления. Слой и канал берём из компонента: «надбавка кешем» —
    -- обычный компонент, а не особый случай.
    for r in
        select ps.unit_id,
               pc.layer,
               g.pnl_item_id,
               pc.channel,
               sum(pc.amount) as amount
          from pay_components pc
          join payslips ps on ps.id = pc.payslip_id
          -- Условия найма, действующие на конец периода. Перевод между
          -- точками в середине месяца пока схлопывается в последнюю точку —
          -- открытый вопрос, см. docs/payroll-engine.md.
          join lateral (
              select et.* from employment_terms et
               where et.employee_id = ps.employee_id and et.valid_from <= v_period_end
               order by et.valid_from desc limit 1
          ) et on true
          join employee_groups g on g.id = et.group_id
         where ps.payrun_id = p_payrun_id
         group by 1, 2, 3, 4
    loop
        select action into v_action from upsert_fact(jsonb_build_object(
            'tenant_id',   v_tenant,
            'period',      v_period,
            'doc_date',    v_period_end,
            'unit_id',     r.unit_id,
            'pnl_item_id', r.pnl_item_id,
            'layer',       r.layer,
            'amount',      r.amount,
            'title',       'Зарплата, начисления',
            'channel',     r.channel,
            'source',      'payroll',
            'source_ref',  p_payrun_id::text,
            'document_id', v_doc,
            'dedup_key',   format('payroll:%s:%s:%s:%s:%s', p_payrun_id,
                                  coalesce(r.unit_id::text, '-'), r.layer,
                                  r.pnl_item_id, r.channel),
            -- Ведомость без точки бывает (офис, не привязанный к пиццерии):
            -- такую сумму отправляем на разнесение, а не теряем.
            'allocation',  case when r.unit_id is null then 'pending' else 'direct' end
        ));
        if v_action <> 'unchanged' then n := n + 1; end if;
    end loop;

    -- Налоги и взносы — отдельными строками, как и договаривались: слой
    -- всегда белый, потому что платятся официально.
    for r in
        select ps.unit_id, sum(ps.tax + ps.contributions) as amount
          from payslips ps
         where ps.payrun_id = p_payrun_id
         group by 1
        having sum(ps.tax + ps.contributions) <> 0
    loop
        select action into v_action from upsert_fact(jsonb_build_object(
            'tenant_id',   v_tenant,
            'period',      v_period,
            'doc_date',    v_period_end,
            'unit_id',     r.unit_id,
            'pnl_item_id', v_tax_item,
            'layer',       'white',
            'amount',      r.amount,
            'title',       'Налоги и взносы с зарплаты',
            'channel',     'bank',
            'source',      'payroll',
            'source_ref',  p_payrun_id::text,
            'document_id', v_doc,
            'dedup_key',   format('payroll_tax:%s:%s', p_payrun_id,
                                  coalesce(r.unit_id::text, '-')),
            'allocation',  case when r.unit_id is null then 'pending' else 'direct' end
        ));
        if v_action <> 'unchanged' then n := n + 1; end if;
    end loop;

    return n;
end $$;


-- --- Валюта консолидации -----------------------------------------------------

-- Последний известный курс на дату. Курс на конец месяца может ещё не
-- приехать — тогда берём предыдущий, а не падаем.
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
    v_rate   numeric;
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


-- --- Сборка P&L --------------------------------------------------------------
-- security_invoker = true обязателен: иначе представление читает данные
-- правами владельца и RLS перестаёт работать — и слои, и тенанты потекут.

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
       f.layer,
       f.counterparty_id,
       f.allocation,
       f.allocation_rule_id,
       f.source,
       f.channel,
       f.amount,
       f.currency,
       -- Если курс к факту ещё не приколочен, считаем на лету по курсу
       -- на конец периода. null = курса нет вообще, и это видно в отчёте.
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

-- P&L по сети: то же, без разреза по точкам. Суммы по точкам и по сети
-- сходятся, потому что нераспределённые факты ('pending') считаются в обоих
-- случаях — просто без точки.
create view pnl_by_network with (security_invoker = true) as
select tenant_id, period, pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(distinct unit_id) as unit_count
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6;

-- Что мешает закрыть период: суммы без точки
create view facts_unallocated with (security_invoker = true) as
select f.tenant_id, f.period, f.id as fact_id, f.title, f.amount, f.currency,
       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source
  from facts f
  left join counterparties c on c.id = f.counterparty_id
 where f.superseded_at is null and f.allocation = 'pending';

-- Готовый отчёт: дерево статей с подытогами. p_unit_id = null — по всей сети.
--
-- amount — сумма по статье в её собственном смысле (выручка положительна,
-- расход положителен). signed_amount — с учётом знака, чтобы подытог вида
-- «результат» считался простым сложением детей.
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


-- --- Изоляция данных --------------------------------------------------------
-- Три ограничения, все restrictive (то есть сужают, объединяются через AND):
-- свой тенант, видимый слой учёта, доступная точка.

alter table facts            enable row level security;
alter table source_documents enable row level security;
alter table fact_batches     enable row level security;

do $$
declare t text;
begin
    foreach t in array array['facts', 'source_documents', 'fact_batches']
    loop
        execute format($f$
            create policy tenant_isolation on %I
                for all
                using (tenant_id in (select app_tenant_ids()))
                with check (tenant_id in (select app_tenant_ids()))
        $f$, t);
    end loop;
end $$;

-- Бухгалтер не видит чёрную кассу. Пишем тоже только в свой слой, иначе
-- ограничение обходится вставкой.
create policy layer_visibility on facts
    as restrictive for all
    using (layer = any (app_visible_layers(tenant_id)))
    with check (layer = any (app_visible_layers(tenant_id)));

-- Управляющий точки видит только свою. Факты без точки (ещё не разнесённые)
-- видны всем, кто вообще имеет доступ к тенанту: разносит их не управляющий.
create policy unit_scope on facts
    as restrictive for all
    using (
        unit_id is null
        or app_visible_units(tenant_id) is null
        or unit_id = any (app_visible_units(tenant_id))
    )
    with check (
        unit_id is null
        or app_visible_units(tenant_id) is null
        or unit_id = any (app_visible_units(tenant_id))
    );
