"""Выплаты по человеку: месяцы видны, скрытые суммы не восстанавливаются (T166).

Экран отвечает на «сколько этот человек получал по месяцам». Он весь состоит из
итогов, а итог — самое опасное, что можно показать в продукте с несколькими
регистрами учёта: сумма, собранная из строк, которых смотрящему не отдали, не
выглядит утечкой. Её не видно глазами, её нельзя заметить на снимке экрана, и
поймать её можно только проверкой вида «итог роли равен сумме тех строк, которые
база этой роли отдала».

Отсюда устройство файла — три группы проверок, и каждая ловит свой класс дефектов.

**1. Парная проверка среза.** Роль с полным набором регистров видит одну сумму,
роль с урезанным — другую, и вторая **меньше**. Одной проверки «урезанная роль
что-то видит» недостаточно: она зелёная и когда срез не работает вовсе. Роль
сужается тем же способом, которым это сделает партнёр через экран ролей
(`narrowed_ledgers`): после D036 роли с неполным набором и правом считать период
в сиде нет, и условие приходится создавать явно.

**2. Экранное число сверяется с базой.** Итог со страницы сравнивается с суммой,
которую та же роль получает **прямым запросом под ролью `app_user`**. Владелец
таблиц политики обходит, а суперпользователь обходит даже `force row level
security`, поэтому проверка доступа обязана переключиться на роль приложения —
именно так дефект видимости регистров однажды прожил незамеченным.

**3. Месяц не появляется из табеля.** Табель регистром не режется — часы не
свойство регистра. Значит, список месяцев, собранный по табелю, назвал бы и
месяц, в котором у человека были только скрытые выплаты. Само появление такого
месяца — сообщение «здесь есть деньги, которых тебе не видно», то есть утечка на
один бит. Проверяется августом: в нём есть табель и есть выплата только во
внутреннем регистре.

Отдельно — граница экрана: он про данные расчёта, а не про справочник, и
управляющему точки не открыт ни ссылкой, ни адресом. Это не косметика: карточку
того же человека он читает законно (T173, ставок в Dodo IS нет вовсе), а сумм
расчёта не видит вовсе — прямое требование к роли.
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal

import psycopg
import pytest

from conftest import (
    JULY,
    JUNE,
    T1,
    as_app_user,
    body,
    login_as,
    narrowed_ledgers,
    wipe_payruns,
)

AUGUST = "2026-08-01"

# Человек этого файла. Условий найма ему намеренно не заводят: они меняли бы
# число сотрудников и версий, на которые смотрят соседние модули, а экрану они
# не нужны — он живёт на строках ведомости.
PROBE = "T166-PAY-HISTORY"

# Суммы выбраны так, чтобы каждую можно было искать в разметке подстрокой и не
# спутать ни с другой суммой этого файла, ни с частью другой. Это не
# придирчивость: проверка «скрытого числа на странице нет» обесценивается, если
# число случайно встречается внутри показанного.
JUNE_OFFICIAL = Decimal("10101.00")
JUNE_INTERNAL = Decimal("3003.00")
JULY_OFFICIAL = Decimal("12012.00")
AUGUST_INTERNAL = Decimal("707.00")

JUNE_WHOLE = JUNE_OFFICIAL + JUNE_INTERNAL          # 13 104,00 — вся строка июня
WHOLE_HISTORY = JUNE_WHOLE + JULY_OFFICIAL + AUGUST_INTERNAL   # 25 823,00
OFFICIAL_HISTORY = JUNE_OFFICIAL + JULY_OFFICIAL               # 22 113,00

# Производные величины: посчитаны по всем регистрам сразу и живут в
# `payslip_totals` под своей политикой. Она решает вопрос по РОЛИ, а не по
# строке (T071, миграция `0023`): полному набору регистров видны итоги обоих
# месяцев, урезанному — ни одного, включая целиком официальный июль. Оба числа
# нужны именно поэтому: одним не отличить «политика по роли» от «политика по
# строке».
JUNE_GROSS = Decimal("17000.00")
JULY_GROSS = Decimal("15015.00")
# Наличная часть июня — в двух местах и **разными числами**, намеренно. В итогах
# строки она стоит своя, в табеле (`timesheets.cash_payout`, откуда расчёт её и
# берёт) — другая. Так проверяется источник, а не совпадение: одинаковые числа
# зеленели бы при любом из двух, а табель регистром не режется, и взять канал
# выплаты оттуда — прямая утечка для роли с урезанным набором регистров.
JUNE_TO_CASH = Decimal("3000.00")
JUNE_CASH_IN_TIMESHEET = Decimal("4004.00")

JUNE_HOURS = Decimal("184.00")
AUGUST_HOURS = Decimal("99.00")


def shown(value: Decimal) -> str:
    from web.format import money

    return money(value)


@pytest.fixture
def sql(web_env):
    """Прямое соединение владельцем — только чтобы подготовить и убрать данные.

    Проверок доступа этим соединением нет ни одной: на владельца политики не
    действуют, и зелёный результат ничего не значил бы.
    """
    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def rls(web_env):
    """Соединение для проверок доступа: своя транзакция, роль `app_user`.

    Отдельно от `sql`, и это не удобство. `set local role` действует до конца
    транзакции, а в `autocommit` каждый оператор — своя транзакция: переключение
    роли не пережило бы следующей строки, и проверка молча шла бы владельцем
    схемы, для которого политик не существует.
    """
    with psycopg.connect(web_env) as conn:  # autocommit выключен — транзакция есть
        yield conn
        conn.rollback()


@pytest.fixture
def person(web_env, sql):
    """Человек с историей: смешанный июнь, официальный июль, скрытый август.

    Кладётся мимо ORM и мимо политик (владельцем схемы): здесь проверяется показ
    и срез, а не расчёт, и суммы должны быть узнаваемыми в разметке.

    Расчёты сносятся перед подготовкой: в той же базе работают модули, которые
    считают период, и без этого итоги зависели бы от порядка файлов.
    """
    wipe_payruns(web_env)
    tenant = sql.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
    unit = sql.execute(
        "select id from units where tenant_id = %s and code = 'NS1'", (tenant,)
    ).fetchone()[0]
    employee = sql.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Проба', 'Историев') returning id""",
        (tenant, PROBE),
    ).fetchone()[0]

    def month(period, parts, totals, hours=None, cash=Decimal(0)):
        payrun = sql.execute(
            """insert into payruns (tenant_id, period) values (%s, %s)
               on conflict (tenant_id, period) do update set period = excluded.period
               returning id""",
            (tenant, period),
        ).fetchone()[0]
        payslip = sql.execute(
            """insert into payslips (tenant_id, payrun_id, employee_id, unit_id)
               values (%s, %s, %s, %s) returning id""",
            (tenant, payrun, employee, unit),
        ).fetchone()[0]
        for ledger, amount in parts:
            sql.execute(
                """insert into pay_components
                       (tenant_id, payslip_id, code, title, amount, ledger)
                   values (%s, %s, 'hours.regular', 'Часы', %s, %s)""",
                (tenant, payslip, amount, ledger),
            )
        # Итоги пишутся ПОСЛЕ компонентов: набор регистров строки наполняет
        # триггер по компонентам (миграция `0009`), и порядок наоборот оставил бы
        # `payslips.ledgers` пустым. Сегодняшняя политика видимости итогов на
        # этот набор уже не смотрит (`0023`), но данные подготовки обязаны быть
        # такими же, какими их делает расчёт: проверка, стоящая на кривом
        # материале, однажды позеленеет не по своей причине.
        net, gross, to_cash = totals
        sql.execute(
            """insert into payslip_totals
                   (payslip_id, tenant_id, net, gross, tax, contributions,
                    total_cost, to_bank, to_cash)
               values (%s, %s, %s, %s, 900.00, 1900.00, %s, %s, %s)""",
            (payslip, tenant, net, gross, gross, net - to_cash, to_cash),
        )
        if hours is not None:
            sql.execute(
                """insert into timesheets
                       (tenant_id, employee_id, unit_id, period, hours,
                        norm_hours, cash_payout)
                   values (%s, %s, %s, %s, %s, 176.00, %s)""",
                (tenant, employee, unit, period, f'{{"regular": {hours}}}', cash),
            )
        return payslip

    month(
        JUNE, (("official", JUNE_OFFICIAL), ("internal", JUNE_INTERNAL)),
        (JUNE_WHOLE, JUNE_GROSS, JUNE_TO_CASH),
        hours=JUNE_HOURS, cash=JUNE_CASH_IN_TIMESHEET,
    )
    month(
        JULY, (("official", JULY_OFFICIAL),),
        (JULY_OFFICIAL, JULY_GROSS, Decimal(0)),
    )
    # Август — месяц, которого урезанной роли быть не должно вовсе: выплата в нём
    # только внутренняя, а табель есть. Именно на нём ловится «список месяцев
    # собран по табелю».
    month(
        AUGUST, (("internal", AUGUST_INTERNAL),),
        (AUGUST_INTERNAL, Decimal("900.00"), Decimal(0)), hours=AUGUST_HOURS,
    )

    try:
        yield str(employee)
    finally:
        # Уборка обязательна и обязана идти даже после падения: база живёт весь
        # прогон, и оставленный человек с табелем сдвинул бы контрольные суммы
        # у всех, кто считает период после нас.
        sql.execute("delete from timesheets where employee_id = %s", (employee,))
        sql.execute(
            "delete from payslip_totals where payslip_id in"
            " (select id from payslips where employee_id = %s)", (employee,)
        )
        sql.execute(
            "delete from pay_components where payslip_id in"
            " (select id from payslips where employee_id = %s)", (employee,)
        )
        sql.execute("delete from payslips where employee_id = %s", (employee,))
        sql.execute("delete from employees where id = %s", (employee,))
        wipe_payruns(web_env)


