"""Зарплатный движок: правила в конфигурации, не в коде."""

from .engine import (
    Component,
    Employee,
    PayrollEngine,
    Payslip,
    Timesheet,
    d,
    insured_base,
    uses_insured_hours,
)
from .presets import Origin, Preset, list_presets, load_preset
from .trace import TraceStep, explain

__all__ = [
    "Component",
    "Employee",
    "Origin",
    "PayrollEngine",
    "Payslip",
    "Preset",
    "Timesheet",
    "TraceStep",
    "d",
    "explain",
    "insured_base",
    "load_preset",
    "list_presets",
    "uses_insured_hours",
]
