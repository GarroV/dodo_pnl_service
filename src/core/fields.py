"""
Поля, которых нет в Django, а в схеме они нужны.

Нативные enum-типы Postgres выбраны сознательно: значение регистра учёта или
статуса периода — это словарь домена, и база должна отвергать чужое значение
сама, а не надеяться на приложение. Django такими типами управлять не умеет,
поэтому сами типы создаются миграцией через `RunSQL`, а здесь живёт только
способ сослаться на них из модели.
"""
from __future__ import annotations

from django.db import models


class EnumField(models.Field):
    """Колонка нативного enum-типа Postgres.

    Читается и пишется как обычная строка: psycopg отдаёт значение enum текстом,
    а параметр без явного типа Postgres приводит к enum сам.
    """

    description = "Нативный enum-тип Postgres"

    def __init__(self, *args, db_type_name: str, **kwargs):
        self.db_type_name = db_type_name
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["db_type_name"] = self.db_type_name
        return name, path, args, kwargs

    def db_type(self, connection) -> str:
        return self.db_type_name

    def cast_db_type(self, connection) -> str:
        return self.db_type_name

    def get_internal_type(self) -> str:
        # Лукапы и сравнения ведут себя как со строкой; тип колонки задаёт db_type.
        return "CharField"