def page(client, person: str) -> str:
    answer = client.get(f"/directory/employees/{person}/pay/")
    assert answer.status_code == 200, f"{answer.status_code}: {body(answer)[:400]}"
    return body(answer)


def user_id(sql, role: str) -> str:
    row = sql.execute(
        "select u.id from users u join memberships m on m.user_id = u.id "
        "join roles r on r.id = m.role_id where r.code = %s", (role,),
    ).fetchone()
    assert row, f"в сиде нет роли {role}"
    return str(row[0])


def visible_sum(rls, who: str, person: str) -> Decimal:
    """Сколько по этому человеку отдаёт база **этой роли**, ролью `app_user`.

    Запрос ровно тот, из которого экран собирает итог (`payrun.person`): ни
    одного условия о регистре и о точке — их ставят политики. Фильтр,
    дописанный сюда, превратил бы проверку в сверку кода с самим собой.
    """
    with as_app_user(rls, who) as conn:
        return Decimal(conn.execute(
            "select coalesce(sum(pc.amount), 0) from pay_components pc"
            " join payslips p on p.id = pc.payslip_id where p.employee_id = %s",
            (person,),
        ).fetchone()[0])


# --- 1. парная проверка среза -------------------------------------------------


def test_the_whole_history_is_visible_to_the_one_who_sees_every_ledger(client, person):
    """Полный набор регистров — все три месяца и итог по ним."""
    login_as(client, "accountant")
    text = page(client, person)
    client.post("/logout/")

    for amount in (JUNE_OFFICIAL, JUNE_INTERNAL, JULY_OFFICIAL, AUGUST_INTERNAL):
        assert shown(amount) in text, f"не показана видимая сумма {shown(amount)}"
    assert shown(WHOLE_HISTORY) in text, "нет итога за всё время"
    assert "Август 2026" in text, "месяц внутреннего регистра пропал у того, кому он открыт"


