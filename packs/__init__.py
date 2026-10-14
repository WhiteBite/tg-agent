"""Реестр декларативных пакетов извлечения."""

from packs.construction import PACK as CONSTRUCTION
from packs.services import PACK as SERVICES

PACKS: dict[str, dict] = {"construction": CONSTRUCTION, "services": SERVICES}
