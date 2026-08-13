"""Зарплатный движок: правила в конфигурации, не в коде."""

from .engine import (
    HOURS,
    Component,
    Employee,
    PayrollEngine,
    Payslip,
    Timesheet,
    d,
    insured_base,
    uses_insured_hours,
    work_measure,
)
from .presets import Origin, Preset, list_presets, load_preset, load_preset_body
from .trace import TraceStep, explain

__all__ = [
    "HOURS",
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
    "load_preset_body",
    "list_presets",
    "uses_insured_hours",
    "work_measure",
]
