from django.apps import AppConfig
from django.db.backends.signals import connection_created


class CoreConfig(AppConfig):
    """Доменная схема: тенанты, оргструктура, справочники, зарплатные таблицы."""

    name = "core"
    verbose_name = "Ядро"

    def ready(self) -> None:
        # Без этого массивы enum-типов приезжают из базы строкой, а не списком —
        # см. core/db_types.py.
        from .db_types import on_connection_created

        connection_created.connect(on_connection_created, dispatch_uid="core.enum_types")
