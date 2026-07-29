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
from .protocol import (
    CURRENT_FRAME_SEQ,
    ProjectionTimeoutError,
    ProtocolRecorder,
    RawFrameInbox,
    project_observable_state,
)


logger = logging.getLogger(__name__)


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
        self._pokedex = GenData.from_gen(gen).pokedex

    def species_types(self, species_id: str) -> list[str]:
        entry = self._pokedex.get(species_id) or {}
        # El serializador persiste los enums de poke-env por NOMBRE
        # (`PokemonType.DARK` -> "DARK"); el dex los trae capitalizados.
        return [str(t).upper() for t in entry.get("types", [])]

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
            if clave in _normalize(line):
                return turn, from_index + offset + 1
            if line.startswith(prefix_move):
                se_movio_en.add(turn)
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
                respaldo = (turn, from_index + offset + 1)
                respaldo_turn = turn
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

    def __init__(
        self,
        *args: Any,
        decision_graph: Any = None,
        decision_budget_seconds: float = 240,
        projection_timeout_seconds: float = 1.0,
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
        self.recorders: dict[str, ProtocolRecorder] = defaultdict(ProtocolRecorder)
        self.steps: dict[str, list[dict | None]] = defaultdict(list)
        # Frames crudos publicados ANTES del lock por batalla. Es lo unico
        # que una decision puede esperar sin trabarse (ver docstring).
        self.frame_inbox = RawFrameInbox()
        # Ultima proyeccion publica valida por tag. La reusa un reintento
        # tras una eleccion rechazada, donde no hay resolucion nueva que
        # esperar.
        self._last_projection: dict[str, dict] = {}
        # Marca explicita de "el proximo choose_move de este tag es un
        # reintento". Se pone al ver el `|error|` y se consume UNA sola vez.
        self._retry_pending: dict[str, bool] = {}
        self._vocabularies: dict[int, PokeEnvVocabulary] = {}
        # Contador consultable: cuantas decisiones fallaron cerrado porque la
        # narracion previa no llego a tiempo. Distinto de cero es fallo, no
        # warning.
        self.projection_timeout_count: int = 0
        self._listener_started = False
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
        # incrementa en `cli._persist_one`, en el momento en que un paso se
        # descarta sin persistir (`step is None` o `step["state"] is None`):
        # ese es el UNICO lugar donde un paso se pierde de verdad del
        # dataset, asi que es el unico que cuenta (evita contar dos veces el
        # mismo paso: `wait_for_pending_steps`, mas abajo, detecta el mismo
        # caso ANTES pero solo loguea, no incrementa). Es el mismo rol que
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

    async def wait_for_background_failure(self) -> Exception:
        return await asyncio.shield(
            asyncio.wrap_future(self._background_failure)
        )

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

        # Hallazgo nuevo (descubierto al implementar D21/C2, no estaba en el
        # brief): si el servidor rechaza la eleccion anterior
        # (`[Unavailable choice]`: el pokemon resulto estar atrapado /
        # `[Invalid choice]`), poke-env vuelve a llamar a `choose_move` para
        # la MISMA decision (rqid nuevo, pero es una correccion, no una
        # decision de juego nueva). Sin esto, la eleccion rechazada —que
        # nunca se ejecuto— quedaba grabada como un paso fantasma. Con la
        # clave vieja (turn_number) muchas veces se pisaba sola por
        # casualidad; con decision_index (D21) queda expuesta: se detecto
        # asi, comparando el conteo de switches grabados contra el
        # protocolo (mas grabados que `|switch|` reales). Se descarta el
        # ULTIMO paso de este tag: la eleccion rechazada es siempre la mas
        # reciente, porque las decisiones son estrictamente secuenciales.
        for parts in split_messages:
            if (
                len(parts) > 2
                and parts[1] == "error"
                and (
                    parts[2].startswith("[Unavailable choice]")
                    or parts[2].startswith("[Invalid choice]")
                )
            ):
                self._discard_last_step(tag)
                # D31: marcar el reintento ANTES de delegar en super(), que
                # es lo unico que funciona para las DOS rutas de rechazo:
                #
                #   [Invalid choice]     -> poke-env vuelve a llamar a
                #     `choose_move` DENTRO del manejo de este mismo frame
                #     (`player.py:322`), sin request nuevo.
                #   [Unavailable choice] -> solo marca `_trying_again` y el
                #     request nuevo llega en un frame POSTERIOR.
                #
                # Un reintento no puede esperar narracion: no hay turno que
                # resolver y la espera venceria siempre. Por eso hace falta
                # una marca explicita y no alcanza con mirar "el frame
                # anterior al request".
                self._retry_pending[tag] = True
                break

        try:
            result = await super()._handle_battle_message(split_messages)
        except Exception as exc:
            if not self._background_failure.done():
                self._background_failure.set_result(exc)
            raise
        if any(
            len(parts) > 1 and parts[1] in ("win", "tie") for parts in split_messages
        ):
            # Despierta a cualquier decision que siguiera esperando: la
            # batalla termino y no va a llegar mas narracion.
            await self.frame_inbox.close(tag)
        return result

    def _discard_last_step(self, tag: str) -> None:
        """Descarta el ultimo paso grabado: su eleccion fue rechazada por el
        servidor y nunca se ejecuto.

        Siempre llega ya finalizado (con `state` real, no `None`): el lote
        que trae el rechazo es, por construccion, POSTERIOR al lote donde se
        tomo esa decision, y ese lote anterior ya paso por
        `_finalize_pending_steps` antes de que este pudiera empezar a
        procesarse (ver `_handle_battle_message`)."""
        if self.steps[tag]:
            self.steps[tag].pop()

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
        deadline = time.monotonic() + self.decision_budget_seconds
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
            self.steps[tag].append(step)

            async def run_random() -> Any:
                step["state"] = await self._resolve_state(
                    tag, snapshot, step=step, cursor=cursor, retry=retry,
                    opponent_side=opponent_side, vocabulary=vocabulary,
                    deadline=deadline,
                )
                return order

            return run_random()

        return self._choose_move_with_graph(
            battle, tag=tag, snapshot=snapshot, captured_legal=captured_legal,
            actor_species=actor_species, cursor=cursor, retry=retry,
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

        index = len(self.steps[tag])
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
        self.steps[tag].append(step)

        async def run_graph() -> Any:
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
            result = await self.decision_graph.ainvoke(graph_input)
            action = result["action"]
            order = action_orders.get(tuple(sorted(action.items())))
            if order is None:
                raise RuntimeError(
                    f"decision graph returned action outside captured mask: {action!r}"
                )
            step["action_taken"] = action
            step["action_path"] = result["action_path"]
            return order

        return run_graph()

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
        budget = max(0.0, deadline - time.monotonic())
        try:
            frame = await self.frame_inbox.wait_for_resolution(
                tag,
                after_seq=cursor,
                timeout=min(self.projection_timeout_seconds, budget),
            )
        except ProjectionTimeoutError:
            # Fallo CERRADO: ni se invoca al proveedor con estado stale ni
            # queda un paso persistible. Si una fila existe, su proyeccion es
            # valida por construccion.
            self.projection_timeout_count += 1
            self._drop_step(tag, step)
            raise
        projected = project_observable_state(
            snapshot,
            frame.lines,
            opponent_side=opponent_side,
            vocabulary=vocabulary,
        )
        self._last_projection[tag] = projected
        return projected

    def _drop_step(self, tag: str, step: dict) -> None:
        """Retira el paso reservado por una decision que fallo cerrado.

        Las decisiones son estrictamente secuenciales, asi que el paso de
        ESTA decision es siempre el ultimo del tag.
        """
        steps = self.steps.get(tag)
        if steps and steps[-1] is step:
            steps.pop()

    async def wait_for_pending_steps(self, tag: str) -> None:
        """Corrige la etiqueta de turno de cada paso contra el protocolo.

        Ya no hay tasks de fondo que esperar (ver docstring de la clase): la
        materializacion del estado es sincronica, dentro del mismo manejo de
        mensajes. Se mantiene el nombre y la firma `async` por compatibilidad
        con `cli.py`, que llama esto antes de leer `self.steps[tag]`.

        Guarda defensiva (I-3 de la review de merge): si algun paso quedara
        sin finalizar —no deberia pasar nunca, dado como esta escrito
        `_handle_battle_message`— loguea en vez de persistir un `None` en
        silencio. El conteo consultable (`lost_step_count`) se lleva en un
        unico lugar, `cli._persist_one` (que es donde el paso efectivamente
        se descarta del dataset): este metodo solo corre ANTES de leer
        `self.steps[tag]` para persistir, asi que contar aca tambien
        duplicaria el mismo paso perdido dos veces.
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
