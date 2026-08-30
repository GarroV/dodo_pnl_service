"""Сумма прописью на языке страницы, а не только на языке исходника (T186/reports).

`src/reports/words.py` порождает числительное алгоритмом — переводного каталога
здесь нет и быть не может, чисел бесконечно много. Значит и проверять нужно не
«текст совпал со словарём», а саму грамматику: правильный род разряда (тысяча —
женского рода и в русском, и в сербском, «одна тысяча», а не «один тысяча»),
славянское правило трёх форм множественного числа (форма зависит не только от
последней цифры, но и от остатка по модулю 100 — иначе 11 и 21 получили бы
одну и ту же форму, хотя у них разные), округление копеек один раз, а не дважды
порознь для рублей и копеек, и явный отказ там, где придумывать нечего
(неизвестная валюта, сумма за пределами заведённых разрядов).

Модуль строго без Django: тест не поднимает ни настройки, ни базу — падение
здесь означает падение самой функции, а не окружения вокруг неё.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from reports.words import in_words

# --------------------------------------------------------------------------
# Таблица примеров из контракта задачи — эталон, с которым сверяется бухгалтер.
# --------------------------------------------------------------------------

EXAMPLES = [
    ("1315160.00", "ru", "RSD",
     "Один миллион триста пятнадцать тысяч сто шестьдесят динаров 00 пар"),
    ("1315160.00", "en", "RSD",
     "One million three hundred fifteen thousand one hundred sixty dinars 00 paras"),
    ("1315160.00", "sr-latn", "RSD",
     "Jedan milion trista petnaest hiljada sto šezdeset dinara 00 para"),
    ("1.01", "ru", "RSD", "Один динар 01 пара"),
    ("2.02", "ru", "RSD", "Два динара 02 пары"),
    ("5.05", "ru", "RSD", "Пять динаров 05 пар"),
    ("21.21", "ru", "RSD", "Двадцать один динар 21 пара"),
    ("0.50", "ru", "RSD", "Ноль динаров 50 пар"),
    ("-100.00", "ru", "RSD", "Минус сто динаров 00 пар"),
    ("1000.00", "ru", "RSD", "Одна тысяча динаров 00 пар"),
    ("2000.00", "ru", "RSD", "Две тысячи динаров 00 пар"),
    ("1.00", "en", "EUR", "One euro 00 cents"),
    ("2.00", "en", "EUR", "Two euros 00 cents"),
    ("1.00", "sr-latn", "RSD", "Jedan dinar 00 para"),
    ("2.00", "sr-latn", "RSD", "Dva dinara 00 para"),
    ("0.00", "ru", "XYZ", "Ноль XYZ 00"),
]


@pytest.mark.parametrize(("amount", "language", "currency", "expected"), EXAMPLES)
def test_matches_contract_example(amount, language, currency, expected):
    """Каждая строка таблицы контракта — отдельная проверка, а не одна общая."""
    assert in_words(Decimal(amount), language=language, currency=currency) == expected


# --------------------------------------------------------------------------
# Три формы множественного числа — славянское правило, не «единственное/
# множественное». 1 / 2 / 5 — три разные формы. 11 и 21 — та же ловушка, что
# и в test_i18n_plurals.py: обе двузначные с виду похожими последними цифрами
# (1 и 1), но 11 берёт форму «много» (по остатку от 100), а 21 — форму «один».
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("1.00", "Один динар 00 пар"),
        ("2.00", "Два динара 00 пар"),
        ("5.00", "Пять динаров 00 пар"),
        ("11.00", "Одиннадцать динаров 00 пар"),
        ("21.00", "Двадцать один динар 00 пар"),
    ],
)
def test_russian_plural_form_follows_slavic_rule(amount, expected):
    """Форма разряда/валюты у 1, 2, 5, 11 и 21 — три разные формы, не две."""
    assert in_words(Decimal(amount), language="ru", currency="RSD") == expected


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("1.00", "Jedan dinar 00 para"),
        ("2.00", "Dva dinara 00 para"),
        ("5.00", "Pet dinara 00 para"),
        ("11.00", "Jedanaest dinara 00 para"),
        ("21.00", "Dvadeset jedan dinar 00 para"),
    ],
)
def test_serbian_plural_form_follows_slavic_rule(amount, expected):
    """То же правило трёх форм у сербской латиницы — язык другой, грамматика та же."""
    assert in_words(Decimal(amount), language="sr-latn", currency="RSD") == expected


def test_thousand_scale_word_agrees_in_gender_with_the_number_before_it():
    """Тысяча — женского рода: «одна»/«две», а не «один»/«два» перед ней."""
    one_thousand = in_words(Decimal("1000.00"), language="ru", currency="RSD")
    two_thousand = in_words(Decimal("2000.00"), language="ru", currency="RSD")
    assert one_thousand.startswith("Одна тысяча")
    assert two_thousand.startswith("Две тысячи")


def test_serbian_thousand_scale_word_agrees_in_gender_with_the_number_before_it():
    """То же в сербском: «jedna»/«dve» hiljada, не «jedan»/«dva»."""
    one_thousand = in_words(Decimal("1000.00"), language="sr-latn", currency="RSD")
    two_thousand = in_words(Decimal("2000.00"), language="sr-latn", currency="RSD")
    assert one_thousand.startswith("Jedna hiljada")
    assert two_thousand.startswith("Dve hiljade")


# --------------------------------------------------------------------------
# Отрицательная сумма и ноль.
# --------------------------------------------------------------------------


def test_negative_amount_gets_a_capitalized_minus_prefix():
    """Минус — первое слово результата, и заглавная буква переходит на него."""
    result = in_words(Decimal("-100.00"), language="ru", currency="RSD")
    assert result == "Минус сто динаров 00 пар"


def test_negative_amount_in_english():
    """Тот же минус на английском — своё слово, не транслитерация русского."""
    result = in_words(Decimal("-5.50"), language="en", currency="EUR")
    assert result == "Minus five euros 50 cents"


def test_zero_amount_is_spelled_out_not_left_blank():
    """Ноль — тоже сумма прописью, а не пустая целая часть."""
    assert in_words(Decimal("0.00"), language="ru", currency="RSD") == "Ноль динаров 00 пар"


# --------------------------------------------------------------------------
# Неизвестная валюта: код на месте названия, разменная единица не выдумывается.
# --------------------------------------------------------------------------


def test_unknown_currency_uses_the_code_itself_instead_of_a_guessed_name():
    """Придуманное название валюты на подписываемом документе хуже отсутствующего."""
    result = in_words(Decimal("120.00"), language="ru", currency="XYZ")
    assert result == "Сто двадцать XYZ 00"


def test_unknown_currency_names_no_minor_unit_at_all():
    """Разменная единица неизвестной валюты не называется никак — только цифры."""
    result = in_words(Decimal("1.99"), language="en", currency="ABC")
    assert result == "One ABC 99"
    assert "cent" not in result.lower()


# --------------------------------------------------------------------------
# Неизвестный язык: тихий откат на английский, а не падение экрана.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["de", "fr-FR", "", "klingon"])
def test_unknown_language_falls_back_to_english_instead_of_raising(language):
    """Новый язык в настройках пользователя не должен ронять ведомость."""
    result = in_words(Decimal("2.00"), language=language, currency="EUR")
    assert result == "Two euros 00 cents"


@pytest.mark.parametrize(
    ("language", "expected_prefix"),
    [
        ("RU", "Один "),
        ("ru-RU", "Один "),
        ("Sr", "Jedan "),
        ("sr-Latn", "Jedan "),
        ("EN", "One "),
    ],
)
def test_language_is_matched_case_insensitively_by_prefix(language, expected_prefix):
    """"sr", "sr-Latn", "ru-RU" нормализуются по префиксу, регистр не важен."""
    result = in_words(Decimal("1.00"), language=language, currency="RSD")
    assert result.startswith(expected_prefix)


# --------------------------------------------------------------------------
# Верхняя граница — до 999 999 999 999. Выше — явный отказ, не молчаливый брак.
# --------------------------------------------------------------------------


def test_amount_at_the_upper_bound_is_accepted():
    """Ровно 999 999 999 999 — ещё внутри заведённых разрядов, не должно падать."""
    result = in_words(Decimal("999999999999.00"), language="en", currency="EUR")
    assert result.startswith("Nine hundred ninety nine billion")
    assert result.endswith("euros 00 cents")


def test_amount_over_the_upper_bound_raises_a_clear_error():
    """На триллион разрядов не заведено — падать нужно явно, а не досчитывать наугад."""
    with pytest.raises(ValueError, match="999 999 999 999"):
        in_words(Decimal("1000000000000.00"), language="ru", currency="RSD")


def test_negative_amount_over_the_upper_bound_also_raises():
    """Граница проверяется по модулю — отрицательная сумма не обходит проверку."""
    with pytest.raises(ValueError):
        in_words(Decimal("-1000000000000.00"), language="ru", currency="RSD")


# --------------------------------------------------------------------------
# Округление — один раз, ROUND_HALF_UP, в самом начале.
# --------------------------------------------------------------------------


def test_rounding_happens_once_so_borderline_fractions_are_predictable():
    """1.005 и 1.004 расходятся ровно там, где решает ROUND_HALF_UP, и не иначе.

    Если бы целая и дробная часть округлялись порознь (например, дробная —
    отдельным округлением до копеек уже после того, как взята целая), 1.005
    мог бы дать то же самое, что и 1.004, или неожиданно перейти в «два».
    Здесь квантование одно, в самом начале, поэтому расхождение предсказуемое:
    .005 округляется вверх (ROUND_HALF_UP), .004 — вниз.
    """
    up = in_words(Decimal("1.005"), language="ru", currency="RSD")
    down = in_words(Decimal("1.004"), language="ru", currency="RSD")
    assert up == "Один динар 01 пара"
    assert down == "Один динар 00 пар"
    assert up != down


def test_in_words_accepts_int_and_str_not_only_decimal():
    """Контракт обещает приведение int/str через Decimal — не только готовый Decimal."""
    assert in_words(100, language="ru", currency="RSD") == "Сто динаров 00 пар"
    assert in_words("100", language="ru", currency="RSD") == "Сто динаров 00 пар"
