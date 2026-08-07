"""Факт наличия скрытого регистра больше не читается приложением (T065).

**Что было.** Суммы закрыты (T050, миграция `0009`), а набор регистров строки —
`payslips.ledgers` — читался всеми в тенанте. Ролью `app_user` под бухгалтером,
которой видно только официальный регистр, запрос

    select e.last_name, p.ledgers from payslips p join employees e on ...

отдавал пары «Курир → {internal}», «ANDRIC → {official, supplementary}». Чисел
здесь нет, и все суммы защищены правильно, но D023 требует «ни строк, ни следа»:
поимённый список людей, у которых есть выплаты в закрытых от роли регистрах, —
это след. В прежнем заходе это было записано честно, как оставленное следующей
очереди («набор регистров строки виден всем в тенанте»), — и вот она.

**Почему не политикой.** Прятать строку `payslips` нельзя: ведомость собирается
запросом к `pay_components` **с присоединением** `payslips`, и скрытая строка
утаскивает за собой видимые официальные компоненты смешанного сотрудника — у
бухгалтера пропадала надбавка за питание кухни, то есть официальный расход,
который она обязана провести. Именно поэтому суммы в своё время вынесли в
`payslip_totals`, а строка осталась видимой. Тот же путь для набора регистров
означал бы третью таблицу-спутник — и триггеру пришлось бы писать в строку,
которую пишущая роль не видит: обновление молча не нашло бы её, набор остался бы
неполным, а неполный набор **открывает** итоги (`ledgers <@ видимые` проходит
тем легче, чем меньше набор). Тихий отказ в сторону «показали лишнее» — худшее,
что здесь можно построить.

**Что сделано.** Колонка закрыта привилегией: роль приложения теряет табличное
право `select` на `payslips` и получает его на все колонки поимённо, кроме
`ledgers`. RLS режет строки и столбцов не умеет, а вот привилегия на столбец —
умеет. Набор регистров нужен самой базе (на нём стоит видимость итогов) и не
нужен приложению ни в одной роли: то же самое всегда выводится из состава
компонентов, которые роль видит.

Следствия, каждое проверено тестом (`tests/test_payslip_visibility.py`):

* `select ledgers from payslips` и `select * from payslips` роли приложения
  отвечают `permission denied for table payslips` — это отказ, а не пустота, и
  так и должно быть: колонку никто не должен спрашивать;
* политика `ledger_visibility` на `payslip_totals` больше не читает колонку
  сама, а зовёт функцию `app_payslip_ledgers_visible()`. Так пришлось сделать
  по факту, а не по замыслу: выражение политики проверяется **привилегиями
  вызывающего**, и политика, читающая закрытую колонку, отвечала всем
  `permission denied for table payslips` — то есть итоги пропали бы у всех,
  включая директора. Проверено прогоном: сначала красным, потом зелёным.
  Внутри `security definer` колонка читается правами владельца;
* триггер `payslip_ledgers_add()` стал `security definer` по той же причине: он
  читает и пишет `ledgers`, и без прав владельца расчёт падал бы отказом в
  привилегии.

**Про `security definer` здесь важно не перепутать две разные вещи.** Он даёт
права владельца на **объекты** (в том числе привилегию на колонку) — этим мы и
пользуемся. Но он **не** отменяет `force row level security`, если владелец —
обычная роль, а не суперпользователь: строки внутри функции по-прежнему режутся
политиками по контексту вызывающего (проверено на живой базе; на этом же стоит
T052). То есть защита не переехала в функцию: какие строки видны, решают
политики, а функция только читает колонку той строки, которая и так видна.

* поле убрано из модели Django, но **не из базы**: ORM выбирает колонки
  поимённо, и пока поле было в модели, любой `Payslip.objects...` (включая
  удаление ведомостей при пересчёте) спрашивал бы закрытую колонку.

**Чего эта миграция не делает.** Она не прячет сам факт, что у сотрудника есть
строка ведомости: `payslips` остаётся видимой в тенанте (с учётом точек), в ней
нет ни чисел, ни регистров. Скрыть и это можно только вместе с пересборкой
ведомости не через присоединение — отдельная работа, не эта.
"""
from django.db import migrations

