"""Управляющий точки читает своих сотрудников (T173, D047).

Что решает эта задача. Управляющему нужны имена, должности и ставки людей своей
точки. Ставок в Dodo IS нет вовсе — это условия найма, наши данные, — поэтому
проверить их можно только у нас. До T173 оба экрана сотрудников были закрыты
правом `directory.manage`, то есть управляющий получал отказ страницей.

Правильная форма решения — та, что уже принята в продукте для контрагентов:
**строки режет база, а право решает «правка или чтение», а не «видно или нет»**.
Отсюда три группы проверок, и каждая ловит свой класс дефектов.

**Срез делает база.** Проверяется дважды: настоящими запросами к продукту (клиент
Django ходит через `DbContextMiddleware`, то есть под ролью `app_user` — политики
действуют) и прямым `select` под той же ролью. Проверка «глазами на список» тут
не годится: пустая страница и отказ базы выглядят одинаково, а отличаются тем,
что первое можно случайно починить забытым фильтром в новом экране. Отдельно
проверяется **подмена адреса**: карточка чужого сотрудника обязана отвечать 404,
а не пустой страницей и не 403 — 403 сказал бы «такой есть, но не для вас», то
есть выдал бы ровно то, что скрывается (D023).

**Право решает форму, а не видимость.** Формы у читателя нет, но на её месте
стоит объяснение теми же словами, которыми ответит сам `POST`: пропавшая без слов
форма читается как поломка продукта (T072). И сам `POST` отвечает 403, ничего не
записав, — иначе отказ был бы косметикой.

**Чтение не расширяет доступ мимоходом.** Остальные справочники, у которых
такого читателя нет, управляющему по-прежнему отвечают отказом — все семь, а не
выборка; сквозной ключ (в Сербии это JMBG, национальный идентификатор) читателю
не показывается вовсе; на карточке и в списке нет ни одного числа, кроме ставки
и коэффициента, и ни одной ссылки в расчёт — сумм месяца здесь быть не должно.

Данные берутся из сида: точка `NS1` у управляющего, `BG1` и `NS2` — чужие.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import as_app_user, body, content, login_as

LIST = "/directory/employees/"

# Люди сида. Управляющий ограничен точкой NS1, поэтому «свой» и «чужие» берутся
# именно оттуда — по внешнему ключу, а не по имени: имя в разметке ещё надо
# найти, а ключ однозначен.
MINE = "JELENA PETROVIC"          # NS1, группа «Офис», официальный регистр
ANOTHER_UNIT = "LENA VASIC"       # BG1 — чужая точка
THIRD_UNIT = "PETAR ZIVKOVIC"     # NS2 — тоже чужая
HIDDEN_LEDGER = "dev-courier-2"   # NS1, но внутренний регистр: управляющему не виден


def person_id(sql, external_id: str) -> str:
    row = sql.execute(
        "select id from employees where external_id = %s", (external_id,)
    ).fetchone()
    assert row, f"в сиде нет сотрудника {external_id} — проверка проверяет пустоту"
    return str(row[0])


def card(sql, external_id: str) -> str:
    return f"{LIST}{person_id(sql, external_id)}/"


@pytest.fixture
def sql(web_env):
    """Прямое соединение владельцем — только чтобы найти и восстановить данные.

    Проверки доступа этим соединением не делаются: на владельца политики не
    действуют, и зелёный результат ничего не значил бы (см. `as_app_user`).
    """
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def rls(web_env):
    """Соединение для проверок доступа: своя транзакция, роль `app_user`.

    Отдельно от `sql`, и это не удобство. `set local role` действует до конца
    транзакции, а в режиме `autocommit` каждый оператор — своя транзакция:
    переключение роли не пережило бы даже следующей строки, и проверка молча шла
    бы владельцем схемы, для которого политик не существует.
    """
    import psycopg

    with psycopg.connect(web_env) as conn:  # autocommit выключен — транзакция есть
        yield conn
        conn.rollback()


def manager_id(sql) -> str:
    row = sql.execute(
        "select u.id from users u join memberships m on m.user_id = u.id "
        "join roles r on r.id = m.role_id where r.code = 'manager'"
    ).fetchone()
    assert row, "в сиде нет управляющего точки"
    return str(row[0])


# --- кто читает ---------------------------------------------------------------


@pytest.mark.parametrize("role", ["director", "accountant", "manager", "admin"])
def test_every_role_reads_the_employee_list(client, web_env, role):
    """Список открыт каждому, кто вошёл: ставки проверяют по нему, а не в Dodo IS."""
    login_as(client, role)
    answer = client.get(LIST)
    assert answer.status_code == 200, f"{role}: {answer.status_code}"
    assert "Сотрудники" in body(answer)
    client.post("/logout/")


def test_the_reader_is_offered_the_screen_in_the_navigation(client, web_env):
    """Пункт есть у того, у кого нет раздела справочников, — и один раз.

    Ссылка на экран, который откажет, — то же самое, что кнопка, которая
    откажет; обратное так же верно: экран, до которого нельзя дойти из шапки,
    для человека не существует.
    """
    login_as(client, "manager")
    page = body(client.get("/periods/"))
    assert f'href="{LIST}"' in page, "управляющему некуда нажать, чтобы дойти до людей"
    assert 'href="/directory/"' not in page, "управляющему обещан раздел справочников"
    client.post("/logout/")

    # У администратора сети раздел справочников есть, и второй вход в тот же
    # экран из той же шапки читался бы как два разных места.
    login_as(client, "admin")
    page = body(client.get("/periods/"))
    assert 'href="/directory/"' in page
    assert f'href="{LIST}"' not in page, "у администратора два входа в один экран"
    client.post("/logout/")


# --- срез делает база ---------------------------------------------------------


def test_the_manager_sees_only_people_of_his_own_unit(client, web_env):
    """Главное требование задачи, проверенное на настоящей странице."""
    login_as(client, "manager")
    page = body(client.get(LIST))
    assert "PETROVIC" in page, "своих людей управляющий не увидел вовсе"
    for alien in ("VASIC", "ZIVKOVIC", "Beograd"):
        assert alien not in page, f"управляющему показали чужое: {alien}"
    client.post("/logout/")


def test_a_person_of_another_unit_is_not_openable_by_address(client, web_env, sql):
    """Подмена адреса, а не отбор в списке: список — не защита.

    404, а не 403: «такой есть, но не для вас» выдало бы ровно то, что
    скрывается. Рядом — контроль на том же самом адресе администратором: без
    него 404 доказывал бы только то, что ссылка битая.
    """
    alien = card(sql, ANOTHER_UNIT)
    login_as(client, "manager")
    assert client.get(alien).status_code == 404
    # И правка вслепую по тому же адресу — тоже 404, а не отказ по праву:
    # иначе по коду ответа было бы видно, что человек существует.
    assert client.post(alien, {"what": "person", "last_name": "ПОДМЕНА",
                               "first_name": "X", "external_id": "x"}).status_code == 404
    client.post("/logout/")

    login_as(client, "admin")
    assert client.get(alien).status_code == 200, "адрес битый — проверка выше ничего не значит"
    client.post("/logout/")


def test_a_person_of_a_ledger_he_does_not_see_is_hidden_too(client, web_env, sql):
    """Своя точка ещё не значит «видно»: регистр режет отдельно (D023).

    Курьер сида работает на NS1, но во внутреннем регистре, а у управляющего его
    нет. Ни в списке, ни по прямому адресу.
    """
    login_as(client, "manager")
    page = body(client.get(LIST))
    assert "Курьеры" not in page, "названа группа регистра, которого роль не видит"
    assert client.get(card(sql, HIDDEN_LEDGER)).status_code == 404
    client.post("/logout/")


def test_the_cut_is_the_database_and_not_the_screen(rls, sql):
    """То же самое прямым запросом под ролью `app_user`.

    Экранные проверки выше зеленели бы и на фильтре, случайно написанном в
    представлении. Здесь ни одного нашего фильтра нет: только политики.
    """
    who = manager_id(sql)
    with as_app_user(rls, who) as conn:
        visible = {
            row[0] for row in conn.execute("select external_id from employees").fetchall()
        }
    assert MINE in visible, "управляющий не видит своих людей — политика режет лишнее"
    for alien in (ANOTHER_UNIT, THIRD_UNIT):
        assert alien not in visible, f"политика отдала чужого человека: {alien}"
    # Сквозной ключ чужого человека — это JMBG. Он не должен приходить ни в
    # каком запросе, а не только не показываться на экране.
    with as_app_user(rls, who) as conn:
        found = conn.execute(
            "select count(*) from employees where external_id = any(%s)",
            ([ANOTHER_UNIT, THIRD_UNIT],),
        ).fetchone()[0]
    assert found == 0


def test_the_alien_card_is_refused_by_the_database_and_not_by_the_view(rls, sql):
    """404 на чужой карточке приходит от базы, а не от нашего фильтра.

    Экранная проверка выше (`test_a_person_of_another_unit_is_not_openable_by_address`)
    зеленела бы и на фильтре по точке, случайно написанном в представлении. Такой
    фильтр — вторая копия правила «свой человек», и разъезжаются такие копии
    молча: политику потом правят, а забытый фильтр в коде продолжает показывать
    старую картину (или наоборот — политику ослабляют, а экран прикрывает дырку).

    Поэтому здесь берётся ровно тот запрос, которым продукт ищет карточку —
    `_employee_or_404`: `Employee.objects.filter(pk=employee_id)`, ни одного
    условия о точке, — и выполняется он под ролью `app_user` с контекстом
    управляющего. Своего человека база отдаёт, чужого не отдаёт вовсе: значит
    представлению просто нечего показать, и 404 стоит не на нашей вежливости.
    """
    who = manager_id(sql)
    mine, alien = person_id(sql, MINE), person_id(sql, ANOTHER_UNIT)
    with as_app_user(rls, who) as conn:
        found = {
            key: conn.execute(
                "select count(*) from employees where id = %s", (key,)
            ).fetchone()[0]
            for key in (mine, alien)
        }
    assert found[mine] == 1, "политика не отдаёт управляющему его собственного человека"
    assert found[alien] == 0, "политика отдала чужого человека по прямому ключу"


# --- право решает правку, а не видимость --------------------------------------


def test_the_reader_gets_facts_instead_of_forms_and_is_told_why(client, web_env, sql):
    """Формы нет — есть объяснение теми же словами, которыми ответит отказ."""
    mine = card(sql, MINE)
    login_as(client, "manager")
    page = body(client.get(mine))
    assert '<form class="card"' not in page, "читателю отдали форму правки"
    assert "Новая версия условий" not in page, "читателю предложено завести версию"
    assert "Ведение справочников" in page, (
        "форма пропала молча — это читается как поломка продукта, а не как запрет"
    )
    # Факты, ради которых экран и открыли: группа, точка, ставка.
    for expected in ("Ставка", "Группа", "Точка", "Офис", "NS1"):
        assert expected in page, f"на карточке нет главного: {expected}"
    client.post("/logout/")


def test_the_reader_sees_the_rate_and_nothing_else_numeric(client, web_env, sql):
    """Ставки видит, сумм расчёта — нет.

    Проверяется не «нет слова ведомость», а все числовые ячейки карточки: любая
    сумма расчёта, попавшая сюда, набрана тем же числовым классом и будет
    поймана. У человека из сида ставка 371 и коэффициент 1,135.

    Подписи столбцов (`th`) из проверки исключены: они тоже помечены числовым
    классом — заголовок обязан стоять над столбцом по тому же краю, — но
    содержат слово, а не значение.
    """
    login_as(client, "manager")
    page = body(client.get(card(sql, MINE)))
    numbers = {
        text.strip().replace(" ", " ").replace(",", ".")
        for text in re.findall(r'<(?:td|span) class="num[^"]*"[^>]*>([^<]*)<', page)
    }
    assert "371.00" in numbers, f"ставки на карточке нет: {numbers}"
    assert numbers <= {"371.00", "1.135", "—"}, (
        f"на карточке читателя лишние числа: {numbers}"
    )
    client.post("/logout/")


def test_the_list_shows_rates_and_no_other_numbers(client, web_env, sql, rls):
    """В списке одно число на человека — его ставка, и ничего кроме.

    Карточку стережёт проверка выше, но столбец с суммой месяца дописывают как
    раз в список: он один из всех экранов чтения похож на отчёт. Сверяется не
    «столбцов пять», а сами значения: каждое число списка обязано быть ставкой,
    которую этому управляющему показывает база. Столбец «за июнь начислено»
    такую проверку не пройдёт, как бы он ни назывался.
    """
    from web.format import EMPTY

    who = manager_id(sql)
    with as_app_user(rls, who) as conn:
        rates = {
            Decimal(row[0]) for row in conn.execute(
                "select base_rate from employment_terms"
            ).fetchall()
        }
    assert rates, "управляющему не видно ни одной ставки — проверка проверяет пустоту"

    login_as(client, "manager")
    page = body(client.get(LIST))
    client.post("/logout/")

    cells = re.findall(r'<td class="num[^"]*"[^>]*>([^<]*)<', page)
    assert cells, "в списке нет ни одной числовой ячейки"
    shown = []
    for text in cells:
        value = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
        if value in ("", EMPTY):
            continue
        shown.append(Decimal(value))
    assert shown, "все числа списка — прочерки: сверять нечего"
    extra = [str(value) for value in shown if value not in rates]
    assert not extra, f"в списке числа, которые базе не ставки: {extra}"


def test_neither_screen_offers_the_payroll_of_the_person(client, web_env, sql):
    """Ставки — да, ведомость и суммы расчёта — нет.

    Читателю открыли справочник, а не расчёт, и разница здесь не косметическая.
    Ставка — условие найма, наши данные (в Dodo IS их нет вовсе), и управляющий
    обязан их проверять. Итог месяца — другое: он режется регистром и правом, и
    расширить доступ мимоходом на этом экране дешевле всего, достаточно дописать
    на карточку строку «за июнь начислено» или ссылку «посмотреть ведомость».

    Проверяются оба экрана и оба способа проговориться — словом и ссылкой.
    Слова взяты настоящие, те, которыми расчёт назван в продукте (`period.html`,
    `trace.html`): проверка на выдуманное слово зеленела бы всегда.
    """
    login_as(client, "manager")
    # `content`, а не `body`: с D064 «Ведомость» стоит пунктом шапки на каждой
    # странице продукта, и проверка по целому документу ловила бы её вместо
    # утечки. Предмет проверки — что отдаёт САМ экран.
    pages = {
        LIST: content(client.get(LIST)),
        "карточка": content(client.get(card(sql, MINE))),
    }
    client.post("/logout/")

    for where, page in pages.items():
        for leak in ("Ведомость", "Итого", "/payslips/", "/timesheets/", "/trace/"):
            assert leak not in page, f"{where}: экран чтения отдаёт расчёт — {leak}"


def test_the_national_id_is_not_shown_to_the_reader(client, web_env, sql):
    """Сквозной ключ (JMBG) — данные администратора, а не точки.

    В сиде внешний ключ совпадает с именем, поэтому на время проверки ему
    ставится опознаваемое значение: иначе «ключа на странице нет» доказывало бы
    только то, что имени там нет.
    """
    probe = "JMBG-PROBE-0102030405060"
    person = person_id(sql, MINE)
    sql.execute("update employees set external_id = %s where id = %s", (probe, person))
    try:
        login_as(client, "manager")
        assert probe not in body(client.get(LIST)), "ключ показан в списке читателю"
        assert probe not in body(client.get(f"{LIST}{person}/")), "ключ показан на карточке"
        # И поиском по нему человека не подобрать: искать по значению, которого
        # на экране нет, — способ его узнать.
        assert "PETROVIC" not in body(client.get(LIST, {"q": probe}))
        client.post("/logout/")

        # Контроль: тому, кто ведёт справочник, ключ по-прежнему нужен и виден —
        # по нему сходится загрузка табеля.
        login_as(client, "admin")
        assert probe in body(client.get(LIST))
        assert "PETROVIC" in body(client.get(LIST, {"q": probe}))
        client.post("/logout/")
    finally:
        sql.execute("update employees set external_id = %s where id = %s", (MINE, person))


def test_a_post_from_the_reader_is_refused_and_changes_nothing(client, web_env, sql):
    """Отказ не косметический: 403 словами и ни одной изменённой строки."""
    person = person_id(sql, MINE)
    before = sql.execute(
        "select last_name from employees where id = %s", (person,)
    ).fetchone()[0]
    terms_before = sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (person,)
    ).fetchone()[0]

    login_as(client, "manager")
    answer = client.post(f"{LIST}{person}/", {
        "what": "person", "last_name": "ПОДМЕНА", "first_name": "X", "external_id": "x",
    })
    assert answer.status_code == 403, body(answer)
    assert "Ведение справочников" in body(answer)

    versions = client.post(f"{LIST}{person}/", {
        "what": "terms", "valid_from": "2026-09-01", "base_rate": "999", "coefficient": "1",
    })
    assert versions.status_code == 403
    client.post("/logout/")

    assert sql.execute(
        "select last_name from employees where id = %s", (person,)
    ).fetchone()[0] == before, "карточка изменилась при отказе"
    assert sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (person,)
    ).fetchone()[0] == terms_before, "завелась версия условий найма при отказе"


def test_the_one_who_manages_the_directory_still_edits(client, web_env, sql):
    """Обратная сторона: формы скрыты по праву, а не пропали у всех."""
    login_as(client, "admin")
    page = body(client.get(card(sql, MINE)))
    assert '<form class="card"' in page and "Новая версия условий" in page
    assert "Ведение справочников" not in page, "тому, у кого право есть, объясняют запрет"
    client.post("/logout/")


def test_reading_people_did_not_open_the_rest_of_the_admin(client, web_env):
    """Открыли один экран, а не справочники целиком.

    Здесь же живёт гарантия, которую раньше несла строка `/directory/employees/`
    в `DIRECTORY_URLS` (`tests/test_directory.py`): право `directory.manage`
    по-прежнему закрывает экран целиком там, где читателя, которому экран нужен
    для работы, нет. Перечислены все такие адреса, а не половина, — иначе
    «доступ расширился мимоходом» ловилось бы через один экран из трёх.

    Контрагентов в списке нет намеренно: они открыты на чтение той же границей
    (`/directory/counterparties/` отвечает управляющему 200) — с них эта форма
    решения и взята. Экран не наш, и стережёт его блок поставщиков.
    """
    login_as(client, "manager")
    for closed in ("/directory/", "/directory/groups/", "/directory/units/",
                   "/directory/legal-entities/", "/directory/calendar/",
                   "/directory/expense-items/", "/directory/tills/"):
        answer = client.get(closed)
        assert answer.status_code == 403, f"{closed}: {answer.status_code}"
    client.post("/logout/")