def test_a_role_with_narrowed_ledgers_gets_a_smaller_total_and_no_trace_of_the_rest(
    client, person, web_env,
):
    """Главная проверка задачи: итог урезанной роли меньше, и остатка не видно.

    Проверяется не «что-то видно», а обе половины сразу: официальные суммы
    показаны, а от внутренних не осталось ни строки, ни вклада в итог, ни
    целого месяца. Без первой половины проверка зеленела бы на сломанном
    экране, который не показывает ничего; без второй — на срезе, которого нет.
    """
    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        text = page(client, person)
        client.post("/logout/")

    assert shown(JUNE_OFFICIAL) in text, "официальную часть июня урезанной роли не показали"
    assert shown(JULY_OFFICIAL) in text, "официальный июль урезанной роли не показали"
    assert shown(OFFICIAL_HISTORY) in text, "итог по видимому срезу не показан"

    for hidden, what in (
        (JUNE_INTERNAL, "сумма внутреннего регистра"),
        (JUNE_WHOLE, "итог смешанного месяца целиком"),
        (AUGUST_INTERNAL, "выплата месяца, целиком скрытого от роли"),
        (WHOLE_HISTORY, "итог по всем регистрам"),
    ):
        assert shown(hidden) not in text, f"на странице {what}: {shown(hidden)}"
    assert "Август 2026" not in text, (
        "назван месяц, в котором у человека только скрытые от роли выплаты"
    )
    assert "Внутренний" not in text, "названо имя регистра, которого роли не видно"


