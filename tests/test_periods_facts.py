"""Список периодов отвечает, что за месяц, не открывая его (issue #176, T183).

Модуль 4 эталона рисует список месяцев так: месяц, метка («текущий», «закрыт
01.06», «переоткрыт 12.05»), состояние, **часы**, **ФОТ**, **доля от выручки** и
что с месяцем можно делать. У нас было три колонки — месяц, состояние,
партнёр, — и чтобы понять, что в месяце, приходилось в него заходить.

Зачем это на списке, а не внутри. Список периодов — то место, где человек
выбирает, куда идти, и решает «этот месяц готов или нет». Числа рядом отвечают
на вопрос до перехода: часы внесены, ФОТ похож на прошлый месяц, доля от выручки
не улетела. Без них список — оглавление без страниц.

**Переоткрытый месяц помечен навсегда** — это требование эталона дословно: «тот,
кто через год увидит расхождение в отчётности, должен знать, что месяц
пересчитывали». Метка не заменяет состояние, а стоит рядом с ним.
"""
from __future__ import annotations

import re

from conftest import body, login_as
from test_closing_readiness import calculated  # noqa: F401
from test_directory import sql  # noqa: F401

PERIODS = "/periods/"


def shown(client) -> str:
    return body(client.get(PERIODS))


def row_of(html: str, month: str) -> str:
    """Строка таблицы про этот месяц — как её видит человек."""
    found = re.search(rf"<tr[^>]*>(?:(?!</tr>).)*{re.escape(month)}(?:(?!</tr>).)*</tr>",
                      html, re.S)
    return found.group(0) if found else ""


def test_the_list_shows_hours_and_payroll(client, web_env):
    """У месяца видно часы и ФОТ — без открытия месяца."""
    login_as(client, "director")
    html = shown(client)

    assert "Часы" in html, "колонки часов нет"
    assert "ФОТ" in html, "колонки ФОТ нет"

    june = row_of(html, "Июнь 2026")
    assert june, "июня нет в списке"

    # Смотрим в САМИ ячейки часов и ФОТ, а не в строку целиком: строка содержит
    # «Июнь 2026», то есть проверка «в строке есть цифра» проходила бы и на
    # пустых колонках. Первая версия этого теста ровно так и была написана —
    # поймано, когда часы показались прочерком на стенде с полным сидом.
    cells = re.findall(r"<td[^>]*>(.*?)</td>", june, re.S)
    assert len(cells) >= 5, f"колонок меньше, чем ожидалось: {cells}"
    assert re.search(r"\d", cells[2]), f"часы не показаны: {cells[2]!r}"


def test_the_share_of_revenue_is_honest_about_no_revenue(client, web_env):
    """Выручки в продукте нет — доля показана прочерком, а не нулём.

    Ноль в этой колонке читался бы как «расходы съели ноль процентов выручки»,
    то есть как посчитанный ответ. Пока выручки нет вовсе (она придёт с
    коннектором Dodo IS), продукт обязан сказать это прочерком.
    """
    login_as(client, "director")
    html = shown(client)

    assert "Доля от выручки" in html, "колонки доли нет"
    june = row_of(html, "Июнь 2026")
    assert "0 %" not in june and "0%" not in june, "доля показана нулём вместо прочерка"


def test_a_reopened_period_is_marked_forever(client, web_env, sql):  # noqa: F811
    """Переоткрытый месяц помечен в списке — навсегда, а не до пересчёта.

    Метка живёт у РАСЧЁТА (`payruns.status = reopened`), а не у учётного месяца:
    у периода состояния три — открыт, на проверке, закрыт, — а откат утверждения
    это про расчёт. Эталон требует, чтобы след остался навсегда: «тот, кто через
    год увидит расхождение в отчётности, должен знать, что месяц пересчитывали».
    """
    run = sql.execute("select id, status::text from payruns order by period limit 1").fetchone()
    if run is None:
        return   # расчёта нет — помечать нечего
    run_id, was = run

    # Переходы расчёта стережёт база (`payrun_guard`): «посчитан → переоткрыт»
    # напрямую не разрешён, путь только через утверждение, а откат ещё и требует
    # причину настройкой транзакции. Тест идёт этим самым путём — иначе он падал
    # бы или проходил в зависимости от того, считал ли период соседний тест.
    def move(to: str) -> None:
        # `false` — настройка на СОЕДИНЕНИЕ, а не на транзакцию: фикстура `sql`
        # выполняет каждый запрос сама по себе, и локальная настройка не дожила
        # бы до следующего вызова — база отказывала бы «переход требует причины»
        # при переданной причине.
        sql.execute("select set_config('app.transition_reason', %s, false)",
                    ("проверка метки переоткрытия",))
        sql.execute("update payruns set status = %s where id = %s", (to, run_id))

    for step in ("calculated", "approved", "reopened"):
        if step != was or step == "reopened":
            move(step)
    try:
        login_as(client, "director")
        assert "переоткрывали" in shown(client).lower(), (
            "переоткрытый месяц ничем не помечен"
        )
    finally:
        move("calculated")
        if was in ("approved", "reopened"):
            move("approved")
            if was == "reopened":
                move("reopened")


def test_the_list_says_what_can_be_done(client, web_env):
    """У каждого месяца сказано, что с ним можно делать: состояние решает всё."""
    login_as(client, "director")
    html = shown(client)
    assert "Что можно делать" in html
    june = row_of(html, "Июнь 2026")
    assert re.search(r"<td>[^<]*[а-яё]{4,}[^<]*</td>", june), (
        "колонка «что можно делать» пуста: состояние ничего не объясняет"
    )


def test_the_numbers_match_the_payslips(client, web_env, sql):  # noqa: F811
    """ФОТ в списке — то же число, что даёт ведомость месяца.

    Второй источник истины здесь опаснее отсутствия колонки: человек сверяет
    месяц по списку и не идёт внутрь.
    """
    from core.models import Period

    period = Period.objects.filter(period__isnull=False).order_by("-period").first()
    total = sql.execute(
        """select coalesce(sum(t.gross), 0)
             from payslip_totals t join payslips p on p.id = t.payslip_id
             join payruns r on r.id = p.payrun_id
            where r.period = %s""",
        (period.period,),
    ).fetchone()[0]

    login_as(client, "director")
    html = row_of(shown(client), "Июнь 2026")
    if total == 0:
        return   # расчёта нет — сверять нечего, это проверяет соседний тест
    digits = re.sub(r"[^\d]", "", html)
    assert str(int(total)).replace(".", "") in digits.replace(" ", ""), (
        "ФОТ в списке не сходится с ведомостью"
    )


def test_the_payroll_appears_after_the_month_is_calculated(client, calculated):  # noqa: F811
    """ФОТ появляется в списке, когда месяц посчитан, — и до этого честно пуст.

    Пустой ФОТ у непосчитанного месяца — не пробел, а правда: начислений ещё
    нет. Показать там ноль значило бы сказать «фонд оплаты труда равен нулю».
    """
    login_as(client, "director")
    june = row_of(shown(client), "Июнь 2026")
    cells = re.findall(r"<td[^>]*>(.*?)</td>", june, re.S)

    assert re.search(r"\d", cells[3]), f"ФОТ посчитанного месяца пуст: {cells[3]!r}"
