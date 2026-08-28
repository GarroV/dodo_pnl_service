"""Сумма прописью для платёжной ведомости — алгоритм, а не каталог перевода.

На ведомости, которую человек подписывает, стоит строка «Сумма прописью: …».
Продукт говорит на трёх языках (ru, en, sr-latn), и строка обязана говорить
на языке страницы. Каталог переводов (`{% blocktranslate %}` + `.po`) сюда не
подходит: числительных бесконечно много, а каталог несёт конечный список строк.
Поэтому числительное порождается алгоритмом — отдельно для каждого языка, со
своими родами, падежами разряда (тысяча — женского рода и в русском, и в
сербском) и славянским правилом трёх форм множественного числа (1 / 2-4 / 5+
и отдельно 11-19, которое не совпадает ни с одной из первых двух форм).

Модуль **чистый Python без Django**: строка на подписываемом документе не
должна зависеть от того, поднят ли Django и собран ли `.mo`-каталог.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Разряды разбираются триадами от старшего к младшему: миллиард, миллион,
# тысяча, единицы. 0 — единицы, здесь разрядное слово не добавляется.
_GROUP_SHIFTS = (9, 6, 3, 0)

# Выше миллиарда прописью не считаем: молчаливая выдача неверной строки на
# подписываемом документе хуже явного отказа.
_MAX_MAJOR = 999_999_999_999


def in_words(amount: Decimal | int | str, *, language: str, currency: str) -> str:
    """Сумма прописью: целая часть словами, дробная — двумя цифрами.

    `amount` приводится к `Decimal`, если пришёл `int`/`str`. Округление —
    `ROUND_HALF_UP` до копеек, один раз в самом начале: если считать целую и
    дробную часть от неокруглённого числа порознь, `0.005` даёт разные ответы
    для рублей и копеек в зависимости от того, где случилось округление.

    `language` нормализуется без учёта регистра: неизвестный язык тихо
    откатывается на английский — страница не должна разваливаться из-за
    нового языка в настройках пользователя.

    `currency` — код ISO. Неизвестный код не выдумывается: основная единица
    называется самим кодом, а разменная не называется вовсе (только две
    цифры) — придуманное название на подписываемом документе хуже отсутствия.

    Raises:
        ValueError: сумма (по модулю) больше `_MAX_MAJOR` — за пределами
            заведённых разрядов (до миллиарда включительно) считать нечем.
    """
    quantized = _quantize(amount)
    is_negative = quantized < 0
    major, minor = _split_cents(abs(quantized))
    if major > _MAX_MAJOR:
        limit = f"{_MAX_MAJOR:,}".replace(",", " ")
        raise ValueError(
            f"Сумма {abs(quantized)} прописью не считается: заведённых разрядов "
            f"хватает только до {limit} (миллиарды)."
        )

    lang = _normalize_language(language)
    currency_code = str(currency).strip().upper()
    if lang == "ru":
        return _slavic_in_words(major, minor, currency_code, is_negative, _RU)
    if lang == "sr-latn":
        return _slavic_in_words(major, minor, currency_code, is_negative, _SR)
    return _english_in_words(major, minor, currency_code, is_negative)


def _quantize(amount: Decimal | int | str) -> Decimal:
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _split_cents(amount: Decimal) -> tuple[int, int]:
    """Целые копейки после округления — дальше работаем только с int."""
    cents = int(amount * 100)
    return divmod(cents, 100)


def _normalize_language(language: str) -> str:
    """"sr", "sr-Latn", "ru-RU" — по префиксу до дефиса; неизвестное — en."""
    prefix = str(language).strip().lower().split("-")[0]
    if prefix == "ru":
        return "ru"
    if prefix == "sr":
        return "sr-latn"
    return "en"


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _currency_word(
    currencies: dict[str, dict[str, tuple[str, ...]]],
    code: str,
    role: str,
    count: int,
    plural_index: Callable[[int], int],
) -> str | None:
    """Слово единицы валюты в нужном числе; неизвестный код — сам код без разменной."""
    forms = currencies.get(code)
    if forms is None:
        return code if role == "major" else None
    return forms[role][plural_index(count)]


# --------------------------------------------------------------------------
# Русский и сербская латиница — общее ядро: оба славянские, у обоих тысяча
# женского рода и одно и то же правило трёх форм множественного числа.
# --------------------------------------------------------------------------


def _slavic_plural_index(n: int) -> int:
    """0 — форма на 1 (21, 101…), 1 — форма на 2-4, 2 — форма на 0, 5-9 и 11-19.

    11-19 — отдельный случай: несмотря на то что это тоже двузначные числа с
    последней цифрой 1-4, форма у них третья («одиннадцать тысяч», не
    «одиннадцать тысяча»), потому что смотрим сначала на остаток от 100.
    """
    if 11 <= n % 100 <= 19:
        return 2
    last = n % 10
    if last == 1:
        return 0
    if 2 <= last <= 4:
        return 1
    return 2


@dataclass(frozen=True)
class _SlavicLexicon:
    ones: tuple[str, ...]  # индекс 0..19, "" на нуле
    ones_fem: dict[int, str]  # {1: ..., 2: ...} — женский род только для разряда тысяч
    tens: dict[int, str]  # индекс 2..9
    hundreds: dict[int, str]  # индекс 1..9
    zero: str
    minus: str
    scales: dict[int, tuple[str, str, str]]  # степень разряда (3, 6, 9) -> три формы
    currencies: dict[str, dict[str, tuple[str, ...]]]  # код -> формы major/minor


_RU = _SlavicLexicon(
    ones=(
        "", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
        "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
        "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
    ),
    ones_fem={1: "одна", 2: "две"},
    tens={
        2: "двадцать", 3: "тридцать", 4: "сорок", 5: "пятьдесят", 6: "шестьдесят",
        7: "семьдесят", 8: "восемьдесят", 9: "девяносто",
    },
    hundreds={
        1: "сто", 2: "двести", 3: "триста", 4: "четыреста", 5: "пятьсот",
        6: "шестьсот", 7: "семьсот", 8: "восемьсот", 9: "девятьсот",
    },
    zero="ноль",
    minus="минус",
    scales={
        3: ("тысяча", "тысячи", "тысяч"),
        6: ("миллион", "миллиона", "миллионов"),
        9: ("миллиард", "миллиарда", "миллиардов"),
    },
    currencies={
        "RSD": {"major": ("динар", "динара", "динаров"), "minor": ("пара", "пары", "пар")},
        "EUR": {"major": ("евро", "евро", "евро"), "minor": ("цент", "цента", "центов")},
    },
)

_SR = _SlavicLexicon(
    ones=(
        "", "jedan", "dva", "tri", "četiri", "pet", "šest", "sedam", "osam", "devet",
        "deset", "jedanaest", "dvanaest", "trinaest", "četrnaest", "petnaest",
        "šesnaest", "sedamnaest", "osamnaest", "devetnaest",
    ),
    ones_fem={1: "jedna", 2: "dve"},
    tens={
        2: "dvadeset", 3: "trideset", 4: "četrdeset", 5: "pedeset", 6: "šezdeset",
        7: "sedamdeset", 8: "osamdeset", 9: "devedeset",
    },
    hundreds={
        1: "sto", 2: "dvesta", 3: "trista", 4: "četiristo", 5: "petsto",
        6: "šeststo", 7: "sedamsto", 8: "osamsto", 9: "devetsto",
    },
    zero="nula",
    minus="minus",
    scales={
        3: ("hiljada", "hiljade", "hiljada"),
        6: ("milion", "miliona", "miliona"),
        9: ("milijarda", "milijarde", "milijardi"),
    },
    currencies={
        "RSD": {"major": ("dinar", "dinara", "dinara"), "minor": ("para", "pare", "para")},
        "EUR": {"major": ("evro", "evra", "evra"), "minor": ("cent", "centa", "centi")},
    },
)


def _slavic_one(digit: int, lexicon: _SlavicLexicon, feminine: bool) -> str:
    if feminine and digit in lexicon.ones_fem:
        return lexicon.ones_fem[digit]
    return lexicon.ones[digit]


def _slavic_group_words(value: int, lexicon: _SlavicLexicon, feminine: bool) -> list[str]:
    """Слова для одной триады (0-999). `feminine` — только для разряда тысяч."""
    words: list[str] = []
    hundreds_digit, remainder = divmod(value, 100)
    if hundreds_digit:
        words.append(lexicon.hundreds[hundreds_digit])
    if remainder >= 20:
        tens_digit, ones_digit = divmod(remainder, 10)
        words.append(lexicon.tens[tens_digit])
        if ones_digit:
            words.append(_slavic_one(ones_digit, lexicon, feminine))
    elif remainder > 0:
        words.append(_slavic_one(remainder, lexicon, feminine))
    return words


def _slavic_in_words(
    major: int, minor: int, currency_code: str, is_negative: bool, lexicon: _SlavicLexicon
) -> str:
    tokens: list[str] = []
    if is_negative:
        tokens.append(lexicon.minus)

    if major == 0:
        tokens.append(lexicon.zero)
    else:
        for shift in _GROUP_SHIFTS:
            value = (major // (10**shift)) % 1000
            if value == 0:
                continue
            # Тысяча — женского рода («одна тысяча», не «один тысяча»),
            # миллион и миллиард — мужского; в русском и сербском одинаково.
            feminine = shift == 3
            tokens.extend(_slavic_group_words(value, lexicon, feminine))
            if shift > 0:
                tokens.append(lexicon.scales[shift][_slavic_plural_index(value)])

    tokens.append(
        _currency_word(lexicon.currencies, currency_code, "major", major, _slavic_plural_index)
    )
    tokens.append(f"{minor:02d}")
    minor_word = _currency_word(
        lexicon.currencies, currency_code, "minor", minor, _slavic_plural_index
    )
    if minor_word is not None:
        tokens.append(minor_word)

    return _capitalize(" ".join(tokens))


# --------------------------------------------------------------------------
# Английский — проще: разряды не склоняются («two million», не «two millions»),
# форм множественного числа две, а не три.
# --------------------------------------------------------------------------

_EN_ONES = (
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen",
)
_EN_TENS = {
    2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
    7: "seventy", 8: "eighty", 9: "ninety",
}
_EN_SCALES = {3: "thousand", 6: "million", 9: "billion"}
_EN_CURRENCIES: dict[str, dict[str, tuple[str, ...]]] = {
    "RSD": {"major": ("dinar", "dinars"), "minor": ("para", "paras")},
    "EUR": {"major": ("euro", "euros"), "minor": ("cent", "cents")},
}


def _english_plural_index(n: int) -> int:
    return 0 if n == 1 else 1


def _english_group_words(value: int) -> list[str]:
    words: list[str] = []
    hundreds_digit, remainder = divmod(value, 100)
    if hundreds_digit:
        words.append(_EN_ONES[hundreds_digit])
        words.append("hundred")
    if remainder >= 20:
        tens_digit, ones_digit = divmod(remainder, 10)
        words.append(_EN_TENS[tens_digit])
        if ones_digit:
            words.append(_EN_ONES[ones_digit])
    elif remainder > 0:
        words.append(_EN_ONES[remainder])
    return words


def _english_in_words(major: int, minor: int, currency_code: str, is_negative: bool) -> str:
    tokens: list[str] = []
    if is_negative:
        tokens.append("minus")

    if major == 0:
        tokens.append("zero")
    else:
        for shift in _GROUP_SHIFTS:
            value = (major // (10**shift)) % 1000
            if value == 0:
                continue
            tokens.extend(_english_group_words(value))
            if shift > 0:
                tokens.append(_EN_SCALES[shift])

    tokens.append(
        _currency_word(_EN_CURRENCIES, currency_code, "major", major, _english_plural_index)
    )
    tokens.append(f"{minor:02d}")
    minor_word = _currency_word(
        _EN_CURRENCIES, currency_code, "minor", minor, _english_plural_index
    )
    if minor_word is not None:
        tokens.append(minor_word)

    return _capitalize(" ".join(tokens))
