"""Журнал переходов сносит база, а не Django (найдено при закрытии T025).

**Что было сломано.** `seed_dev` падал на любой базе, где период хоть раз
утверждали:

    журнал переходов периода только пополняется: DELETE отклонён

Сид сносит расчёты через ORM, а Django исполняет каскад **в Python**: перед
удалением `payruns` он сам вычищает `payrun_transitions` отдельным оператором.
Для триггера `payrun_transitions_append_only` это прямое удаление истории, а не
каскад (`pg_trigger_depth()` равен единице), и он честно отказывает. То есть
после первого же утверждения базу разработки и демо нельзя было пересидировать.

**Почему `DO_NOTHING`, а не ослабление триггера.** Ослабить сторож значило бы
разрешить чистить историю из приложения — ровно то, ради запрета чего он
написан. Настоящий каскад в схеме уже есть: внешний ключ
`payrun_transitions_payrun_fk` поставлен руками в `0041` именно с
`on delete cascade`, и внутри удаления самого расчёта глубина триггеров больше
единицы, так что журнал уходит законно.

`DO_NOTHING` здесь читается не как «ничего не произойдёт», а как «Django в это
не вмешивается»: удалением занимается база. Схему эта миграция не меняет —
`db_constraint=False`, действие удаления Django в схему не пишет, — но состояние
моделей без неё разъедется с кодом, и следующий `makemigrations` сгенерировал бы
её сам в чужой ветке.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0042_payrun_approval")]

    operations = [
        migrations.AlterField(
            model_name="payruntransition",
            name="payrun",
            field=models.ForeignKey(
                db_column="payrun_id",
                db_constraint=False,
                on_delete=django.db.models.deletion.DO_NOTHING,
                to="core.payrun",
            ),
        ),
    ]
