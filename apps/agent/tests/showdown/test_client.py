import asyncio
import logging
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ludex_agent.showdown import client as client_module
from ludex_agent.showdown.client import (
    LudexPlayer,
    _find_action_line,
    _normalize,
    battle_tag_from,
    local_server_configuration,
)
from ludex_agent.showdown.protocol import ProtocolRecorder


def _split(raw: str) -> list[str]:
    return raw.split("|")


def _player() -> LudexPlayer:
    from poke_env import AccountConfiguration

    # Sufijo aleatorio, igual que hace `cli.py` con los jugadores reales. Con
    # un nombre fijo, dos corridas seguidas de la suite chocan contra el
    # servidor local con `|nametaken|` y los tests de integracion erran en
    # bloque — un rojo que no tiene NADA que ver con lo que se esta probando.
    sufijo = random.randint(1000, 9999)
    return LudexPlayer(
        account_configuration=AccountConfiguration(f"Foo{sufijo}", None),
        battle_format="gen6randombattle",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
    )


def test_extrae_el_tag_de_la_primera_linea():
    lote = [[">battle-gen6randombattle-1"], _split("|init|battle")]
    assert battle_tag_from(lote) == "battle-gen6randombattle-1"


def test_encuentra_el_tag_aunque_no_sea_la_primera_linea():
    lote = [_split("|init|battle"), [">battle-gen9ou-42"]]
    assert battle_tag_from(lote) == "battle-gen9ou-42"


def test_sin_linea_de_tag_devuelve_none():
    assert battle_tag_from([_split("|init|battle"), _split("|turn|1")]) is None


def test_lote_vacio_y_lineas_vacias_no_revientan():
    assert battle_tag_from([]) is None
    assert battle_tag_from([[], [""]]) is None


def test_descarta_espacios_alrededor_del_tag():
    assert battle_tag_from([[">battle-x-1  "]]) == "battle-x-1"


def test_la_config_local_conserva_la_url_del_websocket():
    cfg = local_server_configuration("ws://localhost:8100/showdown/websocket")
    assert cfg.websocket_url == "ws://localhost:8100/showdown/websocket"


async def test_un_lote_sin_battle_tag_revienta_en_vez_de_perderse_en_silencio():
    """I1: perder un lote de protocolo rompe la re-derivacion (D17) de esa
    batalla entera. Antes se descartaba con un warning; ahora tiene que
    fallar ruidosamente."""
    player = _player()
    lote = [_split("|turn|1")]  # sin linea ">battle-..."; recorders vacio

    with pytest.raises(RuntimeError):
        await player._handle_battle_message(lote)


# --- C1 (D20): materializacion diferida, no espera antes de responder ---
#
# La primera version de este arreglo esperaba, DENTRO de
# `_handle_battle_message` y ANTES de delegar en `super()`, a que
# `battle.turn` alcanzara a `recorder._current_turn`. Instrumentado en vivo
# (sonda_deteccion_c1.py, en el scratchpad de la sesion), esa desigualdad NO
# se cumple nunca: el lote que trae el |request| todavia no vio la linea
# |turn| siguiente. Peor: esperar ahi bloquea nuestra propia respuesta, y la
# narracion es la RESPUESTA del servidor a que ambos jugadores ya eligieron
# (sonda_causalidad.py: retrasar la respuesta 500ms retrasa la narracion
# exactamente 500ms). Por eso `choose_move` ya no espera nada: responde de
# inmediato y DIFIERE la serializacion a `_materialize_step`, en una task de
# fondo que no bloquea la respuesta.


def _recorder_con(lineas_por_lote: list[list[str]]) -> ProtocolRecorder:
    recorder = ProtocolRecorder()
    for lote in lineas_por_lote:
        recorder.record([_split(l) for l in lote])
    return recorder


async def test_materialize_step_no_espera_si_ya_hay_lineas_nuevas(monkeypatch):
    """Si para cuando se llama ya hay mas lineas que el baseline, no hace
    falta ninguna espera: serializa de inmediato."""
    player = _player()
    battle = SimpleNamespace(turn=1, finished=False, battle_tag="battle-x-1")
    recorder = _recorder_con([["|turn|1"], ["|move|p1a: X|Tackle|p2a: Y"]])
    baseline = 1  # solo la linea de |turn|1
    player.steps["battle-x-1"].append(None)  # el indice ya reservado por choose_move

    calls = []
    monkeypatch.setattr(
        client_module, "serialize_battle",
        lambda b: (calls.append(b), {"turn": 1, "legal_actions": []})[1],
    )

    await player._materialize_step(
        battle, recorder, "battle-x-1", 0, baseline, 1,
        {"kind": "move", "id": "tackle"},
    )

    assert player.turn_alignment_timeouts == 0
    assert len(calls) == 1
    assert player.steps["battle-x-1"][0]["action_taken"] == {"kind": "move", "id": "tackle"}


