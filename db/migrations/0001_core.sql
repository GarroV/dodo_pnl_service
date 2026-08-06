-- =============================================================================
-- 0001 — Ядро: тенанты, оргструктура, справочники, роли
-- =============================================================================
-- Мультитенантный сервис. Изоляция данных между партнёрами — через RLS
-- по tenant_id. Сербия — первый тенант, не архитектура: никакой страновой
-- специфики в схеме нет, всё живёт в конфигурации правил.
-- =============================================================================

create extension if not exists "pgcrypto";

-- --- Слои учёта --------------------------------------------------------------
-- Свойство сотрудника, группы или операции — не компании.
-- В Сербии курьеры целиком в black, кухня проведена официально по минималке (grey).
create type accounting_layer as enum ('white', 'grey', 'black');

create type payout_channel as enum ('bank', 'cash');


-- --- Тенанты и оргструктура --------------------------------------------------

create table tenants (
    id              uuid primary key default gen_random_uuid(),
    code            text not null unique,
    title           text not null,
    country_code    text not null,              -- ISO 3166-1 alpha-2
    base_currency   text not null,              -- валюта учёта, напр. RSD
    report_currency text not null default 'EUR',-- валюта консолидации по сети
    created_at      timestamptz not null default now()
);

comment on column tenants.report_currency is
    'Консолидация по сети идёт в этой валюте, по курсу на конец месяца';

create table legal_entities (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants on delete cascade,
    title       text not null,
    tax_number  text,
    created_at  timestamptz not null default now()
);

-- Точка = пиццерия. Расходы разносятся именно на неё, а не на юрлицо:
-- бухгалтерия работает с юрлицом, для неё пиццерии не существует.
create table units (
    id               uuid primary key default gen_random_uuid(),
    tenant_id        uuid not null references tenants on delete cascade,
    legal_entity_id  uuid references legal_entities on delete set null,
    code             text not null,
    title            text not null,
    opened_at        date,
    closed_at        date,
    external_ids     jsonb not null default '{}'::jsonb,  -- id в Dodo IS и др.
    unique (tenant_id, code)
);


-- --- Пользователи и роли -----------------------------------------------------
-- Роли назначаемые. Видимость слоёв учёта определяется ролью: бухгалтер
-- не должен видеть чёрную кассу, оперативный директор видит всё.

create table roles (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid references tenants on delete cascade,  -- null = системная роль
    code           text not null,
    title          text not null,
    permissions    jsonb not null default '[]'::jsonb,
    visible_layers accounting_layer[] not null default '{white}',
    unique (tenant_id, code)
);

create table memberships (
    id         uuid primary key default gen_random_uuid(),
    tenant_id  uuid not null references tenants on delete cascade,
    user_id    uuid not null,                     -- auth.users
    role_id    uuid not null references roles on delete restrict,
    unit_ids   uuid[],                            -- null = все точки тенанта
    created_at timestamptz not null default now(),
    unique (tenant_id, user_id, role_id)
);


-- --- Производственный календарь ----------------------------------------------

create table calendars (
    id            uuid primary key default gen_random_uuid(),
    country_code  text not null,
    period        date not null,          -- первое число месяца
    norm_hours    numeric(6,2) not null,
    working_days  int not null,
    holidays      date[] not null default '{}',
    unique (country_code, period)
);


-- --- Статьи P&L --------------------------------------------------------------
-- Единый справочник — цель проекта. Дерево, чтобы поддержать вложенность
-- (LC → КС → зарплата кухни) и проценты от выручки на любом уровне.

create table pnl_items (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid references tenants on delete cascade,  -- null = общий справочник
    parent_id   uuid references pnl_items on delete cascade,
    code        text not null,
    title       text not null,
    kind        text not null check (kind in ('revenue', 'expense', 'subtotal')),
    sort_order  int not null default 0,
    unique (tenant_id, code)
);


-- --- Контрагенты и правила разнесения ----------------------------------------
-- Знание «что за поставщик и на какую точку его относить» сейчас живёт
-- в голове одного человека. Здесь оно становится данными.

create type allocation_method as enum (
    'fixed_unit',      -- всегда конкретная точка
    'ask',             -- спрашивать каждый раз
    'even',            -- поровну между точками
    'by_revenue'       -- пропорционально выручке
);

create table counterparties (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants on delete cascade,
    title       text not null,
    tax_number  text,
    aliases     text[] not null default '{}',   -- как его пишут в разных системах
    note        text,
    created_at  timestamptz not null default now()
);

-- Версионируется: изменение правила не должно ломать закрытые периоды.
create table allocation_rules (
    id               uuid primary key default gen_random_uuid(),
    tenant_id        uuid not null references tenants on delete cascade,
    counterparty_id  uuid not null references counterparties on delete cascade,
    pnl_item_id      uuid not null references pnl_items on delete restrict,
    method           allocation_method not null,
    unit_id          uuid references units on delete cascade,  -- для fixed_unit
    layer            accounting_layer not null default 'white',
    valid_from       date not null,
    valid_to         date,
    created_by       uuid,
    created_at       timestamptz not null default now(),
    check (method <> 'fixed_unit' or unit_id is not null),
    check (valid_to is null or valid_to > valid_from)
);

create index on allocation_rules (tenant_id, counterparty_id, valid_from desc);


-- --- Курсы валют -------------------------------------------------------------

create table fx_rates (
    id            uuid primary key default gen_random_uuid(),
    base_currency text not null,
    quote_currency text not null,
    rate_date     date not null,
    rate          numeric(18,8) not null,
    unique (base_currency, quote_currency, rate_date)
);


-- --- Периоды и закрытие ------------------------------------------------------

create type period_status as enum ('open', 'review', 'closed');

create table periods (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants on delete cascade,
    period      date not null,
    status      period_status not null default 'open',
    closed_at   timestamptz,
    closed_by   uuid,
    unique (tenant_id, period)
);
