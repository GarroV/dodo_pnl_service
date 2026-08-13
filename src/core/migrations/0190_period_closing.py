"""Учётный месяц закрывается вместе с утверждением расчёта (T094).

**Что здесь появляется.** `periods.status` перестаёт быть колонкой, которую в
продукте никто не пишет: он становится следствием состояния расчёта — и
единственным путём, которым его вообще можно изменить.

**Почему автоматически, а не отдельной кнопкой.** Разбиралось так.

Отдельное действие «закрыть период» пришлось бы вводить в порядок работы за
месяц четвёртым шагом. Онбординг (T077) обещает три и заканчивается
утверждением: «Утвердите период: расчёт замораживается, откат — только с
причиной». Ни одного условия сверх тех, что уже проверены при утверждении, у
такой кнопки нет — то есть это была бы ручка, за которой ничего не стоит, а
список месяцев продолжал бы врать у каждого, кто её не нажал. Ровно этим
дефект и был: состояние, до которого продукт своими силами не доходит.

Сегодня в продукте месяц закрывает **только** зарплата — других данных за
период он ещё не собирает. Когда появятся (расходы, выписка, P&L), закрытие
месяца станет самостоятельным решением и получит своё действие; пока такого
решения нет, выдумывать его — это догадка о ненаписанном экране, ровно та,
которую `0041` отказалась делать про `paid`.

**Что «закрыт» значит для месяца, у которого расчёт откатили.** Ничего:
месяц снова открыт. «Закрыт» здесь — состояние, а не история. Иначе по списку
месяцев нельзя было бы отличить законченный месяц от того, который прямо
сейчас пересчитывают, а история «утверждали и открыли обратно» и так есть — в
`payrun_transitions`, вместе с причиной и автором.

**Почему триггер, а не код приложения.** Тот же довод, что в `0041`: правило
«месяц закрыт тогда и только тогда, когда расчёт утверждён» — утверждение о
данных. Записанное в `lifecycle.approve()`, оно обходилось бы любым вторым
путём записи (сид, обслуживание, чистый SQL) и разъезжалось бы молча — а
разъехавшееся состояние выглядит на экране ровно как верное.

**Почему прямая правка состояния запрещена.** Хранимая копия чужого состояния
без запрета на запись — это второй источник истины, который однажды разойдётся
с первым. Запрет стоит только на `update` статуса: заведение месяца (`insert`)
остаётся делом администрирования — сид заводит месяцы до того, как появится
хоть один расчёт. Прочие колонки строки месяца правятся как раньше.

**Строки месяца может не быть.** Тогда синхронизировать нечего, и утверждение
проходит молча. Это не проглоченная ошибка: в продукте расчёт заводится со
страницы месяца, то есть строка есть всегда; падать здесь значило бы ломать
обслуживание ради состояния, которого в продукте не бывает.

**Нумерация с 0190** — стройка идёт несколькими блоками в разных копиях
репозитория, номера разведены заранее.
"""
from django.db import migrations

# --- состояние месяца по состоянию расчёта ------------------------------------
# Отдельной функцией, а не выражением внутри триггера: соответствие статусов —
# это правило, и спросить его должен уметь и тест, и следующий читатель.
#
# `paid` в `closed` не отображается намеренно. В цикле `0041` перехода в него
# нет и экрана выплаты нет ни в одной задаче очереди; правило «выплаченный
# месяц тоже закрыт» было бы догадкой о ненаписанном экране. Появится
# переход — появится и строка здесь, вместе с ним.
MAPPING = """
create or replace function period_status_for_payrun(p_status payrun_status)
returns period_status
language sql immutable
as $$
    select case when p_status = 'approved' then 'closed' else 'open' end::period_status
$$;

comment on function period_status_for_payrun(payrun_status) is
    'Состояние учётного месяца по состоянию его расчёта. Единственный источник истины о том, когда месяц закрыт';
"""

DROP_MAPPING = "drop function if exists period_status_for_payrun(payrun_status);"

