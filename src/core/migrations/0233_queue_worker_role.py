"""
Роль рабочего процесса очереди `queue_worker` (T124, issues #66 и #50).

**Противоречие, которое эта миграция снимает.** Целевая конфигурация площадки
(#50): владелец схемы отдельно, продукт подключается логин-ролью без права на
DDL. `0047_queue_privileges` при этом сознательно оставляет `app_user` на
таблицах очереди только право поставить задачу — полезная нагрузка очереди роли
приложения не нужна. Из двух правил вместе следовало, что рабочий процесс
очереди не работает вовсе: он читает `django_q_ormq` соединением напрямую, без
`set local role`, и получал `permission denied for table django_q_ormq`
(проверено фактически при T058 на стенде `dodo-pnl-dep`).

**Выбор сделан осознанно, а не по факту первой ошибки на площадке.** Из трёх
вариантов issue #66 взят второй: **своя роль очереди**, которая выдаётся только
контейнеру очереди.

- Оставить обе службы на роли владельца — значит отказаться от #50 целиком: на
  площадке продукт снова ходил бы ролью, которую политики не ограничивают.
- Выдать права очереди самой `app_user` — значит вернуть ровно то, что забрала
  `0047`: полезная нагрузка снова видна каждому запросу.
- Отдельная роль оставляет обе гарантии на месте: веб внутри запроса ходит
  `app_user` и очереди не видит, а очередь ходит своей ролью и не имеет ничего
  сверх нужного ей.

**Роль групповая (`nologin`), как и `app_user`.** Логин-роль контейнера очереди
заводит развёртывание (пароль в миграции появиться не может) и делает её членом
**двух** ролей: `queue_worker` — читать очередь, `app_user` — работать с данными
внутри задачи (`db_context` делает `set local role app_user`). Логин-роль веба
членом `queue_worker` **не делается**: тогда защита полезной нагрузки держалась
бы на дисциплине кода, а не на правах базы.

Прав на данные продукта у `queue_worker` нет намеренно: в задаче он всё равно
переключается на `app_user`, и своя дорога к таблицам с ФИО и суммами ему не
нужна ни на минуту.
"""
from django.db import migrations

ROLE = "queue_worker"
TABLES = ("django_q_ormq", "django_q_task", "django_q_schedule")

FORWARD = f"""
do $$
declare
    attrs record;
begin
    if not exists (select 1 from pg_roles where rolname = '{ROLE}') then
        -- Умолчания create role как раз нужные; пишем явно, чтобы намерение
        -- читалось из кода, а не из документации Postgres.
        create role {ROLE} nologin nosuperuser nobypassrls nocreatedb nocreaterole;
    end if;

    select rolsuper, rolbypassrls into attrs from pg_roles where rolname = '{ROLE}';

    -- Та же остановка, что у `app_user` (T052): роль очереди тоже подключается
    -- к базе с данными продукта, и роль с bypassrls обнулила бы изоляцию,
    -- никак этого не показав.
    if attrs.rolsuper or attrs.rolbypassrls then
        raise exception
            'роль {ROLE} обходит RLS (superuser=%, bypassrls=%)',
            attrs.rolsuper, attrs.rolbypassrls
            using hint = 'выполните суперпользователем: alter role {ROLE} nosuperuser nobypassrls';
    end if;
end $$;

-- Право переключиться на роль очереди тому, кто накатывает миграции: ему же
-- разбирать очередь руками, когда что-то пошло не так. Тот же приём, что в
-- `0005_app_role`: повторный grant требует прав администратора роли, которых у
-- обычного владельца может не быть.
do $$
begin
    if not pg_has_role(current_user, '{ROLE}', 'member') then
        execute format('grant {ROLE} to %I', current_user);
    end if;
exception when insufficient_privilege then
    raise exception
        'некому выдать роль {ROLE} пользователю % — он не член и не администратор роли',
        current_user
        using hint = 'выполните суперпользователем: grant {ROLE} to <владелец> with admin option';
end $$;

grant usage on schema public to {ROLE};
grant select, insert, update, delete on {", ".join(TABLES)} to {ROLE};

-- Последовательности ключей очереди: без них `insert` в `django_q_ormq` и
-- `django_q_schedule` не проходит. Перебором, а не списком имён: имя
-- последовательности задаёт Django, и переименование сломало бы миграцию молча.
do $$
declare
    seq record;
begin
    for seq in
        select c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'S' and n.nspname = 'public'
           and c.relname like 'django\\_q\\_%'
    loop
        execute format('grant usage, select on sequence %I to {ROLE}', seq.relname);
    end loop;
end $$;
"""

BACKWARD = f"""
revoke all on {", ".join(TABLES)} from {ROLE};
revoke usage on schema public from {ROLE};
-- Саму роль не удаляем: она выдана логин-роли контейнера очереди, и `drop role`
-- оборвал бы очередь молча. Та же причина, что у `app_user` в `0005`.
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0232_merge_expense_items"),
        # Таблицы очереди должны существовать: иначе grant не на что выдавать.
        ("core", "0047_queue_privileges"),
    ]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