async def test_materialize_step_espera_hasta_que_crezca_el_recorder(monkeypatch):
    """El detector es generico (mas lineas, sean las que sean), no atado al
    numero de turno: un cambio forzado tras un debilitamiento (C2) trae
    narracion nueva sin mover el turno."""
    player = _player()
    battle = SimpleNamespace(turn=1, finished=False, battle_tag="battle-x-1")
    recorder = _recorder_con([["|turn|1"]])
    player.steps["battle-x-1"].append(None)

    async def fake_sleep(seconds):
        recorder.record([_split("|move|p1a: X|Tackle|p2a: Y")])

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        client_module, "serialize_battle", lambda b: {"turn": 1, "legal_actions": []}
    )

    await player._materialize_step(
        battle, recorder, "battle-x-1", 0, recorder.line_count, 1,
        {"kind": "move", "id": "tackle"},
    )

    assert player.turn_alignment_timeouts == 0
    assert player.steps["battle-x-1"][0]["action_taken"] == {"kind": "move", "id": "tackle"}


async def test_materialize_step_timeout_no_cuelga_si_no_llega_mas_narracion(monkeypatch):
    """La batalla termino justo despues del |request| y no llega nada mas:
    tiene que cortar por timeout, no colgarse."""
    player = _player()
    battle = SimpleNamespace(turn=5, finished=False, battle_tag="battle-x-1")
    recorder = _recorder_con([["|turn|5"]])
    player.steps["battle-x-1"].append(None)

    reloj = {"t": 0.0}

    def fake_monotonic():
        return reloj["t"]

    async def fake_sleep(seconds):
        reloj["t"] += client_module.TURN_ALIGNMENT_TIMEOUT_SECONDS + 1

    monkeypatch.setattr(client_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        client_module, "serialize_battle", lambda b: {"turn": 5, "legal_actions": []}
    )

    await player._materialize_step(
        battle, recorder, "battle-x-1", 0, recorder.line_count, 5, None
    )

    assert player.turn_alignment_timeouts == 1
    assert player.steps["battle-x-1"][0] is not None  # igual serializa, con el desfase