# --- закрытие и открытие месяца ----------------------------------------------
SYNC = """
create or replace function payrun_sync_period()
returns trigger
language plpgsql
as $$
declare
    v_status period_status := period_status_for_payrun(new.status);
begin
    -- Пересчёт статуса не меняет и месяца не касается: это не переход.
    if tg_op = 'UPDATE' and new.status is not distinct from old.status then
        return null;
    end if;

    -- Кто закрыл месяц — тот, кто утвердил расчёт. Сначала снимок из самой
    -- строки (`approved_by` ставит утверждение), затем контекст приложения —
    -- как в журнале переходов. Оба пусты только когда расчёт перевели мимо
    -- приложения; пусто там честнее выдуманного имени.
    update periods
       set status = v_status,
           closed_at = case when v_status = 'closed' then now() end,
           closed_by = case
               when v_status = 'closed' then coalesce(new.approved_by, app_user_id())
           end
     where tenant_id = new.tenant_id
       and period = new.period
       and status is distinct from v_status;

    return null;
end
$$;

comment on function payrun_sync_period() is
    'Закрывает учётный месяц при утверждении расчёта и открывает при откате. Триггером, а не приложением: иначе состояние месяца зависело бы от пути записи';

create trigger payruns_sync_period
    after insert or update on payruns
    for each row execute function payrun_sync_period();
"""

DROP_SYNC = """
drop trigger if exists payruns_sync_period on payruns;
drop function if exists payrun_sync_period();
"""

# --- состояние месяца задаёт только расчёт ------------------------------------
GUARD = """
create or replace function period_status_guard()
returns trigger
language plpgsql
as $$
begin
    -- Глубина 1 — правка пришла оператором снаружи; синхронизация из триггера
    -- на payruns приходит глубже. Тот же приём, что у журнала переходов.
    if new.status is distinct from old.status and pg_trigger_depth() <= 1 then
        raise exception
            'состояние учётного месяца задаёт расчёт периода: правка «%» → «%» отклонена',
            old.status, new.status
            using hint = 'месяц закрывается утверждением расчёта и открывается откатом';
    end if;
    return new;
end
$$;

comment on function period_status_guard() is
    'Запрещает менять состояние учётного месяца мимо цикла расчёта. Триггер, а не политика: действует и на владельца таблиц';

create trigger periods_status_guard
    before update on periods
    for each row execute function period_status_guard();

comment on column periods.status is
    'Состояние учётного месяца. Следствие состояния расчёта (T094): закрыт = расчёт утверждён. Руками не ставится — запрещено триггером periods_status_guard';
comment on column periods.closed_at is
    'Когда месяц закрылся. Ставится при утверждении расчёта, стирается при откате';
comment on column periods.closed_by is
    'Кто закрыл месяц: тот, кто утвердил расчёт. Пусто у открытого месяца';
"""

DROP_GUARD = """
drop trigger if exists periods_status_guard on periods;
drop function if exists period_status_guard();
"""

# --- уже заведённые месяцы ----------------------------------------------------
# Без этого «правда в списке» началась бы только со следующего утверждения: уже
# утверждённые месяцы остались бы открытыми навсегда, то есть ровно тем
# дефектом, ради которого миграция и написана.
#
# Правило берётся у той же функции, что и триггер, — второго соответствия
# статусов здесь не пишется. Месяц без расчёта открыт по построению: закрыть
# его в продукте нечем.
#
# Идёт **до** сторожа: сторож запрещает менять состояние месяца оператором
# снаружи, а это ровно такой оператор. Разовая правка истории — единственное
# место, где так можно, и она поэтому стоит здесь, а не после.
BACKFILL = """
with want as (
    select p.id,
           coalesce(period_status_for_payrun(r.status), 'open'::period_status) as status,
           r.approved_by
      from periods p
      left join payruns r
             on r.tenant_id = p.tenant_id and r.period = p.period
)
update periods p
   set status = want.status,
       closed_at = case when want.status = 'closed' then now() end,
       closed_by = case when want.status = 'closed' then want.approved_by end
  from want
 where want.id = p.id
   and p.status is distinct from want.status;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0150_payslip_steps")]

    operations = [
        migrations.RunSQL(MAPPING, DROP_MAPPING),
        migrations.RunSQL(SYNC, DROP_SYNC),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.RunSQL(GUARD, DROP_GUARD),
    ]
