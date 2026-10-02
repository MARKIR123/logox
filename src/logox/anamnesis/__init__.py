"""Anamnesis public boundary; importing data does not start a task or touch disk."""

from logox.anamnesis.models import AnamesisEvent, AnamesisStatus

__all__ = ["AnamesisEvent", "AnamesisStatus", "AnamesisService"]


def __getattr__(name: str):
    if name == "AnamesisService":
        from logox.anamnesis.service import AnamesisService

        return AnamesisService
    raise AttributeError(name)
