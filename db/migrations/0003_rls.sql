-- =============================================================================
-- 0003 — Изоляция данных между тенантами
-- =============================================================================
-- Мультитенантный SaaS: данные партнёров не должны пересекаться ни при каких
-- обстоятельствах. Плюс отдельно ограничивается видимость слоёв учёта —
-- бухгалтер не видит чёрную кассу.
-- =============================================================================

-- Тенанты, доступные текущему пользователю
create or replace function auth_tenant_ids()
returns setof uuid
language sql stable security definer
set search_path = public
as $$
    select tenant_id from memberships where user_id = auth.uid()
$$;

-- Слои учёта, видимые текущему пользователю в рамках тенанта
create or replace function auth_visible_layers(p_tenant uuid)
returns accounting_layer[]
language sql stable security definer
set search_path = public
as $$
    select coalesce(
        (select array_agg(distinct l)
           from memberships m
           join roles r on r.id = m.role_id
           cross join unnest(r.visible_layers) as l
          where m.user_id = auth.uid() and m.tenant_id = p_tenant),
        '{}'::accounting_layer[]
    )
$$;


-- --- Базовая политика: только свои тенанты -----------------------------------

do $$
declare t text;
begin
    foreach t in array array[
        'legal_entities', 'units', 'roles', 'memberships', 'pnl_items',
        'counterparties', 'allocation_rules', 'periods',
        'rule_overrides', 'employee_groups', 'employees', 'employment_terms',
        'timesheets', 'payruns', 'payslips', 'pay_components'
    ]
    loop
        execute format('alter table %I enable row level security', t);
        execute format($f$
            create policy tenant_isolation on %I
                for all
                using (tenant_id in (select auth_tenant_ids()))
                with check (tenant_id in (select auth_tenant_ids()))
        $f$, t);
    end loop;
end $$;


-- --- Слои учёта --------------------------------------------------------------
-- Поверх изоляции по тенанту: строки чужого слоя не видны.

create policy layer_visibility on pay_components
    for select
    using (layer = any (auth_visible_layers(tenant_id)));

create policy layer_visibility on allocation_rules
    for select
    using (layer = any (auth_visible_layers(tenant_id)));


-- --- Справочники без тенанта --------------------------------------------------
-- Пресеты и календари общие, читать может любой авторизованный.

alter table rule_presets enable row level security;
alter table calendars    enable row level security;
alter table fx_rates     enable row level security;

create policy read_all on rule_presets for select using (auth.uid() is not null);
create policy read_all on calendars    for select using (auth.uid() is not null);
create policy read_all on fx_rates     for select using (auth.uid() is not null);

-- tenants: пользователь видит только те, где состоит
alter table tenants enable row level security;
create policy own_tenants on tenants
    for select using (id in (select auth_tenant_ids()));
