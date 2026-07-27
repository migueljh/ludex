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


# --- C-1 (review de merge, D22): materializacion SINCRONICA, sin task de
# fondo ---
#
# El diseño anterior (D20, 3ea7caf) diferia la serializacion ENTERA a una
# task de fondo que esperaba, con timeout, a que crecieran las lineas del
# recorder. El problema: `battle` es mutable, y para cuando esa task por fin
# corria, el planificador podia haber despachado YA la decision SIGUIENTE,
# dejando la fila con `action_taken` de una decision y `legal_actions`/
# `state` de otra (medido: 7/6625 filas). La correccion: `choose_move`
# captura `legal_actions`/`action_taken`/`decision_turn` sincronicamente
# (nunca se leen de nuevo de `battle`) y `_finalize_pending_steps` completa
# el resto del `state` (mi lado + el del rival) tambien sincronicamente,
# llamado desde `_handle_battle_message` justo despues de que
# `super()._handle_battle_message()` termina de procesar el LOTE COMPLETO —
# nunca en una task planificada aparte, asi que ninguna decision futura
# puede colarse entre medio.


def _recorder_con(lineas_por_lote: list[list[str]]) -> ProtocolRecorder:
    recorder = ProtocolRecorder()
    for lote in lineas_por_lote:
        recorder.record([_split(l) for l in lote])
    return recorder


class FakeOrder:
    def __init__(self, mid=None, species=None):
        if species is not None:
            self.order = SimpleNamespace(species=species)
        else:
            self.order = SimpleNamespace(id=mid)


def test_choose_move_captura_legal_actions_y_action_taken_sincronicamente():
    """`legal_actions` y `action_taken` tienen que venir de la MISMA llamada
    a `choose_move`: es lo que garantiza `action_taken in legal_actions` por
    construccion, sin depender de ningun timing posterior."""
    player = _player()
    tag = "battle-x-1"

    class FakeMove:
        id = "tackle"

    battle = SimpleNamespace(
        turn=3, battle_tag=tag, player_role="p1",
        available_moves=[FakeMove()], available_switches=[], can_mega_evolve=False,
    )

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="tackle"),
    ):
        player.choose_move(battle)

    step = player.steps[tag][0]
    assert step["action_taken"] == {"kind": "move", "id": "tackle"}
    assert step["legal_actions"] == [{"kind": "move", "id": "tackle"}]
    assert step["decision_turn"] == 3
    assert step["state"] is None  # se completa en _finalize_pending_steps
    assert player._sides[tag] == "p1"


def test_choose_move_reserva_el_indice_en_orden_de_decision():
    """Dos decisiones seguidas (p.ej. un cambio forzado tras un
    debilitamiento) quedan en el orden en que se DECIDIERON."""
    player = _player()
    tag = "battle-x-1"
    battle = SimpleNamespace(
        turn=1, battle_tag=tag, player_role="p1",
        available_moves=[], available_switches=[], can_mega_evolve=False,
    )

    calls = []

    def fake_super_choose_move(self, b):
        order = FakeOrder(mid=f"move{len(calls)}")
        calls.append(order)
        return order

    with patch.object(client_module.RandomPlayer, "choose_move", fake_super_choose_move):
        player.choose_move(battle)
        player.choose_move(battle)

    assert len(player.steps[tag]) == 2
    assert [s["state"] for s in player.steps[tag]] == [None, None]
    assert player._pending_finalize[tag] == [0, 1]


