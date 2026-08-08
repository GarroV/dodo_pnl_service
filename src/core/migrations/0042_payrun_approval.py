"""Утверждение периода и откат с причиной (T025).

**Что здесь появляется.**

1. Откат без причины отвергает база. Не форма и не представление: проверка,
   которую обходит любой второй путь записи, гарантией не является — тот же
   довод, по которому в `0041` заморозку держит триггер, а не политика.
2. Права `period.approve` и `period.reopen` перестают быть надписью в роли:
   запись в `payruns` требует того права, которое соответствует тому, **чем
   строка становится**.
3. Имя автора перехода можно показать на экране, не открывая чужие строки
   `users`.

**Почему причину сторожит отдельный триггер, а не `payrun_guard`.** Чтобы
дописать одну проверку в `payrun_guard`, пришлось бы скопировать в эту миграцию
всю функцию из `0041` вместе с её доводами — и дальше два её экземпляра
расходились бы молча. Разделение честное и по смыслу: «куда можно перейти» и
«что человек обязан объяснить» — разные вопросы. Триггеры `payruns_guard` и
`payruns_reason_guard` оба `before update`, порядок между ними задаёт имя
(алфавитный), и он верный: сначала законность перехода, потом объяснение.

**Почему право по новому статусу.** `with check` старой строки не видит, поэтому
правило записано по тому, чем строка становится. Этого достаточно: в `approved`
попадают только утверждением, в `reopened` — только откатом, а правку
утверждённой строки без смены статуса не пускает триггер `0041`.

**Почему право заменяет `payrun.calculate`, а не добавляется к нему.** Политика
`payrun_calculate_update` из `0022` требовала право считать на **любую** запись
в `payruns`. Ограничивающая политика сверху дала бы «утвердить может тот, кто
умеет считать **и** имеет право утверждать», то есть `period.approve` так и не
стал бы самостоятельным правом, а роль-утверждающий без права считать не смогла
бы ничего. Поэтому политика правки переписывается целиком.

**Почему имя автора — функция, а не политика на `users`.** По `0010` человек
видит в `users` только свою строку. Открыть коллег политикой значило бы отдать
строку целиком — вместе с хэшем пароля, почтой и признаком активности. Нужно же
ровно одно поле, поэтому `security definer`-функция отдаёт **только имя** и
только тому, с кем у автора есть общий тенант.

**Нумерация с 0042**: `0030+` занят блоком `timesheets`, `0040`/`0041` — своими.
"""
from django.db import migrations

# --- какие переходы требуют объяснения ---------------------------------------
# Рядом с payrun_next_statuses(), и по той же причине: один источник истины.
# Приложение спрашивает **у базы**, обязательна ли причина, и не заводит своего
# списка — иначе форма однажды перестала бы спрашивать то, чего база требует.
REASON_RULE = """
create or replace function payrun_reason_required(p_to payrun_status)
returns boolean
language sql immutable
as $$
    select p_to = 'reopened'
$$;

comment on function payrun_reason_required(payrun_status) is
    'Требует ли переход в этот статус объяснения. Единственный источник истины о том, где причина обязательна';
"""

DROP_REASON_RULE = "drop function if exists payrun_reason_required(payrun_status);"

REASON_GUARD = """
create or replace function payrun_reason_guard()
returns trigger
language plpgsql
as $$
begin
    if new.status is not distinct from old.status then
        return new;
    end if;

    -- Пробелы объяснением не считаются: «причина есть» и «в поле что-то
    -- напечатали» — разные утверждения, а в журнале должно остаться первое.
    if payrun_reason_required(new.status)
       and nullif(btrim(coalesce(current_setting('app.transition_reason', true), '')), '') is null
    then
        raise exception 'переход расчёта в «%» требует причины', new.status
            using hint = 'причина передаётся настройкой транзакции app.transition_reason';
    end if;

    return new;
end
$$;

comment on function payrun_reason_guard() is
    'Требует причину там, где она обязательна (откат). Триггер, а не проверка в приложении: иначе её обходит любой другой путь записи';

create trigger payruns_reason_guard
    before update on payruns
    for each row execute function payrun_reason_guard();
"""

DROP_REASON_GUARD = """
drop trigger if exists payruns_reason_guard on payruns;
drop function if exists payrun_reason_guard();
"""

# --- права на утверждение и откат ---------------------------------------------
RIGHTS = """
drop policy if exists payrun_calculate_update on payruns;

create policy payrun_lifecycle_update on payruns
    as restrictive for update
    with check (
        case status
            when 'approved' then app_has_permission(tenant_id, 'period.approve')
            when 'reopened' then app_has_permission(tenant_id, 'period.reopen')
            else app_has_permission(tenant_id, 'payrun.calculate')
        end
    );

comment on column payruns.approved_by is
    'Кто утвердил расчёт в последний раз. История целиком — в payrun_transitions; '
    'здесь снимок для выборок по самому расчёту. Откат значение не стирает: '
    'триггер payrun_guard намеренно не даёт откату менять ничего, кроме статуса.';
"""

DROP_RIGHTS = """
drop policy if exists payrun_lifecycle_update on payruns;

create policy payrun_calculate_update on payruns
    as restrictive for update
    with check (app_has_permission(tenant_id, 'payrun.calculate'));
"""

# --- имя автора перехода ------------------------------------------------------
DISPLAY_NAME = """
create or replace function app_user_display_name(p_user uuid)
returns text
language sql stable security definer
set search_path = public
as $$
    select coalesce(nullif(u.full_name, ''), u.username)
      from users u
     where u.id = p_user
       and exists (
           select 1
             from memberships mine
             join memberships theirs on theirs.tenant_id = mine.tenant_id
            where mine.user_id = app_user_id()
              and theirs.user_id = p_user
       )
$$;

comment on function app_user_display_name(uuid) is
    'Имя человека для показа рядом с его действием. Только имя и только тем, с кем есть общий тенант: строка users закрыта политикой и содержит хэш пароля';
"""

DROP_DISPLAY_NAME = "drop function if exists app_user_display_name(uuid);"


class Migration(migrations.Migration):
    dependencies = [("core", "0041_payrun_lifecycle")]

    operations = [
        migrations.RunSQL(REASON_RULE, DROP_REASON_RULE),
        migrations.RunSQL(REASON_GUARD, DROP_REASON_GUARD),
        migrations.RunSQL(RIGHTS, DROP_RIGHTS),
        migrations.RunSQL(DISPLAY_NAME, DROP_DISPLAY_NAME),
    ]
