"""Правила страны правятся в продукте, а не только в YAML (T165).

**Что было.** Тело пресета страны — ночные часы, больничные, ставки взносов,
пороги — живёт в `rule_presets` и приезжает туда один раз командой
`manage.py load_presets` из файла `src/payroll/presets/*.yaml`. То есть чтобы
поднять минимальную зарплату Сербии, нужен разработчик и доступ к серверу.
Владелец 2026-08-18 искал эти правила в продукте и не нашёл. Партнёр при этом
может переопределить любое из них для себя — но это не то же самое: страна
задаёт базу, и база обязана меняться там же, где живёт.

**Почему нельзя было просто открыть тело на запись правом партнёра.** У
`rule_presets` нет `tenant_id`: он в `SHARED_TABLES` (`0004_rls`), и два
партнёра одной страны читают одну и ту же строку. Партнёр, получивший право
править тело, менял бы расчёт соседу — молча и задним числом. Именно это и
записала `0180_rules_permissions`, отказавшись заводить туда право: «заводить
сюда „право править пресет страны“ значило бы разрешить одному партнёру менять
расчёт другому». Довод в силе, поэтому право здесь **не партнёрское**.

**Кто вправе: `platform_admins`.** Отдельная таблица учёток, у которых есть
право вести правила стран. Не колонка в `users` и не право в роли, и оба выбора
осознанные:

* не роль — роль выдаётся внутри тенанта (`memberships`), а правила страны
  тенанту не принадлежат; право «на всю страну», выданное внутри одного
  партнёра, было бы ровно той дырой, против которой возражала `0180`;
* не колонка в `users` — на `users` стоит политика `users_change_own_row`, и
  человек правит свою строку сам (пароль). Колонка «я администратор платформы» в
  той же строке означала бы, что её можно себе выписать.

Поэтому таблица **без единой политики на запись**: `select` — только свою
строку (чтобы продукт мог показать человеку его собственные возможности),
`insert/update/delete` не разрешены никому из приложения, а права роли
`app_user` на них отозваны явно — два замка вместо одного, тем же приёмом, что
у `users` в `0010`. Первая запись сюда делается тем же путём, что и первая
учётка: администратором базы, миграцией или командой. Иначе никак и не может
быть — кто-то первый должен получить право до того, как продукт умеет его
выдавать.

**Что даёт право.** Ровно две операции над `rule_presets`: завести новую версию
тела и закрыть предыдущую датой. Удаления нет вовсе — версия правила страны
объясняет, почему закрытый месяц посчитан именно так, и удалённая унесла бы
объяснение с собой. Тем же доводом на экране правил нет кнопки «удалить
версию».

**Закрытый период не двигается, и это устройство, а не проверка.** Тело не
переписывается по месту: правка заводит **новую строку** `rule_presets` с датой
начала действия, а прежняя закрывается этим же днём (`valid_to`). Сборка правил
берёт версию, действовавшую в считаемом месяце (`core.rules._in_force`), поэтому
июнь после августовской правки собирается из июньской строки — байт в байт той
же. Непересечение версий держит уже стоящее ограничение
`rule_presets_no_overlap`.

**`edited_at` и `edited_by` — не украшение, а защита от молчаливого откатa.**
`import_presets` кладёт тело `update_or_create` по ключу «код + дата начала», то
есть повторный `load_presets` **перезаписал бы** правку, сделанную в продукте, и
не сказал бы об этом ни слова. Отметка о правке в продукте останавливает
загрузку по этой строке и заставляет команду сказать вслух, что она пропустила.
Без отметки продукт и файл разъезжались бы, и разъезжались бы тихо.

**Обратимость.** Откат снимает политики, функцию, колонки и таблицу права.
Тела пресетов не трогаются: строки, заведённые в продукте, остаются обычными
версиями пресета и продолжают работать — расчёт про способ их появления ничего
не знает.
"""
from django.db import migrations, models

import core.models

