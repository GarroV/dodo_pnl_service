-- =============================================================================
-- 0002 — Зарплатный движок
-- =============================================================================
-- Правила начисления лежат в конфигурации, не в коде. Пресет страны —
-- отправная точка, партнёр и группа переопределяют его слоями.
-- Всё версионируется по датам: пересчёт задним числом не должен ломать
-- уже закрытые периоды.
-- =============================================================================

-- --- Пресеты и слои правил ---------------------------------------------------

create table rule_presets (
    id            uuid primary key default gen_random_uuid(),
    code          text not null,               -- 'serbia-2026'
    title         text not null,
    country_code  text not null,
    body          jsonb not null,              -- то, что сейчас в YAML
    valid_from    date not null,
    valid_to      date,
    unique (code, valid_from)
);

comment on table rule_presets is
    'Готовый набор правил страны из коробки. Новая страна = новый пресет, не новый код';

-- Переопределения поверх пресета. scope_type задаёт уровень слоя.
create type rule_scope as enum ('country', 'tenant', 'group', 'employee');

create table rule_overrides (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants on delete cascade,
    scope_type  rule_scope not null,
    scope_id    uuid,                          -- id группы или сотрудника
    path        text not null,                 -- 'hour_types.night.pay_percent'
    value       jsonb not null,
    valid_from  date not null,
    valid_to    date,
    created_by  uuid,
    created_at  timestamptz not null default now(),
    check (valid_to is null or valid_to > valid_from)
);

create index on rule_overrides (tenant_id, scope_type, scope_id, valid_from desc);


-- --- Группы сотрудников ------------------------------------------------------
-- Слой учёта и схема расчёта привязаны к группе, на уровне человека
-- переопределяются. Курьеры, кухня, управляющие, офис, временные.

create table employee_groups (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants on delete cascade,
    code         text not null,
    title        text not null,
    scheme       text not null,                 -- ключ схемы из пресета
    layer        accounting_layer not null default 'white',
    pnl_item_id  uuid references pnl_items on delete set null,
    unique (tenant_id, code)
);


-- --- Сотрудники --------------------------------------------------------------
-- ФИО между системами не совпадают, поэтому ключ — внешний идентификатор.

create table employees (
    id            uuid primary key default gen_random_uuid(),
    tenant_id     uuid not null references tenants on delete cascade,
    external_id   text not null,                -- сквозной ключ, напр. JMBG
    first_name    text not null,
    last_name     text not null,
    external_ids  jsonb not null default '{}'::jsonb,   -- id в Dodo IS, бухпрограмме
    hired_at      date,
    dismissed_at  date,
    unique (tenant_id, external_id)
);

-- Условия найма версионируются: ставка, коэффициент, группа и точка меняются
-- со временем, а перевод между точками в середине месяца должен корректно
-- разносить зарплату по P&L обеих точек.
create table employment_terms (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references tenants on delete cascade,
    employee_id  uuid not null references employees on delete cascade,
    group_id     uuid not null references employee_groups on delete restrict,
    unit_id      uuid references units on delete set null,
    base_rate    numeric(12,4) not null,
    coefficient  numeric(8,4) not null default 1,
    scheme       text,                          -- переопределяет схему группы
    layer        accounting_layer,              -- переопределяет слой группы
    valid_from   date not null,
    valid_to     date,
    check (valid_to is null or valid_to > valid_from)
);

create index on employment_terms (tenant_id, employee_id, valid_from desc);


-- --- Табель ------------------------------------------------------------------
-- На первом этапе часы вводятся вручную, позже подтягиваются из Dodo IS.

create table timesheets (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references tenants on delete cascade,
    employee_id    uuid not null references employees on delete cascade,
    unit_id        uuid references units on delete set null,
    period         date not null,
    insured_hours  numeric(8,2) not null default 0,   -- база для взносов
    norm_hours     numeric(8,2) not null,
    hours          jsonb not null default '{}'::jsonb, -- {regular: 176, sick: 20, ...}
    deduction      numeric(14,2) not null default 0,   -- удержания
    source         text not null default 'manual',     -- manual | dodo_is | import
    created_at     timestamptz not null default now(),
    unique (tenant_id, employee_id, period, unit_id)
);


-- --- Расчёт ------------------------------------------------------------------

create type payrun_status as enum ('draft', 'calculated', 'approved', 'paid');

create table payruns (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants on delete cascade,
    period      date not null,
    preset_id   uuid references rule_presets on delete restrict,
    status      payrun_status not null default 'draft',
    calculated_at timestamptz,
    approved_by uuid,
    unique (tenant_id, period)
);

create table payslips (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references tenants on delete cascade,
    payrun_id      uuid not null references payruns on delete cascade,
    employee_id    uuid not null references employees on delete cascade,
    unit_id        uuid references units on delete set null,
    net            numeric(14,2) not null default 0,
    gross          numeric(14,2) not null default 0,
    tax            numeric(14,2) not null default 0,
    contributions  numeric(14,2) not null default 0,
    total_cost     numeric(14,2) not null default 0,
    to_bank        numeric(14,2) not null default 0,
    to_cash        numeric(14,2) not null default 0,
    notes          text[] not null default '{}',
    unique (payrun_id, employee_id)
);

-- Атом расчёта. Из компонентов собирается и ведомость, и строки P&L,
-- где зарплата и налоги идут раздельно. Надбавка наличными — не особый
-- случай, а обычный компонент с channel = 'cash'.
create table pay_components (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references tenants on delete cascade,
    payslip_id  uuid not null references payslips on delete cascade,
    code        text not null,                 -- 'hours.regular', 'minimum_guarantee'
    title       text not null,
    amount      numeric(14,2) not null,
    layer       accounting_layer not null,
    channel     payout_channel not null default 'bank',
    taxable     boolean not null default true
);

create index on pay_components (tenant_id, payslip_id);
create index on pay_components (tenant_id, code);