def test_derived_numbers_are_shown_only_to_a_role_that_sees_every_ledger(
    client, person, web_env,
):
    """Бруто, налог и взносы — по роли, а не по строке (T071, миграция `0023`).

    Этот экран — первый читатель `payslip_totals` в продукте, и политика у неё
    устроена жёстче, чем «все регистры этой строки»: итоги видит тот, кому видны
    **все регистры вообще**, независимо от содержимого строки. Так сделано
    намеренно — пока видимость производной строки зависела от её содержимого,
    отсутствие числа называло людей поимённо.

    Отсюда и проверка: у полного набора регистров итоги есть у всех месяцев, у
    урезанного — ни у одного, включая целиком официальный июль. Прочерк вместо
    них обязан быть на экране: молчание читалось бы как «налогов не было».
    """
    from web.format import EMPTY

    login_as(client, "accountant")
    full = page(client, person)
    client.post("/logout/")
    assert shown(JUNE_GROSS) in full, "итогов смешанного месяца нет у того, кому видно всё"
    assert shown(JULY_GROSS) in full, "итогов официального месяца нет у того, кому видно всё"

    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        narrow = page(client, person)
        client.post("/logout/")

    assert shown(JUNE_GROSS) not in narrow, "итоги смешанного месяца показаны урезанной роли"
    assert shown(JULY_GROSS) not in narrow, (
        "итоги показаны по одной строке: видимость итогов обязана быть свойством "
        "роли, иначе отсутствие числа называет людей (T071)"
    )
    assert EMPTY in narrow, "на месте закрытых итогов нет прочерка"


def test_the_payout_channel_comes_from_the_row_totals_and_not_from_the_timesheet(
    client, person, web_env,
):
    """Наличная часть — из итогов строки, а не из табеля. Проверяется источник.

    Табель регистром не режется: часы не свойство регистра, и политики на нём
    только по точке. Поэтому `timesheets.cash_payout` — готовое число,
    посчитанное по всему человеку, и показать его значило бы отдать урезанной
    роли ровно то, что от неё закрыто.

    В подготовке эти два числа **разные**, и проверяется именно это: показано
    число итогов, а число табеля не показано ни разу. При одинаковых числах
    проверка была бы зелёной при любом из двух источников — то есть не
    проверяла бы ничего. Именно в такую пустую проверку эта и превратилась
    сначала: у роли с урезанным набором итогов нет вовсе (T071), и «числа нет
    на экране» доказывало только это.
    """
    login_as(client, "accountant")
    full = page(client, person)
    client.post("/logout/")
    assert shown(JUNE_TO_CASH) in full, "наличная часть из итогов строки не показана"
    assert shown(JUNE_CASH_IN_TIMESHEET) not in full, (
        "канал выплаты взят из табеля — числа, посчитанного по всем регистрам сразу"
    )

    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        narrow = page(client, person)
        client.post("/logout/")
    for hidden in (JUNE_TO_CASH, JUNE_CASH_IN_TIMESHEET):
        assert shown(hidden) not in narrow, (
            f"наличная часть показана роли, которой итоги не отданы: {shown(hidden)}"
        )


# --- 2. экранное число сверяется с базой --------------------------------------


def test_the_total_on_the_screen_equals_what_the_database_gives_that_role(
    client, person, sql, rls, web_env,
):
    """Итог экрана равен сумме строк, которые база отдаёт этой роли. Обеим ролям.

    Это и есть инвариант задачи, проверенный не глазами. Сумма берётся ролью
    `app_user` — на владельца схемы политики не действуют, и сверка с ним
    доказывала бы только то, что арифметика в Python работает.
    """
    who = user_id(sql, "accountant")

    login_as(client, "accountant")
    full_page = page(client, person)
    client.post("/logout/")
    full_db = visible_sum(rls, who, person)
    assert shown(full_db) in full_page, f"итог экрана не сходится с базой: {full_db}"
    assert full_db == WHOLE_HISTORY, "подготовка данных разъехалась с проверкой"

    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        narrow_page = page(client, person)
        client.post("/logout/")
        narrow_db = visible_sum(rls, who, person)

    assert shown(narrow_db) in narrow_page, f"итог экрана не сходится с базой: {narrow_db}"
    assert narrow_db < full_db, (
        "урезанной роли база отдала столько же — сужение регистров не сработало, "
        "и проверка среза выше ничего не доказывает"
    )