TABLE = """
comment on table platform_admins is
    'Учётки, которые вправе вести правила стран (T165). Право не партнёрское: '
    'тело пресета общее для всех партнёров страны, поэтому оно не выдаётся роль'
    'ю внутри тенанта. Записывается администратором базы — из приложения ни од'
    'ной политики на запись нет';
comment on column platform_admins.user_id is 'Учётка. Своя строка человеку видна, чужие — нет';
comment on column platform_admins.note is 'Зачем выдано: имя, повод, кто выдал. Для разбора потом';

alter table platform_admins enable row level security;
alter table platform_admins force row level security;

-- Только своя строка: продукт должен уметь показать человеку его собственные
-- возможности, но список администраторов платформы — не то, что видно партнёру.
create policy platform_admins_own_row on platform_admins
    for select using (user_id = app_user_id());

-- Ни одной политики на запись, и вдобавок отозванные права: право выдаётся
-- вне приложения, как и первая учётка.
revoke insert, update, delete on platform_admins from app_user;

create or replace function app_is_platform_admin()
returns boolean
language sql stable
as $$
    -- Контекст не выставлен — `app_user_id()` даёт null, exists даёт false:
    -- запрет по умолчанию, как и у всех остальных функций контекста.
    select exists (select 1 from platform_admins where user_id = app_user_id())
$$;

comment on function app_is_platform_admin() is
    'Вправе ли текущий пользователь вести правила стран. Без контекста — false';
"""

DROP_TABLE = """
drop function if exists app_is_platform_admin();
drop policy if exists platform_admins_own_row on platform_admins;
alter table platform_admins no force row level security;
alter table platform_admins disable row level security;
"""

# Тело пресета страны открывается на запись — но только праву платформы и только
# на две операции: завести версию и закрыть предыдущую. `delete` не открывается.
#
# Политики пермиссивные, а не `as restrictive`, и это не оплошность: на
# `rule_presets` до сих пор не было **ни одной** политики на запись, то есть
# запись была закрыта всем. Ограничивающая политика сужает уже разрешённое —
# сужать здесь нечего. Разрешение и появляется этими двумя строками.
POLICIES = """
create policy country_rules_insert on rule_presets
    as permissive for insert
    with check (app_is_platform_admin());

-- `using (true)` — намеренно, и это не дырка. У `update` без `using` строки
-- вообще не попадают под правку, и запрет выглядит как «изменено 0 строк»:
-- ровно тот тихий отказ, против которого возражает комментарий в `0022`. Читать
-- эти строки и так вправе каждый, у кого есть контекст (`read_all` из
-- `0004_rls`), поэтому `using (true)` ничего не открывает — а решает всё
-- `with check`, и отказ приходит громким `InsufficientPrivilege`.
create policy country_rules_update on rule_presets
    as permissive for update
    using (true)
    with check (app_is_platform_admin());

comment on column rule_presets.edited_at is
    'Когда тело правили в продукте. Не пусто — load_presets эту версию не перезаписывает (T165)';
comment on column rule_presets.edited_by is 'Кто правил тело в продукте';
"""

DROP_POLICIES = """
drop policy if exists country_rules_insert on rule_presets;
drop policy if exists country_rules_update on rule_presets;
comment on column rule_presets.edited_at is null;
comment on column rule_presets.edited_by is null;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0247_employment_terms_work_measure")]

    operations = [
        migrations.CreateModel(
            name="PlatformAdmin",
            fields=[
                ("user", models.OneToOneField(
                    db_column="user_id", on_delete=models.CASCADE,
                    primary_key=True, serialize=False, to="core.user",
                )),
                ("granted_at", models.DateTimeField(db_default=core.models.now_default())),
                ("note", models.TextField(db_default="")),
            ],
            options={"db_table": "platform_admins"},
        ),
        migrations.AddField(
            model_name="rulepreset",
            name="edited_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="rulepreset",
            name="edited_by",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.RunSQL(TABLE, DROP_TABLE),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
