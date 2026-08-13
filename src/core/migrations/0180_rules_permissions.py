"""Право `rules.manage` перестаёт быть надписью (T090).

**Тот же долг, что платила `0130`, и последний его кусок.** Миграция `0022`
завела проверку прав на запись для табеля и расчёта и там же записала, чего она
намеренно не делает: `directory.manage`, `rules.manage`, `roles.manage` не
проверяются, потому что экранов нет, а «политика, стоящая там, где никто не
пишет, — это не защита, а догадка о будущем устройстве». `0130` закрыла
справочники, когда появилась админка. Теперь появился экран правил — платим за
`rules.manage` той же парой «политика в базе + внятный отказ словами»
(`web/permissions.py`).

**Что было до этой миграции.** На `rule_overrides` стоит только изоляция
тенанта из `0004_rls` — `for all` с проверкой `tenant_id in (select
app_tenant_ids())`. То есть **любой** член тенанта мог завести переопределение
правила: управляющий точки — сменить процент оплаты больничного, бухгалтер —
переключить меру работы курьеров. Никто этого не делал ровно по той же причине,
что и со справочниками: не было экрана. Дыра тем не менее шире справочной —
переопределение правила меняет деньги сразу всем, кого правило касается.

**Почему только `rule_overrides` и почему без `rule_presets`.** Тело пресета
страны — общий справочник без `tenant_id` (`0004_rls`, `SHARED_TABLES`), и
`app_has_permission(tenant_id, …)` к нему не приставить. Но и права ему не
нужно: продукт его не правит вовсе. Экран правит слой партнёра, а страна
приезжает первичной загрузкой ролью владельца схемы (`manage.py load_presets`).
Заводить сюда «право править пресет страны» значило бы разрешить одному
партнёру менять расчёт другому — см. `web/rules.py`, правило первое.

Форма политик — как в `0022` и `0130`: `as restrictive`, только на запись,
чтение не трогаем. Право вести правила — не право их видеть: след расчёта
называет правило и его версию любому, кто вправе видеть саму сумму (D025), и
запрет на чтение оторвал бы объяснение от объясняемого.

**`using` на `update` намеренно нет** — по тому же доводу, что в `0022`:
политика ограничивающая, она сужает уже разрешённое, и `using` превратил бы
громкий отказ в тихое «изменено 0 строк».

**Чего эта миграция по-прежнему не делает.** `roles.manage` не проверяет:
экрана ролей нет, и заводить политику под ненаписанный экран — та же догадка,
против которой возражала `0022`. Долг остаётся записанным здесь, а не забытым.
"""
from django.db import migrations

POLICIES = """
create policy rules_manage_insert on rule_overrides
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'rules.manage'));

create policy rules_manage_update on rule_overrides
    as restrictive for update
    with check (app_has_permission(tenant_id, 'rules.manage'));

create policy rules_manage_delete on rule_overrides
    as restrictive for delete
    using (app_has_permission(tenant_id, 'rules.manage'));
"""

DROP_POLICIES = """
drop policy if exists rules_manage_insert on rule_overrides;
drop policy if exists rules_manage_update on rule_overrides;
drop policy if exists rules_manage_delete on rule_overrides;
"""


class Migration(migrations.Migration):

    # Зависимость от текущего листа основной ветки, а не от того, что был листом
    # в начале работы: ветки сводит диспетчер, и две миграции от одного родителя
    # дали бы две головы, между которыми `migrate` на чистой базе отказывается
    # выбирать.
    dependencies = [
        ("core", "0150_payslip_steps"),
    ]

    operations = [
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
