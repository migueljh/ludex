"""Jugador que graba mientras juega.

Elige al azar entre las acciones legales: el entregable de esta rebanada no es
que juegue bien, es que grabe bien.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from poke_env import ServerConfiguration
from poke_env.player import RandomPlayer

from ..state.actions import action_from_order, legal_actions
from ..state.serializer import serialize_battle
from .protocol import ProtocolRecorder


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

    Lo que SI hacia falta esperar es el lado del rival (D20 original: mi
    equipo llega fresco por el `|request|`, pero Showdown narra "lo que paso
    en el turno" —incluida la revelacion del rival— DESPUES del `|request|`,
    en el MISMO lote). La clave (ver `_handle_battle_message`): ese lote
    COMPLETO ya esta grabado en `self.recorders[tag]` ANTES de que
    `super()._handle_battle_message()` procese una sola linea (el recorder
    graba de una sola vez, no incrementalmente), y para cuando esa llamada a
    `super()` REGRESA, poke-env ya proceso el lote entero —incluida la
    narracion que estaba pendiente cuando `choose_move` corrio a mitad de
    camino— y actualizo `battle.opponent_team` en consecuencia. Por eso
    `_finalize_pending_steps` corre ENTRE que `super()._handle_battle_message`
    regresa y que nuestro propio `_handle_battle_message` regresa: sigue
    siendo parte de la MISMA llamada sincronica que proceso el lote, nunca una
    task planificada aparte, asi que NINGUNA decision futura puede
    "adelantarsele": la unica forma de que se procese el lote de la decision
    N+1 es que esta llamada retorne primero, y para entonces la decision N ya
    quedo finalizada.

    `legal_actions` y `turn` de cada paso SIEMPRE se sobreescriben con el
    valor capturado en `choose_move` (nunca con lo que `serialize_battle`
    recalcularia en el momento de finalizar): eso es lo que hace que
    `action_taken in legal_actions` valga por CONSTRUCCION, no por
    casualidad de timing.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorders: dict[str, ProtocolRecorder] = defaultdict(ProtocolRecorder)
        self.steps: dict[str, list[dict | None]] = defaultdict(list)
        # Indices de self.steps[tag] cuyo "state" todavia es None: se llenan
        # sincronicamente en _finalize_pending_steps, nunca en una task de
        # fondo (ver docstring de la clase).
        self._pending_finalize: dict[str, list[int]] = defaultdict(list)
        # player_role no cambia en el curso de una batalla: capturarlo UNA
        # vez en choose_move deja a _correct_step_turns sin necesidad de
        # tocar `self.battles`/`battle` para nada.
        self._sides: dict[str, str] = {}

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
                break

        result = await super()._handle_battle_message(split_messages)
        # SIGUE siendo la misma llamada sincronica: ninguna otra invocacion de
        # _handle_battle_message para este tag puede haber corrido entre
        # medio. Si `choose_move` reservo un paso en algun punto de este
        # mismo lote (typicamente cero o uno; en cadenas de cambio forzado,
        # mas de uno), se finaliza AHORA, con `battle` ya al dia con TODA la
        # narracion de este lote.
        self._finalize_pending_steps(tag)
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
        order = super().choose_move(battle)
        tag = battle.battle_tag
        self._sides.setdefault(tag, battle.player_role)
        action_taken = action_from_order(order)
        # Todo esto se lee AHORA, sincronicamente, y nunca se vuelve a leer
        # de `battle`: `available_moves`/`available_switches` ya reflejan el
        # `|request|` que acaba de procesar `parse_request` (poke-env llama a
        # choose_move DESPUES de eso), asi que estan tan al dia como pueden
        # estarlo. `action_taken in legal_actions` queda garantizado por
        # construccion porque las dos vienen de esta MISMA linea de codigo.
        step = {
            "turn": battle.turn,
            "decision_turn": battle.turn,
            "action_taken": action_taken,
            "legal_actions": legal_actions(battle),
            "state": None,  # se completa en _finalize_pending_steps
        }
        index = len(self.steps[tag])
        self.steps[tag].append(step)
        self._pending_finalize[tag].append(index)
        return order

    def _finalize_pending_steps(self, tag: str) -> None:
        """Completa `state` (mi lado + el del rival + el resto del estado)
        para cada paso reservado durante ESTE MISMO lote de protocolo.

        Sincronico, nunca una task: se llama desde `_handle_battle_message`,
        justo despues de que `super()._handle_battle_message()` termino de
        procesar el lote completo (incluida la narracion que quedaba
        pendiente cuando `choose_move` corrio a mitad de camino). No hay
        forma de que otra decision se cuele entre medio: la unica manera de
        que se procese el lote de la decision SIGUIENTE es que esta misma
        llamada retorne primero.

        `turn` y `legal_actions` se sobreescriben con lo capturado en
        `choose_move` (nunca con lo que `serialize_battle` calcularia ahora):
        eso es lo que preserva el invariante `action_taken in legal_actions`
        aunque haya mas de un paso pendiente en el mismo lote (p.ej. un
        cambio forzado tras un debilitamiento).
        """
        pending = self._pending_finalize.get(tag)
        if not pending:
            return
        battle = self.battles.get(tag)
        if battle is None:
            return
        fresh_state = serialize_battle(battle)
        for index in pending:
            step = self.steps[tag][index]
            if step is None:
                continue
            step["state"] = {
                **fresh_state,
                "turn": step["decision_turn"],
                "legal_actions": step["legal_actions"],
            }
        self._pending_finalize[tag] = []

    async def wait_for_pending_steps(self, tag: str) -> None:
        """Corrige la etiqueta de turno de cada paso contra el protocolo.

        Ya no hay tasks de fondo que esperar (ver docstring de la clase): la
        materializacion del estado es sincronica, dentro del mismo manejo de
        mensajes. Se mantiene el nombre y la firma `async` por compatibilidad
        con `cli.py`, que llama esto antes de leer `self.steps[tag]`.

        Guarda defensiva (I-3 de la review de merge): si algun paso quedara
        sin finalizar —no deberia pasar nunca, dado como esta escrito
        `_handle_battle_message`— loguea en vez de persistir un `None` en
        silencio.
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
