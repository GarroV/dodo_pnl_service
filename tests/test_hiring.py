"""Приём на работу, увольнение и «как считается этот человек» (T164).

Три дыры одного узла, и проверки здесь разложены по ним же.

**Сотрудника нельзя было завести из интерфейса.** Так было решено (D029):
карточки приезжают загрузкой таблицы партнёра. Для стройки на тестовых данных
этого хватало, для передачи продукта партнёру — нет: человек выходит на работу
пятнадцатого числа, и до следующей загрузки таблицы его в системе **не
существует**. Проверяется поэтому не только «форма есть», а то, ради чего она
нужна: заводится человек ВМЕСТЕ с первой версией условий найма, а отвергнутая
форма не оставляет за собой половину человека — карточку без условий, которую
расчёт не знает и о которой узнают на закрытии месяца.

**Схема расчёта была свободным текстом.** Знать надо было ключ из YAML
(`standard`, `half_time`, `half_time_min_base`), проверки не было, и опечатка
означала молча несчитанного человека. Проверяется, что список приезжает из правил
страны (а не вписан в шаблон — вписанный разъедется с пресетом молча), что чужое
значение отвергается словами, и что уже лежащее в базе незнакомое значение
**не подменяется** при сохранении формы: подмена схемы у того, кто правил дату
увольнения, — худший исход из всех.

**Мера работы задавалась только на группу и только правом `rules.manage`.**
Теперь она есть у человека, в его условиях найма, версией по датам — и ведёт её
тот, кто ведёт условия найма. Проверяется вся дорога: с экрана в базу, из базы в
табель и из базы в расчёт. Без последних двух проверка доказывала бы, что
колонка записывается, а не что она чем-то управляет.

Право проверяется **и базой**, а не только экраном: политики `employees` и
`employment_terms` (`0130_directory_permissions`) стоят на записи, и проверка
идёт ролью `app_user` — на владельца схемы политики не действуют, и зелёный
результат владельцем ничего не значил бы.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as

LIST = "/directory/employees/"
NEW = "/directory/employees/new/"
JUNE = date(2026, 6, 1)


@pytest.fixture
def sql(web_env):
    """Прямое соединение владельцем — только чтобы готовить и убирать состояние.

    Проверки доступа этим соединением не делаются: на владельца политики не
    действуют (см. `as_app_user` в `conftest`).
    """
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def rls(web_env):
    """Соединение для проверок доступа: своя транзакция, роль `app_user`.

    Отдельно от `sql`: `set local role` действует до конца транзакции, а в
    `autocommit` каждый оператор — своя транзакция, и проверка молча пошла бы
    владельцем схемы.
    """
    import psycopg

    with psycopg.connect(web_env) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def only_seeded_people(sql):
    """Люди, заведённые тестом, убираются — кем бы тест ни закончился.

    База `web_env` живёт весь прогон и одна на все модули: лишний человек
    двигает счётчики справочника и списки других проверок. Убираем по разнице
    «кто был до», а не по имени: тест, упавший на середине, мог оставить кого
    угодно.
    """
    before = [str(row[0]) for row in sql.execute("select id from employees").fetchall()]
    yield
    sql.execute(
        "delete from employment_terms where employee_id <> all(%s::uuid[])", (before,)
    )
    sql.execute("delete from employees where id <> all(%s::uuid[])", (before,))


def a_group(ledger: str = "official"):
    """Группа сида видимого всем регистра — в неё и заводим."""
    from core.models import EmployeeGroup

    group = EmployeeGroup.objects.filter(ledger=ledger).order_by("code").first()
    assert group is not None, f"в сиде нет группы регистра {ledger}"
    return group


def a_unit():
    from core.models import Unit

    unit = Unit.objects.order_by("code").first()
    assert unit is not None, "в сиде нет ни одной точки"
    return unit


def hire_form(**changes) -> dict:
    """Заполненная форма заведения. Меняем в ней ровно то, что проверяем."""
    form = {
        "last_name": "ПРОВЕРКА", "first_name": "Новичок",
        "external_id": "0101990800777",
        "hired_at": "2026-06-15",
        "group": str(a_group().id),
        "unit": str(a_unit().id),
        "base_rate": "400,50",
        "coefficient": "1",
        "scheme": "",
        "work_measure": "",
        "ledger": "",
    }
    form.update(changes)
    return form


def created(sql, external_id: str = "0101990800777"):
    return sql.execute(
        "select id, hired_at, dismissed_at from employees where external_id = %s",
        (external_id,),
    ).fetchone()


# --- 1. Заведение -------------------------------------------------------------


def test_an_employee_is_created_together_with_his_employment_terms(
    client, web_env, sql, only_seeded_people,
):
    """Главная проверка задачи: с экрана появляется человек, которого знает расчёт.

    Условия найма проверяются наравне с карточкой. Человек без них не попадёт ни
    в табель, ни в ведомость — то есть «заведён» он был бы только на вид.
    """
    login_as(client, "admin")
    answer = client.post(NEW, hire_form())
    assert answer.status_code == 302, body(answer)

    person = created(sql)
    assert person, "сотрудник не появился в базе"
    assert person[1] == date(2026, 6, 15), "дата приёма не записана"
    assert answer["Location"].startswith(f"{LIST}{person[0]}/"), answer["Location"]

    terms = sql.execute(
        "select valid_from, valid_to, base_rate, coefficient, group_id "
        "from employment_terms where employee_id = %s", (person[0],),
    ).fetchall()
    assert len(terms) == 1, f"версий условий найма не одна: {terms}"
    assert terms[0][0] == date(2026, 6, 15), (
        "условия найма начинаются не с даты приёма — человек принят одним числом, "
        "а считается с другого"
    )
    assert terms[0][1] is None, "первая версия сразу закрыта"
    assert terms[0][2] == Decimal("400.5000")
    assert str(terms[0][4]) == str(a_group().id)

    # И он виден там, где его будут искать: в списке и своей карточкой.
    assert "ПРОВЕРКА" in body(client.get(LIST))
    card = body(client.get(f"{LIST}{person[0]}/"))
    assert "ПРОВЕРКА" in card and "400,5" in card.replace("&nbsp;", " ")
    client.post("/logout/")


def test_the_created_person_is_told_what_to_do_next(
    client, web_env, sql, only_seeded_people,
):
    """«Заведён» — не то же самое, что «попадёт в ведомость», и продукт это говорит.

    Без часов человек в расчёт не попадёт, и молчание об этом читается как
    «готово»: следующий шаг человек узнал бы из пустой ведомости.

    И сказано ровно то, что продукт умеет сегодня. Строку табеля создаёт пока
    только загрузка таблицы за месяц (`timesheets/importer.py` — единственное
    место, где она появляется), а экран табеля показывает тех, у кого строка уже
    есть. Поэтому проверяется не слово «табель», а **загрузка**: обещание
    «внесите ему часы в табеле» отправило бы человека искать строку, которой там
    нет, — а обещание, которого продукт не держит, хуже отсутствующей
    возможности. Дыра записана в журнал блока.
    """
    login_as(client, "admin")
    answer = client.post(NEW, hire_form(), follow=True)
    page = body(answer)
    assert "заведён" in page.lower(), page[:400]
    assert "загрузка таблицы" in page, "не сказано, откуда у человека возьмутся часы"
    client.post("/logout/")


def test_a_refused_creation_leaves_no_half_a_person(
    client, web_env, sql, only_seeded_people,
):
    """Отказ не оставляет карточку без условий найма — ни одной строки.

    Это и есть цена одной точки сохранения. Человек без условий найма — молчащий
    дефект: он есть в справочнике, но расчёт его не знает, и узнаётся это на
    закрытии месяца.
    """
    before = sql.execute("select count(*) from employees").fetchone()[0]

    login_as(client, "admin")
    # Ставка не число — отказ на разборе, уже после того как карточка собрана.
    refused = client.post(NEW, hire_form(base_rate="четыреста"))
    assert refused.status_code == 400, refused.status_code
    assert "нужно число" in body(refused)

    # И то же самое отказом базы, а не разбором: точки с таким номером нет.
    absent = client.post(NEW, hire_form(unit="00000000-0000-0000-0000-000000000000"))
    assert absent.status_code == 400, absent.status_code

    assert sql.execute("select count(*) from employees").fetchone()[0] == before, (
        "отвергнутая форма завела человека"
    )
    assert not created(sql)
    client.post("/logout/")


def test_a_duplicate_external_key_is_refused_in_words(
    client, web_env, sql, only_seeded_people,
):
    """Повторный сквозной ключ — отказ словами, а не оборванный запрос.

    По этому ключу сходится загрузка табеля: два человека с одним JMBG означают
    часы, приехавшие не тому.
    """
    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302

    again = client.post(NEW, hire_form(last_name="ДРУГОЙ"))
    assert again.status_code in (400, 409), again.status_code
    words = body(again)
    assert "Не сохранено" in words
    assert "0101990800777" in words or "ключ" in words.lower(), words[:500]

    assert sql.execute(
        "select count(*) from employees where external_id = %s", ("0101990800777",),
    ).fetchone()[0] == 1
    client.post("/logout/")


# Что заведение закрыто **базой**, а не только экраном, проверяется ролью
# `app_user` в `tests/test_directory_policy.py`
# (`test_a_role_without_the_right_cannot_hire`): там живут все проверки политик
# справочников, и вторая их копия здесь разъехалась бы с первой.


# --- 2. Увольнение ------------------------------------------------------------


def test_dismissing_is_a_date_and_the_person_stays(
    client, web_env, sql, only_seeded_people,
):
    """Уволить — поставить дату. Карточка, история и закрытые месяцы остаются.

    Удаление здесь было бы дефектом, а не удобством: ведомость июня ссылается на
    человека, и снесённая строка уносит смысл этой ссылки.
    """
    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302
    person = created(sql)

    card = f"{LIST}{person[0]}/"
    answer = client.post(card, {
        "what": "person", "last_name": "ПРОВЕРКА", "first_name": "Новичок",
        "external_id": "0101990800777",
        "hired_at": "2026-06-15", "dismissed_at": "2026-06-30",
    })
    assert answer.status_code == 302, body(answer)
    assert created(sql)[2] == date(2026, 6, 30), "дата увольнения не записана"

    # Человек на месте: карточка открывается, версия условий найма цела.
    assert client.get(card).status_code == 200
    assert sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (person[0],),
    ).fetchone()[0] == 1
    assert "2026-06-30" in body(client.get(LIST)), "в списке не видно, что человек уволен"

    # И обратимо: дату можно очистить.
    back = client.post(card, {
        "what": "person", "last_name": "ПРОВЕРКА", "first_name": "Новичок",
        "external_id": "0101990800777", "hired_at": "2026-06-15", "dismissed_at": "",
    })
    assert back.status_code == 302
    assert created(sql)[2] is None, "увольнение необратимо"
    client.post("/logout/")


def test_a_dismissal_before_the_hire_date_is_refused(
    client, web_env, sql, only_seeded_people,
):
    """Уволен раньше, чем принят, — отказ словами, а не строка в базе."""
    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302
    person = created(sql)

    refused = client.post(f"{LIST}{person[0]}/", {
        "what": "person", "last_name": "ПРОВЕРКА", "first_name": "Новичок",
        "external_id": "0101990800777",
        "hired_at": "2026-06-15", "dismissed_at": "2026-06-01",
    })
    assert refused.status_code == 400, refused.status_code
    assert created(sql)[2] is None
    client.post("/logout/")


# --- 3. Схема расчёта выбором из списка ---------------------------------------


def schemes_in_rules(sql) -> set[str]:
    row = sql.execute(
        "select array(select jsonb_object_keys(body -> 'schemes')) from rule_presets "
        "where country_code = 'RS' limit 1"
    ).fetchone()
    assert row and row[0], "в базе нет правил страны — выбирать не из чего"
    return set(row[0])


def options_of(html: str, name: str) -> set[str]:
    """Значения `<option>` того `<select>`, который называется этим именем."""
    block = re.search(
        rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S,
    )
    assert block, f"на странице нет списка выбора «{name}»"
    return set(re.findall(r'<option value="([^"]*)"', block.group(1)))


def test_the_scheme_is_chosen_from_the_country_rules_not_typed(client, web_env, sql):
    """Список схем приезжает из правил страны — и на карточке, и на форме заведения.

    Сверяется с самим пресетом, а не с ожидаемым набором из трёх ключей: список,
    вписанный в шаблон, разъедется с правилами молча — новая схема страны
    появится в расчёте и не появится на экране.
    """
    expected = schemes_in_rules(sql)
    person = sql.execute(
        "select id from employees order by external_id limit 1"
    ).fetchone()[0]

    login_as(client, "admin")
    for url in (f"{LIST}{person}/", NEW):
        html = body(client.get(url))
        assert 'name="scheme"' in html
        assert '<input' not in re.search(
            r'.{200}name="scheme"', html, re.S,
        ).group(0), f"{url}: схема всё ещё набирается текстом"
        assert expected <= options_of(html, "scheme"), (
            f"{url}: список схем не сходится с правилами страны"
        )
    client.post("/logout/")


def selected_option(html: str, name: str) -> str:
    """Значение варианта, отмеченного в списке при загрузке страницы."""
    block = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S)
    assert block, f"на странице нет списка выбора «{name}»"
    chosen = re.findall(r'<option value="([^"]*)"[^>]*\sselected', block.group(1))
    return chosen[0] if chosen else ""


def test_the_form_does_not_choose_the_group_for_the_human(client, web_env):
    """Обязательный список не выбирает сам: пока не выбрали — не выбрано ничего.

    Найдено смоуком в браузере, и в разметке этого не видно вовсе: `<select>`
    без пустого варианта браузер отмечает первым пунктом сам. На форме
    заведения это означало человека, попавшего в первую по алфавиту группу —
    «Временные работы», — не выбранную никем. У группы своя схема расчёта и свой
    регистр учёта, то есть это другие деньги и другая видимость строки.

    Проверяются оба обязательных списка, где выбирать не из чего заранее: группа
    у нового человека и схема расчёта у новой группы.
    """
    login_as(client, "admin")
    people = body(client.get(NEW))
    assert selected_option(people, "group") == "", (
        "форма заведения выбрала группу за человека"
    )
    assert "выберите группу" in people, "пустой вариант не подписан словами"

    group = body(client.get("/directory/groups/new/"))
    assert selected_option(group, "scheme") == "", (
        "форма новой группы выбрала схему расчёта за человека"
    )
    client.post("/logout/")


def test_a_scheme_that_is_not_in_the_rules_is_refused(
    client, web_env, sql, only_seeded_people,
):
    """Чужая схема отвергается словами — и версия условий найма не заводится.

    Список в браузере — не защита: форму отправляют и мимо него. Опечатка,
    доехавшая до базы, означает человека, которого расчёт отвергнет по имени в
    день закрытия месяца.
    """
    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302
    person = created(sql)[0]

    refused = client.post(f"{LIST}{person}/", {
        "what": "terms", "valid_from": "2026-07-01",
        "group": str(a_group().id), "unit": str(a_unit().id),
        "base_rate": "400", "coefficient": "1",
        "scheme": "half_tiem",  # опечатка в half_time
        "work_measure": "", "ledger": "",
    })
    assert refused.status_code == 400, refused.status_code
    assert "такого варианта нет" in body(refused)
    assert sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (person,),
    ).fetchone()[0] == 1, "версия завелась при отказе"
    client.post("/logout/")


def test_a_stored_scheme_unknown_to_the_rules_is_kept_and_marked(
    client, web_env, sql, only_seeded_people,
):
    """Незнакомая схема, уже лежащая в базе, не подменяется молча.

    Худший исход этой задачи выглядел бы так: человек открыл карточку, чтобы
    поставить дату увольнения, браузер отметил в списке первую схему — и
    «Сохранить» перевело человека на другой расчёт. Поэтому значение остаётся в
    списке и помечается словами.
    """
    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302
    person = created(sql)[0]
    sql.execute(
        "update employment_terms set scheme = 'привет_из_прошлого' where employee_id = %s",
        (person,),
    )

    html = body(client.get(f"{LIST}{person}/"))
    assert "привет_из_прошлого" in options_of(html, "scheme"), (
        "лежащее в базе значение выброшено из списка — «Сохранить» подменит схему"
    )
    assert "в правилах страны такой нет" in html, "подмена не помечена словами"

    # И его можно сохранить обратно как есть: отказ на своём же предложении был
    # бы тупиком — карточку нельзя было бы править вовсе.
    kept = client.post(f"{LIST}{person}/", {
        "what": "terms", "valid_from": "2026-07-01",
        "group": str(a_group().id), "unit": str(a_unit().id),
        "base_rate": "500", "coefficient": "1",
        "scheme": "привет_из_прошлого", "work_measure": "", "ledger": "",
    })
    assert kept.status_code == 302, body(kept)
    assert sql.execute(
        "select scheme from employment_terms where employee_id = %s "
        "order by valid_from desc limit 1", (person,),
    ).fetchone()[0] == "привет_из_прошлого"
    client.post("/logout/")


def test_the_card_reads_the_scheme_back_in_words_not_in_yaml(
    client, web_env, sql, only_seeded_people,
):
    """Выбрал из списка — прочитал словом. Ключ из YAML на экран не выходит.

    «Ни разу не открыв YAML» — это про оба направления. Пока история версий
    показывала сам ключ, человек выбирал «Прямая выплата без пересчета», а
    читал обратно `direct`: чтобы понять карточку, приходилось идти в правила
    страны глазами. Найдено смоуком в браузере.
    """
    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302
    person = created(sql)[0]

    html = body(client.get(f"{LIST}{person}/"))
    table = re.search(r"<table>.*?</table>", html, re.S)
    assert table, "на карточке нет истории условий найма"
    cells = [re.sub(r"<[^>]+>", "", cell).strip()
             for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", table.group(0), re.S)]
    for key in schemes_in_rules(sql):
        assert key not in cells, f"в истории стоит ключ схемы из правил: {key}"
    # И подпись действующей схемы там есть — иначе «ключа нет» доказывало бы,
    # что столбец просто пуст.
    titles = set(sql.execute(
        "select array(select value -> 'title' ->> 'ru' from rule_presets, "
        "jsonb_each(body -> 'schemes') where country_code = 'RS')"
    ).fetchone()[0])
    assert titles & set(cells), f"схема не названа словом: {cells}"
    client.post("/logout/")


def test_the_group_scheme_is_a_list_too(client, web_env, sql):
    """У группы схема тоже выбирается, а не набирается: дыра была общая."""
    expected = schemes_in_rules(sql)
    group = sql.execute(
        "select id from employee_groups where ledger = 'official' limit 1"
    ).fetchone()[0]

    login_as(client, "admin")
    html = body(client.get(f"/directory/groups/{group}/"))
    assert expected <= options_of(html, "scheme")
    client.post("/logout/")


# --- 4. Чем меряется работа — у человека --------------------------------------


def test_the_work_measure_is_offered_and_stored_for_one_person(
    client, web_env, sql, only_seeded_people,
):
    """Способ оплаты задаётся человеку, а не только всей его группе.

    Список — из правил страны, тем же приёмом, что схема: `work_measures` в
    пресете, а не набор в шаблоне.
    """
    measures = set(sql.execute(
        "select array(select jsonb_object_keys(body -> 'work_measures')) "
        "from rule_presets where country_code = 'RS' limit 1"
    ).fetchone()[0])

    login_as(client, "admin")
    assert client.post(NEW, hire_form()).status_code == 302
    person = created(sql)[0]

    html = body(client.get(f"{LIST}{person}/"))
    assert measures <= options_of(html, "work_measure"), (
        "список способов не сходится с правилами страны"
    )

    saved = client.post(f"{LIST}{person}/", {
        "what": "terms", "valid_from": "2026-07-01",
        "group": str(a_group().id), "unit": str(a_unit().id),
        "base_rate": "400,50", "coefficient": "1",
        "scheme": "", "work_measure": "fixed_amount", "ledger": "",
    })
    assert saved.status_code == 302, body(saved)
    assert sql.execute(
        "select work_measure from employment_terms where employee_id = %s "
        "order by valid_from desc limit 1", (person,),
    ).fetchone()[0] == "fixed_amount"

    # Смена способа — новая версия, а не правка старой: прошлый месяц считается
    # по-прежнему.
    versions = sql.execute(
        "select valid_from, work_measure from employment_terms where employee_id = %s "
        "order by valid_from", (person,),
    ).fetchall()
    assert [(v[0], v[1]) for v in versions] == [
        (date(2026, 6, 15), None), (date(2026, 7, 1), "fixed_amount"),
    ], versions
    client.post("/logout/")


def test_setting_the_measure_needs_no_right_to_manage_rules(
    client, web_env, sql, only_seeded_people,
):
    """Мера человека — условие найма, и `rules.manage` для неё не нужен.

    Ровно эта половина дыры и была: способ существовал только правилом группы, а
    правило правится другим правом. Партнёр вправе развести ведение справочников
    и ведение правил по разным людям — и тогда кадровик не смог бы оформить
    сдельного курьера, не получив доступ ко всем правилам страны.
    """
    sql.execute(
        "update roles set permissions = permissions - 'rules.manage' "
        "where code = 'admin' and tenant_id is not null"
    )
    try:
        login_as(client, "admin")
        assert client.post(NEW, hire_form()).status_code == 302
        person = created(sql)[0]
        saved = client.post(f"{LIST}{person}/", {
            "what": "terms", "valid_from": "2026-07-01",
            "group": str(a_group().id), "unit": str(a_unit().id),
            "base_rate": "400,50", "coefficient": "1",
            "scheme": "", "work_measure": "deliveries", "ledger": "",
        })
        assert saved.status_code == 302, body(saved)
        assert sql.execute(
            "select work_measure from employment_terms where employee_id = %s "
            "order by valid_from desc limit 1", (person,),
        ).fetchone()[0] == "deliveries"
        client.post("/logout/")
    finally:
        sql.execute(
            "update roles set permissions = permissions || '[\"rules.manage\"]'::jsonb "
            "where code = 'admin' and tenant_id is not null"
        )


def test_the_person_measure_reaches_the_timesheet(client, web_env):
    """Табель спрашивает величину за месяц у того, кому задана своя мера.

    Дорога с экрана в базу ничего не стоит, если она никуда не ведёт.
    Проверяется поэтому следующий экран: у сдельного человека в табеле
    появляется ячейка величины — та самая, куда вводят доставки вместо часов.
    """
    from core.models import EmploymentTerm

    login_as(client, "director")
    url = grid_url(client)
    assert 'name="piece"' not in body(client.get(url)), (
        "ячейка величины есть до всякой сдельной меры — проверка проверяет пустоту"
    )

    term = one_june_term()
    EmploymentTerm.objects.filter(pk=term.pk).update(work_measure="deliveries")
    try:
        html = body(client.get(url))
        assert 'name="piece"' in html, "табель не спрашивает величину у сдельного человека"
        assert "Доставки" in html, "у ячейки не сказано, что в неё вводят"
    finally:
        EmploymentTerm.objects.filter(pk=term.pk).update(work_measure=None)
    client.post("/logout/")


def test_the_person_measure_reaches_the_calculation(client, web_env, period_restored):
    """И расчёт считает по мере человека, а не по мере его группы.

    Последнее звено. Без него всё выше доказывало бы, что колонка записывается,
    а не что она чем-то управляет: движок мог бы по-прежнему читать правило
    группы, и разошлись бы экран и деньги молча.
    """
    from conftest import wipe_payruns
    from core.models import EmploymentTerm
    from core.models import Timesheet as Row
    from payrun.calc import compute

    wipe_payruns(web_env)
    term = one_june_term()
    row = Row.objects.filter(period=JUNE, employee_id=term.employee_id).first()
    assert row is not None, "у выбранного человека нет табеля за июнь"

    def parts_of():
        _, slips = compute(term.tenant_id, JUNE)
        slip = next(
            slip for case, slip in slips if case.employee_id == term.employee_id
        )
        return {c.code: c.amount for c in slip.components}

    by_hours = parts_of()
    assert any(code.startswith("hours.") for code in by_hours), (
        f"человек и без сдельной меры считается не по часам: {by_hours}"
    )

    Row.objects.filter(pk=row.pk).update(piece_value=Decimal("45000.00"))
    EmploymentTerm.objects.filter(pk=term.pk).update(work_measure="fixed_amount")
    try:
        by_person = parts_of()
        assert by_person != by_hours, "мера человека ни на что не влияет"
        # Фиксированная выплата: величина из табеля и есть начисление, ставка не
        # применяется вовсе. Число проверяемое руками — в этом и смысл.
        assert by_person.get("piecework.fixed_amount") == Decimal("45000.00"), by_person
        assert not [code for code in by_person if code.startswith("hours.")], (
            f"часы начислены человеку, которому задана сдельная мера: {by_person}"
        )
    finally:
        EmploymentTerm.objects.filter(pk=term.pk).update(work_measure=None)
        Row.objects.filter(pk=row.pk).update(piece_value=0)
        wipe_payruns(web_env)


def one_june_term():
    """Условия найма человека, у которого есть табель за июнь.

    Берётся первый по внешнему ключу, а не «какой-нибудь»: проверка обязана
    падать одинаково при повторном прогоне.
    """
    from core.models import EmploymentTerm
    from core.models import Timesheet as Row

    row = (
        Row.objects.filter(period=JUNE)
        .select_related("employee")
        .order_by("employee__external_id")
        .first()
    )
    assert row is not None, "в сиде нет ни одного табеля за июнь"
    term = (
        EmploymentTerm.objects.filter(employee_id=row.employee_id)
        .order_by("valid_from")
        .last()
    )
    assert term is not None, "у человека с табелем нет условий найма"
    return term


def grid_url(client) -> str:
    from conftest import period_url

    match = re.search(r"([0-9a-f-]{36})", period_url(client))
    return f"/timesheets/{match.group(1)}/"