APP_COLUMNS = ["id", "tenant_id", "payrun_id", "employee_id", "unit_id", "notes"]

# Табличную привилегию обязательно снять целиком: `revoke select (ledgers)` при
# живой табличной привилегии не делает ничего — она покрывает все колонки, в том
# числе будущие. Ловушка тихая: отзыв проходит без ошибки, а колонка остаётся
# читаемой.
PRIVILEGES = f"""
revoke select on payslips from app_user;
grant select ({", ".join(APP_COLUMNS)}) on payslips to app_user;

comment on column payslips.ledgers is
    'Регистры учёта, из которых собрана строка. Заполняется триггером, читается только базой: роли приложения привилегия на колонку не выдана (T065)';
"""

RESTORE_PRIVILEGES = "grant select on payslips to app_user;"

# security definer — потому что функция читает и пишет колонку, закрытую от роли
# приложения. Строки при этом по-прежнему видны через политики: definer не
# обходит force RLS у обычного владельца.
TRIGGER = """
create or replace function payslip_ledgers_add()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    -- Только пополнение: читать pay_components отсюда нельзя, на них своя RLS,
    -- и набор вышел бы неполным именно у той роли, от которой мы закрываемся.
    update payslips
       set ledgers = (
               select array_agg(distinct value order by value)
                 from unnest(ledgers || new.ledger) as value
           )
     where id = new.payslip_id
       and not (new.ledger = any (ledgers));
    return null;
end
$$;
"""

# Политика видимости итогов переезжает на функцию: выражение политики
# проверяется привилегиями вызывающего, и прямое чтение закрытой колонки
# отвечало бы `permission denied` всем ролям сразу.
POLICY = """
create or replace function app_payslip_ledgers_visible(p_payslip uuid)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1 from payslips p
         where p.id = p_payslip
           and p.ledgers <@ app_visible_ledgers(p.tenant_id)
    )
$$;

comment on function app_payslip_ledgers_visible(uuid) is
    'Видны ли пользователю ВСЕ регистры строки ведомости. Читает закрытую колонку правами владельца; строки при этом режутся политиками как обычно';

drop policy if exists ledger_visibility on payslip_totals;

create policy ledger_visibility on payslip_totals
    as restrictive for select
    using (app_payslip_ledgers_visible(payslip_totals.payslip_id));
"""

RESTORE_POLICY = """
drop policy if exists ledger_visibility on payslip_totals;

create policy ledger_visibility on payslip_totals
    as restrictive for select
    using (exists (
        select 1 from payslips p
         where p.id = payslip_totals.payslip_id
           and p.ledgers <@ app_visible_ledgers(p.tenant_id)
    ));

drop function if exists app_payslip_ledgers_visible(uuid);
"""

RESTORE_TRIGGER = """
create or replace function payslip_ledgers_add()
returns trigger
language plpgsql
as $$
begin
    update payslips
       set ledgers = (
               select array_agg(distinct value order by value)
                 from unnest(ledgers || new.ledger) as value
           )
     where id = new.payslip_id
       and not (new.ledger = any (ledgers));
    return null;
end
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0020_employee_visibility")]

    operations = [
        migrations.RunSQL(TRIGGER, RESTORE_TRIGGER),
        migrations.RunSQL(POLICY, RESTORE_POLICY),
        migrations.RunSQL(PRIVILEGES, RESTORE_PRIVILEGES),
        # Колонка остаётся в базе — из модели уходит только знание о ней.
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RemoveField(model_name="payslip", name="ledgers")],
            database_operations=[],
        ),
    ]