def test_no_number_on_the_narrowed_screen_comes_from_outside_the_visible_rows(
    client, person, sql, rls, web_env,
):
    """Ни одного числа мимо базы — и ищутся они по всей странице, а не по ячейкам.

    Проверка на весь экран, а не на перечисленные заранее суммы: столбец с чужим
    числом дописывают как раз мимо списка ожидаемого.

    Числа ищутся **во всём тексте страницы**, а не только в числовых ячейках, и
    это не запас прочности. Первая версия смотрела на ячейки с классом `num` — и
    не увидела бы наличную часть, которая стоит подписью под суммой ВНУТРИ такой
    ячейки, то есть ровно тот вид утечки, от которого написан соседний тест.
    Утечка редко приходит колонкой; чаще подписью.

    Часы и норма табеля в разрешённых намеренно — они приходят из табеля,
    регистром не режутся и к регистрам отношения не имеют: часы не свойство
    регистра.
    """
    who = user_id(sql, "accountant")
    with narrowed_ledgers(web_env, "accountant", ["official"]):
        with as_app_user(rls, who) as conn:
            amounts = {
                Decimal(row[0]) for row in conn.execute(
                    "select pc.amount from pay_components pc"
                    " join payslips p on p.id = pc.payslip_id where p.employee_id = %s",
                    (person,),
                ).fetchall()
            }
            totals = {
                Decimal(value)
                for row in conn.execute(
                    "select t.net, t.gross, t.tax, t.contributions, t.to_bank, t.to_cash"
                    " from payslip_totals t join payslips p on p.id = t.payslip_id"
                    " where p.employee_id = %s", (person,),
                ).fetchall()
                for value in row
            }
        login_as(client, "accountant")
        text = page(client, person)
        client.post("/logout/")

    allowed = {shown(value) for value in amounts | totals}
    # Сумма видимых строк и среднее по ним — производные того же множества, а не
    # новые данные: они обязаны быть на экране, и их надо разрешить поимённо.
    allowed |= {shown(OFFICIAL_HISTORY), shown(OFFICIAL_HISTORY / 2)}
    # Часы и норма табеля — единственное на экране, что деньгами не является и
    # регистром не режется.
    allowed |= {shown(JUNE_HOURS), "176,00"}

    # Всё, что выглядит как сумма продукта: цифры, пробелы-разделители тысяч и
    # два знака после запятой (`web.format.money` на русской странице).
    found = set(re.findall(r"\d[\d ]*,\d\d", text))
    extra = found - allowed
    assert extra == set(), f"на экране числа, которых база этой роли не давала: {extra}"
    # Проверка обязана что-то находить: пустой набор означал бы сломанный поиск,
    # а не чистый экран.
    assert shown(JUNE_OFFICIAL) in found, "поиск чисел на странице ничего не нашёл"


# --- 3. месяц не появляется из табеля -----------------------------------------


def test_a_month_hidden_by_the_ledger_does_not_come_back_through_the_timesheet(
    client, person, web_env,
):
    """У августа есть табель и только скрытая выплата — месяца быть не должно.

    Появившийся месяц с прочерком вместо суммы — не «пустая строка», а
    сообщение «здесь есть деньги, которых тебе не видно». Один бит, но это тот
    самый бит, ради которого регистры и разделены.
    """
    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        text = page(client, person)
        client.post("/logout/")

    assert "Август 2026" not in text, "месяц скрытых выплат вернулся на экран"
    assert shown(AUGUST_HOURS) not in text, "часы месяца скрытых выплат показаны"
    assert "Июнь 2026" in text and "Июль 2026" in text, "проверка проверяет пустоту"


def test_hours_of_a_visible_month_are_shown(client, person):
    """Обратная сторона: у видимого месяца часы обязаны быть.

    Иначе проверка выше зеленела бы на экране, который часов не показывает
    вовсе, — и «часов скрытого месяца нет» ничего бы не значило.
    """
    login_as(client, "accountant")
    text = page(client, person)
    client.post("/logout/")
    assert shown(JUNE_HOURS) in text, "часы видимого месяца пропали"


# --- экран говорит на языке страницы ------------------------------------------


