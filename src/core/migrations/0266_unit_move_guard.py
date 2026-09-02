"""Закрытый месяц не переезжает вместе с точкой (T189, issue #179).

`0265` научила связь точки с юрлицом версиям по датам. Версии сами по себе ещё
не обещают главного, что требует задача: **перенос не меняет ни одного закрытого
месяца**. Версию можно завести задним числом, перенаправить на другое лицо,
обрезать или стереть — и отчёт за уже сданный май ответит по-новому.

Здесь ставится стена.

**Триггер, а не политика и не `check`.** Политику обходит владелец таблиц — на
этом проект уже обжигался, потому тесты доступа и гоняются ролью `app_user`.
`check` не обходит никто, но он видит только свою строку, а «закрыт ли этот
месяц» написано в другой таблице. Остаётся триггер — ровно тот выбор и по тому
же доводу, что у `facts_guard` (`0230`): закрытый месяц обязан быть закрыт для
всех путей записи, включая суперпользователя.

**Где написано «месяц закрыт».** В двух местах, и стена спрашивает оба:
`periods.status = 'closed'` (то же состояние, что стережёт `facts_guard`) и
`payruns.status = 'approved'`. По `0190` одно следует из другого — статус месяца
и есть отражение статуса расчёта, — но строки месяца может не быть вовсе, и
тогда единственным свидетелем остаётся расчёт. Спрашивать одного из двоих
значило бы оставить дыру ровно там, где данных меньше всего.

**Что именно запрещено.** Не «правка версий», а изменение ответа на вопрос
«чьей точка была в закрытом месяце»:

* завести версию с датой внутри закрытого месяца — у точки, у которой версии
  уже есть;
* перенаправить на другое юрлицо версию, начавшуюся не позже закрытого месяца;
* сдвинуть границу версии так, чтобы сдвиг пришёлся на закрытый месяц;
* удалить версию, действовавшую в закрытом месяце.

**Первая версия точки — не перенос, и она разрешена.** Точку заводят и после
того, как партнёр закрыл первый месяц; её первая версия идёт от даты открытия и
формально накрывает закрытые месяцы. Отказ здесь запретил бы заводить точки
всякому, кто хоть раз закрыл месяц, а закрытый месяц от этой записи не меняется:
у новой точки в нём нет ни строки. Условие поэтому — «у точки уже есть версии»,
то есть есть что переписывать.

**Удаление точки и партнёра проходит.** Сид и уборка сносят тенант целиком, и
каскад доходит до версий. Он не упирается в стену потому, что расчёты и месяцы
Django удаляет раньше точек (порядок в `seed_dev` и `demo/seed` объявлен явно, и
он же есть в `conftest.wipe_payruns`): к моменту, когда очередь доходит до
версий, закрытых месяцев у тенанта уже нет.

**Середина месяца остаётся открытым вопросом эталона.** Модуль 11 спрашивает
прямо: «что делать, если дату переноса ставят в середине месяца». Стена этот
вопрос не решает и не притворяется, что решила: внутрь закрытого месяца перенос
не пускается вовсе, а в открытом месяце дата принимается любая — отчёт спросит
юрлицо на первое число, то есть перенос от 15-го числа скажется со следующего
месяца. Придумать здесь разрезание месяца значило бы ответить за владельца.
"""

from django.db import migrations

EDGE = """
-- Последний день последнего закрытого месяца партнёра. Нет таких — null.
--
-- `security definer` здесь по делу, а не по привычке: стена обязана видеть
-- закрытые месяцы целиком, кем бы ни шла запись. Иначе роль, которой месяцы
-- не видны, проходила бы сквозь стену — не обойдя её, а просто не встретив.
create or replace function tenant_closed_through(p_tenant uuid)
returns date
language sql stable security definer
set search_path = public
as $$
    select max(edge) from (
        select (date_trunc('month', period) + interval '1 month - 1 day')::date as edge
          from periods
         where tenant_id = p_tenant and status = 'closed'
        union all
        select (date_trunc('month', period) + interval '1 month - 1 day')::date
          from payruns
         where tenant_id = p_tenant and status = 'approved'
    ) closed
$$;

comment on function tenant_closed_through(uuid) is
    'Последний день последнего закрытого месяца: закрытый учётный период или утверждённый расчёт (T189)';
"""

DROP_EDGE = "drop function if exists tenant_closed_through(uuid);"

GUARD = """
create or replace function unit_legal_entity_guard()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
    v_edge  date;
    v_month text;
begin
    v_edge := tenant_closed_through(coalesce(new.tenant_id, old.tenant_id));
    if v_edge is null then
        return coalesce(new, old);
    end if;
    v_month := to_char(v_edge, 'YYYY-MM');

    if tg_op = 'INSERT' then
        -- Первая версия точки переписывать нечего: до неё ответа не было вовсе.
        if new.valid_from <= v_edge
           and exists (select 1 from unit_legal_entities where unit_id = new.unit_id)
        then
            raise exception 'месяц % закрыт: перенос точки в другое юрлицо этой датой отклонён',
                v_month
                using hint = 'возьмите дату после закрытого месяца — прошлые месяцы остаются за прежним юрлицом';
        end if;
        return new;
    end if;

    if tg_op = 'DELETE' then
        if old.valid_from <= v_edge then
            raise exception 'месяц % закрыт: удаление версии юрлица точки отклонено', v_month
                using hint = 'закрытый месяц обязан помнить, за каким юрлицом он числился';
        end if;
        return old;
    end if;

    -- UPDATE: запрещено ровно то, что меняет ответ за закрытый месяц.
    if new.legal_entity_id is distinct from old.legal_entity_id
       and old.valid_from <= v_edge
    then
        raise exception 'месяц % закрыт: смена юрлица у действовавшей тогда версии отклонена',
            v_month
            using hint = 'перенос оформляется новой версией с даты после закрытого месяца';
    end if;

    if new.valid_from is distinct from old.valid_from
       and least(new.valid_from, old.valid_from) <= v_edge
    then
        raise exception 'месяц % закрыт: сдвиг начала версии отклонён', v_month
            using hint = 'начало версии нельзя двигать через закрытый месяц';
    end if;

    if new.valid_to is distinct from old.valid_to
       and least(
               coalesce(new.valid_to, 'infinity'::date),
               coalesce(old.valid_to, 'infinity'::date)
           ) <= v_edge
    then
        raise exception 'месяц % закрыт: конец версии внутри него отклонён', v_month
            using hint = 'закрыть версию можно датой после закрытого месяца';
    end if;

    return new;
end $$;

comment on function unit_legal_entity_guard() is
    'Стена закрытого месяца: перенос точки в другое юрлицо не переписывает уже сданные месяцы (T189)';

create trigger unit_legal_entity_guard_trg
    before insert or update or delete on unit_legal_entities
    for each row execute function unit_legal_entity_guard();
"""

DROP_GUARD = """
drop trigger if exists unit_legal_entity_guard_trg on unit_legal_entities;
drop function if exists unit_legal_entity_guard();
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0265_unit_legal_entity_versions'),
    ]

    operations = [
        migrations.RunSQL(EDGE, DROP_EDGE),
        migrations.RunSQL(GUARD, DROP_GUARD),
    ]
