"""Ifyllning av ansökningsformulär i en övervakad webbläsare."""

from jobaplier.apply.form.applier import FormApplier
from jobaplier.apply.form.ats import AtsAdapter
from jobaplier.apply.form.fields import FIELD_PATTERNS, Applicant, classify_field
from jobaplier.apply.form.generic import (
    FillReport,
    FormAdapter,
    GenericAdapter,
    highlight,
)
from jobaplier.apply.form.reachmee import ReachMeeAdapter
from jobaplier.apply.form.teamtailor import TeamtailorAdapter
from jobaplier.apply.form.varbi import VarbiAdapter


def default_adapters() -> list[FormAdapter]:
    """Specifika adaptrar först; den generella tar allt annat och måste vara sist."""
    return [TeamtailorAdapter(), VarbiAdapter(), ReachMeeAdapter(), GenericAdapter()]


__all__ = [
    "FIELD_PATTERNS",
    "Applicant",
    "AtsAdapter",
    "FillReport",
    "FormAdapter",
    "FormApplier",
    "GenericAdapter",
    "ReachMeeAdapter",
    "TeamtailorAdapter",
    "VarbiAdapter",
    "classify_field",
    "default_adapters",
    "highlight",
]