@pytest.mark.parametrize("language", ["en", "sr-latn"])
def test_the_screen_leaves_no_russian_on_a_foreign_page(client, person, language):
    """Ни одного русского слова продукта на нерусской странице — с данными внутри.

    Почему это не дубль `tests/test_i18n_screens.py`. Тот обход **тоже** заходит
    сюда (адрес добавлен в его `screens`), но заходит на общей базе, где расчёта
    может не быть вовсе, — и видит пустое состояние, в котором ни таблиц, ни
    подписей под суммами нет. Проверено порчей: перевод, сделанный на импорте
    модуля, обход не покраснел. Здесь данные свои, поэтому страница рисуется
    целиком.

    Ловится этим ровно один класс дефектов, и он уже случился на этом экране:
    строка, собранная в Python **константой модуля**, переводится ОДИН раз при
    импорте — на языке, активном в тот момент, то есть навсегда по-русски.
    Каталог при этом полон и зелён, сравнивать не с чем, и увидеть это можно
    только на нарисованной странице (T017).

    Имя человека из проверки исключено намеренно: ФИО — данные партнёра, а не
    слова продукта, и переводить их значило бы решать за бухгалтера, как зовут
    его сотрудника (тот же довод, что у `data_terms` в обходе).
    """
    client.cookies["django_language"] = language
    login_as(client, "accountant")
    text = page(client, person)
    client.post("/logout/")
    client.cookies.pop("django_language", None)

    # Смотрится только содержимое экрана. Шапка продукта сюда не входит и не
    # должна: в ней стоят имя вошедшего, название партнёра и кнопка «Русский» —
    # данные и подпись, которые по-русски написаны законно. Стережёт шапку общий
    # обход (`tests/test_i18n_screens.py`), и удваивать его здесь значило бы
    # чинить в своём файле чужой экран.
    inside = re.search(r"<main\b.*?</main>", text, re.S)
    assert inside, "на странице нет содержимого — проверять нечего"
    # Комментарии шаблона человеку не видны, а русского в них много и по делу.
    visible = re.sub(r"<!--.*?-->", " ", inside.group(0), flags=re.S)
    for hidden in ("Проба", "Историев"):
        visible = visible.replace(hidden, " ")

    russian = sorted(set(re.findall(r"[а-яёА-ЯЁ]{3,}", visible)))
    assert russian == [], f"русские слова на странице языка {language}: {russian}"

    # И обратная сторона: страница действительно нарисовалась с данными, а не
    # оказалась пустой — иначе «русского нет» ничего не значит.
    assert shown(JUNE_OFFICIAL) in text, "страница пуста, проверять нечего"


# --- граница экрана: это расчёт, а не справочник ------------------------------


def test_the_unit_manager_is_refused_with_words(client, web_env, sql):
    """Управляющему точки экран не открыт — и отказ объясняет, почему.

    Человек берётся его собственный, из сида: на чужого ответ был бы 404, и
    проверка зеленела бы по другой причине. Отказ громкий (403 словами), а не
    пустая страница: исчезнувший без объяснения экран читается как поломка.
    """
    mine = sql.execute(
        "select id from employees where external_id = %s", ("JELENA PETROVIC",)
    ).fetchone()
    assert mine, "в сиде нет человека точки NS1 — проверка проверяет пустоту"

    login_as(client, "manager")
    answer = client.get(f"/directory/employees/{mine[0]}/pay/")
    client.post("/logout/")
    assert answer.status_code == 403, body(answer)[:400]
    assert "Расчёт периода" in body(answer), "отказ не назвал действия"


def test_the_network_admin_is_refused_too(client, web_env, sql, person):
    """Все три регистра — ещё не право на данные расчёта.

    У администратора сети регистры полные, а прав на расчёт нет ни одного, и
    это намеренно (`core.roles`). Проверка стоит рядом с проверкой управляющего
    потому, что ловит другую ошибку: «открыть тому, кому видно всё».
    """
    login_as(client, "admin")
    answer = client.get(f"/directory/employees/{person}/pay/")
    client.post("/logout/")
    assert answer.status_code == 403, body(answer)[:400]