def test_finalize_pending_steps_completa_state_con_lo_capturado_en_choose_move(monkeypatch):
    """`turn` y `legal_actions` del `state` finalizado tienen que quedar
    exactamente como los capturo `choose_move`, NUNCA como los recalcularia
    `serialize_battle` en el momento de finalizar (que podria reflejar ya la
    decision SIGUIENTE si hay mas de un paso pendiente en el mismo lote)."""
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append({
        "turn": 3, "decision_turn": 3,
        "action_taken": {"kind": "move", "id": "tackle"},
        "legal_actions": [{"kind": "move", "id": "tackle"}],
        "state": None,
    })
    player._pending_finalize[tag] = [0]
    battle = SimpleNamespace()
    player.battles[tag] = battle

    monkeypatch.setattr(
        client_module, "serialize_battle",
        lambda b: {"turn": 999, "legal_actions": ["lo que sea"], "opponent": {"pokemon": []}},
    )

    player._finalize_pending_steps(tag)

    step = player.steps[tag][0]
    assert step["state"]["turn"] == 3  # NO 999
    assert step["state"]["legal_actions"] == [{"kind": "move", "id": "tackle"}]  # NO recalculado
    assert step["state"]["opponent"] == {"pokemon": []}
    assert player._pending_finalize[tag] == []


def test_finalize_pending_steps_sin_pending_no_hace_nada():
    player = _player()
    player._finalize_pending_steps("tag-inexistente")  # no debe reventar


def test_finalize_pending_steps_sin_battle_no_revienta():
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append({
        "turn": 1, "decision_turn": 1, "action_taken": None,
        "legal_actions": [], "state": None,
    })
    player._pending_finalize[tag] = [0]
    player._finalize_pending_steps(tag)  # sin player.battles[tag]: no revienta
    assert player.steps[tag][0]["state"] is None  # queda sin finalizar, no crashea


async def test_wait_for_pending_steps_corrige_turnos_sin_ninguna_task():
    """Ya no hay tasks de fondo: solo corrige la etiqueta de turno."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([["|turn|4", "|move|p1a: X|Tackle|p2a: Y"]])
    player._sides[tag] = "p1"
    player.steps[tag] = [{
        "turn": 4, "decision_turn": 4, "state": {"turn": 4},
        "action_taken": {"kind": "move", "id": "tackle"},
    }]

    await player.wait_for_pending_steps(tag)

    assert player.steps[tag][0]["turn"] == 4


async def test_wait_for_pending_steps_sin_pasos_no_revienta():
    player = _player()
    await player.wait_for_pending_steps("tag-inexistente")


async def test_wait_for_pending_steps_loguea_si_algo_quedo_sin_finalizar(caplog):
    """Guarda I-3: si un paso quedara con `state=None` (no deberia pasar con
    la captura sincronica), tiene que quedar rastro en el log, no perderse
    en silencio."""
    player = _player()
    tag = "battle-x-1"
    player.steps[tag] = [{
        "turn": 1, "decision_turn": 1, "state": None,
        "action_taken": {"kind": "move", "id": "tackle"},
    }]

    with caplog.at_level(logging.ERROR):
        await player.wait_for_pending_steps(tag)

    assert any("sin materializar" in r.message for r in caplog.records)


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
    player._sides[tag] = "p1"
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
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 4, "decision_turn": 4, "state": {},
         "action_taken": {"kind": "move", "id": "outrage"}},
    ]

    player._correct_step_turns(tag)

    # Sin match dentro del margen: la etiqueta cruda (4) queda sin tocar, NO
    # se le asigna por error el turno 50.
    assert player.steps[tag][0]["turn"] == 4


def test_correct_step_turns_sin_side_conocido_no_revienta():
    player = _player()
    player.steps["tag-sin-side"] = [
        {"turn": 1, "state": {}, "action_taken": {"kind": "move", "id": "x"}}
    ]
    player._correct_step_turns("tag-sin-side")  # no debe lanzar


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


def test_discard_last_step_descarta_el_ultimo_paso():
    """El paso descartado llega siempre ya finalizado (ver docstring de
    `_discard_last_step`): no hay ninguna task de fondo que cancelar."""
    player = _player()
    tag = "battle-x-1"
    player.steps[tag].append({
        "turn": 3, "decision_turn": 3, "state": {"turn": 3},
        "action_taken": {"kind": "switch", "species": "regice"},
    })

    player._discard_last_step(tag)

    assert player.steps[tag] == []


async def test_discard_last_step_sin_pasos_no_revienta():
    player = _player()
    player._discard_last_step("tag-vacio")
