"""Sesiones secuenciales con `N` configurable y stop-after-current.

Spec 7.1: "La sesion es secuencial, tiene `N` configurable y al cancelarse
termina la batalla actual antes de parar." Solo una solicitud de
matchmaking puede estar activa a la vez (`POST /sessions` -> 409 si ya hay
una corriendo); `DELETE /sessions/{id}` setea stop-after-current y JAMAS
cancela la task de la batalla en curso.

Fase 3 Task 8 (D65 S6.2): el unico tipo de sesion es `SessionKind.LADDER`.
Antes de cada `Player.ladder(1)`, `SessionRunner` evalua el interlock
quintuple de ladder (mas el formato de aceptacion) y falla cerrado ANTES de
abrir socket o enviar `/search`: solo `official` + formato de aceptacion +
DB `acceptance` no canonica + `ladder_enabled` + `confirm` por llamada +
cuenta de testing confirmada. La generacion es siempre un parametro: el
formato exigido llega como `LadderGates.required_format`, nunca como literal
en codigo de produccion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ludex_agent.config import _reject_unsafe_official_database


class ActiveMatchmakingError(RuntimeError):
    """Ya hay una sesion de matchmaking activa; no se admite una segunda."""


class LadderInterlockError(RuntimeError):
    """Falta al menos una condicion del interlock de ladder (D65 S6.2).

    Se rechaza ANTES de abrir socket o enviar `/search`; nunca hay una
    llamada parcial a `Player.ladder(1)`.
    """


class SessionKind(str, Enum):
    """Tipo de sesion de matchmaking. En Fase 3 solo existe `LADDER`."""

    LADDER = "ladder"


class _LadderPlayer(Protocol):
    async def ladder(self, n_battles: int) -> None:
        ...


@dataclass(frozen=True)
class LadderGates:
    """Condiciones duras del interlock de ladder, todas exigidas a la vez.

    La generacion es un parametro: `required_format` es el formato de
    aceptacion que trae el llamador (la ruta, desde configuracion), nunca un
    valor fijo en este modulo. `database_url` se inspecciona por su
    host/port/path para rechazar la base canonica de Ludex.
    """

    connection_mode: str
    battle_format: str
    required_format: str
    database_role: str
    database_url: str
    ladder_enabled: bool
    confirm: bool
    testing_account_confirmed: bool

    def missing_interlocks(self) -> tuple[str, ...]:
        missing: list[str] = []
        if self.connection_mode != "official":
            missing.append("connection_mode=official")
        if self.battle_format != self.required_format:
            missing.append(f"battle_format={self.required_format}")
        if self.database_role != "acceptance":
            missing.append("database_role=acceptance")
        if not self.ladder_enabled:
            missing.append("ladder_enabled")
        if not self.confirm:
            missing.append("confirm")
        if not self.testing_account_confirmed:
            missing.append("testing_account_confirmed")
        return tuple(missing)


def check_ladder_interlocks(gates: LadderGates | None) -> None:
    """Falla cerrado si falta un interlock o si la DB oficial es insegura.

    Reusa el MISMO guardarrail de config (D65 S5.4, `_reject_unsafe_official_database`)
    en vez de reimplementar la prohibicion de DB canonica: en modo `official`
    exige `DATABASE_ROLE=acceptance` y DSN no canonico.
    """
    if gates is None:
        raise LadderInterlockError("sin interlocks configurados (fail-closed)")
    if gates.connection_mode == "official":
        try:
            _reject_unsafe_official_database(
                gates.connection_mode, gates.database_role, gates.database_url,
            )
        except RuntimeError as exc:
            raise LadderInterlockError(str(exc)) from exc
    missing = gates.missing_interlocks()
    if missing:
        raise LadderInterlockError(
            "faltan interlocks de ladder: " + ", ".join(missing)
        )


@dataclass
class SessionRunner:
    player: _LadderPlayer
    kind: SessionKind = SessionKind.LADDER
    gates: LadderGates | None = None
    _active: bool = field(default=False, init=False)
    _stop_requested: bool = field(default=False, init=False)
    _battles_played: int = field(default=0, init=False)

    async def start(self, n_battles: int) -> None:
        if self._active:
            raise ActiveMatchmakingError(
                "Ya hay una sesion de matchmaking activa"
            )
        if self.kind is SessionKind.LADDER:
            check_ladder_interlocks(self.gates)
        self._active = True
        self._stop_requested = False
        try:
            for _ in range(n_battles):
                if self._stop_requested:
                    break
                await self.player.ladder(1)
                self._battles_played += 1
        finally:
            self._active = False

    async def stop(self) -> None:
        """Marca stop-after-current. No cancela la batalla en curso."""
        self._stop_requested = True

    @property
    def battles_played(self) -> int:
        return self._battles_played

    @property
    def source(self) -> str:
        """Fuente a persistir en `battles.source` para las batallas de esta
        sesion: `ladder` para `SessionKind.LADDER` (D65 S6.2)."""
        if self.kind is SessionKind.LADDER:
            return "ladder"
        return self.kind.value
