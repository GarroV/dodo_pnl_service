"""Матрица прав как конфигурация: три состояния вместо двух (T203, #129, #130).

Q027 отвечен решением D060: выбирать одну картину доступа не нужно — «надо будет
обсуждать с партнёром на месте… система управления доступами должна быть гибкая,
модульная». Значит строится механизм настройки, а не спор о наборах.

Механизм — таблица «роль × право» с тремя состояниями: `included` (входит в
набор), `optional` (партнёр вправе выдать) и `never` (не выдаётся никогда).

**Зачем третье состояние.** Конструктор прав из галочек рано или поздно соберёт
опасную комбинацию: управляющему точки включат ведение ролей, и дальше он
выпишет себе всё остальное сам. `never` делает такую клетку невозможной по
построению, а не по бдительности того, кто нажимает, — и ровно поэтому тонкая
настройка остальных клеток становится безопасной.

**Почему стена — `check`, а не политика.** Политики RLS владелец таблиц
обходит; на этом в проекте уже обжигались, и поэтому тесты доступа гоняются
ролью `app_user`. `check` не обходит никто: ни владелец схемы, ни
суперпользователь, ни новый экран, который забудет спросить. Стена, которую
снимает одна забытая проверка, стеной не является.

**Пустая матрица — это «стен не заведено», а не «нельзя ничего».** Роль,
заведённая до этой миграции, продолжает работать как прежде. Обратное умолчание
молча обесправило бы каждый поднятый стенд — тот же класс молчания, из-за
которого появилась доставка форм ролей (`core/role_delivery.py`).

**Стен ровно две, и обе названы эталоном словами**, а не выведены из значков
матрицы: «доступ выдаёт только администратор партнёра» и «менять правила
расчёта — формулы начислений — только администратор». Остальное, чего у роли
нет, остаётся `optional`: владелец просил гибкости прямо, и лишняя стена была
бы решением, принятым за него.
"""

from django.db import migrations, models

# Снимок матрицы на момент миграции. Не импорт из `core.roles` намеренно:
# миграция — снимок схемы во времени, а код уходит вперёд. Дальше форму везёт
# доставка (`manage.py roles_sync`), у которой на это есть свой механизм с
# различением «код ушёл вперёд» и «партнёр правил руками».
WALLS = "'{\"rules.manage\": \"never\", \"roles.manage\": \"never\"}'::jsonb"

FUNCTION = """
-- Все ли выданные права укладываются в матрицу роли.
--
-- `immutable`, потому что функция зависит ТОЛЬКО от своих аргументов: это
-- условие пригодности для `check`. Незнакомое состояние (и отсутствие клетки)
-- считается разрешающим — см. про пустую матрицу в шапке миграции.
create or replace function app_permissions_within_matrix(p_permissions jsonb, p_states jsonb)
returns boolean
language sql immutable
as $$
    select not exists (
        select 1
          from jsonb_array_elements_text(
                   case when jsonb_typeof(p_permissions) = 'array'
                        then p_permissions
                        else '[]'::jsonb
                   end
               ) as code
         where coalesce(p_states, '{}'::jsonb) ->> code = 'never'
    )
$$;

comment on function app_permissions_within_matrix(jsonb, jsonb) is
    'Укладываются ли права роли в её матрицу. Стена (never) держится check-ом, а не политикой: политики владелец таблиц обходит, check не обходит никто';
"""

DROP_FUNCTION = "drop function if exists app_permissions_within_matrix(jsonb, jsonb);"

COMMENTS = """
comment on column roles.permission_states is
    'Матрица роли: код права -> included (входит в набор) / optional (партнёр вправе выдать) / never (не выдаётся никогда). Пусто = стен не заведено (T203, D060)';
"""

# Матрица приезжает существующим ролям по коду. Администратор не упомянут
# намеренно: он может ВСЁ (D052), то есть стен у него нет ни одной, и пустая
# матрица описывает это точнее любого перечисления.
BACKFILL = f"""
update roles
   set permission_states = coalesce(permission_states, '{{}}'::jsonb) || {WALLS}
 where code in ('director', 'accountant', 'manager');
"""

UNBACKFILL = "update roles set permission_states = '{}'::jsonb;"

CONSTRAINT = """
alter table roles
    add constraint roles_permissions_within_matrix
    check (app_permissions_within_matrix(permissions, permission_states));
"""

DROP_CONSTRAINT = "alter table roles drop constraint if exists roles_permissions_within_matrix;"


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0262_employee_allowances'),
    ]

    operations = [
        migrations.AddField(
            model_name='role',
            name='permission_states',
            field=models.JSONField(db_default={}),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(FUNCTION, DROP_FUNCTION),
        migrations.RunSQL(BACKFILL, UNBACKFILL),
        migrations.RunSQL(CONSTRAINT, DROP_CONSTRAINT),
    ]
