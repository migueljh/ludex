"""Jugador que graba mientras juega.

Elige al azar entre las acciones legales: el entregable de esta rebanada no es
que juegue bien, es que grabe bien.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from poke_env import ServerConfiguration
from poke_env.battle.field import Field
from poke_env.battle.pokemon_type import PokemonType
from poke_env.battle.side_condition import STACKABLE_CONDITIONS, SideCondition
from poke_env.battle.weather import Weather
from poke_env.data import GenData
from poke_env.player import RandomPlayer

from ..state.actions import action_from_order, legal_actions
from ..state.serializer import serialize_battle
from ..graph.execute import execute_action
from ..db.pending_repository import PendingDecisionRecord
from ..hitl import (
    ApprovalKey,
    ApprovalMode,
    ApprovalPolicy,
    ApprovalProposal,
    ConnectionMode,
    EventHub,
    ProductionApprovalPolicy,
    create_gate,
)
from ..hitl.registry import ApprovalRegistry
from .protocol import (
    CURRENT_FRAME_SEQ,
    ProjectionTimeoutError,
    ProjectionAmbiguityError,
    ProtocolRecorder,
    RawFrameInbox,
    normalize_id,
    project_observable_state,
)


logger = logging.getLogger(__name__)


class ChoiceProtocolError(RuntimeError):
    """El protocolo de choices perdio una correlacion que Ludex necesita."""


class DecisionsClosedError(RuntimeError):
    """Una decision intento admitirse despues de `drain_inflight_decisions`.

    D46: la barrera es terminal para la instancia. Cualquier `choose_move`
    cuya coroutine arranca despues de que la barrera cerro la admision falla
    ruidosamente ACA, antes de tocar proyeccion, grafo o calc."""


@dataclass
class OutboundCommand:
    sequence: int
    message: str
    message_2: str | None
    phase: str

    @property
    def effective_message(self) -> str:
        return self.message_2 or self.message


@dataclass
class PendingChoice:
    decision_index: int
    attempt_index: int
    phase: str
    request_rqid: int | None
    request_frame_seq: int
    step_index: int
    step: dict | None
    outbound_seq: int | None = None
    outbound_message: str | None = None
    outbound_phase: str | None = None


_AUXILIARY_UNDO_ERRORS = {
    "[Invalid choice] There's nothing to cancel",
    (
        "[Invalid choice] Sorry, too late to cancel; "
        "the next turn has already started"
    ),
}

_FATAL_ROOM_CHOICE_ERRORS = {
    "[Invalid choice] There's nothing to choose",
    (
        "[Invalid choice] Sorry, too late to make a different move; "
        "the next turn has already started"
    ),
}


def battle_tag_from(split_messages: list[list[str]]) -> str | None:
    """El battle_tag llega como una linea `>battle-...`.

    Funcion pura y separada a proposito: es la unica logica de este modulo que
    se puede testear sin levantar un WebSocket, y de ella depende que un lote
    de protocolo se guarde o se pierda.
    """
    for parts in split_messages:
        if parts and parts[0].startswith(">"):
            return parts[0][1:].strip()
    return None


def local_server_configuration(ws_url: str) -> ServerConfiguration:
    """El server local corre con --no-security: la URL de auth no se usa."""
    return ServerConfiguration(
        ws_url, "https://play.pokemonshowdown.com/action.php?"
    )


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class PokeEnvVocabulary:
    """Traduce nombres del protocolo usando el dex y los enums de poke-env.

    Vive de este lado a proposito: `showdown/protocol.py` no importa poke-env,
    y asi el proyector se testea sin levantar nada. Ademas mantiene las
    inferencias legitimas ancladas al dex y a los enums de la libreria, nunca
    a una lista a mano (ver .claude/agent-recording/SKILL.md).

    La generacion es siempre un parametro: se instancia una por `battle.gen`.
    """

    def __init__(self, gen: int) -> None:
        data = GenData.from_gen(gen)
        self._gen = gen
        self._pokedex = data.pokedex
        self._moves = data.moves

    def species_types(self, species_id: str) -> list[str]:
        entry = self._pokedex.get(species_id) or {}
        # El serializador persiste los enums de poke-env por NOMBRE
        # (`PokemonType.DARK` -> "DARK"); el dex los trae capitalizados.
        return [str(t).upper() for t in entry.get("types", [])]

    def base_species(self, species_id: str) -> str:
        """Identidad canonica: la especie base del dex.

        Es el criterio de `Pokemon.identifies_as` (`pokemon.py:435-438`), que
        es como poke-env decide si dos nombres son el MISMO miembro del equipo.
        `baseSpecies` viene capitalizado para las formas alternativas
        ("Camerupt" para `cameruptmega`), asi que hay que normalizarlo.
        """
        entry = self._pokedex.get(species_id) or {}
        base = entry.get("baseSpecies")
        return normalize_id(str(base)) if base else species_id

    def _is_forme_change_forme(self, entry: dict) -> bool:
        """Mismo predicado que `_update_from_pokedex` (`pokemon.py:650-655`)."""
        forme = entry.get("forme")
        if not forme:
            return False
        forme = str(forme)
        return (
            forme.startswith("Mega")
            or forme in ("Primal", "Stellar", "Terastal")
            or forme.endswith("-Tera")
        )

    def forme_change_ability(self, species_id: str) -> str | None:
        """La ability de una forma Mega/Primal, que la property `ability` de
        poke-env prefiere sobre la revelada (`pokemon.py:861-871`)."""
        entry = self._pokedex.get(species_id) or {}
        if not self._is_forme_change_forme(entry):
            return None
        abilities = entry.get("abilities") or {}
        raw = abilities.get("0")
        return normalize_id(str(raw)) if raw else None

    def unique_ability(self, species_id: str) -> str | None:
        """La ability cuando el dex lista EXACTAMENTE una posible.

        Inferencia legitima y anclada al dex: si Zoroark solo puede tener
        Illusion, saberlo no es informacion oculta. poke-env hace lo mismo en
        `_update_from_pokedex` (`pokemon.py:658-661`), con el mismo corte de
        `gen >= 3`.
        """
        if self._gen < 3:
            return None
        entry = self._pokedex.get(species_id) or {}
        if self._is_forme_change_forme(entry):
            return None
        abilities = list((entry.get("abilities") or {}).values())
        if len(abilities) != 1:
            return None
        return normalize_id(str(abilities[0]))

    def move_max_pp(self, move_id: str) -> int | None:
        """PP maximo, con la misma cuenta que `Move.max_pp` (`move.py:476`)."""
        entry = self._moves.get(move_id) or {}
        pp = entry.get("pp")
        if not isinstance(pp, int):
            return None
        return pp * 8 // 5

    def type_name(self, raw: str) -> str | None:
        """Nombre de tipo narrado ("Water", "Flying") -> nombre del enum.

        Anclado a `PokemonType.from_name`, que es lo que usa poke-env para
        `typechange` (`pokemon.py:567-570`), y no a una tabla a mano.
        """
        try:
            return PokemonType.from_name(raw.strip()).name
        except (KeyError, ValueError, AttributeError):
            return None

    def weather_name(self, raw: str) -> str | None:
        if not raw or raw.lower() == "none":
            return None
        try:
            return Weather.from_showdown_message(raw).name
        except (KeyError, ValueError, AttributeError):
            return None

    def field_name(self, raw: str) -> str | None:
        try:
            return Field.from_showdown_message(raw).name
        except (KeyError, ValueError, AttributeError):
            return None

    def side_condition_name(self, raw: str) -> str | None:
        try:
            return SideCondition.from_showdown_message(raw).name
        except (KeyError, ValueError, AttributeError):
            return None

    def side_condition_is_stackable(self, raw: str) -> bool:
        try:
            return SideCondition.from_showdown_message(raw) in STACKABLE_CONDITIONS
        except (KeyError, ValueError, AttributeError):
            return False


# Margen de turnos mas alla de `decision_turn` (battle.turn capturado
# sincronicamente en choose_move, SIEMPRE <= el turno real) que
# `_find_action_line` esta dispuesta a mirar. Ver su docstring: sin este
# techo, una decision cuya accion no se ejecuto nunca (excusada: par, sueño,
# fallo) deja el cursor atrasado, y la busqueda de la SIGUIENTE decision
# puede "encontrar" por accidente una repeticion mucho mas adelante del
# mismo movimiento o especie (turnos aleatorios de gen6randombattle repiten
# nombres todo el tiempo: Stone Edge, Shadow Ball, volver a cambiar al mismo
# pokemon). Medido empiricamente: 2 turnos de margen no alcanzaba a evitarlo
# en cadenas largas de decisiones excusadas; ninguna decision real necesito
# jamas buscar mas de unos pocos turnos de margen.
ACTION_SEARCH_MARGIN_TURNS = 3

# D65 (MON-33 Task 3): las once columnas de metadata D38 (F2-08) que un
# `human_override` deja NULL como grupo y que `human_approved`/
# `timeout_auto` conservan completas. El envelope de la propuesta en
# `pending_decisions` se arma con estas mismas claves.
_D38_METADATA_KEYS = (
    "rationale", "confidence", "alternatives", "target", "provider", "model",
    "decision_latency_ms", "input_tokens", "output_tokens", "cached_input_tokens",
    "reasoning_tokens",
)


def _ident_matches(field: str, actor_species: str | None) -> bool:
    """Compara un campo `{side}a: Nombre` (ya extraido de una linea de
    protocolo, en la posicion que sea) contra el pokemon que actua.

    Extraida de `_actor_matches` (fix-cursor, D22) para reusarla en el
    respaldo de Encore (fix-flaky, D23), donde el nombre del OBJETIVO esta
    en `parts[4]` (`|move|{opp}a: Caster|Encore|{side}a: Target`), no en
    `parts[2]` como en `|cant|`/`|faint|`/confusion.
    """
    if actor_species is None:
        return True
    ident = field.split(": ", 1)[-1]
    return _normalize(ident) == actor_species


def _actor_matches(parts: list[str], actor_species: str | None) -> bool:
    """El respaldo (`|cant|`/`|faint|`/autogolpe por confusion) solo puede
    resolver la decision del pokemon que REALMENTE estaba en la cancha
    cuando se pidio la accion.

    Defecto 1 (fix-cursor, docs/DECISIONS.md D22): sin este chequeo, un
    `|faint|` (o un `|cant|`) de OTRO pokemon del mismo lado — en un turno
    posterior, dentro de una cadena de cambios sin ninguna relacion con la
    decision que se esta resolviendo — se acepta igual, porque el respaldo
    viejo solo miraba el lado (`p1a`/`p2a`), nunca el nombre. Eso adelanta
    el cursor global mas alla de la evidencia real de la decision SIGUIENTE,
    que entonces ya no puede encontrarla (verificado sobre datos reales:
    battle-gen6randombattle-398, decision 45 le robaba la linea a la 46).

    `actor_species is None` deja el chequeo deshabilitado (compatibilidad
    con llamadores/tests que no lo conocen): no reduce ninguna cobertura
    existente, solo AGREGA una condicion cuando el dato esta disponible.
    """
    if len(parts) < 3:
        return actor_species is None
    return _ident_matches(parts[2], actor_species)


def _illusion_revela_a(
    recorder: ProtocolRecorder, from_index: int, side: str, clave: str
) -> bool:
    """Confirma que un switch-in disfrazado por Illusion era en realidad
    `clave` (fix-flaky, D23): cazado en vivo en
    `battle-gen6randombattle-657` (Zoroark elegido, Showdown narra el
    cambio como `|switch|p1a: Drapion|...` — el nombre del ULTIMO
    companero de equipo vivo, nunca "Zoroark" — y solo revela la verdad 14
    turnos despues, con `|replace|p1a: Zoroark|Zoroark, ...` seguido de
    `|-end|p1a: Zoroark|Illusion`).

    Sin techo: a diferencia de cant/faint/confusion/Encore (que resuelven
    dentro de `ACTION_SEARCH_MARGIN_TURNS`), la Illusion se rompe cuando el
    pokemon disfrazado recibe cierto tipo de golpe, o nunca si no lo recibe
    — no hay ventana de turnos razonable que lo acote. Se corta apenas
    aparece OTRO `|switch|{side}a:` propio: esa racha ya termino sin
    revelarse, y seguir buscando mas alla arriesgaria confirmar la
    revelacion de un pokemon DISTINTO que entro despues (el mismo riesgo de
    "robo de linea" que el cursor global evita en los demas respaldos).
    """
    prefix_switch = f"|switch|{side}a:"
    prefix_replace = f"|replace|{side}a:"
    for _, line in recorder.entries_from(from_index):
        if line.startswith(prefix_switch):
            return False
        if line.startswith(prefix_replace):
            parts = line.split("|")
            if len(parts) < 4:
                return False
            especie = _normalize(parts[3].split(",", 1)[0])
            return especie == clave
    return False


def _request_propio_confirma_activo(line: str, side: str, clave: str) -> bool:
    """Confirma la especie activa desde el `|request|` privado propio.

    El JSON contiene los seis miembros del equipo, por lo que buscar
    `clave` como substring produciria falsos positivos para cualquier
    pokemon en banca. Solo cuenta el miembro con `active: true`, y solo si
    `side.id` coincide con el lado cuya decision se esta corrigiendo. Esto
    impide usar un request ajeno para desenmascarar la Illusion del rival.
    """
    prefix = "|request|"
    if not line.startswith(prefix):
        return False
    try:
        request = json.loads(line[len(prefix):])
    except (json.JSONDecodeError, TypeError):
        return False
    request_side = request.get("side")
    if not isinstance(request_side, dict) or request_side.get("id") != side:
        return False
    pokemon = request_side.get("pokemon")
    if not isinstance(pokemon, list):
        return False
    for member in pokemon:
        if not isinstance(member, dict) or member.get("active") is not True:
            continue
        ident = member.get("ident")
        if not isinstance(ident, str) or not ident.startswith(f"{side}: "):
            return False
        return _normalize(ident.split(": ", 1)[1]) == clave
    return False


def _find_action_line(
    recorder: ProtocolRecorder,
    side: str,
    action_taken: dict | None,
    from_index: int,
    max_turn: int,
    actor_species: str | None = None,
) -> tuple[int, int] | None:
    """Busca, desde una posicion GLOBAL en el protocolo (no solo un turno),
    la primera linea `|move|` o `|switch|` propia que menciona la accion
    elegida, sin pasarse de `max_turn`. Devuelve `(turno, indice_global_
    siguiente)` o `None`.

    D20 (C1, segunda vuelta) combina DOS salvaguardas, cada una necesaria por
    su cuenta:

    1. Cursor GLOBAL (`from_index`, no un rango de turnos): dos decisiones
       que mencionan la MISMA especie o el MISMO movimiento (dos turnos
       seguidos de Outrage por el bloqueo del movimiento) no pueden compartir
       la misma linea de protocolo. Se llama en orden estricto de decision
       (ver `LudexPlayer._correct_step_turns`), asi que la linea que
       "pertenece" a la decision N nunca puede reasignarsele a la N+1.
    2. Techo `max_turn` (`decision_turn + ACTION_SEARCH_MARGIN_TURNS`): sin
       el, una decision excusada (ver mas abajo) deja el cursor atrasado, y
       la busqueda de la decision SIGUIENTE puede escaparse muchos turnos
       hacia adelante y encontrar por accidente una repeticion NO
       relacionada del mismo nombre. `decision_turn` es `battle.turn`
       capturado sincronicamente al decidir: SIEMPRE es <= el turno real,
       nunca un techo falso.

    Si la accion no se ejecuto, Showdown SI deja rastro, y la primera version
    de esta funcion no lo miraba: `|cant|` cuando el juego impidio la accion
    (sueño, paralisis, congelamiento), `|faint|` propio cuando al pokemon lo
    debilitaron antes de poder actuar, y `|-activate|{side}a: Name|confusion`
    cuando el pokemon se autogolpeo por confusion en vez de ejecutar la
    accion elegida (defecto 1, fix-cursor: este tercer caso faltaba y NO es
    teorico, ver mas abajo). Ese rastro aparece en el bloque donde la
    decision se RESOLVIO, que puede ser el mismo turno que `decision_turn` o
    uno mas adelante. Sin mirarlo, esas filas quedaban etiquetadas un turno
    antes: era el 100% del residual que sobrevivio a la primera vuelta de C1.

    **fix-flaky (D23): dos formas mas, cazadas cazando el defecto conocido
    del residual de C1 en vivo (ver docs/DECISIONS.md D23), no teorizadas:**

    - **El rival nos encoreo antes de que nuestra propia accion se
      ejecutara**: `|move|{opp}a: Caster|Encore|{side}a: Name` nombrando a
      NUESTRO propio actor. Encore tiene prioridad y, si el rival lo usa el
      mismo turno en que nosotros ibamos a actuar, fuerza la repeticion de
      NUESTRO ultimo movimiento usado en vez de la accion recien elegida —
      Showdown nunca narra la accion elegida, solo la repeticion forzada
      (`battle-gen6randombattle-558`: Skarmory elige Spikes, Politoed lo
      encorea, Skarmory repite Stealth Rock; `battle-gen6randombattle-581`:
      mismo patron con Volbeat/Thunder Wave). Sin este respaldo, el cursor
      quedaba atrasado en `decision_turn` (nunca se corregia) en vez de
      avanzar al turno donde realmente se resolvio.

      **MON-21 (D45): el anuncio de Encore NO es, por si solo, la
      resolucion — es la CONFIRMACION de que la repeticion forzada que le
      sigue, en el MISMO turno, es la resolucion.** La primera version de
      este respaldo devolvia el cursor apenas encontraba la linea de
      Encore, dejando SIN CONSUMIR la linea `|move|{side}a:...|` real que
      Showdown narra a continuacion (la repeticion forzada en si). Bajo
      Encore + trapping esa linea sobrante queda ahi, disponible, y la
      SIGUIENTE decision (que bajo esta combinacion esta forzada a elegir el
      MISMO movimiento — Encore no deja otra opcion y el trapping le saca el
      cambio) la matchea por `clave` como si fuera su propia resolucion,
      heredando el turno de la decision ANTERIOR (`battle-gen6randombattle-
      3349`, decisiones 26/turno23 y 38/turno35, MON-11 R4). Peor: cuando el
      Encore del rival ocurre DESPUES de que nuestra propia accion de ESTE
      turno ya se resolvio (nada que confirmar en este bloque — el Encore
      solo bloqueara la decision futura), no hay ninguna repeticion en el
      mismo turno que lo confirme, y el respaldo no deberia anclar nada
      aca: la busqueda tiene que seguir hacia el turno donde la decision
      encoreada+atrapada realmente se resuelve. Por eso el Encore del rival
      ya NO fija `respaldo` de forma terminal: queda pendiente
      (`encore_rival_turno`) y solo se confirma —devolviendo el cursor
      DESPUES de la linea de repeticion, no antes— si esa repeticion propia
      aparece en el mismo turno; si el turno cambia sin ella, la busqueda
      sigue de largo sin anclarse ahi.
    - **La batalla termino antes de que la decision se resolviera**:
      `|win|`/`|tie|` en cualquier punto de la ventana de busqueda. Un
      `|win|`/`|tie|` cierra la trayectoria: no hay decision siguiente que
      pueda robarle esta linea, asi que no hace falta chequear actor ni lado.
      (`battle-gen6randombattle-571`: Raikou elige Substitute, pero Suicune
      se remata solo con el retroceso de Struggle en el turno siguiente y la
      batalla termina antes de que a Raikou le tocara jugar esa accion).

    De ahi la definicion que gobierna `turn_number` en el dataset:

        Una fila pertenece al turno en que su decision se RESOLVIO, sin
        importar como se resolvio — se ejecuto, el juego la impidio, al
        pokemon lo debilitaron antes, se autogolpeo por confusion, el rival
        lo encoreo primero, o la batalla termino antes de que le tocara.

    La linea de resolucion se usa solo como RESPALDO, nunca antes de agotar
    la busqueda del `|move|`/`|switch|` real: un `|faint|` propio puede ser
    posterior a una accion que si se ejecuto, y en ese caso la evidencia
    buena es el `|move|`, no el debilitamiento.

    **`actor_species`** (defecto 1, fix-cursor, D22): el respaldo (`|cant|`/
    `|faint|`/autogolpe) solo puede resolver la decision del pokemon que
    REALMENTE estaba en la cancha al pedirse la accion — ver `_actor_matches`.
    Sin este chequeo, cuando la accion elegida no deja NINGUNA de las tres
    evidencias dentro de su propio bloque (el caso real: un autogolpe por
    confusion, que esta funcion no reconocia), la busqueda seguia de largo y
    el respaldo viejo aceptaba el `|faint|`/`|cant|` de OTRO pokemon del
    mismo lado en un turno posterior — adelantando el cursor global mas alla
    de la evidencia real de la decision SIGUIENTE, que entonces ya no podia
    encontrarla. Verificado sobre datos reales
    (`battle-gen6randombattle-398`): la decision 45 (autogolpe de Muk, sin
    rastro reconocido) le robaba asi la linea a la decision 46 (un cambio a
    Ludicolo que si tenia su propia linea, dos turnos antes de donde el
    robo la dejo inalcanzable). Reconocer el autogolpe por confusion YA
    arregla ese caso puntual; el chequeo de `actor_species` cierra la clase
    entera (cualquier `|cant|`/`|faint|` de un pokemon que no es el que
    actua, sea cual sea la razon por la que su propia evidencia no aparecio).
    """
    if action_taken is None:
        return None
    clave = _normalize(action_taken.get("id") or action_taken.get("species") or "")
    if not clave:
        return None
    # Hidden Power: poke-env nombra la accion con el tipo (`hiddenpowerice`)
    # pero Showdown NUNCA lo narra — la linea dice solo "Hidden Power". Sin
    # este recorte la busqueda no matchea jamas y la fila se queda etiquetada
    # un turno antes. Recorte especifico a Hidden Power, no una regla generica
    # de prefijos: hay 17 Hidden Power que comparten el id base y una regla
    # amplia colapsaria movimientos que no tienen nada que ver.
    if clave.startswith("hiddenpower"):
        clave = "hiddenpower"
    accion_es_movimiento = action_taken.get("kind") == "move"
    opp_side = "p2" if side == "p1" else "p1"
    prefix_move = f"|move|{side}a:"
    prefix_switch = f"|switch|{side}a:"
    prefix_cant = f"|cant|{side}a:"
    prefix_faint = f"|faint|{side}a:"
    prefix_confusion = f"|-activate|{side}a:"
    prefix_opp_encore = f"|move|{opp_side}a:"
    respaldo: tuple[int, int] | None = None
    respaldo_turn: int | None = None
    encore_rival_turno: int | None = None
    request_activo_turn: int | None = None
    se_movio_en: set[int] = set()
    for offset, (turn, line) in enumerate(recorder.entries_from(from_index)):
        if turn > max_turn:
            break
        if respaldo is not None and turn != respaldo_turn:
            # Defecto 1 (fix-cursor, D22): ya encontramos, DENTRO del bloque
            # de esta decision, evidencia de que se resolvio SIN ejecutar el
            # movimiento/cambio elegido (cant/faint/confusion). Una decision
            # se resuelve una sola vez: un match "real" del mismo nombre en
            # un turno POSTERIOR no puede ser el de ESTA decision, tiene que
            # ser el de la SIGUIENTE, que repite el mismo movimiento (p.ej.
            # varios Encore seguidos porque el rival no tiene un movimiento
            # repetible que encorear). Sin este corte, la busqueda segui­a de
            # largo y esa repeticion posterior se aceptaba como si fuera la
            # resolucion de ESTA decision, robandole la linea a la que
            # realmente le pertenecia (verificado sobre datos reales,
            # battle-gen6randombattle-408: la decision "Encore" que se
            # resolvia con `|cant|...|frz` en su propio turno terminaba
            # apropiandose del `|move|...|Encore||[still]` de la decision
            # SIGUIENTE, dos turnos mas adelante).
            break
        if encore_rival_turno is not None and turn != encore_rival_turno:
            # MON-21 (D45): el Encore del rival que vimos no tuvo, dentro de
            # SU PROPIO turno, una repeticion propia que lo confirme -- no
            # intercepto esta decision (es residuo de una decision anterior
            # ya resuelta por otro camino, p.ej. un `|move|` real que ya se
            # ejecuto ANTES de que el rival encoreara). Dejar de tratarlo
            # como pendiente: no hay que anclar aca, la resolucion real de
            # esta decision esta mas adelante.
            encore_rival_turno = None
        if (
            not accion_es_movimiento
            and _request_propio_confirma_activo(line, side, clave)
        ):
            # El request llega antes de la narracion publica del turno. La
            # evidencia privada identifica al verdadero activo pese a
            # Illusion, pero el cursor se consume recien al encontrar el
            # `|switch|` propio asociado: devolver desde el request dejaria
            # esa linea disponible para que la decision siguiente la robe.
            request_activo_turn = turn
        elif line.startswith(prefix_move) or line.startswith(prefix_switch):
            # Ancla el match al TOKEN del movimiento/especie (`parts[3]`), no
            # a la linea entera. Hallazgo nuevo (F2-01, segunda ronda):
            # `clave in _normalize(line)` matcheaba de mas cuando el nombre
            # elegido aparecia dentro de un sufijo `[from] move: X` de OTRO
            # movimiento -- Sleep Talk llamando a Rest se narra
            # `|move|p1a: Spiritomb|Rest||[from] move: Sleep Talk|[still]`, y
            # buscar "sleeptalk" en esa linea entera matcheaba aunque la linea
            # es la resolucion de REST, no de un segundo Sleep Talk. Medido:
            # `battle-gen6randombattle-1925`, decision 32 se apropiaba de esa
            # linea (la misma que ya habia consumido la decision 31) y quedaba
            # con `turn_number=28` en vez de su turno real.
            move_parts = line.split("|")
            if len(move_parts) > 3 and clave in _normalize(move_parts[3]):
                return turn, from_index + offset + 1
            if line.startswith(prefix_move):
                se_movio_en.add(turn)
                if encore_rival_turno == turn:
                    # MON-21 (D45): esta es la repeticion forzada que el
                    # Encore del rival, visto antes EN EL MISMO turno,
                    # anunciaba -- aunque no mencione `clave` (la decision
                    # elegio otra cosa y Encore la reemplazo por el ultimo
                    # movimiento usado). Consumir ESTA linea, no la del
                    # anuncio: dejarla sin consumir es lo que la decision
                    # SIGUIENTE (forzada, bajo Encore+trapping, a elegir
                    # exactamente este mismo movimiento) terminaba robando
                    # como si fuera su propia resolucion.
                    return turn, from_index + offset + 1
            elif request_activo_turn == turn:
                return turn, from_index + offset + 1
            elif (
                not accion_es_movimiento
                and _illusion_revela_a(recorder, from_index + offset + 1, side, clave)
            ):
                # fix-flaky (D23): Illusion disfraza el switch-in con el
                # NOMBRE de otro miembro del equipo (Showdown narra
                # `|switch|{side}a: OtroNombre|...`, nunca el nombre real) —
                # el cambio SI se ejecuto, solo que la clave elegida
                # ("zoroark") nunca puede matchear contra esa linea. La unica
                # evidencia posible es la revelacion posterior
                # (`|replace|{side}a: Zoroark|Zoroark, ...`), que Showdown
                # manda cuando la Illusion se rompe — sin techo de turno
                # posible (`battle-gen6randombattle-657`: la revelacion
                # tardo 14 turnos, muy por fuera de `ACTION_SEARCH_MARGIN_
                # TURNS`), asi que esto es la UNICA excepcion de esta funcion
                # que no respeta `max_turn`. Es un match REAL (el cambio si
                # paso), no un respaldo de "no se ejecuto": por eso retorna
                # directo en vez de guardarse en `respaldo`.
                return turn, from_index + offset + 1
        elif respaldo is None:
            parts = line.split("|")
            if line.startswith(prefix_cant) and _actor_matches(parts, actor_species):
                respaldo = (turn, from_index + offset + 1)
                respaldo_turn = turn
            elif (
                line.startswith(prefix_faint)
                and turn not in se_movio_en
                and _actor_matches(parts, actor_species)
            ):
                # Un debilitamiento propio DESPUES de haberse movido en el
                # mismo bloque no resuelve esta decision: resuelve la de
                # alguien que ya actuo. El chequeo de actor descarta ademas
                # el debilitamiento de OTRO pokemon del mismo lado.
                respaldo = (turn, from_index + offset + 1)
                respaldo_turn = turn
            elif (
                line.startswith(prefix_confusion)
                and len(parts) >= 4
                and parts[3] == "confusion"
                and turn not in se_movio_en
                and _actor_matches(parts, actor_species)
            ):
                # Autogolpe por confusion: el pokemon eligio un movimiento
                # pero se golpeo a si mismo en su lugar. Showdown narra esto
                # como `|-activate|{side}a: Name|confusion`, nunca como
                # `|move|` ni `|cant|` — sin reconocerlo aca, la decision
                # queda sin ninguna evidencia dentro de su propio bloque y la
                # busqueda sigue de largo (ver docstring, defecto 1).
                respaldo = (turn, from_index + offset + 1)
                respaldo_turn = turn
            elif (
                accion_es_movimiento
                and line.startswith(prefix_opp_encore)
                and len(parts) >= 5
                and parts[3] == "Encore"
                and parts[4].startswith(f"{side}a:")
                and turn not in se_movio_en
                and _ident_matches(parts[4], actor_species)
            ):
                # fix-flaky (D23): el rival nos encoreo (prioridad +2) antes
                # de que nuestra propia accion elegida se ejecutara. Showdown
                # fuerza la repeticion de NUESTRO ultimo movimiento usado en
                # vez de narrar la accion recien elegida — no hay `|move|`
                # con la clave elegida en ningun lado. `parts[4]` (no
                # `parts[2]`, que es quien LANZA Encore) tiene el objetivo:
                # `|move|{opp}a: Caster|Encore|{side}a: Target`.
                #
                # `accion_es_movimiento`: Encore SOLO fuerza la repeticion de
                # un MOVIMIENTO, nunca bloquea un cambio — un pokemon
                # encoreado se puede cambiar libremente. Sin este chequeo,
                # una decision de CAMBIO real (que resuelve mas adelante, en
                # el turno del switch) se descartaba como si el Encore la
                # hubiera bloqueado, cortando la busqueda ANTES de encontrar
                # su linea real (medido en vivo: `battle-gen6randombattle-
                # 684`, decision 22, cambio a Kangaskhan-Mega que SI ocurre
                # un turno despues; el respaldo de Encore, sin este chequeo,
                # se apropiaba del turno de la decision y la busqueda cortaba
                # ahi, sin llegar nunca al `|switch|` real).
                #
                # MON-21 (D45): a diferencia de cant/faint/confusion/win/tie,
                # este anuncio NO fija `respaldo` de forma terminal -- solo
                # CONFIRMA que la proxima linea `|move|{side}a:...|` en este
                # mismo turno (arriba, junto a `se_movio_en`) es la
                # resolucion real. Si esa linea no aparece antes de que el
                # turno cambie, el chequeo de arriba limpia
                # `encore_rival_turno` y la busqueda sigue de largo.
                encore_rival_turno = turn
            elif line.startswith("|win|") or line.startswith("|tie|"):
                # fix-flaky (D23): la batalla termino antes de que la
                # decision se resolviera (el rival se remato con retroceso,
                # o cualquier otro cierre repentino). No hace falta chequear
                # actor ni lado: un `|win|`/`|tie|` cierra la trayectoria
                # entera, asi que no hay decision siguiente a la que
                # robarle esta linea.
                respaldo = (turn, from_index + offset + 1)
                respaldo_turn = turn
    return respaldo


class LudexPlayer(RandomPlayer):
    """RandomPlayer que captura protocolo crudo y estado por turno.

    Un ProtocolRecorder por battle_tag, nunca compartido entre jugadores: el
    |request| de cada uno trae su propio equipo.

    ## C-1 (review de merge) — por que la materializacion vuelve a ser
    SINCRONICA, y por que ESO no resucita la carrera que esta reescritura
    reemplaza (ver docs/DECISIONS.md D22 para la version completa)

    El diseño anterior (D20, commit `3ea7caf`) diferia la serializacion
    entera —`legal_actions` INCLUIDO— a una task de fondo (`asyncio.create_
    task`) que esperaba, con un timeout de 0.2s, a que llegara narracion
    nueva antes de llamar a `serialize_battle(battle)`. El problema no era el
    timeout: es que `battle` es un objeto MUTABLE compartido, y para cuando
    esa task por fin corre, el planificador de asyncio puede haber
    despachado YA la SIGUIENTE decision (`choose_move` de la N+1) antes de
    que la task de la decision N llegara a ejecutarse. La foto que se toma
    en ese momento es la del punto de decision N+1, no la de N: la fila queda
    con `action_taken` de N y `legal_actions`/`state` de N+1. Medido contra
    la base real: 7 de 6625 filas (~0.11%) con `action_taken` fuera de su
    propia `legal_actions` — un piso, no un techo, porque es una carrera
    contra el planificador, no un desfase sistematico: en una maquina
    cargada, con GC pausando el loop, o con mas batallas concurrentes, puede
    empeorar sin que nada lo acote.

    La correccion: **nada que tenga que ser consistente con la decision se
    lee de `battle` despues de que la decision paso**. `choose_move` captura
    TODO lo que define la decision (`legal_actions`, `action_taken`,
    `decision_turn`) en el mismo instante sincronico de siempre — eso nunca
    fue el problema, `battle.available_moves`/`available_switches` ya
    reflejan el estado correcto en ese momento porque vienen del `|request|`,
    que `parse_request` ya proceso antes de que poke-env llame a
    `choose_move` (ver `player.py:289-294` en poke-env).

    ## D31 (MON-6) — CORRECCION: la narracion NO viene en el mismo lote

    La version anterior de este docstring afirmaba que Showdown narra el turno
    "DESPUES del `|request|`, en el MISMO lote", y sobre esa premisa se
    construyo `_finalize_pending_steps`. **Es falso, y medido**: son frames de
    websocket DISTINTOS, cada uno con su propia task
    (`asyncio.create_task` por frame en `ps_client.listen`).

    Por eso `_finalize_pending_steps` no arreglaba nada: corria al final del
    lote del `|request|`, cuando la narracion todavia no habia llegado. El
    resultado medido sobre el corpus real: en 297 de 762 decisiones (39.0%)
    el snapshot le informaba al proveedor un pokemon activo rival
    EQUIVOCADO, y 270 de esas 297 (90.9%) mostraban exactamente el activo de
    la decision anterior.

    Lo que si es cierto, y lo que sostiene el diseño actual (sonda causal en
    `docs/superpowers/specs/2026-07-29-f2-01-prelock-snapshot-design.md`):

    - `NARR(k)`, la narracion que resuelve la decision ANTERIOR, llega al
      socket 0.022-5.557 ms despues del `|request|` y **antes** de que
      enviemos nuestra eleccion. Demorar la eleccion 500 ms no la mueve
      (76/76 decisiones). **No depende de nuestra respuesta.**
    - `NARR(k+1)`, la que resuelve la decision ACTUAL, si depende: llega
      recien 4-45 ms DESPUES del envio. Esperar esa es el punto muerto que
      documenta `.claude/agent-recording/SKILL.md`, y sigue prohibido.
    - Lo que mantiene a `NARR(k)` fuera de `Battle` no es el cable sino el
      lock por batalla de poke-env (`ps_client.py:171-176`): su task ya
      arranco, pero queda encolada esperando el mismo lock que esta decision
      mantiene abierto (medido: bloqueada 501 ms con un hold de 500 ms).

    De ahi el camino pre-lock: un observador envuelve
    `PSClient._handle_message` y publica el frame CRUDO en `frame_inbox`
    ANTES de que el original intente adquirir el lock. La decision espera esa
    señal —nunca a `Battle` ni al recorder, que solo avanzan bajo el lock— y
    aplica una proyeccion pura sobre el snapshot inmutable.

    ## Lo que D20/D22 dejaron bien y se conserva

    `choose_move` captura TODO lo que define la decision (`legal_actions`,
    `action_taken`, `decision_turn`, mapa accion->`BattleOrder`) en el mismo
    instante sincronico, antes del primer `await`. `action_taken in
    legal_actions` vale por CONSTRUCCION porque las dos salen de la misma
    linea de codigo. Y nada que deba ser consistente con la decision se relee
    de `battle` despues: la proyeccion sale del protocolo crudo (D17), nunca
    del objeto mutable. Eso no cambia; lo que se corrige es la premisa de
    DISPONIBILIDAD, no la disciplina.
    """

    # Un retry de Ludex siempre vuelve a pasar por `choose_move`: el default
    # aleatorio de poke-env (0.1%) enviaria una orden sin snapshot ni slot.
    DEFAULT_CHOICE_CHANCE = 0.0

    def __init__(
        self,
        *args: Any,
        decision_graph: Any = None,
        decision_budget_seconds: float = 240,
        projection_timeout_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        # MON-35 (Fase 3 Task 5): gate HITL integrado entre `ainvoke` y
        # `execute_action` (D65 S3.1). Todo lo que sigue es inyectable y
        # tiene defaults no-bloqueantes: la politica de produccion saltea
        # SIEMPRE `local`, asi que una construccion desnuda nunca gatea.
        # `run`/`benchmark`/`matrix-run` pasan `NeverGateApprovalPolicy`
        # explicitamente (cli.py); los tests inyectan una politica que
        # gatea para ejercer HITL contra un Showdown local.
        approval_policy: ApprovalPolicy | None = None,
        connection_mode: ConnectionMode = "local",
        approval_mode: ApprovalMode = "hitl",
        approval_timeout_seconds: float = 10.0,
        pending_repository: Any | None = None,
        approval_registry: ApprovalRegistry | None = None,
        event_hub: EventHub | None = None,
        **kwargs: Any,
    ) -> None:
        # El listener se arranca DESPUES de instalar el observador pre-lock:
        # si poke-env lo arrancara desde su propio __init__, el primer frame
        # podria entrar sin observar. Se preserva lo que pidio el caller: si
        # pidio False, no se arranca nada.
        caller_wants_listening = kwargs.pop("start_listening", True)
        super().__init__(*args, start_listening=False, **kwargs)
        self.decision_graph = decision_graph
        self.decision_budget_seconds = decision_budget_seconds
        self.projection_timeout_seconds = projection_timeout_seconds
        # F2-10: el reloj que gobierna deadlines y métricas es inyectable.
        # En producción es `time.monotonic`; en tests usamos un reloj falso.
        self._clock = clock
        self.recorders: dict[str, ProtocolRecorder] = defaultdict(ProtocolRecorder)
        self.steps: dict[str, list[dict | None]] = defaultdict(list)
        # Frames crudos publicados ANTES del lock por batalla. Es lo unico
        # que una decision puede esperar sin trabarse (ver docstring).
        self.frame_inbox = RawFrameInbox()
        # Ultima proyeccion publica valida por tag. La reusa un reintento
        # tras una eleccion rechazada, donde no hay resolucion nueva que
        # esperar.
        self._last_projection: dict[str, dict] = {}
        # MON-26: watermark de frames entregados a la proyeccion, POR TAG:
        # seq del ultimo frame de la ventana consumida por la decision
        # anterior de ESTA batalla. Se inicializa en 0 (nada consumido) y
        # avanza en `_resolve_state`. Un reintento NO consume frames y por
        # eso no lo toca.
        self._projected_until: dict[str, int] = {}
        # Memoria PUBLICA entre decisiones, por tag e identidad canonica:
        # tipos/ability/moves persistentes que un typechange o un Transform
        # tapan temporalmente. `switch_out` (`pokemon.py:600-612`) en
        # poke-env nunca resetea `_type_1`/`_type_2`, solo los overrides
        # temporales -- sin esta memoria vivendo MAS ALLA de una sola
        # proyeccion, la version anterior perdia los tipos de una Mega o el
        # moveset/ability propios de un Transform en cuanto la decision que
        # los aplico terminaba (finding 1, TECH LEAD REVIEW sobre `b784bcc`).
        self._temporary_state: dict[str, dict[str, dict]] = {}
        # Marca explicita de "el proximo choose_move de este tag es un
        # reintento". Se pone al ver el `|error|` y se consume UNA sola vez.
        self._retry_pending: dict[str, bool] = {}
        # F2-02: una decision canonica puede tener varios intentos, pero solo
        # existe un slot final. La identidad vive fuera del dict persistible.
        self._pending_choices: dict[str, PendingChoice] = {}
        self._request_heads: dict[str, tuple[int | None, int]] = {}
        self._outbound_sequences: dict[str, int] = defaultdict(int)
        self._last_outbound: dict[str, OutboundCommand] = {}
        self._terminal_failures: dict[str, ChoiceProtocolError] = {}
        self.rejected_choice_count: int = 0
        self.auxiliary_command_error_count: int = 0
        self.fatal_choice_error_count: int = 0
        self._vocabularies: dict[int, PokeEnvVocabulary] = {}
        # Contador consultable: cuantas decisiones fallaron cerrado porque la
        # narracion previa no llego a tiempo. Distinto de cero es fallo, no
        # warning.
        self.projection_timeout_count: int = 0
        # Contador consultable, mismo espiritu que el anterior: cuantas
        # decisiones fallaron cerrado porque un evento nombraba a un pokemon
        # propio que ya no era el activo del snapshot (Transform/Reflect
        # Type/-copyboost con nickname ambiguo o no resoluble).
        self.projection_ambiguity_count: int = 0
        self._listener_started = False
        self._install_outbound_observer()
        self._install_prelock_observer()
        if caller_wants_listening:
            self.start_listening()
        # player_role no cambia en el curso de una batalla: capturarlo UNA
        # vez en choose_move deja a _correct_step_turns sin necesidad de
        # tocar `self.battles`/`battle` para nada.
        self._sides: dict[str, str] = {}
        # I3 (review de merge): contador consultable de pasos perdidos, para
        # que "no hay huecos hoy" (0 huecos de decision_index medidos sobre
        # toda la base) deje de depender solo de que nadie mire. Se
        # incrementa en el gate de `cli._persist_one`, despues de corregir
        # turnos pero ANTES del primer `save_step`: un slot `None`, un estado
        # incompleto o una choice que no llego a `resolved` invalida TODA la
        # trayectoria y no puede dejar escrituras parciales ni reward. Ese es
        # el unico lugar que cuenta para no inflar dos veces la misma perdida.
        # Es el mismo rol que
        # cumpliria un `turn_alignment_timeouts`: un numero que un runner
        # desatendido (miles de batallas, `agent play -n grande`) puede
        # loguear o alertar sin tener que grepear el log linea por linea.
        self.lost_step_count: int = 0
        # poke-env procesa mensajes en `ps_client.loop`, que puede vivir en
        # otro hilo distinto del loop que espera `battle_against`. Un Future
        # de `concurrent.futures` permite publicar el fallo entre ambos loops
        # sin depender de que la task interna de poke-env sea awaited.
        self._background_failure: concurrent.futures.Future[Exception] = (
            concurrent.futures.Future()
        )
        # D46/MON-23: ownership propio del ciclo de vida de una decision.
        # `choose_move` corre como task fire-and-forget de `listen()` en
        # `ps_client.loop` (POKE_LOOP en produccion), hermana -- no hija --
        # de la task de `battle_against`. Cancelar `battle_against` nunca
        # cancela esta task; sin este registro nadie sabe si una decision
        # (proyeccion, grafo/calc, ejecucion) sigue en vuelo cuando un
        # caller cierra `CalcClient`. Registrado/liberado desde DENTRO de la
        # coroutine que `choose_move` devuelve (`run_random`/`run_graph`),
        # nunca via `PSClient._active_tasks`.
        self._decision_tasks: set[asyncio.Task[Any]] = set()
        # Terminal: una vez True, ninguna decision nueva se admite. Sólo se
        # lee/escribe desde codigo que corre en `ps_client.loop`
        # (`_admit_decision`, que corre dentro de la decision misma; y
        # `_drain_on_poke_loop`, agendada ahi vía `run_coroutine_threadsafe`)
        # -- nunca hay dos escritores concurrentes de threads distintos.
        self._decisions_closed = False
        # MON-20 DIAG-A: ETAPA actual de cada decision en vuelo, por task.
        # Las decisiones corren como tasks fire-and-forget en
        # `ps_client.loop` (POKE_LOOP en produccion), hermanas de
        # `battle_against`: `asyncio.all_tasks()` desde el loop del caller
        # no las ve y `PendingChoice.phase` solo habla de la correlacion de
        # choices, no del grafo. Sin este registro, una decision atascada en
        # un await SQL sin deadline (context_repository) o en el proveedor
        # es INVISIBLE hasta el timeout de la batalla. Escritura SOLO desde
        # codigo que corre en `ps_client.loop` (los nodos del grafo, dentro
        # de la task de la decision); lectura desde
        # `decision_snapshot()`, que tambien agenda su captura ahi. Nunca
        # serializa prompts, credenciales, respuestas, filas ni payloads.
        self._decision_stages: dict[asyncio.Task[Any], str] = {}
        # Idempotencia cross-loop de `drain_inflight_decisions`, mismo patron
        # que `_background_failure`: un `concurrent.futures.Future` no esta
        # atado a ningun loop, así que un segundo llamador (de cualquier
        # loop) puede esperar el MISMO drenaje en vez de disparar otro.
        self._drain_future: concurrent.futures.Future[None] | None = None
        # MON-35: componentes del gate HITL. La politica decide si el gate
        # existe; en modo skip no se construye ningun PendingApproval y por
        # lo tanto ningun Future. `_pending_repository` es el UNICO escritor
        # de `pending_decisions` y solo corre en POKE_LOOP (D65 S3.3): si la
        # politica exige gate y no hay repositorio inyectado, la decision
        # falla CERRADA (la auditoria no puede desaparecer en silencio).
        # Registry/hub por defecto: instancias frescas en memoria (puros,
        # sin loop); el runner oficial inyecta los compartidos con la API.
        if approval_policy is None:
            approval_policy = ProductionApprovalPolicy()
        self._approval_policy = approval_policy
        self._connection_mode = connection_mode
        self._approval_mode = approval_mode
        self._approval_timeout_seconds = approval_timeout_seconds
        self._pending_repository = pending_repository
        self._approval_registry = (
            approval_registry if approval_registry is not None
            else ApprovalRegistry()
        )
        self._event_hub = event_hub if event_hub is not None else EventHub()

    async def wait_for_background_failure(self) -> Exception:
        return await asyncio.shield(
            asyncio.wrap_future(self._background_failure)
        )

    def _admit_decision(self) -> asyncio.Task[Any]:
        """Registra la task ACTUAL como decision en vuelo.

        Debe ser la primera linea sincronica del cuerpo de la coroutine que
        `choose_move` devuelve, antes de tocar proyeccion/grafo/calc. Es
        atomica respecto de `_drain_on_poke_loop` porque ninguna de las dos
        funciones yieldea el loop entre chequear `_decisions_closed` y
        actuar sobre `_decision_tasks`: en un loop de asyncio de un solo
        hilo, dos bloques sincronicos nunca se intercalan, así que no puede
        colarse una decision nueva entre que el drenaje cierra la admision y
        captura su foto del conjunto.
        """
        if self._decisions_closed:
            raise DecisionsClosedError(
                "LudexPlayer ya cerro la admision de decisiones "
                "(drain_inflight_decisions ya corrio); esta decision no "
                "puede alcanzar calc"
            )
        task = asyncio.current_task()
        assert task is not None, (
            "_admit_decision solo se puede llamar desde dentro de una task"
        )
        self._decision_tasks.add(task)
        return task

    def _release_decision(self, task: asyncio.Task[Any]) -> None:
        self._decision_tasks.discard(task)
        self._decision_stages.pop(task, None)
        # Los nodos del grafo corren en tasks propias (executor de
        # LangGraph) y pueden haber dejado un stage registrado: se barren
        # las terminadas. `_release_decision` corre en `ps_client.loop`
        # despues de que `ainvoke` retorno, asi que para entonces estan
        # todas done.
        for done in [t for t in self._decision_stages if t.done()]:
            self._decision_stages.pop(done, None)

    def _set_decision_stage(self, task: asyncio.Task[Any], stage: str) -> None:
        """Registra la etapa actual de la decision `task`.

        SOLO desde codigo que corre en `ps_client.loop` (dentro de la task
        de la decision): es el mismo regimen de hilos que `_decision_tasks`.
        """
        self._decision_stages[task] = stage

    def record_decision_stage(self, stage: str) -> None:
        """Callback para los nodos del grafo (`build_decision_graph`).

        LangGraph ejecuta cada nodo en una task PROPIA (el executor crea
        una por paso), asi que `asyncio.current_task()` aca es la task del
        nodo — la que tiene el chain de awaits profundo con la linea exacta
        que la decision espera. Se registra bajo ESA task: es lo que
        `decision_snapshot()` busca. Un nodo que no corra dentro de una
        task (tests, invocacion directa del grafo) se ignora.
        """
        task = asyncio.current_task()
        if task is not None:
            self._decision_stages[task] = stage

    async def _capture_decision_snapshot(self) -> list[dict[str, Any]]:
        """Cuerpo del snapshot. Corre ENTERO en `ps_client.loop`.

        Captura tasks + etapas + stacks desde el MISMO loop que los escribe
        (regimen de hilos identico al del drain). Los frames se reducen a
        (module, function, line): NUNCA se serializan `f_locals` — una
        variable local de un frame del proveedor puede contener una clave
        API, y los frames de contexto traen filas y payloads.

        `Task.get_stack()` solo expone el frame del coroutine raiz de la
        task; el chain real de awaits (run_graph -> ainvoke -> nodo ->
        repositorio) se camina con `cr_await` sobre `task.get_coro()`, que
        es lo que identifica la LINEA EXACTA que la decision espera.
        """
        entries: list[dict[str, Any]] = []
        for task in list(self._decision_tasks) + [
            t for t in self._decision_stages if t not in self._decision_tasks
        ]:
            if task.done():
                continue
            frames: list[dict[str, str | int]] = []
            current = task.get_coro()
            while current is not None:
                # Los chains de await mezclan coroutines (`cr_frame`/
                # `cr_await`) con async generators (`ag_frame`/`ag_await`,
                # p.ej. el `astream` de LangGraph).
                frame = getattr(current, "cr_frame", None) or getattr(
                    current, "ag_frame", None
                )
                if frame is not None:
                    frames.append({
                        "module": frame.f_globals.get("__name__", ""),
                        "function": frame.f_code.co_name,
                        "line": frame.f_lineno,
                    })
                current = getattr(current, "cr_await", None) or getattr(
                    current, "ag_await", None
                )
            entries.append({
                "task_id": id(task),
                "stage": self._decision_stages.get(task),
                "frames": frames,
            })
        return entries

    async def decision_snapshot(self) -> list[dict[str, Any]]:
        """Estado observado de las decisiones en vuelo, para diagnosticos.

        Devuelve una entrada por decision registrada: `task_id`, `stage`
        (la etapa del grafo en la que esta, o `None` si todavia no marco
        ninguna) y `frames` sanitizados (module/function/line) que
        identifican la LINEA EXACTA que la decision esta esperando — el
        stack de `ps_client.loop`, no el del caller.

        Diagnostico puro: no cancela, no espera, no cambia el flujo. El
        proximo diagnostico en vivo (MON-20 DIAG-A) la usa en checkpoints
        fijos para localizar un cuelgue antes del timeout de la batalla.
        """
        future = asyncio.run_coroutine_threadsafe(
            self._capture_decision_snapshot(), self.ps_client.loop
        )
        return await asyncio.wrap_future(future)

    async def _drain_on_poke_loop(self) -> None:
        """Cuerpo real del drenaje. Corre ENTERO en `ps_client.loop`.

        Cerrar la admision y capturar el conjunto en vuelo son un solo
        bloque sincronico (sin `await` entre medio): ninguna decision que
        recien esté arrancando puede ver `_decisions_closed is False` y
        registrarse despues de que esta funcion ya tomó su foto.
        """
        self._decisions_closed = True
        pending = list(self._decision_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def drain_inflight_decisions(self) -> None:
        """Barrera publica, terminal, idempotente y cross-loop-safe.

        Cierra la admision de decisiones nuevas, cancela toda decision en
        vuelo (proyeccion, grafo/calc, ejecucion incluidos, porque todo eso
        vive dentro de la MISMA coroutine que `_admit_decision` registro) y
        espera su finalizacion antes de retornar. Los callers que comparten
        `CalcClient`/`ContextRepository` con el grafo de decision DEBEN
        invocarla antes de cerrarlos (D46/MON-23).

        Es seguro llamarla mas de una vez (idempotente) y con cero
        decisiones en vuelo (conjunto vacio). Es terminal: despues de la
        primera vez, esta instancia nunca vuelve a admitir decisiones.
        """
        if self._drain_future is None:
            self._drain_future = asyncio.run_coroutine_threadsafe(
                self._drain_on_poke_loop(), self.ps_client.loop
            )
        await asyncio.wrap_future(self._drain_future)

    def _publish_background_failure(self, exc: Exception) -> None:
        if not self._background_failure.done():
            self._background_failure.set_result(exc)

    def _install_outbound_observer(self) -> None:
        """Correlaciona cada comando de batalla antes del primer `await`.

        El original se invoca exactamente una vez. Un fallo de websocket
        deja la fase `failed`, nunca un intento ficticiamente `sent`.
        """
        client = self.ps_client
        self._send_message_original = client.send_message

        async def observed_send(
            message: str, room: str = "", message_2: str | None = None
        ) -> Any:
            tag = room if room.startswith("battle-") else None
            outbound: OutboundCommand | None = None
            pending: PendingChoice | None = None
            if tag is not None:
                sequence = self._outbound_sequences[tag] + 1
                self._outbound_sequences[tag] = sequence
                outbound = OutboundCommand(
                    sequence=sequence,
                    message=message,
                    message_2=message_2,
                    phase="sending",
                )
                self._last_outbound[tag] = outbound
                if outbound.effective_message.startswith("/choose"):
                    pending = self._pending_choices.get(tag)
                    if pending is None or pending.phase not in ("reserved", "retried"):
                        exc = ChoiceProtocolError(
                            f"{tag}: /choose outbound sin decision reservada "
                            f"correlacionable (phase={getattr(pending, 'phase', None)!r})"
                        )
                        self.fatal_choice_error_count += 1
                        self._publish_background_failure(exc)
                        raise exc
                    if pending.outbound_seq is not None:
                        exc = ChoiceProtocolError(
                            f"{tag}: decision {pending.decision_index} intento "
                            f"{pending.attempt_index} envio mas de un /choose"
                        )
                        self.fatal_choice_error_count += 1
                        self._publish_background_failure(exc)
                        raise exc
                    pending.outbound_seq = sequence
                    pending.outbound_message = outbound.effective_message
                    pending.outbound_phase = "sending"
            try:
                result = await self._send_message_original(message, room, message_2)
            except Exception as exc:
                if outbound is not None:
                    outbound.phase = "failed"
                if pending is not None and outbound is not None:
                    if pending.outbound_seq == outbound.sequence:
                        pending.outbound_phase = "failed"
                self._publish_background_failure(exc)
                raise
            if outbound is not None:
                outbound.phase = "sent"
            if pending is not None and outbound is not None:
                if pending.outbound_seq == outbound.sequence:
                    pending.outbound_phase = "sent"
            return result

        client.send_message = observed_send

    def _install_prelock_observer(self) -> None:
        """Envuelve `PSClient._handle_message` para publicar el frame crudo
        ANTES de que el original intente adquirir `_battle_locks[tag]`.

        Es el unico punto de poke-env 0.15.0 que corre dentro de la task del
        frame y antes del lock (`ps_client.py:156-176`). Se ENVUELVE, no se
        reemplaza: el original se sigue llamando con el mismo mensaje, sin
        consumir, filtrar ni reordenar nada. `listen()` no se copia ni se
        toca.

        `tests/showdown/test_pokeenv_contract.py` falla ruidosamente si
        poke-env mueve el lock o cambia esta firma.
        """
        client = self.ps_client
        original = client._handle_message
        inbox = self.frame_inbox

        async def observed(message: str) -> Any:
            if message.startswith(">battle"):
                lines = tuple(message.split("\n"))
                tag = lines[0][1:].strip()
                if tag:
                    frame = await inbox.publish(tag, lines)
                    # Cada frame corre en su propia task, con su propia copia
                    # del contexto: asi `choose_move` sabe cual es SU frame
                    # aunque haya varios encolados detras del lock.
                    CURRENT_FRAME_SEQ.set(frame.seq)
            return await original(message)

        client._handle_message = observed

    def start_listening(self) -> None:
        """Arranca el listener que `__init__` difirio. Idempotente."""
        if self._listener_started:
            return
        self._listener_started = True
        client = self.ps_client
        client._listening_coroutine = asyncio.run_coroutine_threadsafe(
            client.listen(), client.loop
        )

    def _vocabulary(self, gen: int) -> PokeEnvVocabulary:
        vocabulary = self._vocabularies.get(gen)
        if vocabulary is None:
            vocabulary = PokeEnvVocabulary(gen)
            self._vocabularies[gen] = vocabulary
        return vocabulary

    async def _handle_battle_message(self, split_messages: list[list[str]]) -> Any:
        tag = battle_tag_from(split_messages)
        if tag is None and len(self.recorders) == 1:
            # Rama de respaldo: hoy inalcanzable, porque poke-env garantiza el
            # tag en la primera linea. Se conserva por si esa garantia cambia.
            tag = next(iter(self.recorders))
        if tag is None:
            # I1: nunca descartar en silencio. El protocolo es la fuente de
            # verdad (D17) y perder un lote rompe la re-derivacion del estado
            # para esa batalla entera. Un warning se puede ignorar; esto no.
            raise RuntimeError(
                f"lote de protocolo sin battle_tag, {len(split_messages)} lineas "
                "descartadas: se perderia la fuente de verdad de esa batalla"
            )
        # Grabado ENTERO, de una sola vez, ANTES de delegar en super(): para
        # cuando super() dispare choose_move a mitad de este mismo lote, el
        # recorder ya tiene TODAS las lineas del lote (incluida la narracion
        # que todavia no proceso poke-env). Esto es lo que permite que
        # _correct_step_turns (que solo lee el recorder, nunca `battle`)
        # jamas quede corriendo por detras de lo que ya se grabo.
        self.recorders[tag].record(split_messages)
        terminal = any(
            len(parts) > 1 and parts[1] in ("win", "tie")
            for parts in split_messages
        )
        deinit = any(
            len(parts) > 1 and parts[1] == "deinit" for parts in split_messages
        )
        try:
            delegated: list[list[str]] = []
            for parts in split_messages:
                if len(parts) > 2 and parts[1] == "request" and parts[2]:
                    self._observe_request(tag, parts[2])
                if len(parts) > 2 and parts[1] == "error":
                    classification = self._classify_choice_error(tag, parts[2])
                    if classification == "rejection":
                        self._reject_pending_choice(tag)
                    elif classification == "auxiliary":
                        self.auxiliary_command_error_count += 1
                        last = self._last_outbound[tag]
                        logger.warning(
                            "choice_auxiliary_error battle_tag=%s command=%s "
                            "outbound_seq=%d error=%r count=%d",
                            tag,
                            last.effective_message,
                            last.sequence,
                            parts[2],
                            self.auxiliary_command_error_count,
                        )
                        # El recorder ya conserva el frame integro. Esta unica
                        # linea no atraviesa el retry demasiado amplio de
                        # poke-env; las demas se delegan sin cambio.
                        continue
                    elif classification == "fatal":
                        raise self._fatal_choice_error(tag, parts[2])
                if len(parts) > 1 and parts[1] in ("win", "tie"):
                    self._resolve_terminal_choice(tag, parts[1])
                if len(parts) > 1 and parts[1] == "deinit":
                    pending = self._pending_choices.get(tag)
                    if pending is not None:
                        raise self._fatal_choice_error(
                            tag,
                            "deinit con decision pendiente "
                            f"index={pending.decision_index} phase={pending.phase}",
                        )
                delegated.append(parts)

            result = await super()._handle_battle_message(delegated)
        except Exception as exc:
            self._publish_background_failure(exc)
            if deinit:
                if isinstance(exc, ChoiceProtocolError):
                    self._terminal_failures[tag] = exc
                await self.frame_inbox.close(tag)
                self._cleanup_choice_tracking(tag)
            raise
        if terminal or deinit:
            # Despierta a cualquier decision que siguiera esperando: la
            # batalla termino y no va a llegar mas narracion.
            await self.frame_inbox.close(tag)
            self._cleanup_choice_tracking(tag)
        return result

    def _current_frame_seq(self, tag: str) -> int:
        return CURRENT_FRAME_SEQ.get() or self.frame_inbox.last_seq(tag)

    def _observe_request(self, tag: str, raw_request: str) -> None:
        try:
            request = json.loads(raw_request)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ChoiceProtocolError(f"{tag}: request invalido: {exc}") from exc
        rqid = request.get("rqid")
        identity = (rqid if isinstance(rqid, int) else None, self._current_frame_seq(tag))
        pending = self._pending_choices.get(tag)
        if pending is not None:
            if pending.phase == "rejected":
                if not self._retry_pending.get(tag):
                    raise ChoiceProtocolError(
                        f"{tag}: request con decision rechazada sin retry pendiente"
                    )
                # Unavailable revela informacion y trae el request del retry.
                # No prueba que el intento rechazado se haya resuelto.
            elif pending.phase in ("reserved", "retried"):
                if pending.outbound_phase not in ("sending", "sent"):
                    raise ChoiceProtocolError(
                        f"{tag}: request nuevo antes de enviar decision "
                        f"{pending.decision_index} (phase={pending.outbound_phase!r})"
                    )
                self._resolve_pending_choice(tag)
            else:
                raise ChoiceProtocolError(
                    f"{tag}: request con pending phase={pending.phase!r}"
                )
        self._request_heads[tag] = identity

    def _classify_choice_error(self, tag: str, text: str) -> str | None:
        is_choice_error = text.startswith("[Unavailable choice]") or text.startswith(
            "[Invalid choice]"
        )
        if "The battle crashed" in text or text in _FATAL_ROOM_CHOICE_ERRORS:
            return "fatal"
        last = self._last_outbound.get(tag)
        if text in _AUXILIARY_UNDO_ERRORS:
            if (
                last is not None
                and last.effective_message == "/undo"
                and last.phase in ("sending", "sent")
            ):
                return "auxiliary"
            return "fatal"
        if not is_choice_error:
            return None
        pending = self._pending_choices.get(tag)
        correlated = (
            pending is not None
            and pending.phase in ("reserved", "retried")
            and pending.outbound_seq is not None
            and pending.outbound_seq == getattr(last, "sequence", None)
            and pending.outbound_message == getattr(last, "effective_message", None)
            and pending.outbound_message is not None
            and pending.outbound_message.startswith("/choose")
            and pending.outbound_phase in ("sending", "sent")
            and self._request_heads.get(tag)
            == (pending.request_rqid, pending.request_frame_seq)
        )
        return "rejection" if correlated else "fatal"

    def _reject_pending_choice(self, tag: str) -> None:
        pending = self._pending_choices[tag]
        pending.phase = "rejected"
        pending.step = None
        self.steps[tag][pending.step_index] = None
        self._retry_pending[tag] = True
        self.rejected_choice_count += 1

    def _resolve_pending_choice(self, tag: str) -> None:
        pending = self._pending_choices[tag]
        pending.phase = "resolved"
        self._pending_choices.pop(tag, None)
        self._retry_pending.pop(tag, None)

    def _resolve_terminal_choice(self, tag: str, event: str) -> None:
        pending = self._pending_choices.get(tag)
        if pending is None:
            return
        if (
            pending.phase not in ("reserved", "retried")
            or pending.outbound_phase not in ("sending", "sent")
        ):
            raise self._fatal_choice_error(
                tag,
                f"{event} con decision index={pending.decision_index} "
                f"phase={pending.phase} outbound_phase={pending.outbound_phase}",
            )
        self._resolve_pending_choice(tag)

    def _fatal_choice_error(self, tag: str, detail: str) -> ChoiceProtocolError:
        self.fatal_choice_error_count += 1
        return ChoiceProtocolError(f"{tag}: choice protocol fatal: {detail}")

    def _cleanup_choice_tracking(self, tag: str) -> None:
        self._pending_choices.pop(tag, None)
        self._retry_pending.pop(tag, None)
        self._request_heads.pop(tag, None)
        self._last_outbound.pop(tag, None)
        self._outbound_sequences.pop(tag, None)
        self._last_projection.pop(tag, None)
        self._temporary_state.pop(tag, None)
        self._projected_until.pop(tag, None)

    def trajectory_blocker(self, tag: str) -> tuple[int, str] | None:
        terminal = self._terminal_failures.get(tag)
        if terminal is not None:
            return (-1, f"terminal_failure:{terminal}")
        pending = self._pending_choices.get(tag)
        if pending is not None:
            return (pending.decision_index, pending.phase)
        for index, step in enumerate(self.steps.get(tag, [])):
            if step is None:
                return (index, "missing")
            if step.get("state") is None:
                return (index, "incomplete")
            if step.get("action_taken") is None:
                return (index, "action_missing")
        return None

    def _reserve_step(
        self,
        tag: str,
        step: dict,
        *,
        retry: bool,
        request_rqid: int | None,
        request_frame_seq: int,
    ) -> int:
        if retry:
            pending = self._pending_choices.get(tag)
            if pending is None or pending.phase != "rejected":
                raise ChoiceProtocolError(
                    f"{tag}: retry sin decision rechazada correlacionada"
                )
            identity = self._request_heads.get(
                tag, (pending.request_rqid, pending.request_frame_seq)
            )
            pending.attempt_index += 1
            pending.phase = "retried"
            pending.request_rqid, pending.request_frame_seq = identity
            pending.step = step
            pending.outbound_seq = None
            pending.outbound_message = None
            pending.outbound_phase = None
            self.steps[tag][pending.step_index] = step
            return pending.decision_index
        if tag in self._pending_choices:
            pending = self._pending_choices[tag]
            raise ChoiceProtocolError(
                f"{tag}: nueva decision antes de resolver index="
                f"{pending.decision_index} phase={pending.phase}"
            )
        index = len(self.steps[tag])
        self.steps[tag].append(step)
        self._pending_choices[tag] = PendingChoice(
            decision_index=index,
            attempt_index=0,
            phase="reserved",
            request_rqid=request_rqid,
            request_frame_seq=request_frame_seq,
            step_index=index,
            step=step,
        )
        self._request_heads.setdefault(tag, (request_rqid, request_frame_seq))
        return index

    def choose_move(self, battle: Any) -> Any:
        """Captura TODO lo que define la decision, sincronicamente, y devuelve
        una coroutine que primero espera la narracion previa —ya emitida por
        el servidor, independiente de nuestra respuesta— y recien despues
        decide.

        Las dos rutas (random y grafo) son asincronas y comparten la MISMA
        proyeccion: `graph_input["raw_state"]` y `step["state"]` son el mismo
        objeto, asi que no pueden contradecirse.
        """
        tag = battle.battle_tag
        self._sides.setdefault(tag, battle.player_role)
        # Se consume UNA sola vez (D31, reintento por eleccion rechazada).
        retry = self._retry_pending.pop(tag, False)
        # Cursor ANTES de cualquier await: el `seq` del frame que trajo ESTE
        # `|request|`, no el ultimo publicado. Bajo carga hay varios frames
        # encolados detras del lock y `last_seq` devolveria uno posterior;
        # con el ContextVar cada decision espera exactamente la narracion que
        # sigue a SU request, y dos decisiones nunca consumen el mismo frame.
        cursor = CURRENT_FRAME_SEQ.get()
        if cursor is None:
            cursor = self.frame_inbox.last_seq(tag)
        captured_legal = legal_actions(battle)
        snapshot = {**serialize_battle(battle), "legal_actions": captured_legal}
        opponent_side = "p2" if battle.player_role == "p1" else "p1"
        vocabulary = self._vocabulary(battle.gen)
        deadline = self._clock() + self.decision_budget_seconds
        # Todo esto se lee AHORA, sincronicamente, y nunca se vuelve a leer
        # de `battle`: `available_moves`/`available_switches` ya reflejan el
        # `|request|` que acaba de procesar `parse_request` (poke-env llama a
        # choose_move DESPUES de eso), asi que estan tan al dia como pueden
        # estarlo. `action_taken in legal_actions` queda garantizado por
        # construccion porque las dos vienen de esta MISMA linea de codigo.
        # Defecto 1 (fix-cursor, D22): el pokemon activo AHORA es el que va a
        # intentar ejecutar `action_taken`. Se guarda normalizado para que
        # `_find_action_line` pueda verificar que un `|cant|`/`|faint|`/
        # autogolpe por confusion de respaldo pertenece a ESTE pokemon, no a
        # otro del mismo lado (ver `_actor_matches`). `getattr` con default
        # `None`: en el `forceSwitch` que sigue a un debilitamiento no hay
        # pokemon activo propio (ya fainted), y ahi el respaldo no aplica de
        # todos modos porque un cambio siempre deja su propia linea `|switch|`.
        #
        # `base_species`, NO `species`: Showdown identifica al actor en
        # `|move|`/`|switch|`/`|cant|`/`|faint|` SIEMPRE con el nombre base,
        # nunca con la forma (verificado contra la base real: `p1a: Arceus`
        # para las 6 formas de plato, `p1a: Rotom` para las 6 de electrodomes-
        # tico, `p1a: Giratina` para Origin, igual con Wormadam/Keldeo/
        # Landorus/Thundurus/Shaymin). `mon.species` para esos SI incluye la
        # forma (`arceuspoison`, `rotommow`): comparar contra eso rompia el
        # chequeo para cualquier pokemon con forma, descartando por error un
        # `|faint|`/`|cant|` que si le pertenecia (medido: Arceus-Poison,
        # decision "Earth Power" nunca ejecutada porque a Arceus lo debilito
        # un Porygon-Z mas rapido — el `|faint|p1a: Arceus` real quedaba
        # rechazado porque "arceus" != "arceuspoison").
        actor = getattr(battle, "active_pokemon", None)
        actor_species = (
            _normalize(getattr(actor, "base_species", "") or actor.species)
            if actor is not None else None
        )
        last_request = getattr(battle, "last_request", {}) or {}
        raw_rqid = last_request.get("rqid") if isinstance(last_request, dict) else None
        request_rqid = raw_rqid if isinstance(raw_rqid, int) else None

        if self.decision_graph is None:
            order = super().choose_move(battle)
            step = {
                "turn": battle.turn,
                "decision_turn": battle.turn,
                "action_taken": action_from_order(order),
                "legal_actions": captured_legal,
                "actor_species": actor_species,
                "decision_path": "random",
                "state": None,
            }
            self._reserve_step(
                tag,
                step,
                retry=retry,
                request_rqid=request_rqid,
                request_frame_seq=cursor,
            )

            async def run_random() -> Any:
                task = self._admit_decision()
                try:
                    self._set_decision_stage(task, "resolve_state")
                    step["state"] = await self._resolve_state(
                        tag, snapshot, step=step, cursor=cursor, retry=retry,
                        opponent_side=opponent_side, vocabulary=vocabulary,
                        deadline=deadline,
                    )
                    return order
                finally:
                    self._release_decision(task)

            return run_random()

        return self._choose_move_with_graph(
            battle, tag=tag, snapshot=snapshot, captured_legal=captured_legal,
            actor_species=actor_species, cursor=cursor, retry=retry,
            request_rqid=request_rqid,
            opponent_side=opponent_side, vocabulary=vocabulary,
            deadline=deadline,
        )

    def _choose_move_with_graph(
        self,
        battle: Any,
        *,
        tag: str,
        snapshot: dict,
        captured_legal: list[dict],
        actor_species: str | None,
        cursor: int,
        retry: bool,
        request_rqid: int | None,
        opponent_side: str,
        vocabulary: PokeEnvVocabulary,
        deadline: float,
    ) -> Any:
        """Construye el mapa accion->`BattleOrder` ANTES de devolver la
        coroutine: ni la espera ni el grafo pueden cambiarlo."""
        action_orders: dict[tuple[tuple[str, Any], ...], Any] = {}
        for move in battle.available_moves:
            action = {"kind": "move", "id": move.id}
            action_orders[tuple(sorted(action.items()))] = self.create_order(move)
            if getattr(battle, "can_mega_evolve", False):
                mega_action = {**action, "mega": True}
                action_orders[tuple(sorted(mega_action.items()))] = self.create_order(
                    move, mega=True
                )
        for mon in battle.available_switches:
            action = {"kind": "switch", "species": mon.species}
            action_orders[tuple(sorted(action.items()))] = self.create_order(mon)

        step = {
            "turn": battle.turn,
            "decision_turn": battle.turn,
            "action_taken": None,
            "action_path": None,
            "legal_actions": captured_legal,
            "actor_species": actor_species,
            "decision_path": "graph",
            "state": None,
        }
        index = self._reserve_step(
            tag,
            step,
            retry=retry,
            request_rqid=request_rqid,
            request_frame_seq=cursor,
        )
        # MON-35: el attempt VIGENTE se captura sincronicamente, junto con la
        # reserva: es la clave (tag, decision, attempt) del gate de este
        # intento (D65 S4). Un retry lo tiene incrementado por `_reserve_step`.
        attempt_index = self._pending_choices[tag].attempt_index

        async def run_graph() -> Any:
            # D46/MON-23: primera linea sincronica, antes de tocar
            # proyeccion/grafo/calc. Ver `_admit_decision`.
            task = self._admit_decision()
            try:
                self._set_decision_stage(task, "resolve_state")
                projected = await self._resolve_state(
                    tag, snapshot, step=step, cursor=cursor, retry=retry,
                    opponent_side=opponent_side, vocabulary=vocabulary,
                    deadline=deadline,
                )
                # MISMA referencia para el proveedor y para la fila: por
                # construccion no pueden representar puntos distintos.
                step["state"] = projected
                graph_input = {
                    "raw_state": projected,
                    "turn_id": f"{tag}:{index}",
                    "deadline": deadline,
                }
                # Los nodos del grafo publican sus propias etapas via
                # `on_stage` (record_decision_stage) cuando el grafo se
                # construyo con ese cable; el marker grueso de aca queda
                # para grafos sin cable (tests) y para el tramo dentro de
                # `ainvoke` que no es de ningun nodo.
                self._set_decision_stage(task, "graph")
                result = await self.decision_graph.ainvoke(graph_input)
                # MON-35 (D65 S3.1): el gate vive FUERA del grafo,
                # exactamente entre `ainvoke` y `execute_action`. Un override
                # se valida contra la MISMA mascara capturada con la que se
                # construyo `action_orders`; la accion final es la que gano
                # el CAS y `execute_action` corre EXACTAMENTE una vez.
                self._set_decision_stage(task, "approval_gate")
                action = result["action"]
                model_envelope = {
                    key: result.get(key) for key in _D38_METADATA_KEYS
                }
                action, approval_outcome = await self._apply_approval_gate(
                    tag=tag,
                    decision_index=index,
                    attempt_index=attempt_index,
                    action=action,
                    legal_actions=captured_legal,
                    model_envelope=model_envelope,
                    deadline=deadline,
                )
                self._set_decision_stage(task, "execute")
                # F2-09 (D39): adapter explícito del resultado del grafo a la
                # ejecución de poke-env. NO es un nodo LangGraph: el mapa
                # accion->BattleOrder se captura SÍNCRONO antes del primer await
                # y poke-env exige el BattleOrder como retorno de choose_move.
                # `None` (fuera de la máscara capturada) se convierte en error.
                order = execute_action(action, action_orders)
                if order is None:
                    raise RuntimeError(
                        f"decision graph returned action outside captured mask: {action!r}"
                    )
                step["action_taken"] = action
                if approval_outcome == "human_override":
                    # D65 S5.2: override -> action_source 'human', las 11
                    # columnas D38 NULL COMO GRUPO y la propuesta descartada
                    # permanece completa en `pending_decisions`. El CHECK
                    # `trajectory_steps_approval_outcome_action_source_check`
                    # rechaza cualquier otra combinacion.
                    step["action_source"] = "human"
                    step["action_path"] = None
                    step["reasoning"] = None
                    for key in _D38_METADATA_KEYS:
                        step[key] = None
                else:
                    # F2-08 (D38): metadata de la decision canónica, desde el
                    # resultado del grafo (que sale del envelope de la llamada
                    # LLM aceptada, nunca de estado compartido) hasta el step
                    # que persiste `_persist_one`. La ruta random no pasa por
                    # aca y queda NULL. `get` con default None protege a un
                    # grafo fake que no conozca estas claves.
                    step["action_source"] = "agent"
                    step["action_path"] = result["action_path"]
                    step["reasoning"] = result.get("reasoning")
                    for key in _D38_METADATA_KEYS:
                        step[key] = result.get(key)
                # Tercer eje ortogonal (D65 S5.2): NULL en el camino sin gate
                # (comportamiento historico).
                step["approval_outcome"] = approval_outcome
                return order
            finally:
                self._release_decision(task)

        return run_graph()

    async def _apply_approval_gate(
        self,
        *,
        tag: str,
        decision_index: int,
        attempt_index: int,
        action: dict[str, object],
        legal_actions: list[dict[str, object]],
        model_envelope: dict[str, object],
        deadline: float,
    ) -> tuple[dict[str, object], str | None]:
        """Gate exact-once entre `ainvoke` y `execute_action` (D65 S3.1).

        La politica decide si el gate existe: en modo skip no se construye
        ningun `PendingApproval` y por lo tanto ningun `Future` (run/
        benchmark/matrix-run). Si la politica exige gate:
        1. persiste la fila `awaiting` ANTES de publicar (D65 S5.1, canario
           19: la fila durable existe antes del evento WS);
        2. abre el attempt vigente en el `ApprovalRegistry` (un intento
           nuevo reemplaza al rechazado: superseded, D65 S4.3);
        3. publica `decision_proposed` por el `EventHub` integrado;
        4. espera la resolucion con el reloj D42 inyectado;
        5. persiste el outcome y, si el CAS no lo gano un operador via API
           (esa ruta publica su propio `decision_resolved`), lo publica aca;
        6. devuelve la accion final para `execute_action` EXACTAMENTE una
           vez.

        Shutdown: si la task se cancela con el gate abierto, el gate se
        descarta del registry (deja de ser alcanzable via API) y no se crea
        ningun step; la fila `awaiting` huerfana la barre `abort_stale` en
        el proximo arranque (D65 S4.3: `aborted`, sin step).
        """
        gate = create_gate(
            self._approval_policy,
            connection_mode=self._connection_mode,
            approval_mode=self._approval_mode,
            key=ApprovalKey(
                battle_tag=tag,
                decision_index=decision_index,
                attempt_index=attempt_index,
            ),
            proposal=ApprovalProposal(
                action=action,
                legal_actions=legal_actions,
                model_envelope=model_envelope,
            ),
            approval_timeout_seconds=self._approval_timeout_seconds,
            decision_deadline=deadline,
            clock=self._clock,
            tick=None,
        )
        if gate is None:
            return action, None
        repository = self._pending_repository
        if repository is None:
            raise RuntimeError(
                f"{tag}: la politica exige gate de aprobacion para la "
                f"decision {decision_index} pero no hay "
                "PendingDecisionRepository inyectado"
            )
        await repository.insert_awaiting(
            PendingDecisionRecord(key=gate.key, proposal=gate.proposal)
        )
        self._approval_registry.open(gate)
        self._event_hub.publish(f"battle:{tag}", {
            "type": "decision_proposed",
            "decision_index": decision_index,
            "attempt_index": attempt_index,
            "action": dict(action),
            "legal_actions": [dict(entry) for entry in legal_actions],
        })
        try:
            gate_start = self._clock()
            resolution = await gate.await_resolution()
            approval_wait_ms = int(round((self._clock() - gate_start) * 1000))
        except BaseException:
            self._approval_registry.discard(tag, decision_index)
            raise
        await repository.resolve(gate.key, resolution, approval_wait_ms)
        if resolution.resolved_by != "operator":
            self._event_hub.publish(f"battle:{tag}", {
                "type": "decision_resolved",
                "decision_index": decision_index,
                "outcome": resolution.outcome,
                "resolved_by": resolution.resolved_by,
            })
        return resolution.action, resolution.outcome

    async def _resolve_state(
        self,
        tag: str,
        snapshot: dict,
        *,
        step: dict,
        cursor: int,
        retry: bool,
        opponent_side: str,
        vocabulary: PokeEnvVocabulary,
        deadline: float,
    ) -> dict:
        """Estado observable con el que se decide y que se persiste.

        Espera la narracion previa —publicada pre-lock, ya emitida por el
        servidor y ajena a nuestra respuesta— y le aplica una proyeccion pura
        al snapshot inmutable. Nunca relee `Battle`.
        """
        if retry:
            # Un reintento no tiene resolucion nueva que esperar: el turno no
            # avanzo. Esperar aca venceria siempre.
            previous = self._last_projection.get(tag)
            if previous is None:
                self._drop_step(tag, step)
                raise RuntimeError(
                    f"reintento de eleccion en {tag} sin proyeccion publica "
                    "previa valida: no hay estado observable con el que decidir"
                )
            # Se reusa SOLO la parte publica, nunca el dict entero: la
            # mascara pudo cambiar al descubrirse `trapped`, y el snapshot
            # propio de este reintento es el nuevo.
            return {
                **snapshot,
                "turn": previous.get("turn", snapshot.get("turn")),
                "opponent": copy.deepcopy(previous["opponent"]),
                "field": copy.deepcopy(previous["field"]),
            }
        budget = max(0.0, deadline - self._clock())
        try:
            window = await self.frame_inbox.wait_for_resolution(
                tag,
                # MON-26: el watermark es el seq del ULTIMO frame entregado
                # a la proyeccion anterior de ESTE tag (por batalla, nunca
                # global: con concurrencia > 1 un cursor compartido haria que
                # dos batallas se roben frames en silencio). La ventana
                # devuelve los frames de resolucion entre el watermark y el
                # frame de cierre -- incluida cualquier narracion que un
                # request `wait:true` haya interpuesto antes del request
                # activo (battle-gen6randombattle-67, turno 2).
                after_seq=self._projected_until.get(tag, 0),
                until_seq=cursor,
                timeout=min(self.projection_timeout_seconds, budget),
            )
        except ProjectionTimeoutError:
            # Fallo CERRADO: ni se invoca al proveedor con estado stale ni
            # queda un paso persistible. Si una fila existe, su proyeccion es
            # valida por construccion.
            self.projection_timeout_count += 1
            self._drop_step(tag, step)
            raise
        # El watermark avanza al entregar la ventana, ANTES de proyectar: si
        # la proyeccion falla cerrado (`-swapboost`), los frames ya fueron
        # consumidos y la decision siguiente no tiene que volver a tropezar
        # con el mismo frame envenenado.
        # MON-26 R2 (F4): el contrato del inbox permite ventana VACIA cuando
        # el cursor de esta decision quedo por DETRAS del watermark (dos
        # decisiones resolviendo al mismo frame de cierre; medido). Sin
        # guarda, `window[-1]` lanzaria `IndexError`, que escapa de los dos
        # `except` de fallo cerrado SIN `_drop_step`. Se convierte en el
        # mismo fallo cerrado de siempre: ni proveedor con estado ambiguo ni
        # fila persistida.
        if not window:
            self.projection_timeout_count += 1
            self._drop_step(tag, step)
            raise ProjectionTimeoutError(
                f"{tag} entrego una ventana de frames vacia para la decision "
                f"(watermark={self._projected_until.get(tag, 0)}, "
                f"request={cursor}): no se puede demostrar cual es la "
                "narracion de esta decision"
            )
        self._projected_until[tag] = window[-1].seq
        # MON-26 R3: las lineas de frames con seq < cursor (los del gap,
        # interpuestos por un request `wait:true`) llegan de frames que
        # poke-env YA proceso antes de capturar el snapshot: sus efectos
        # (incluido el descuento de PP de `Move.use()`) ya estan aplicados
        # y el proyector no debe reaplicar el unico efecto NO idempotente.
        pre_applied = sum(
            len(frame.lines) for frame in window if frame.seq < cursor
        )
        try:
            projected = project_observable_state(
                snapshot,
                tuple(line for frame in window for line in frame.lines),
                opponent_side=opponent_side,
                vocabulary=vocabulary,
                # Mismo dict, por tag, a traves de TODA la batalla: es lo
                # unico que le permite a un Transform o una Mega aplicado en
                # ESTA decision seguir siendo correcto varias decisiones
                # despues, cuando recien ahi llegue su switch-out.
                persistent_state=self._temporary_state.setdefault(tag, {}),
                pre_applied=pre_applied,
            )
        except ProjectionAmbiguityError:
            # Mismo fallo CERRADO que el timeout: la alternativa era copiar
            # silenciosamente del activo post-resolucion equivocado (el bug
            # medido de Transform/Reflect Type/-copyboost). Ni proveedor con
            # estado ambiguo ni fila persistida.
            self.projection_ambiguity_count += 1
            self._drop_step(tag, step)
            raise
        self._last_projection[tag] = projected
        return projected

    def _drop_step(self, tag: str, step: dict) -> None:
        """Retira el paso reservado por una decision que fallo cerrado.

        Las decisiones son estrictamente secuenciales, asi que el paso de
        ESTA decision es siempre el ultimo del tag.
        """
        steps = self.steps.get(tag)
        pending = self._pending_choices.get(tag)
        if pending is not None and pending.step is step:
            if steps and pending.step_index == len(steps) - 1:
                steps.pop()
            elif steps and pending.step_index < len(steps):
                steps[pending.step_index] = None
            self._pending_choices.pop(tag, None)
            self._retry_pending.pop(tag, None)

    async def wait_for_pending_steps(self, tag: str) -> None:
        """Corrige la etiqueta de turno de cada paso contra el protocolo.

        Ya no hay tasks de fondo que esperar (ver docstring de la clase): la
        materializacion del estado es sincronica, dentro del mismo manejo de
        mensajes. Se mantiene el nombre y la firma `async` por compatibilidad
        con `cli.py`, que llama esto antes de leer `self.steps[tag]`.

        Guarda defensiva (I-3 de la review de merge): si algun paso quedara
        sin finalizar —no deberia pasar nunca, dado como esta escrito
        `_handle_battle_message`— deja el diagnostico en el log. El gate
        atomico de `cli._persist_one`, que corre inmediatamente despues,
        incrementa `lost_step_count` y lanza el error tipado antes de escribir
        ningun step. Contar tambien aca duplicaria la misma perdida.
        """
        sin_finalizar = [
            i for i, s in enumerate(self.steps.get(tag, [])) if s is not None and s["state"] is None
        ]
        if sin_finalizar:
            logger.error(
                "%d paso(s) de %s quedaron sin materializar (indices %s): "
                "esto no deberia pasar nunca con la captura sincronica",
                len(sin_finalizar), tag, sin_finalizar,
            )
        self._correct_step_turns(tag)

    def _correct_step_turns(self, tag: str) -> None:
        """D20 (C1): corrige `turn` de cada paso contra el protocolo, EN
        ORDEN de decision y con un cursor global que solo avanza.

        Lee SOLO el recorder (protocolo crudo, D17), nunca `battle`: para
        cuando esto corre la batalla puede haber terminado, y ademas no hace
        falta nada mas que las lineas ya grabadas.
        """
        recorder = self.recorders[tag]
        side = self._sides.get(tag)
        if side is None:
            return
        cursor = 0
        for step in self.steps[tag]:
            if step is None:
                continue
            max_turn = step["decision_turn"] + ACTION_SEARCH_MARGIN_TURNS
            found = _find_action_line(
                recorder, side, step["action_taken"], cursor, max_turn,
                actor_species=step.get("actor_species"),
            )
            if found is not None:
                step["turn"], cursor = found
                estado = step.get("state")
                if estado is None:
                    continue
                if step.get("decision_path") == "graph":
                    # D31: este dict es el MISMO que vio el proveedor. Mutarlo
                    # aca reescribiria a posteriori la evidencia de la
                    # decision, asi que se VERIFICA en vez de corregir. La
                    # proyeccion ya fijo `turn` desde el `|turn|N` de la
                    # narracion previa; si no coincide con la linea que
                    # resolvio la accion, hay algo que no entendemos y tiene
                    # que hacer ruido, no quedar tapado.
                    if estado.get("turn") != step["turn"]:
                        raise RuntimeError(
                            f"{tag} decision {estado.get('turn')!r}: el turno "
                            f"proyectado desde la narracion previa no coincide "
                            f"con el turno {step['turn']} donde la accion se "
                            f"resolvio en el protocolo"
                        )
                else:
                    # Ruta random: conserva la correccion historica (D20/C1).
                    # No hay proveedor que haya visto este dict.
                    estado["turn"] = step["turn"]