async def test_choose_move_reserva_el_indice_antes_de_la_task_de_fondo():
    """Dos decisiones seguidas (p.ej. un cambio forzado tras un
    debilitamiento) tienen que quedar en el orden en que se DECIDIERON, no en
    el orden en que termine de esperar su propia task."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag].record([_split("|turn|1")])

    class FakeOrder:
        def __init__(self, mid):
            self.order = SimpleNamespace(id=mid)

    async def nunca_termina(*a, **kw):
        # nunca completa de verdad en este test: solo importa que el indice
        # ya haya sido reservado ANTES de que esta corra.
        await asyncio.sleep(1000)

    battle = SimpleNamespace(
        turn=1, finished=False, battle_tag=tag, player_role="p1",
    )

    calls = []

    def fake_super_choose_move(self, b):
        order = FakeOrder(f"move{len(calls)}")
        calls.append(order)
        return order

    with patch.object(client_module.RandomPlayer, "choose_move", fake_super_choose_move):
        with patch.object(player, "_materialize_step", nunca_termina):
            player.choose_move(battle)
            player.choose_move(battle)

    assert len(player.steps[tag]) == 2
    assert all(s is None for s in player.steps[tag])
    assert len(player._pending_step_tasks[tag]) == 2

    for task in player._pending_step_tasks[tag]:
        task.cancel()
    await asyncio.gather(*player._pending_step_tasks[tag], return_exceptions=True)


async def test_wait_for_pending_steps_espera_todas_las_tasks():
    player = _player()
    tag = "battle-x-1"

    async def marca(idx):
        player.steps[tag][idx] = {"marcado": True}

    player.steps[tag] = [None, None]
    player._pending_step_tasks[tag] = [
        asyncio.create_task(marca(0)), asyncio.create_task(marca(1))
    ]

    await player.wait_for_pending_steps(tag)

    assert player.steps[tag] == [{"marcado": True}, {"marcado": True}]


async def test_wait_for_pending_steps_sin_tasks_no_revienta():
    player = _player()
    await player.wait_for_pending_steps("tag-inexistente")


# --- _find_action_line / _correct_step_turns: correccion de la etiqueta de
# turno contra el protocolo, con un cursor GLOBAL (D20, segunda vuelta) ---


def test_find_action_line_encuentra_el_turno_del_movimiento():
    recorder = _recorder_con([
        ["|turn|4", "|move|p1a: X|Tackle|p2a: Y"],
        ["|turn|5", "|move|p1a: X|Recover|p1a: X"],
    ])
    turno, siguiente = _find_action_line(
        recorder, "p1", {"kind": "move", "id": "tackle"}, 0, max_turn=4
    )
    assert turno == 4


def test_find_action_line_encuentra_el_turno_del_switch():
    recorder = _recorder_con([
        ["|turn|4"],
        ["|turn|5", "|switch|p1a: Magnezone|Magnezone, L83|252/252"],
    ])
    turno, _ = _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "magnezone"}, 0, max_turn=5
    )
    assert turno == 5


def test_find_action_line_ancla_en_cant_cuando_la_accion_no_se_ejecuto():
    """El movimiento elegido no se ejecuto porque el juego lo impidio.

    Showdown SI deja rastro: `|cant|`. La fila pertenece al turno donde la
    decision se RESOLVIO, y esa resolucion es "no pudo moverse". Antes esto
    devolvia None y la etiqueta se quedaba un turno atras: era el 100% del
    residual que sobrevivio a la primera vuelta de C1.
    """
    recorder = _recorder_con([["|turn|4", "|cant|p1a: X|par"]])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "tackle"}, 0, max_turn=4
    ) == (4, 2)


def test_find_action_line_ignora_un_faint_posterior_al_propio_movimiento():
    """El respaldo NO puede robarle la resolucion a una accion que si se
    ejecuto: si el pokemon propio se movio y RECIEN DESPUES lo debilitaron,
    la evidencia buena es el `|move|`, no el `|faint|`."""
    recorder = _recorder_con([[
        "|turn|4", "|move|p1a: X|Tackle|p2a: Y", "|faint|p1a: X",
    ]])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "tackle"}, 0, max_turn=4
    ) == (4, 2)


def test_find_action_line_sin_ninguna_evidencia_devuelve_none():
    """Sin `|move|`, sin `|switch|`, sin `|cant|` y sin `|faint|` propio no
    hay nada que corregir: el llamador deja el turno tal cual."""
    recorder = _recorder_con([["|turn|4", "|move|p2a: Y|Tackle|p1a: X"]])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "tackle"}, 0, max_turn=4
    ) is None


def test_find_action_line_accion_none_devuelve_none():
    recorder = _recorder_con([["|turn|4"]])
    assert _find_action_line(recorder, "p1", None, 0, max_turn=4) is None


def test_find_action_line_no_reusa_una_linea_ya_consumida():
    """El hallazgo clave de la segunda vuelta de D20: dos decisiones que
    mencionan la MISMA especie (dos Outrage seguidos por el bloqueo del
    movimiento) no pueden compartir la misma linea de protocolo. Buscar
    desde el cursor que devolvio la primera decision fuerza a encontrar la
    SEGUNDA aparicion, no la primera de nuevo."""
    recorder = _recorder_con([
        ["|turn|4", "|move|p1a: X|Outrage|p2a: Y"],
        ["|turn|5", "|move|p1a: X|Outrage|p2a: Y"],
    ])
    accion = {"kind": "move", "id": "outrage"}
    primero = _find_action_line(recorder, "p1", accion, 0, max_turn=4)
    assert primero is not None
    turno1, cursor1 = primero
    assert turno1 == 4

    segundo = _find_action_line(recorder, "p1", accion, cursor1, max_turn=5)
    assert segundo is not None
    turno2, _ = segundo
    assert turno2 == 5, "no debe reencontrar la linea de turno 4 ya consumida"


def test_find_action_line_no_pasa_el_techo_de_turno():
    """El techo (`max_turn`) evita que una decision encuentre por accidente
    una repeticion lejana del mismo nombre.

    Lo que se prueba es que NO devuelve el turno 50. Devuelve el turno 4,
    donde el `|cant|` muestra por que esa decision nunca se ejecuto: esa es
    la resolucion real y es la etiqueta correcta.
    """
    recorder = _recorder_con([
        ["|turn|4", "|cant|p1a: X|par"],
        ["|turn|50", "|move|p1a: X|Outrage|p2a: Y"],
    ])
    encontrado = _find_action_line(
        recorder, "p1", {"kind": "move", "id": "outrage"}, 0, max_turn=6
    )
    assert encontrado is not None
    assert encontrado[0] == 4, "el techo tiene que impedir el salto al turno 50"


def test_correct_step_turns_corrige_en_orden_con_un_cursor_que_avanza():
    """Prueba de integracion chica: dos decisiones "outrage" seguidas,
    ambas con `turn` crudo (sin corregir) apuntando al mismo numero, deben
    terminar en turnos DISTINTOS tras la correccion."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        ["|turn|4", "|move|p1a: X|Outrage|p2a: Y"],
        ["|turn|5", "|move|p1a: X|Outrage|p2a: Y"],
    ])
    player.battles[tag] = SimpleNamespace(player_role="p1")
    player.steps[tag] = [
        {"turn": 4, "decision_turn": 4, "state": {},
         "action_taken": {"kind": "move", "id": "outrage"}},
        {"turn": 4, "decision_turn": 4, "state": {},
         "action_taken": {"kind": "move", "id": "outrage"}},
    ]

    player._correct_step_turns(tag)

    assert [s["turn"] for s in player.steps[tag]] == [4, 5]


