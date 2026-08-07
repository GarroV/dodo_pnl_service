"""
Выплата наличными и ручная корректировка в табеле (T051, issue #43).

Движок принимал `cash_payout` и `manual_correction` с самого начала, а колонок
для них не было: сид терял значения, `payslips.to_cash` всегда оставался нулём,
а вносить правку через интерфейс было некуда. Ошибка была молчаливая — расчёт
сходился сам с собой и расходился с таблицей бухгалтерии ровно там, где она
правит руками.

Правка хранится со следом (D025): без автора и внятной причины база её не
принимает. Комментарии к новым колонкам — в миграции `0008_correction_comments`.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_rule_periods'),
    ]

    operations = [
        migrations.AddField(
            model_name='timesheet',
            name='cash_payout',
            field=models.DecimalField(db_default=0, decimal_places=2, max_digits=14),
        ),
        migrations.AddField(
            model_name='timesheet',
            name='corrected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='timesheet',
            name='corrected_by',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='timesheet',
            name='correction_reason',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='timesheet',
            name='manual_correction',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddConstraint(
            model_name='timesheet',
            constraint=models.CheckConstraint(condition=models.Q(('manual_correction__isnull', True), models.Q(('corrected_by__isnull', False), ('correction_reason__isnull', False), ('correction_reason__regex', '\\S')), _connector='OR'), name='timesheets_correction_trace_check'),
        ),
    ]
