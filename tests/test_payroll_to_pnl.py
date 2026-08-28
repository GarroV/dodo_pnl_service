"""Утверждённая ведомость попадает в P&L фактами (issue #201, T208).

Найдено при постройке отчёта: за посчитанный июнь в системе 35 ведомостей и
**ноль** строк в таблице фактов. Зарплата — самая большая статья расходов
партнёра, ради неё продукт и начинали, а в отчёт она не приходила вовсе:
цепочка «данные → отчёт» была разорвана ровно там, где всё уже посчитано.

Решения, принятые здесь, и почему именно такие.

**Переносим при утверждении, а не при расчёте.** До утверждения ведомость —
черновик: её пересчитывают по десять раз, и каждый пересчёт менял бы P&L. После
утверждения расчёт заморожен, и число в отчёте перестаёт «дышать».

**Строк P&L две — «Зарплата» и «Налоги с зарплаты».** Обе уже есть в
справочнике, выдумывать новые не нужно. Разделение важно для чтения отчёта:
партнёр смотрит на них по-разному — одно платится человеку, другое государству.

**Регистр наследуется от компонента.** Иначе P&L по официальному регистру
показал бы суммы дополнительного — то есть ровно то, что разграничение регистров
и запрещает.

**Точка — от ведомости.** У офисного персонала её нет, и такой факт становится
`pending`: его разнесёт правило (D055, T197), как и любой расход юрлица.

**Перенос идемпотентен.** Ключ выводится из ведомости, поэтому повторное
утверждение (после отката и нового) заменяет строки версией, а не удваивает
расход.
"""
from __future__ import annotations

from decimal import Decimal

from conftest import body, login_as
from test_closing_readiness import calculated  # noqa: F401
from test_directory import sql  # noqa: F401


def payroll_facts(sql) -> list:  # noqa: F811
    return sql.execute(
        """select i.code, f.ledger::text, f.amount, f.unit_id is not null, f.allocation::text
             from facts f join pnl_items i on i.id = f.pnl_item_id
            where f.dedup_key like 'payrun:%%' and f.superseded_at is null
            order by i.code, f.ledger"""
    ).fetchall()


def approve(client, url):
    return client.post(url + "approve/", {"postpone_blockers": "1"}, follow=True)


def test_approving_the_month_puts_the_payroll_into_the_facts(client, sql, calculated):  # noqa: F811
    """Утвердили месяц — зарплата появилась в фактах."""
    assert not payroll_facts(sql), "факты зарплаты были ещё до утверждения"

    login_as(client, "director")
    answer = approve(client, calculated)
    assert answer.status_code == 200, body(answer)[:400]

    rows = payroll_facts(sql)
    assert rows, "после утверждения зарплаты в фактах нет"
    assert {row[0] for row in rows} <= {"labour_cost", "payroll_taxes"}, (
        f"зарплата легла в неожиданные строки P&L: {rows}"
    )


def test_the_amount_matches_the_payslips(client, sql, calculated):  # noqa: F811
    """Сумма фактов сходится с ведомостью — до копейки.

    Это главная проверка задачи: расхождение здесь означает, что P&L и ведомость
    показывают разные деньги за один месяц, и который из двух прав, никто не
    скажет.
    """
    login_as(client, "director")
    approve(client, calculated)

    in_facts = sql.execute(
        """select coalesce(sum(-amount), 0) from facts
            where dedup_key like 'payrun:%%' and superseded_at is null"""
    ).fetchone()[0]
    in_payslips = sql.execute(
        """select coalesce(sum(t.total_cost), 0) from payslip_totals t
             join payslips p on p.id = t.payslip_id
             join payruns r on r.id = p.payrun_id
            where r.period = '2026-06-01'"""
    ).fetchone()[0]

    assert in_facts == in_payslips, (
        f"в P&L {in_facts}, в ведомостях {in_payslips} — деньги разошлись"
    )


def test_the_expense_is_negative(client, sql, calculated):  # noqa: F811
    """Зарплата — расход, и знак у неё расходный.

    Положительная сумма в строке расходов сложилась бы с выручкой, и результат
    месяца оказался бы завышен вдвое на величину ФОТ.
    """
    login_as(client, "director")
    approve(client, calculated)

    assert all(row[2] < 0 for row in payroll_facts(sql)), payroll_facts(sql)


def test_a_second_approval_does_not_double_the_payroll(client, sql, calculated):  # noqa: F811
    """Переоткрыли, утвердили заново — расход не удвоился."""
    login_as(client, "director")
    approve(client, calculated)
    first = sum((row[2] for row in payroll_facts(sql)), Decimal("0"))

    client.post(calculated + "reopen/", {"reason": "проверка идемпотентности"},
                follow=True)
    client.post(calculated + "calculate/", {"inline": "1"}, follow=True)
    approve(client, calculated)

    again = sum((row[2] for row in payroll_facts(sql)), Decimal("0"))
    assert again == first, f"после повторного утверждения стало {again} вместо {first}"


def test_the_ledger_comes_from_the_component(client, sql, calculated):  # noqa: F811
    """Регистр факта — тот же, что у компонента расчёта.

    Иначе P&L по официальному регистру показал бы суммы дополнительного — ровно
    то, что разграничение регистров запрещает.
    """
    login_as(client, "director")
    approve(client, calculated)

    ledgers = {row[1] for row in payroll_facts(sql)}
    in_components = set(sql.execute(
        """select distinct c.ledger::text from pay_components c
             join payslips p on p.id = c.payslip_id
             join payruns r on r.id = p.payrun_id
            where r.period = '2026-06-01'"""
    ).fetchall())
    assert ledgers <= {row[0] for row in in_components}, (
        f"в фактах регистры {ledgers}, в расчёте {in_components}"
    )


def test_the_report_shows_the_payroll_after_approval(client, sql, calculated):  # noqa: F811
    """И главное: в отчёте появилась зарплата — ради этого всё и делалось."""
    login_as(client, "director")
    approve(client, calculated)

    html = body(client.get(calculated + "pnl/"))
    assert "арплат" in html, "в отчёте P&L нет зарплаты"