def test_correct_step_turns_no_encuentra_mas_alla_del_margen():
    """Una decision excusada (sin match) no debe permitir que la SIGUIENTE
    decision encuentre una repeticion muy lejana del mismo nombre."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        ["|turn|4", "|cant|p1a: X|par"],
        ["|turn|50", "|move|p1a: X|Outrage|p2a: Y"],
    ])
    player.battles[tag] = SimpleNamespace(player_role="p1")
    player.steps[tag] = [
        {"turn": 4, "decision_turn": 4, "state": {},
         "action_taken": {"kind": "move", "id": "outrage"}},
    ]

    player._correct_step_turns(tag)

    # Sin match dentro del margen: la etiqueta cruda (4) queda sin tocar, NO
    # se le asigna por error el turno 50.
    assert player.steps[tag][0]["turn"] == 4


def test_correct_step_turns_sin_battle_no_revienta():
    player = _player()
    player.steps["tag-sin-battle"] = [
        {"turn": 1, "state": {}, "action_taken": {"kind": "move", "id": "x"}}
    ]
    player._correct_step_turns("tag-sin-battle")  # no debe lanzar


def test_normalize_saca_toda_la_puntuacion():
    assert _normalize("Farfetch’d") == "farfetchd"
    assert _normalize("Mr. Mime") == "mrmime"


# --- eleccion rechazada por el servidor: descartar el paso fantasma ---
#
# Hallazgo nuevo (no estaba en el brief, se encontro verificando C2 contra
# datos reales): un pokemon "maybeTrapped" puede resultar atrapado de
# verdad, y el servidor rechaza el switch elegido con
# `|error|[Unavailable choice] Can't switch: ...`. poke-env vuelve a llamar
# a choose_move para la MISMA decision (con un rqid nuevo). Sin descartar la
# eleccion rechazada, quedaba grabado un paso que nunca se ejecuto.


async def _sin_super(*a, **kw):
    """No-op: evita que _handle_battle_message llegue al poke-env real, que
    bloquearia esperando una batalla que estos tests nunca crean via `init`."""
    return None


async def test_un_error_de_eleccion_no_disponible_descarta_el_ultimo_paso():
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append({"action_taken": {"kind": "switch", "species": "regice"}})
    player.recorders[tag]  # crea el recorder por defaultdict

    lote = [_split(">battle-x-1"), _split(
        "|error|[Unavailable choice] Can't switch: The active Pokémon is trapped"
    )]
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message(lote)

    assert player.steps[tag] == []


async def test_un_error_de_eleccion_invalida_tambien_descarta_el_ultimo_paso():
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append({"action_taken": {"kind": "move", "id": "tackle"}})

    lote = [_split(">battle-x-1"), _split("|error|[Invalid choice] Move disabled")]
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message(lote)

    assert player.steps[tag] == []


async def test_un_error_no_relacionado_no_descarta_nada():
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append({"action_taken": {"kind": "move", "id": "tackle"}})

    lote = [_split(">battle-x-1"), _split("|error|[Something else] no importa")]
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message(lote)

    assert len(player.steps[tag]) == 1


async def test_discard_last_step_cancela_la_task_de_fondo_pendiente():
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append(None)

    async def nunca_termina():
        await asyncio.sleep(1000)

    task = asyncio.create_task(nunca_termina())
    player._pending_step_tasks[tag].append(task)

    player._discard_last_step(tag)

    assert player.steps[tag] == []
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_discard_last_step_sin_pasos_no_revienta():
    player = _player()
    player._discard_last_step("tag-vacio")