def test_the_card_offers_the_screen_only_to_the_one_who_may_open_it(client, web_env, sql):
    """Ссылка есть у того, кто ведёт расчёт, и её нет у остальных.

    Ссылка на отказ хуже отсутствующей: человек уходит со своей страницы, чтобы
    прочитать запрет. А управляющему её быть не должно и по второй причине —
    на его экранах чтения не должно быть ни одной ссылки в расчёт (T173).
    """
    mine = sql.execute(
        "select id from employees where external_id = %s", ("JELENA PETROVIC",)
    ).fetchone()[0]
    card = f"/directory/employees/{mine}/"

    login_as(client, "manager")
    reader = body(client.get(card))
    client.post("/logout/")
    assert "/pay/" not in reader, "управляющему предложена ссылка в расчёт"
    assert "Выплаты по месяцам" not in reader

    login_as(client, "accountant")
    keeper = body(client.get(card))
    client.post("/logout/")
    assert f"{card}pay/" in keeper, "тому, кто ведёт расчёт, ссылки на выплаты нет"


def test_an_alien_person_is_not_found_rather_than_refused(client, web_env, sql):
    """Чужой человек — 404, а не 403: 403 сказал бы, что он существует.

    Право спрашивается ПОСЛЕ поиска человека, как на карточке (T173). Рядом
    контроль тем, кому адрес открыт: без него 404 доказывал бы только то, что
    ссылка битая.
    """
    alien = sql.execute(
        "select id from employees where external_id = %s", ("LENA VASIC",)
    ).fetchone()[0]

    login_as(client, "manager")
    assert client.get(f"/directory/employees/{alien}/pay/").status_code == 404
    client.post("/logout/")

    login_as(client, "accountant")
    assert client.get(f"/directory/employees/{alien}/pay/").status_code == 200
    client.post("/logout/")


def test_without_login_the_screen_sends_to_the_entrance(client, web_env, sql):
    person = sql.execute(
        "select id from employees where external_id = %s", ("JELENA PETROVIC",)
    ).fetchone()[0]
    answer = client.get(f"/directory/employees/{person}/pay/")
    assert answer.status_code == 302 and "/login/" in answer["Location"]


def test_a_person_without_payslips_is_told_why_the_screen_is_empty(client, web_env, sql):
    """Пустой экран объясняет следующий шаг, а не сообщает о пустоте."""
    wipe_payruns(web_env)
    person = sql.execute(
        "select id from employees where external_id = %s", ("JELENA PETROVIC",)
    ).fetchone()[0]

    login_as(client, "accountant")
    text = body(client.get(f"/directory/employees/{person}/pay/"))
    client.post("/logout/")
    assert "Выплат по этому человеку не видно" in text
    assert "после расчёта" in text, "пустое состояние не говорит, откуда возьмутся месяцы"


# --- запрет живёт в вызываемом, а не в вызывающем -------------------------------


def test_the_history_itself_refuses_a_role_without_the_right(web_env, sql):
    """`build_history` отказывает сама, без экрана.

    Почему это отдельная проверка, а не дубль проверки страницы. Разбор дифа
    свежим взглядом доказал прогоном: под ролью управляющего точки **база
    отдаёт** суммы официального регистра по его собственной точке. Политики
    режут по регистру и по точке, но не по праву вести расчёт — и это решение
    осознанное, его причина записана в миграции `0022`.

    Значит на этой оси база не подстрахует, и пока проверка стояла в
    представлении, запрет держался ровно одной строкой в одном месте. Второй
    вызов — выгрузка, API, фоновая задача — унаследовал бы данные, но не запрет.

    Проверка смотрит именно на функцию, а не на страницу: страница уже покрыта
    отдельно (`test_the_unit_manager_is_refused_with_words`), и её зелёный цвет
    ничего не говорит о том, безопасно ли звать функцию мимо неё.
    """
    from types import SimpleNamespace

    from payrun import person as history
    from web import permissions

    nobody = SimpleNamespace(
        tenant_id=T1, permissions=["timesheet.edit", "unit.close"],
        role_title="Управляющий точки", visible_ledgers=["official"],
    )
    with pytest.raises(permissions.PermissionRefused):
        history.build_history(T1, uuid.uuid4(), who=nobody)

    allowed = SimpleNamespace(
        tenant_id=T1, permissions=["payrun.calculate"],
        role_title="Бухгалтер", visible_ledgers=["official"],
    )
    # Тому, кому положено, функция отвечает историей, а не отказом: без этой
    # половины проверка была бы зелёной и у функции, которая отказывает всем.
    assert history.build_history(T1, uuid.uuid4(), who=allowed).months == []
