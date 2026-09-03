"""Рамка страны: что партнёру разрешено доопределять, а что нет (T192).

Эталон модуля 17 формулирует это одной фразой: «страна задаёт значение и рамку,
юрлицо доопределяет внутри рамки… юрлицо может стать строже рамки, но никогда —
мягче». До этой задачи у нас была только первая половина: значение страны есть,
рамки нет, и партнёр переопределял любое правило в любую сторону — процент
больничного можно было опустить с 0,65 до 0,10, и никто не останавливал.

Здесь — чистый Python без ORM и без Django: рамка приезжает телом пресета
страны, а сравнение значения с ней не зависит ни от базы, ни от языка страницы.
Слова отказа живут в `web/rules.py`, потому что они переводятся.

## Где рамка лежит и почему отдельным разделом

Тело пресета — дерево правил (`hour_types.night.pay_percent`). Рамка описывает
правило, а не считается по нему, поэтому кладётся **зеркальным** разделом
`frames` с тем же путём:

    frames:
      hour_types:
        night:
          pay_percent:
            mode: frame
            min: country
            source: "Закон о раду, чл. 108"

Зеркалом, а не соседним ключом рядом со значением (`pay_percent_frame`), по
двум причинам. Соседний ключ стал бы обычным листом пресета: он попал бы в
список правил экрана и в расчёт как правило, которого движок не знает. И
адресоваться к нему пришлось бы путём, который на один сегмент длиннее
настоящего, — то есть у правила появилось бы два адреса.

Раздел `frames` при этом не правило: на экране правил его нет, и переопределить
его партнёр не может (`web/rules.NOT_RULES`). Рамка, которую двигает тот, кого
она держит, рамкой быть перестаёт.

## Три режима — те же, что в эталоне

* `lock` — значение задано законом и на уровне партнёра не переопределяется
  вовсе. Ни мягче, ни строже: это не коридор, а константа.
* `frame` — переопределяется, но только в строгую сторону. Куда именно строгая
  сторона, говорит **само правило**, а не код: у процента больничного мягче
  значит меньше, у потолка переработки — больше.
* `free` (он же «записи нет») — партнёр ставит любое значение. Умолчание.

## Направление рамки не угадывается

Границы объявлены явно (`min` и `max`), и это главное решение модуля. Код не
знает и не может знать, в какую сторону «мягче» у произвольного правила: для
`hour_types.sick.pay_percent` мягче — ниже, для лимита переработки — выше, а
для доли ставки, которой закон не касается, направления нет вовсе. Правило, у
которого направление неизвестно, рамки не получает — и это честнее, чем рамка,
поставленная наугад: она отвергала бы верную правку словами про закон.

Граница пишется числом либо словом `country` — «не мягче того, что стоит у
страны на эту же дату». Второе почти всегда и нужно: минимум закона и есть
значение страны, и при индексации минималки рамка едет вместе с ней сама.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Раздел тела пресета, в котором лежат рамки. Именем, а не «первым ключом,
# похожим на рамку»: тело приезжает из jsonb, и угадывание разделов по составу
# ключей — способ однажды принять правило за рамку.
FRAMES_KEY = "frames"

# Граница «как у страны на эту же дату». Словом, а не числом, потому что число
# устарело бы при первой же индексации: рамка обязана ехать вместе со значением.
COUNTRY = "country"

MODES = ("lock", "frame", "free")

# Ключ, по которому узел зеркала опознаётся как рамка, а не как ветка пути.
MODE_KEY = "mode"

__all__ = [
    "COUNTRY", "FRAMES_KEY", "Frame", "FrameMisconfigured", "MODES", "Violation",
    "check", "frame_for", "frames_of", "problems_in",
]


@dataclass(frozen=True)
class Frame:
    """Рамка одного правила: режим, границы и источник.

    `minimum` и `maximum` — число либо `COUNTRY`. Пусто — границы с этой стороны
    нет. У `lock` границ нет вовсе: там ограничение не в величине.
    """

    path: str
    mode: str
    minimum: Any = None
    maximum: Any = None
    source: str = ""

    @property
    def restricts(self) -> bool:
        """Ограничивает ли рамка правку вообще. У `free` — нет."""
        return self.mode in ("lock", "frame")

    def bound(self, side: str, country_value: Any) -> Any:
        """Граница числом: `country` разворачивается в значение страны."""
        raw = self.minimum if side == "min" else self.maximum
        return country_value if raw == COUNTRY else raw


@dataclass(frozen=True)
class Violation:
    """Чем именно значение не подошло. Слова — снаружи, здесь только факт.

    `kind`: `locked` — правило не переопределяется вовсе; `below` — ниже нижней
    границы; `above` — выше верхней. `bound` — та самая граница числом, чтобы
    отказ назвал её, а не отправил человека искать.
    """

    kind: str
    bound: Any = None
    source: str = ""


class FrameMisconfigured(ValueError):
    """Рамка описана так, что применить её нельзя.

    Отдельным исключением, а не молчаливым пропуском: рамка, которую не удалось
    прочитать, — это отсутствующая рамка, и узнать об этом надо на загрузке
    страны, а не через полгода по заведённому переопределению.
    """


def frames_of(body: dict[str, Any]) -> dict[str, Frame]:
    """Все рамки тела пресета: путь правила → рамка.

    Обход идёт вглубь, пока не встретится узел с ключом `mode` — он и есть
    рамка. Так зеркало повторяет дерево правил любой глубины и не требует
    списка разделов в коде: новая страна принесёт свои пути сама.
    """
    section = body.get(FRAMES_KEY)
    if not isinstance(section, dict):
        return {}

    found: dict[str, Frame] = {}

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if not isinstance(value, dict):
                raise FrameMisconfigured(
                    f"рамка '{path}' описана значением, а не набором полей: "
                    f"нужен хотя бы {MODE_KEY}"
                )
            if MODE_KEY in value:
                found[path] = _frame(path, value)
                continue
            walk(value, path + ".")

    walk(section, "")
    return found


def frame_for(body: dict[str, Any], path: str) -> Frame | None:
    """Рамка этого правила. Нет записи — нет рамки, и это разрешение.

    Наследования от узла-предка нет намеренно: рамка на `hour_types` целиком
    означала бы одну границу для процента оплаты и для признака «входит во
    взносы», то есть рамку, поставленную наугад сразу на всё.
    """
    return frames_of(body).get(path)


def _frame(path: str, spec: dict[str, Any]) -> Frame:
    mode = spec.get(MODE_KEY)
    if mode not in MODES:
        raise FrameMisconfigured(
            f"рамка '{path}': режим '{mode}' неизвестен. Допустимо: {', '.join(MODES)}"
        )
    return Frame(
        path=path,
        mode=str(mode),
        minimum=spec.get("min"),
        maximum=spec.get("max"),
        source=str(spec.get("source") or ""),
    )


def check(frame: Frame | None, value: Any, country_value: Any) -> Violation | None:
    """Проходит ли значение рамку. Проходит — None, не проходит — чем именно.

    `country_value` нужен для границ, объявленных словом `country`. Значения
    страны нет (правило завёл сам партнёр) — такая граница не проверяется: рамки
    без страны не бывает, а отказать по невычислимой границе значило бы отказать
    молча и непонятно.
    """
    if frame is None or not frame.restricts:
        return None
    if frame.mode == "lock":
        return Violation(kind="locked", source=frame.source)

    for side, kind in (("min", "below"), ("max", "above")):
        edge = frame.bound(side, country_value)
        if edge is None:
            continue
        if not _comparable(value, edge):
            raise FrameMisconfigured(
                f"рамка '{frame.path}': граница {side}={edge!r} несравнима со "
                f"значением {value!r}. Границы ставятся числам, а не тексту"
            )
        if (kind == "below" and value < edge) or (kind == "above" and value > edge):
            return Violation(kind=kind, bound=edge, source=frame.source)
    return None


def _comparable(value: Any, edge: Any) -> bool:
    """Сравнимы ли значение и граница как числа.

    `bool` исключён нарочно, хотя в Python он число: «не ниже нет» читается как
    ошибка перевода, а не как правило. Выключатель, который нельзя выключить, —
    это `lock`, и выражается он режимом, а не границей.
    """
    return all(
        isinstance(item, (int, float)) and not isinstance(item, bool)
        for item in (value, edge)
    )


def problems_in(body: dict[str, Any]) -> list[str]:
    """Что не так с рамками этого пресета. Пусто — рамки описаны верно.

    Проверка отдельной функцией, а не отказом при чтении: пресет страны читается
    на каждом расчёте, и падать там из-за опечатки в рамке значило бы остановить
    расчёт вместо того, чтобы не пустить правку.

    **Единственный барьер стоит на первичной загрузке** —
    `core.rules._refuse_broken_frames`, и это не «одно из мест», а всё место
    целиком: раздел `frames` попадает в базу только оттуда (на экране он закрыт
    ответом 404 на любом уровне). Пока барьера не было, опечатка в режиме рамки
    доезжала до базы молча, а вылезала через сутки — пятисоткой на сохранении
    ЧУЖОГО правила у ЛЮБОГО партнёра страны: разбор обходит весь раздел на
    каждое чтение и падает на первом плохом узле. Найдено приёмкой T192.
    """
    try:
        frames = frames_of(body)
    except FrameMisconfigured as broken:
        return [str(broken)]

    found: list[str] = []
    for path, frame in sorted(frames.items()):
        found += _problems_of(body, path, frame)
    return found


def _problems_of(body: dict[str, Any], path: str, frame: Frame) -> list[str]:
    found: list[str] = []
    value = _value_at(body, path)
    if value is _MISSING:
        found.append(f"рамка '{path}': такого правила в пресете нет")
    if frame.mode != "free" and not frame.source:
        # Эталон модуля 17: «Источник. Статья закона или решение сети с датой.
        # Без источника правило не сохранить». Рамка без источника через год
        # неотличима от чьей-то догадки, и снять её будет некому.
        found.append(f"рамка '{path}': не назван источник")
    if frame.mode == "frame" and frame.minimum is None and frame.maximum is None:
        found.append(f"рамка '{path}': режим frame без границ ничего не держит")
    if frame.mode != "frame" and (frame.minimum is not None or frame.maximum is not None):
        found.append(f"рамка '{path}': границы имеют смысл только у режима frame")
    found += _bound_problems(path, frame, value)
    return found


def _bound_problems(path: str, frame: Frame, value: Any) -> list[str]:
    found: list[str] = []
    for side, raw in (("min", frame.minimum), ("max", frame.maximum)):
        if raw is None or raw == COUNTRY:
            continue
        if not _comparable(0, raw):
            found.append(f"рамка '{path}': граница {side} должна быть числом или '{COUNTRY}'")
    if frame.mode == "frame" and value is not _MISSING and not _comparable(value, 0):
        found.append(
            f"рамка '{path}': режим frame держит величину, а значение правила — "
            f"{type(value).__name__}. Такому правилу подходит lock, а не frame"
        )
    return found


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - только для сообщений
        return "<нет такого правила>"


_MISSING = _Missing()


def _value_at(body: dict[str, Any], path: str) -> Any:
    node: Any = body
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node
