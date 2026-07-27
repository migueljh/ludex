"""Jugador que graba mientras juega.

Elige al azar entre las acciones legales: el entregable de esta rebanada no es
que juegue bien, es que grabe bien.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from typing import Any

from poke_env import ServerConfiguration
from poke_env.player import RandomPlayer

from ..state.actions import action_from_order
from ..state.serializer import serialize_battle
from .protocol import ProtocolRecorder


logger = logging.getLogger(__name__)

# C1 (review final, causa raiz re-medida: ver docs/DECISIONS.md D20). La
# brecha real medida con la materializacion diferida (sonda_defer.py, 3
# batallas completas, 160 decisiones) fue de 0 a ~11ms. Este techo es
# generoso a proposito para que casi nunca se llegue a usar; si se llega, se
# serializa con lo que haya (el desfase de siempre), nunca peor que hoy.
TURN_ALIGNMENT_TIMEOUT_SECONDS = 0.2


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


def _find_action_line(
    recorder: ProtocolRecorder,
    side: str,
    action_taken: dict | None,
    from_index: int,
    max_turn: int,
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
    (sueño, paralisis, congelamiento) y `|faint|` propio cuando al pokemon lo
    debilitaron antes de poder actuar. Ese rastro aparece en el bloque donde
    la decision se RESOLVIO, que es un turno mas adelante que `decision_turn`.
    Sin mirarlo, esas filas quedaban etiquetadas un turno antes: era el 100%
    del residual que sobrevivio a la primera vuelta de C1.

    De ahi la definicion que gobierna `turn_number` en el dataset:

        Una fila pertenece al turno en que su decision se RESOLVIO, sin
        importar como se resolvio — se ejecuto, el juego la impidio, o al
        pokemon lo debilitaron antes.

    La linea de resolucion se usa solo como RESPALDO, nunca antes de agotar
    la busqueda del `|move|`/`|switch|` real: un `|faint|` propio puede ser
    posterior a una accion que si se ejecuto, y en ese caso la evidencia
    buena es el `|move|`, no el debilitamiento.
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
    prefix_move = f"|move|{side}a:"
    prefix_switch = f"|switch|{side}a:"
    prefix_cant = f"|cant|{side}a:"
    prefix_faint = f"|faint|{side}a:"
    respaldo: tuple[int, int] | None = None
    se_movio_en: set[int] = set()
    for offset, (turn, line) in enumerate(recorder.entries_from(from_index)):
        if turn > max_turn:
            break
        if line.startswith(prefix_move) or line.startswith(prefix_switch):
            if clave in _normalize(line):
                return turn, from_index + offset + 1
            if line.startswith(prefix_move):
                se_movio_en.add(turn)
        elif respaldo is None:
            if line.startswith(prefix_cant):
                respaldo = (turn, from_index + offset + 1)
            elif line.startswith(prefix_faint) and turn not in se_movio_en:
                # Un debilitamiento propio DESPUES de haberse movido en el
                # mismo bloque no resuelve esta decision: resuelve la de
                # alguien que ya actuo.
                respaldo = (turn, from_index + offset + 1)
    return respaldo


class LudexPlayer(RandomPlayer):
    """RandomPlayer que captura protocolo crudo y estado por turno.

    Un ProtocolRecorder por battle_tag, nunca compartido entre jugadores: el
    |request| de cada uno trae su propio equipo.

    ## C1 — por que la serializacion se DIFIERE, y por que NO se espera antes
    de responder (ver docs/DECISIONS.md D20 para la version completa)

    La primera version de este arreglo esperaba, DENTRO de
    `_handle_battle_message` y ANTES de delegar en `super()` (que es quien
    dispara `choose_move`), a que la narracion del turno alcanzara al
    `|request|`. Instrumentado en vivo (`sonda_causalidad.py`, en el
    scratchpad de la sesion de arreglos), esto NUNCA se resuelve solo: la
    narracion de un turno es la RESPUESTA del servidor a que ambos jugadores
    ya eligieron para ESE turno. Retrasar nuestra propia respuesta 500ms
    retrasa la narracion exactamente 500ms (no es "ya estaba en el cable",
    es consecuencia causal de responder). Esperar antes de responder es
    esperar algo que no puede llegar sin que primero respondamos: con
    max_concurrent_battles=1 y las dos puntas (agente y rival) en el mismo
    proceso, esto agota el timeout en el 100% de las decisiones de una
    batalla real (medido con `sonda_deteccion_c1_v2.py`), sin arreglar nada.

    La version que SI funciona: `choose_move` sigue devolviendo la orden de
    inmediato (sin tocar la latencia de juego), pero la SERIALIZACION del
    paso se difiere a una task de fondo que espera, acotado, a que lleguen
    mas lineas de protocolo antes de fotografiar el estado. Como esa espera
    ya NO bloquea nuestra respuesta, el servidor puede seguir, y la
    narracion llega sola (medido: 0-11ms sobre 160 decisiones reales, cero
    timeouts). El orden de los pasos se preserva reservando el indice de la
    lista ANTES de lanzar la task (ver `choose_move`), no dejando que cada
    task decida su propia posicion.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorders: dict[str, ProtocolRecorder] = defaultdict(ProtocolRecorder)
        self.steps: dict[str, list[dict | None]] = defaultdict(list)
        self._pending_step_tasks: dict[str, list[asyncio.Task]] = defaultdict(list)
        # I5/C1: cuantas veces salto el timeout de la espera de alineacion.
        # La verificacion de esta rama exige reportar este numero; si un
        # cambio futuro del servidor rompe el supuesto de orden medido, esto
        # deja de ser silencioso.
        self.turn_alignment_timeouts = 0

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
                break

        return await super()._handle_battle_message(split_messages)

    def _discard_last_step(self, tag: str) -> None:
        """Descarta el ultimo paso grabado: su eleccion fue rechazada por el
        servidor y nunca se ejecuto. Cancela tambien su task de fondo, si
        todavia esta pendiente."""
        if self.steps[tag]:
            self.steps[tag].pop()
        if self._pending_step_tasks[tag]:
            task = self._pending_step_tasks[tag].pop()
            if not task.done():
                task.cancel()

    def choose_move(self, battle: Any) -> Any:
        order = super().choose_move(battle)
        tag = battle.battle_tag
        recorder = self.recorders[tag]
        action_taken = action_from_order(order)
        # Capturado AHORA, sincronicamente: SIEMPRE <= el turno real en el
        # que esta accion termina ejecutandose (ver `_find_action_line`).
        decision_turn = battle.turn

        # Reservar el indice ANTES de lanzar la task de fondo: dos decisiones
        # seguidas (p.ej. un cambio forzado tras un debilitamiento, C2) no
        # pueden competir por la misma posicion, y el orden de la lista tiene
        # que reflejar el orden real de las decisiones, no el de quien
        # termine de esperar primero.
        index = len(self.steps[tag])
        self.steps[tag].append(None)
        baseline = recorder.line_count
        task = asyncio.create_task(
            self._materialize_step(
                battle, recorder, tag, index, baseline, decision_turn, action_taken
            )
        )
        self._pending_step_tasks[tag].append(task)
        return order

    async def _materialize_step(
        self,
        battle: Any,
        recorder: ProtocolRecorder,
        tag: str,
        index: int,
        baseline: int,
        decision_turn: int,
        action_taken: dict | None,
    ) -> None:
        """Espera, acotado, a que llegue narracion nueva y RECIEN AHI serializa.

        No se compara contra el numero de turno (ni el de `battle.turn` ni el
        de `recorder`): un cambio forzado tras un debilitamiento (C2) trae
        narracion nueva SIN mover el numero de turno, asi que atar la espera
        a un cambio de turno haria que esos casos agoten el timeout siempre.
        El detector generico —lineas nuevas para este battle_tag, sean las
        que sean— cubre ambos casos por igual.
        """
        start = time.monotonic()
        while recorder.line_count == baseline and not battle.finished:
            if time.monotonic() - start > TURN_ALIGNMENT_TIMEOUT_SECONDS:
                self.turn_alignment_timeouts += 1
                logger.warning(
                    "timeout esperando narracion pendiente en %s (decision "
                    "%d): %d lineas grabadas y ninguna nueva en %.0fms; se "
                    "serializa con el desfase de siempre",
                    tag, index, baseline, TURN_ALIGNMENT_TIMEOUT_SECONDS * 1000,
                )
                break
            await asyncio.sleep(0.001)

        state = serialize_battle(battle)
        # La etiqueta de turno arranca en `decision_turn` (capturado
        # sincronicamente en choose_move), NO en `state["turn"]`
        # (=battle.turn en este instante): para cuando esta task corre,
        # battle.turn ya puede haber avanzado de mas (el mismo desfase que
        # motiva toda esta correccion), y usarlo como default dejaria una
        # etiqueta peor que `decision_turn` en el caso EXCUSADO (mas abajo)
        # donde no hay nada que corregir. `_correct_step_turns` la mueve
        # HACIA ADELANTE hasta donde la decision se RESOLVIO: donde se
        # ejecuto, o —si nunca se ejecuto— donde el protocolo muestra por que
        # (`|cant|` por sueño o paralisis, `|faint|` propio antes de actuar).
        # Solo si no hay ninguna de las dos evidencias se queda en
        # `decision_turn`, que es siempre el mejor valor disponible. Se
        # sobreescribe tambien dentro de `state`, para que la columna
        # `turn_number` y `state->>'turn'` nunca diverjan dentro de la
        # MISMA fila (ver la correccion simetrica en `_correct_step_turns`).
        state["turn"] = decision_turn
        self.steps[tag][index] = {
            "turn": decision_turn,
            "decision_turn": decision_turn,
            "state": state,
            "action_taken": action_taken,
        }

    async def wait_for_pending_steps(self, tag: str) -> None:
        """Espera a que TODAS las materializaciones de fondo de esta batalla
        terminen y CORRIGE la etiqueta de turno de cada paso contra el
        protocolo. El runner tiene que llamar esto antes de leer
        `self.steps[tag]`: si no, puede encontrar placeholders `None` sin
        serializar, o turnos sin corregir."""
        tasks = self._pending_step_tasks.get(tag, [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._correct_step_turns(tag)

    def _correct_step_turns(self, tag: str) -> None:
        """D20 (C1): corrige `turn` de cada paso contra el protocolo, EN
        ORDEN de decision y con un cursor global que solo avanza.

        Por que una pasada final y no la correccion en cada task (la primera
        version de este arreglo): las tasks de fondo terminan en el orden en
        que le llega la narracion a CADA UNA, no necesariamente en el orden
        en que se tomaron las decisiones. Corrigiendo aca, ya con todos los
        pasos materializados y en la lista en su orden real (`choose_move`
        reserva el indice sincronicamente), el cursor nunca puede
        "adelantarsele" a una decision anterior ni reusar su linea.
        """
        recorder = self.recorders[tag]
        battle = self.battles.get(tag)
        side = battle.player_role if battle is not None else None
        if side is None:
            return
        cursor = 0
        for step in self.steps[tag]:
            if step is None:
                continue
            max_turn = step["decision_turn"] + ACTION_SEARCH_MARGIN_TURNS
            found = _find_action_line(recorder, side, step["action_taken"], cursor, max_turn)
            if found is not None:
                step["turn"], cursor = found
                # El `turn` DENTRO del estado serializado tiene que quedar
                # coherente con la etiqueta de la fila (review final: "state
                #['turn'] sale de battle.turn, que va un turno atras del
                # protocolo"). Si no se corrigiera aca tambien, la columna
                # `turn_number` y el campo `state->>'turn'` podrian
                # divergir dentro de la MISMA fila.
                step["state"]["turn"] = step["turn"]
