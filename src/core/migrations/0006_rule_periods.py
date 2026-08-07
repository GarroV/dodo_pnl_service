"""
Непересечение периодов действия версионируемых правил (D015, T010).

Правила действуют «с даты по дату». Двух версий на одну дату быть не может:
расчёт молча возьмёт одну из них и посчитает месяц не тем правилом — ошибка,
которая не падает, а выдаёт неверное число. Postgres 18 умеет это стандартными
темпоральными ограничениями, мы на 17 — тот же инвариант даёт `EXCLUDE` поверх
`btree_gist` (расширение ставится миграцией `0001_types`).

Ключи и границы периода описаны у самих ограничений в `core/models.py`.
"""

import uuid

import django.contrib.postgres.constraints
import django.db.models.functions.comparison
from django.db import migrations, models

import core.models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_app_role'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='allocationrule',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(expressions=[('tenant', '='), ('counterparty', '='), ('ledger', '='), (core.models.DateRange('valid_from', 'valid_to', models.Value('[)')), '&&')], name='allocation_rules_no_overlap'),
        ),
        migrations.AddConstraint(
            model_name='employmentterm',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(expressions=[('tenant', '='), ('employee', '='), (core.models.DateRange('valid_from', 'valid_to', models.Value('[)')), '&&')], name='employment_terms_no_overlap'),
        ),
        migrations.AddConstraint(
            model_name='ruleoverride',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(expressions=[('tenant', '='), ('scope_type', '='), (django.db.models.functions.comparison.Coalesce('scope_id', models.Value(uuid.UUID('00000000-0000-0000-0000-000000000000')), output_field=models.UUIDField()), '='), ('path', '='), (core.models.DateRange('valid_from', 'valid_to', models.Value('[)')), '&&')], name='rule_overrides_no_overlap'),
        ),
        migrations.AddConstraint(
            model_name='rulepreset',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(expressions=[('code', '='), (core.models.DateRange('valid_from', 'valid_to', models.Value('[)')), '&&')], name='rule_presets_no_overlap'),
        ),
    ]
