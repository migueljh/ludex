import asyncio
import logging
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from poke_env.data import GenData

from ludex_agent.graph.provider import TransientProviderError
from ludex_agent.showdown import client as client_module
from ludex_agent.showdown.client import (
    LudexPlayer,
    _find_action_line,
    _normalize,
    battle_tag_from,
    local_server_configuration,
)
from ludex_agent.showdown.protocol import CURRENT_FRAME_SEQ, ProtocolRecorder


def _split(raw: str) -> list[str]:
    return raw.split("|")


def _player(**kwargs) -> LudexPlayer:
    from poke_env import AccountConfiguration

    # Sufijo aleatorio, igual que hace `cli.py` con los jugadores reales. Con
    # un nombre fijo, dos corridas seguidas de la suite chocan contra el
    # servidor local con `|nametaken|` y los tests de integracion erran en
    # bloque — un rojo que no tiene NADA que ver con lo que se esta probando.
    sufijo = random.randint(1000, 9999)
    kwargs.setdefault("start_listening", False)
    return LudexPlayer(
        account_configuration=AccountConfiguration(f"Foo{sufijo}", None),
        battle_format="gen6randombattle",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
        **kwargs,
    )


def _fake_battle(**overrides) -> SimpleNamespace:
    """Battle minimo que `serialize_battle` puede recorrer entero.

    Desde D31 `choose_move` serializa en las DOS rutas (antes solo la del
    grafo lo hacia), asi que un doble sin `format`/`gen`/`weather` ya no
    alcanza.
    """
    base = dict(
        turn=3, battle_tag="battle-x-1", player_role="p1",
        format="gen6randombattle", gen=6,
        weather={}, fields={}, side_conditions={}, opponent_side_conditions={},
        team={}, opponent_team={},
        available_moves=[], available_switches=[], can_mega_evolve=False,
        active_pokemon=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _descartar(coro) -> None:
    """`choose_move` devuelve una coroutine en ambas rutas (D31). Cuando el
    test solo mira la captura SINCRONICA, se cierra sin ejecutarla."""
    coro.close()


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

    battle = _fake_battle(battle_tag=tag, available_moves=[FakeMove()])

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="tackle"),
    ):
        _descartar(player.choose_move(battle))

    step = player.steps[tag][0]
    assert step["action_taken"] == {"kind": "move", "id": "tackle"}
    assert step["legal_actions"] == [{"kind": "move", "id": "tackle"}]
    assert step["decision_turn"] == 3
    assert step["state"] is None  # lo completa la proyeccion pre-lock
    assert player._sides[tag] == "p1"


def test_choose_move_reserva_el_indice_en_orden_de_decision():
    """Dos decisiones seguidas (p.ej. un cambio forzado tras un
    debilitamiento) quedan en el orden en que se DECIDIERON."""
    player = _player()
    tag = "battle-x-1"
    battle = _fake_battle(turn=1, battle_tag=tag)

    calls = []

    def fake_super_choose_move(self, b):
        order = FakeOrder(mid=f"move{len(calls)}")
        calls.append(order)
        return order

    with patch.object(client_module.RandomPlayer, "choose_move", fake_super_choose_move):
        _descartar(player.choose_move(battle))
        # La decision siguiente solo puede reservarse despues de que el
        # servidor resolvio la anterior. Este test aisla la numeracion; los
        # tests contractuales de request/win cubren la señal de resolucion.
        player._resolve_pending_choice(tag)
        _descartar(player.choose_move(battle))

    assert len(player.steps[tag]) == 2
    assert [s["state"] for s in player.steps[tag]] == [None, None]
    assert [s["action_taken"]["id"] for s in player.steps[tag]] == ["move0", "move1"]


async def test_grafo_usa_foto_y_mapa_capturados_antes_del_primer_await():
    tag = "battle-graph-1"

    class FakeMove:
        id = "tackle"

    battle = _fake_battle(battle_tag=tag, available_moves=[FakeMove()])

    class MutatingGraph:
        async def ainvoke(self, graph_input):
            assert graph_input["raw_state"]["legal_actions"] == [
                {"kind": "move", "id": "tackle"}
            ]
            battle.available_moves = [SimpleNamespace(id="surf")]
            battle.turn = 99
            return {
                "action": {"kind": "move", "id": "tackle"},
                "action_path": "llm",
            }

    player = _player(decision_graph=MutatingGraph(), decision_budget_seconds=5)
    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "turn": b.turn,
            "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [
                {"kind": "move", "id": move.id}
                for move in b.available_moves
            ],
        },
    ):
        pending = player.choose_move(battle)
        # La coroutine todavía no empezó: si snapshot/mapa se construyeran
        # dentro de ella, esta mutación contaminaría la decisión.
        battle.available_moves = [SimpleNamespace(id="surf")]
        battle.turn = 99
        await player.frame_inbox.publish(tag, ("|upkeep", "|turn|4"))
        order = await pending

    step = player.steps[tag][0]
    assert order.order.id == "tackle"
    assert step["decision_turn"] == 3
    assert step["legal_actions"] == [{"kind": "move", "id": "tackle"}]
    # El turno sale del `|turn|N` de la narración previa, no de battle.turn.
    assert step["state"]["turn"] == 4
    assert step["action_taken"] == {"kind": "move", "id": "tackle"}
    assert step["action_path"] == "llm"


async def test_grafo_mapea_variante_mega_a_su_battle_order():
    tag = "battle-graph-mega"
    move = SimpleNamespace(id="meteormash")
    battle = _fake_battle(
        turn=1, battle_tag=tag, available_moves=[move], can_mega_evolve=True
    )

    class MegaGraph:
        async def ainvoke(self, graph_input):
            return {
                "action": {"kind": "move", "id": "meteormash", "mega": True},
                "action_path": "fallback",
            }

    player = _player(decision_graph=MegaGraph())
    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "turn": 1, "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": client_module.legal_actions(b),
        },
    ):
        pending = player.choose_move(battle)
        await player.frame_inbox.publish(tag, ("|upkeep",))
        order = await pending

    assert order.mega is True
    assert player.steps[tag][0]["action_path"] == "fallback"


async def test_timeout_de_proyeccion_no_deja_ninguna_fila_persistible():
    """Fallo CERRADO (D31). Si la narracion previa no llega, NO se decide con
    el snapshot stale ni se persiste una fila degradada: se descarta el paso
    reservado y el error se propaga.

    El contador solo no alcanza como aserción: lo que importa es que no
    quede NADA persistible, porque una fila marcada igual contaminaria el
    corpus antes de que el auditor pudiera excluirla."""
    class NeverCalledGraph:
        async def ainvoke(self, graph_input):  # pragma: no cover
            raise AssertionError(
                "el proveedor no puede invocarse con estado stale"
            )

    player = _player(
        decision_graph=NeverCalledGraph(), projection_timeout_seconds=0.01
    )
    tag = "battle-timeout"
    battle = _fake_battle(
        battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
    )

    pending = player.choose_move(battle)
    # Se reservo el paso sincronicamente...
    assert len(player.steps[tag]) == 1
    # ...pero nunca llega narracion.
    with pytest.raises(client_module.ProjectionTimeoutError):
        await pending

    assert player.steps[tag] == [], (
        "el paso reservado tiene que desaparecer: una fila que exista debe "
        "tener proyeccion valida por construccion"
    )
    assert player.projection_timeout_count == 1


async def test_swapboost_falla_cerrado_sin_dejar_fila_ni_invocar_proveedor():
    """Mismo mecanismo de fallo cerrado que el timeout (D31), pero para
    `ProjectionAmbiguityError` (TECH LEAD REVIEW sobre `410eabb`, finding 3):
    un `-swapboost` real no es representable sin el boost propio de antes
    del intercambio, y `_resolve_state` tiene que descartar el paso
    reservado en vez de persistir un boost del rival sabidamente stale."""
    class NeverCalledGraph:
        async def ainvoke(self, graph_input):  # pragma: no cover
            raise AssertionError(
                "el proveedor no puede invocarse con un boost ambiguo"
            )

    player = _player(decision_graph=NeverCalledGraph())
    tag = "battle-swapboost"
    battle = _fake_battle(
        battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
    )

    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "turn": b.turn,
            "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        },
    ):
        pending = player.choose_move(battle)
        assert len(player.steps[tag]) == 1
        await player.frame_inbox.publish(
            tag, ("|-swapboost|p1a: Tentacruel|p2a: Ludicolo|spa",)
        )
        with pytest.raises(client_module.ProjectionAmbiguityError):
            await pending

    assert player.steps[tag] == [], (
        "el paso reservado tiene que desaparecer, igual que con el timeout"
    )
    assert player.projection_ambiguity_count == 1


async def test_timeout_consume_el_presupuesto_de_decision():
    """La espera no puede exceder el presupuesto de la decision."""
    player = _player(
        decision_graph=None, projection_timeout_seconds=30,
        decision_budget_seconds=0.01,
    )
    tag = "battle-budget"
    battle = _fake_battle(battle_tag=tag)

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="tackle"),
    ):
        pending = player.choose_move(battle)
        with pytest.raises(client_module.ProjectionTimeoutError):
            await pending

    assert player.steps[tag] == []


async def test_reintento_reusa_solo_la_parte_publica_de_la_proyeccion():
    """Tras una eleccion rechazada no hay resolucion nueva que esperar, asi
    que el reintento reusa la ultima proyeccion publica. Pero SOLO
    `opponent`/`field`/`turn`: la mascara pudo cambiar al descubrirse
    `trapped`, y el snapshot propio del reintento es el nuevo."""
    player = _player()
    tag = "battle-retry"

    player._last_projection[tag] = {
        "turn": 7,
        "opponent": {"pokemon": [{"species": "latias", "active": True}]},
        "field": {"weather": {"RAINDANCE": 5}, "field_effects": {},
                  "my_side": {}, "opponent_side": {}},
        "legal_actions": [{"kind": "move", "id": "yaviejo"}],
    }
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    _reservar_choice(player, tag, rqid=4)
    await _enviar(player, tag, "/choose move tackle")
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"],
            _split("|error|[Unavailable choice] Move disabled"),
        ])
        await player._handle_battle_message([
            [f">{tag}"],
            _split('|request|{"rqid":6}'),
        ])

    # En el reintento el pokemon resulto atrapado: solo queda un movimiento.
    battle = _fake_battle(
        battle_tag=tag, turn=99,
        last_request={"rqid": 6},
        available_moves=[SimpleNamespace(id="struggle")],
    )
    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="struggle"),
    ):
        pending = player.choose_move(battle)
        await pending

    estado = player.steps[tag][0]["state"]
    assert estado["opponent"]["pokemon"][0]["species"] == "latias"
    assert estado["field"]["weather"] == {"RAINDANCE": 5}
    assert estado["turn"] == 7
    # La mascara NUEVA, no la de la proyeccion vieja.
    assert estado["legal_actions"] == [{"kind": "move", "id": "struggle"}]
    # La marca se consume exactamente una vez.
    assert player._retry_pending.get(tag) in (None, False)


async def test_reintento_sin_proyeccion_previa_falla_ruidosamente():
    player = _player()
    tag = "battle-retry-sin-nada"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    _reservar_choice(player, tag, rqid=4)
    await _enviar(player, tag, "/choose move tackle")
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"],
            _split("|error|[Unavailable choice] Move disabled"),
        ])
        await player._handle_battle_message([
            [f">{tag}"],
            _split('|request|{"rqid":6}'),
        ])
    battle = _fake_battle(battle_tag=tag, last_request={"rqid": 6})

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="tackle"),
    ):
        pending = player.choose_move(battle)
        with pytest.raises(RuntimeError, match="sin proyeccion publica"):
            await pending

    assert player.steps[tag] == []


# --- MON-18/D37, LINEAR_VERDICT R1 L-03: el diseño de la marca de PP bajo
# Pressure depende de que dos resoluciones del MISMO battle_tag reusen el
# MISMO objeto `persistent_state` (`self._temporary_state.setdefault(tag,
# {})`, sin tocar). Un contrapeso que arme dos dicts manuales y los pase a
# `project_observable_state` directamente no prueba esto: seguiria en
# verde aunque el caller real dejara de reutilizar el dict. Este test
# atraviesa el flujo publico real (`choose_move`) dos veces para el mismo
# tag y espia `project_observable_state` (sin modificar produccion) para
# capturar el objeto que RECIBE en cada llamada.


async def test_dos_resoluciones_del_mismo_battle_tag_comparten_persistent_state():
    tag = "battle-persistent-identity"
    capturados: list[dict] = []
    real = client_module.project_observable_state

    def espia(*args, **kwargs):
        capturados.append(kwargs["persistent_state"])
        return real(*args, **kwargs)

    class FakeMove:
        id = "tackle"

    battle = _fake_battle(battle_tag=tag, available_moves=[FakeMove()])
    player = _player(decision_graph=None)

    with patch.object(client_module, "project_observable_state", espia), \
         patch.object(
             client_module, "serialize_battle",
             lambda b: {
                 "turn": b.turn,
                 "opponent": {"pokemon": []},
                 "field": {"weather": {}, "field_effects": {},
                           "my_side": {}, "opponent_side": {}},
                 "legal_actions": [{"kind": "move", "id": "tackle"}],
             },
         ), patch.object(
             client_module.RandomPlayer, "choose_move",
             lambda self, b: FakeOrder(mid="tackle"),
         ):
        pending1 = player.choose_move(battle)
        await player.frame_inbox.publish(tag, ("|upkeep",))
        await pending1
        player._resolve_pending_choice(tag)

        pending2 = player.choose_move(battle)
        await player.frame_inbox.publish(tag, ("|upkeep",))
        await pending2

    assert len(capturados) == 2
    assert capturados[0] is capturados[1], (
        "las dos resoluciones del mismo battle_tag tienen que compartir el "
        "MISMO objeto persistent_state -- si el caller reemplazara "
        "setdefault(tag, {}) por un dict nuevo en cada llamada, esto "
        "fallaria aunque cada resolucion individual siguiera viendose bien"
    )


async def _noop_async(self, split_messages):
    return None


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


def test_find_action_line_no_matchea_un_sufijo_from_move_de_otro_movimiento():
    """Hallazgo real (F2-01, segunda ronda), NO teorizado: Sleep Talk que
    llama a Rest se narra `|move|p1a: X|Rest||[from] move: Sleep Talk|[still]`.
    Buscar "sleeptalk" como SUBSTRING de la linea entera matcheaba esa linea
    de Rest, aunque el nombre elegido este solo en el SUFIJO que dice quien
    causo el movimiento, no en `parts[3]` (el movimiento que de verdad se
    ejecuto). Medido en `battle-gen6randombattle-1925`: la decision 32
    (tambien Sleep Talk) se apropiaba de esta linea -- la misma que ya habia
    resuelto la decision anterior -- y quedaba con el turno equivocado.

    Sin anclar al token, esta decision encontraria erroneamente el turno 4
    (la linea de Rest) en vez de su propia resolucion en el turno 5.
    """
    recorder = _recorder_con([
        ["|turn|4", "|move|p1a: X|Rest||[from] move: Sleep Talk|[still]"],
        ["|turn|5", "|move|p1a: X|Sleep Talk|p1a: X"],
    ])
    accion = {"kind": "move", "id": "sleeptalk"}
    encontrado = _find_action_line(recorder, "p1", accion, 0, max_turn=5)
    assert encontrado is not None
    turno, _ = encontrado
    assert turno == 5, "no puede matchear el 'Sleep Talk' del sufijo [from]"


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


def test_find_action_line_no_sigue_de_largo_tras_encontrar_respaldo():
    """Defecto 1 (fix-cursor), segunda causa real: si esta decision ya se
    resolvio por `|cant|` DENTRO de su propio turno, un `|move|` del MISMO
    nombre en un turno POSTERIOR no puede ser evidencia de ESTA decision —
    tiene que ser el de la decision SIGUIENTE, que repite el movimiento
    (reproduccion minima de battle-gen6randombattle-408: Volbeat congelado
    elige Encore, se resuelve como `|cant|...|frz` en su propio turno; dos
    turnos despues el SIGUIENTE intento de Encore falla y narra
    `|move|...Encore||[still]`). Sin este corte, la primera decision se
    apropiaba de la linea de la segunda."""
    recorder = _recorder_con([
        ["|turn|4", "|cant|p1a: X|frz"],
        ["|turn|6", "|move|p1a: X|Encore||[still]"],
    ])
    encontrado = _find_action_line(
        recorder, "p1", {"kind": "move", "id": "encore"}, 0, max_turn=7,
        actor_species="x",
    )
    assert encontrado == (4, 2), (
        "tiene que quedarse en el cant de SU propio turno, no robarle "
        "la linea de Encore a la decision siguiente"
    )


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


# --- Defecto 1 (fix-cursor): el autogolpe por confusion no deja `|move|` ni
# `|cant|`, solo `|-activate|{side}a: Name|confusion`. Sin reconocerlo, la
# busqueda se pasa de largo y el respaldo content-blind puede aceptar el
# `|faint|`/`|cant|` de OTRO pokemon, en un turno posterior, robandole la
# linea a la decision siguiente (ver docs/DECISIONS.md D22, batalla real
# battle-gen6randombattle-398, decision 45). ---


def test_find_action_line_ancla_en_autogolpe_por_confusion():
    """Showdown SI deja rastro de un autogolpe por confusion: `-activate`
    seguido de `confusion`. Antes de este fix, `_find_action_line` no lo
    miraba y la busqueda se pasaba de largo hasta encontrar (o no) otra
    cosa mas adelante."""
    recorder = _recorder_con([
        ["|turn|4", "|-activate|p1a: Muk|confusion", "|-damage|p1a: Muk|132/328"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "brickbreak"}, 0, max_turn=4,
        actor_species="muk",
    ) == (4, 2)


def test_find_action_line_respaldo_no_roba_faint_de_otro_pokemon():
    """El respaldo (`|faint|`) NO puede resolver la decision de un pokemon
    con el nombre de OTRO: sin este chequeo, un debilitamiento no
    relacionado (de un pokemon distinto, en un turno posterior) se acepta
    como si fuera la resolucion de la decision actual — y eso es
    exactamente lo que le robaba la linea a la decision siguiente en la
    batalla 398."""
    recorder = _recorder_con([
        ["|turn|4"],
        ["|turn|5", "|faint|p1a: Swellow"],
    ])
    accion = {"kind": "move", "id": "brickbreak"}
    # Con el actor correcto (Muk) declarado, el faint de Swellow no cuenta:
    # no hay evidencia real de Muk en la ventana, resultado None.
    assert _find_action_line(
        recorder, "p1", accion, 0, max_turn=7, actor_species="muk"
    ) is None


def test_find_action_line_respaldo_de_faint_del_mismo_pokemon_si_cuenta():
    """El chequeo de actor no rompe el caso que SI esta cubierto desde
    2651081: mi propio pokemon (el mismo que iba a actuar) se debilito
    antes de poder ejecutar la accion elegida."""
    recorder = _recorder_con([["|turn|4", "|faint|p1a: Muk"]])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "brickbreak"}, 0, max_turn=4,
        actor_species="muk",
    ) == (4, 2)


def test_find_action_line_sin_actor_species_mantiene_compat():
    """Sin pasar `actor_species` (firma vieja / llamador que no lo conoce),
    el chequeo queda deshabilitado: no rompe a nadie que no lo use."""
    recorder = _recorder_con([
        ["|turn|4"],
        ["|turn|5", "|faint|p1a: Swellow"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "brickbreak"}, 0, max_turn=7
    ) == (5, 3)


def test_correct_step_turns_autogolpe_por_confusion_no_le_roba_la_linea_a_la_siguiente():
    """Reproduccion minima de la batalla real (battle-gen6randombattle-398,
    decisiones 45/46): Muk elige Brick Break, se autogolpea por confusion en
    el turno 4 (sin `|move|` ni `|cant|`), y en el turno 5 el jugador cambia
    a Ludicolo. Antes del fix, la decision de Muk se quedaba sin encontrar
    nada dentro del margen viejo O (con margen mas largo) terminaba
    aceptando un `|faint|` de otro pokemon mas adelante, adelantando el
    cursor y dejando el cambio a Ludicolo sin encontrar SU linea."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        [
            "|turn|4",
            "|-activate|p1a: Muk|confusion",
            "|-damage|p1a: Muk|132/328",
        ],
        ["|turn|5", "|switch|p1a: Ludicolo|Ludicolo, L88, M|57/284"],
    ])
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 4, "decision_turn": 4, "state": {}, "actor_species": "muk",
         "action_taken": {"kind": "move", "id": "brickbreak"}},
        {"turn": 4, "decision_turn": 4, "state": {}, "actor_species": None,
         "action_taken": {"kind": "switch", "species": "ludicolo"}},
    ]

    player._correct_step_turns(tag)

    assert player.steps[tag][0]["turn"] == 4, "Muk se resolvio en el turno del autogolpe"
    assert player.steps[tag][1]["turn"] == 5, (
        "el cambio a Ludicolo tiene que encontrar SU propia linea, no "
        "quedar bloqueado por el autogolpe de la decision anterior"
    )


def test_find_action_line_ancla_en_encore_del_rival():
    """fix-flaky (D23): reproduccion minima de
    `battle-gen6randombattle-558` (cazada en vivo, ver docs/DECISIONS.md
    D23). Skarmory elige Spikes; el rival lo encorea (prioridad +2) antes de
    que Spikes se ejecute, y Showdown fuerza la repeticion del ultimo
    movimiento usado (Stealth Rock) en su lugar. La accion elegida
    ('spikes') no aparece en NINGUN lado del protocolo."""
    recorder = _recorder_con([
        [
            "|turn|26",
            "|move|p2a: Politoed|Encore|p1a: Skarmory",
            "|-start|p1a: Skarmory|Encore",
            "|move|p1a: Skarmory|Stealth Rock||[still]",
            "|-fail|p1a: Skarmory",
        ],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "spikes"}, 0, max_turn=26,
        actor_species="skarmory",
    ) == (26, 2)


def test_find_action_line_encore_no_bloquea_un_cambio():
    """Regresion (fix-flaky, D23): Encore SOLO fuerza la repeticion de un
    MOVIMIENTO, nunca bloquea un cambio — un pokemon encoreado se puede
    cambiar libremente. Reproduccion minima de
    `battle-gen6randombattle-684` (decision 22, cambio real a
    Kangaskhan-Mega un turno despues del Encore): sin el chequeo de
    `accion_es_movimiento`, el respaldo de Encore se apropiaba del turno de
    la decision y la busqueda cortaba ahi, sin llegar nunca al `|switch|`
    real."""
    recorder = _recorder_con([
        [
            "|turn|22",
            "|move|p2a: Shuckle|Encore|p1a: Gourgeist",
            "|-start|p1a: Gourgeist|Encore",
        ],
        ["|turn|23", "|switch|p1a: Kangaskhan|Kangaskhan-Mega, L71, F|267/267"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "kangaskhanmega"}, 0,
        max_turn=25, actor_species="gourgeist",
    ) == (23, 5)


def test_find_action_line_encore_del_rival_no_roba_a_otro_pokemon():
    """El respaldo de Encore, igual que cant/faint/confusion, solo puede
    resolver la decision del pokemon que REALMENTE estaba en la cancha: un
    Encore que nombra a OTRO pokemon del mismo lado no cuenta."""
    recorder = _recorder_con([
        ["|turn|9", "|move|p2a: Foo|Encore|p1a: Otro"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "spikes"}, 0, max_turn=9,
        actor_species="skarmory",
    ) is None


def test_find_action_line_ancla_en_win():
    """fix-flaky (D23): reproduccion minima de
    `battle-gen6randombattle-571`. Raikou elige Substitute, pero la batalla
    termina (el rival se remata con el retroceso de Struggle) antes de que
    le toque jugarla. `|win|` no nombra a nadie: no hace falta chequear
    actor ni lado, la trayectoria termina ahi sin decision siguiente."""
    recorder = _recorder_con([
        ["|turn|64", "|faint|p2a: Suicune", "|win|LudexBot7039"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "substitute"}, 0, max_turn=66,
        actor_species="raikou",
    ) == (64, 3)


def test_find_action_line_ancla_en_tie():
    recorder = _recorder_con([["|turn|40", "|tie|"]])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "substitute"}, 0, max_turn=40,
    ) == (40, 2)


def test_find_action_line_illusion_confirma_un_switch_disfrazado():
    """fix-flaky (D23): reproduccion minima de
    `battle-gen6randombattle-657`. Se elige cambiar a Zoroark, pero su
    habilidad (Illusion) hace que Showdown narre el cambio con el nombre de
    OTRO miembro del equipo (`Drapion`, el ultimo companero vivo) — nunca
    "Zoroark". La unica evidencia es la revelacion posterior
    (`|replace|.../-end|...|Illusion`), que en la batalla real tardo 14
    turnos: muy por fuera de `ACTION_SEARCH_MARGIN_TURNS`, por eso esta
    confirmacion no respeta `max_turn`."""
    recorder = _recorder_con([
        ["|turn|4", "|switch|p1a: Drapion|Drapion, L83, M|230/230"],
        [
            "|turn|17",
            "|replace|p1a: Zoroark|Zoroark, L81, F",
            "|-end|p1a: Zoroark|Illusion",
        ],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "zoroark"}, 0, max_turn=4,
    ) == (4, 2)


def test_find_action_line_illusion_no_revelada_no_confirma_nada():
    """Si la Illusion nunca se rompe (o el pokemon vuelve a cambiar antes de
    revelarse), no hay evidencia: la busqueda no puede confirmar el switch y
    devuelve None, igual que cualquier otro caso sin rastro."""
    recorder = _recorder_con([
        ["|turn|4", "|switch|p1a: Drapion|Drapion, L83, M|230/230"],
        ["|turn|5", "|switch|p1a: Drapion|Drapion, L83, M|230/230"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "zoroark"}, 0, max_turn=5,
    ) is None


def test_find_action_line_request_propio_confirma_illusion_no_revelada():
    """Si Zoroark sale antes de romper Illusion, el protocolo publico nunca
    lo nombra. El request privado propio posterior al cambio si lo identifica
    como el miembro activo y es evidencia positiva del turno de resolucion."""
    recorder = _recorder_con([
        [
            "|turn|1",
            '|request|{"side":{"id":"p1","pokemon":['
            '{"ident":"p1: Zoroark","details":"Zoroark, L81, M","active":true},'
            '{"ident":"p1: Druddigon","details":"Druddigon, L85, M","active":false}'
            "]}}",
            "|switch|p1a: Barbaracle|Barbaracle, L82, F|230/230",
        ],
        ["|turn|4", "|switch|p1a: Druddigon|Druddigon, L85, M|270/270"],
    ])

    assert _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "zoroark"}, 0, max_turn=1,
    ) == (1, 3)


def test_find_action_line_request_no_matchea_pokemon_en_banca():
    """No se busca por substring en el JSON: Zoroark aparece en el equipo,
    pero no es el miembro activo, por lo que no prueba que haya entrado."""
    recorder = _recorder_con([[
        "|turn|1",
        '|request|{"side":{"id":"p1","pokemon":['
        '{"ident":"p1: Druddigon","details":"Druddigon, L85, M","active":true},'
        '{"ident":"p1: Zoroark","details":"Zoroark, L81, M","active":false}'
        "]}}",
        "|switch|p2a: Suicune|Suicune, L80|100/100",
    ]])

    assert _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "zoroark"}, 0, max_turn=1,
    ) is None


def test_find_action_line_request_del_rival_nunca_es_evidencia():
    """La evidencia privada vale solo para nuestro lado. Aunque un request
    ajeno expusiera Zoroark activo, no puede corregir una accion de p1."""
    recorder = _recorder_con([[
        "|turn|1",
        '|request|{"side":{"id":"p2","pokemon":['
        '{"ident":"p2: Zoroark","details":"Zoroark, L81, M","active":true}'
        "]}}",
        "|switch|p2a: Barbaracle|Barbaracle, L82, F|230/230",
    ]])

    assert _find_action_line(
        recorder, "p1", {"kind": "switch", "species": "zoroark"}, 0, max_turn=1,
    ) is None


def test_choose_move_captura_actor_species():
    """El pokemon activo al momento de decidir se captura sincronicamente,
    igual que `legal_actions`/`action_taken`: es lo que permite que el
    respaldo de `_find_action_line` verifique que un `|cant|`/`|faint|`/
    autogolpe por confusion pertenece al MISMO pokemon que esta decision,
    no a otro del mismo lado (ver D22)."""
    player = _player()
    tag = "battle-x-1"

    class FakeMove:
        id = "brickbreak"

    class FakeActive:
        species = "Muk"
        base_species = "Muk"

    battle = _fake_battle(
        battle_tag=tag, available_moves=[FakeMove()], active_pokemon=FakeActive()
    )

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="brickbreak"),
    ):
        _descartar(player.choose_move(battle))

    assert player.steps[tag][0]["actor_species"] == "muk"


def test_choose_move_captura_base_species_no_la_forma():
    """Regresion (fix-cursor): Showdown identifica al actor en `|move|`/
    `|switch|`/`|cant|`/`|faint|` SIEMPRE con el nombre BASE, nunca con la
    forma (verificado sobre datos reales: `p1a: Arceus`, nunca
    `p1a: Arceus-Poison`, para las 6 formas de plato; lo mismo con Rotom,
    Giratina-Origin, Wormadam, Keldeo-Resolute, Landorus-Therian,
    Thundurus-Therian, Shaymin-Sky). `mon.species` para esos pokemon SI
    incluye la forma (`arceuspoison`). Capturar `species` en vez de
    `base_species` rompia el chequeo de actor para CUALQUIER decision de un
    pokemon con forma: un `|faint|p1a: Arceus` real se rechazaba porque
    "arceuspoison" != "arceus", y la decision se quedaba sin corregir."""
    player = _player()
    tag = "battle-x-1"

    class FakeMove:
        id = "earthpower"

    class FakeActive:
        species = "arceuspoison"  # lo que devuelve poke-env para Arceus-Poison
        base_species = "arceus"   # lo que Showdown usa como identificador

    battle = _fake_battle(
        turn=37, battle_tag=tag, available_moves=[FakeMove()],
        active_pokemon=FakeActive(),
    )

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="earthpower"),
    ):
        _descartar(player.choose_move(battle))

    assert player.steps[tag][0]["actor_species"] == "arceus", (
        "tiene que capturar la forma BASE (lo que Showdown narra como "
        "actor), no la especie completa con forma"
    )


def test_find_action_line_respaldo_con_pokemon_de_forma_no_se_pierde():
    """Integracion chica de la regresion real: Arceus (base_species=arceus)
    elige Earth Power, un Porygon-Z mas rapido lo debilita antes de que le
    toque actuar. El `|faint|p1a: Arceus` (sin forma) tiene que contar como
    resolucion de ESA decision cuando `actor_species="arceus"`."""
    recorder = _recorder_con([
        ["|turn|38", "|move|p2a: Porygon-Z|Tri Attack|p1a: Arceus", "|faint|p1a: Arceus"],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "earthpower"}, 0, max_turn=40,
        actor_species="arceus",
    ) == (38, 3)


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


class _RecordingWebsocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


def _reservar_choice(
    player: LudexPlayer,
    tag: str,
    *,
    rqid: int = 4,
    frame_seq: int = 10,
    move_id: str = "tackle",
) -> dict:
    """Reserva una decision por el camino publico de `choose_move`.

    La coroutine se cierra porque estos tests ejercen la reconciliacion que
    ocurre antes de decidir de nuevo; no necesitan esperar otra narracion.
    """

    class FakeMove:
        id = move_id

    battle = _fake_battle(
        battle_tag=tag,
        last_request={"rqid": rqid},
        available_moves=[FakeMove()],
    )
    token = CURRENT_FRAME_SEQ.set(frame_seq)
    try:
        with patch.object(
            client_module.RandomPlayer,
            "choose_move",
            lambda self, b: FakeOrder(mid=move_id),
        ):
            _descartar(player.choose_move(battle))
    finally:
        CURRENT_FRAME_SEQ.reset(token)
    return player.steps[tag][0]


async def _enviar(player: LudexPlayer, tag: str, message: str) -> None:
    await player.ps_client.send_message(message, tag)


async def test_unavailable_correlacionado_invalida_sin_eliminar_el_slot():
    player = _player()
    tag = "battle-x-1"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    rechazado = _reservar_choice(player, tag)
    await _enviar(player, tag, "/choose move tackle")

    lote = [_split(">battle-x-1"), _split(
        "|error|[Unavailable choice] Can't switch: The active Pokémon is trapped"
    )]
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message(lote)

    assert len(player.steps[tag]) == 1, "el slot canonico no se elimina"
    assert player.steps[tag][0] is None, "un intento rechazado no es persistible"
    assert rechazado not in player.steps[tag]
    assert getattr(player, "rejected_choice_count", 0) == 1
    assert player._retry_pending.get(tag) is True
    assert websocket.sent == [f"{tag}|/choose move tackle"]


async def test_undo_real_preserva_el_choice_pendiente_y_no_reintenta(caplog):
    player = _player()
    tag = "battle-x-1"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    reservado = _reservar_choice(player, tag)
    await _enviar(player, tag, "/choose move tackle")
    await _enviar(player, tag, "/undo")

    lote = [
        _split(">battle-x-1"),
        _split("|error|[Invalid choice] There's nothing to cancel"),
    ]
    with caplog.at_level(logging.WARNING), patch.object(
        client_module.RandomPlayer, "_handle_battle_message", _sin_super
    ):
        await player._handle_battle_message(lote)

    assert player.steps[tag] == [reservado]
    assert player._retry_pending.get(tag) in (None, False)
    assert getattr(player, "auxiliary_command_error_count", 0) == 1
    assert getattr(player, "rejected_choice_count", 0) == 0
    assert any(
        f"battle_tag={tag}" in record.message and "command=/undo" in record.message
        for record in caplog.records
    ), "el error auxiliar debe dejar un log estructurado y consultable"
    assert websocket.sent == [
        f"{tag}|/choose move tackle",
        f"{tag}|/undo",
    ]


@pytest.mark.parametrize(
    "error",
    [
        "[Invalid choice] There's nothing to choose",
        (
            "[Invalid choice] Sorry, too late to make a different move; "
            "the next turn has already started"
        ),
        "[Invalid choice] The battle crashed",
    ],
)
async def test_room_level_stale_choose_y_battle_crashed_fallan_cerrado(error):
    player = _player()
    tag = "battle-x-1"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    reservado = _reservar_choice(player, tag)
    await _enviar(player, tag, "/choose move tackle")

    lote = [_split(">battle-x-1"), _split(f"|error|{error}")]
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        with pytest.raises(RuntimeError, match=tag):
            await player._handle_battle_message(lote)

    assert player.steps[tag] == [reservado]
    assert player._retry_pending.get(tag) in (None, False)
    assert getattr(player, "rejected_choice_count", 0) == 0
    failure = await player.wait_for_background_failure()
    assert tag in str(failure)


async def test_invalid_correlaciona_aunque_responda_durante_sending():
    """La observacion outbound debe existir antes del primer await del send."""
    player = _player()
    tag = "battle-race-1"
    _reservar_choice(player, tag)
    original_calls = 0

    async def immediate_response(message, room="", message_2=None):
        nonlocal original_calls
        original_calls += 1
        pending = player._pending_choices[tag]
        assert pending.outbound_phase == "sending", (
            "CANARIO: la respuesta se inyecta antes de que send pueda marcar sent"
        )
        with patch.object(
            client_module.RandomPlayer, "_handle_battle_message", _sin_super
        ):
            await player._handle_battle_message([
                [f">{tag}"],
                _split("|error|[Invalid choice] Move disabled"),
            ])

    player._send_message_original = immediate_response
    await _enviar(player, tag, "/choose move tackle")

    assert original_calls == 1, "el wrapper delega exactamente una vez"
    assert player.rejected_choice_count == 1
    assert player.steps[tag] == [None]
    assert player._pending_choices[tag].phase == "rejected"
    assert player._pending_choices[tag].outbound_phase == "sent"


async def test_fallo_de_send_no_deja_un_intento_ficticiamente_enviado():
    player = _player()
    tag = "battle-send-failed"
    _reservar_choice(player, tag)
    original_calls = 0

    async def failing_send(message, room="", message_2=None):
        nonlocal original_calls
        original_calls += 1
        raise OSError("socket closed")

    player._send_message_original = failing_send
    with pytest.raises(OSError, match="socket closed"):
        await _enviar(player, tag, "/choose move tackle")

    assert original_calls == 1
    pending = player._pending_choices[tag]
    assert pending.outbound_phase == "failed"
    assert player._last_outbound[tag].phase == "failed"
    assert player.rejected_choice_count == 0


async def test_invalid_reintenta_dentro_del_handler_y_reemplaza_el_slot_random():
    player = _player()
    tag = "battle-invalid-inline"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    rechazado = _reservar_choice(player, tag, rqid=4, move_id="tackle")
    await _enviar(player, tag, "/choose move tackle")
    player._last_projection[tag] = {
        "turn": 7,
        "opponent": {"pokemon": [{"species": "latias", "active": True}]},
        "field": {"weather": {}, "field_effects": {},
                  "my_side": {}, "opponent_side": {}},
    }
    retry_battle = _fake_battle(
        battle_tag=tag,
        turn=7,
        last_request={"rqid": 4},
        available_moves=[SimpleNamespace(id="struggle")],
    )
    retries_inside_handler = 0

    async def vendor_invalid_retry(self, split_messages):
        nonlocal retries_inside_handler
        retries_inside_handler += 1
        with patch.object(
            client_module.RandomPlayer,
            "choose_move",
            lambda owner, battle: FakeOrder(mid="struggle"),
        ):
            order = await self.choose_move(retry_battle)
        assert order.order.id == "struggle"
        await self.ps_client.send_message("/choose move struggle", tag)

    with patch.object(
        client_module.RandomPlayer,
        "_handle_battle_message",
        vendor_invalid_retry,
    ):
        await player._handle_battle_message([
            [f">{tag}"],
            _split("|error|[Invalid choice] Move disabled"),
        ])

    assert retries_inside_handler == 1, "CANARIO: el retry ocurrio en el handler"
    assert player.rejected_choice_count == 1
    assert len(player.steps[tag]) == 1
    final = player.steps[tag][0]
    assert final is not rechazado
    assert final["action_taken"] == {"kind": "move", "id": "struggle"}
    assert final["state"]["legal_actions"] == [
        {"kind": "move", "id": "struggle"}
    ]
    pending = player._pending_choices[tag]
    assert pending.decision_index == 0
    assert pending.attempt_index == 1
    assert pending.phase == "retried"
    assert websocket.sent == [
        f"{tag}|/choose move tackle",
        f"{tag}|/choose move struggle",
    ]


async def test_multiples_rechazos_comparten_indice_y_wait_resuelve_el_final():
    player = _player()
    tag = "battle-multiple-rejections"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    _reservar_choice(player, tag, rqid=4, move_id="tackle")
    await _enviar(player, tag, "/choose move tackle")
    player._last_projection[tag] = {
        "turn": 5,
        "opponent": {"pokemon": []},
        "field": {"weather": {}, "field_effects": {},
                  "my_side": {}, "opponent_side": {}},
    }

    async def reject_and_request(rqid: int) -> None:
        with patch.object(
            client_module.RandomPlayer, "_handle_battle_message", _sin_super
        ):
            await player._handle_battle_message([
                [f">{tag}"],
                _split("|error|[Unavailable choice] Move disabled"),
            ])
            await player._handle_battle_message([
                [f">{tag}"],
                _split(f'|request|{{"rqid":{rqid}}}'),
            ])

    async def retry(move_id: str, rqid: int) -> None:
        battle = _fake_battle(
            battle_tag=tag,
            turn=5,
            last_request={"rqid": rqid},
            available_moves=[SimpleNamespace(id=move_id)],
        )
        with patch.object(
            client_module.RandomPlayer,
            "choose_move",
            lambda owner, b: FakeOrder(mid=move_id),
        ):
            await player.choose_move(battle)
        await _enviar(player, tag, f"/choose move {move_id}")

    await reject_and_request(6)
    await retry("struggle", 6)
    await reject_and_request(8)
    await retry("scratch", 8)

    # wait:true no llama choose_move en poke-env, pero confirma que el ultimo
    # intento fue aceptado. El canario exacto demuestra dos rechazos reales.
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"],
            _split('|request|{"rqid":10,"wait":true}'),
        ])

    assert player.rejected_choice_count == 2
    assert len(player.steps[tag]) == 1
    assert player.steps[tag][0]["action_taken"] == {
        "kind": "move", "id": "scratch"
    }
    assert player._pending_choices.get(tag) is None
    assert player.trajectory_blocker(tag) is None


@pytest.mark.parametrize(
    ("request_extra", "label"),
    [
        ("", "ordinario"),
        (',"forceSwitch":[true]', "forceSwitch"),
    ],
)
async def test_request_siguiente_resuelve_y_reserva_el_indice_siguiente(
    request_extra, label
):
    """Un request nuevo resuelve la accion previa, incluso en el mismo turno."""
    player = _player()
    tag = f"battle-next-{label}"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    first = _reservar_choice(player, tag, rqid=4, move_id="tackle")
    await _enviar(player, tag, "/choose move tackle")

    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"],
            _split(f'|request|{{"rqid":6{request_extra}}}'),
        ])

    assert player._pending_choices.get(tag) is None, (
        f"CANARIO: el request {label} no confirmo la decision anterior"
    )
    second_battle = _fake_battle(
        battle_tag=tag,
        turn=3,
        last_request={"rqid": 6},
        available_moves=[SimpleNamespace(id="scratch")],
    )
    with patch.object(
        client_module.RandomPlayer,
        "choose_move",
        lambda owner, battle: FakeOrder(mid="scratch"),
    ):
        _descartar(player.choose_move(second_battle))

    assert player.steps[tag][0] is first
    assert len(player.steps[tag]) == 2
    assert player._pending_choices[tag].decision_index == 1
    assert player._pending_choices[tag].attempt_index == 0


async def test_retry_graph_reemplaza_accion_path_y_reasoning_del_rechazado():
    player = _player()
    tag = "battle-graph-retry"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    rechazado = _reservar_choice(player, tag, rqid=4, move_id="tackle")
    rechazado["action_path"] = "REJECTED_PATH"
    rechazado["reasoning"] = "REJECTED_REASONING"
    await _enviar(player, tag, "/choose move tackle")
    player._last_projection[tag] = {
        "turn": 3,
        "opponent": {"pokemon": []},
        "field": {"weather": {}, "field_effects": {},
                  "my_side": {}, "opponent_side": {}},
    }
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"],
            _split("|error|[Unavailable choice] Move disabled"),
        ])
        await player._handle_battle_message([
            [f">{tag}"],
            _split('|request|{"rqid":6}'),
        ])

    class RetryGraph:
        async def ainvoke(self, graph_input):
            assert graph_input["turn_id"] == f"{tag}:0"
            return {
                "action": {"kind": "move", "id": "struggle"},
                "action_path": "fallback",
                "reasoning": "FRESH_REASONING",
            }

    player.decision_graph = RetryGraph()
    battle = _fake_battle(
        battle_tag=tag,
        last_request={"rqid": 6},
        available_moves=[SimpleNamespace(id="struggle")],
    )
    await player.choose_move(battle)

    final = player.steps[tag][0]
    assert final is not rechazado
    assert final["action_path"] == "fallback"
    assert final["reasoning"] == "FRESH_REASONING"
    assert "REJECTED_PATH" not in repr(final)
    assert "REJECTED_REASONING" not in repr(final)
    assert player.rejected_choice_count == 1


@pytest.mark.parametrize("terminal", ["win", "tie"])
async def test_win_y_tie_resuelven_y_limpian_un_intento_aceptado(terminal):
    player = _player()
    tag = f"battle-{terminal}-cleanup"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    step = _reservar_choice(player, tag)
    step["state"] = {"legal_actions": step["legal_actions"]}
    await _enviar(player, tag, "/choose move tackle")
    await player.frame_inbox.publish(tag, ("|upkeep",))

    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"], _split(f"|{terminal}|Bot")
        ])

    assert player._pending_choices.get(tag) is None
    assert tag not in player._last_outbound
    assert tag not in player._request_heads
    assert tag not in player._outbound_sequences
    assert player.frame_inbox.retained(tag) == 0
    assert player.trajectory_blocker(tag) is None


async def test_win_con_intento_rechazado_falla_cerrado():
    player = _player()
    tag = "battle-win-rejected"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    _reservar_choice(player, tag)
    await _enviar(player, tag, "/choose move tackle")
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        await player._handle_battle_message([
            [f">{tag}"],
            _split("|error|[Unavailable choice] Move disabled"),
        ])
        with pytest.raises(RuntimeError, match=r"phase=rejected"):
            await player._handle_battle_message([
                [f">{tag}"], _split("|win|Bot")
            ])

    assert player.rejected_choice_count == 1, "CANARIO: hubo un rechazo"
    assert player.trajectory_blocker(tag) == (0, "rejected")


async def test_deinit_con_pending_falla_y_limpia_la_correlacion():
    player = _player()
    tag = "battle-deinit-pending"
    websocket = _RecordingWebsocket()
    player.ps_client.websocket = websocket
    _reservar_choice(player, tag)
    await _enviar(player, tag, "/choose move tackle")

    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        with pytest.raises(RuntimeError, match=r"deinit.*phase=reserved"):
            await player._handle_battle_message([
                [f">{tag}"], _split("|deinit")
            ])

    assert tag not in player._pending_choices
    assert tag not in player._last_outbound
    assert tag not in player._request_heads
    blocker = player.trajectory_blocker(tag)
    assert blocker is not None and blocker[1].startswith("terminal_failure:")


async def test_invalid_desconocido_sin_choice_correlacionable_falla_cerrado():
    player = _player()
    tag = "battle-invalid-unknown"
    lote = [[f">{tag}"], _split("|error|[Invalid choice] Future server text")]
    with patch.object(client_module.RandomPlayer, "_handle_battle_message", _sin_super):
        with pytest.raises(RuntimeError, match=tag):
            await player._handle_battle_message(lote)

    assert player.steps[tag] == []
    assert player.rejected_choice_count == 0
    assert player.recorders[tag].all_lines[-1].endswith("Future server text")


async def test_excepcion_de_mensajes_se_publica_al_runner():
    player = _player()
    failure = TransientProviderError("provider transport failed")

    async def failing_super(self, split_messages):
        raise failure

    lote = [_split(">battle-test-failure"), _split("|turn|1")]
    with patch.object(
        client_module.RandomPlayer, "_handle_battle_message", failing_super
    ):
        with pytest.raises(TransientProviderError):
            await player._handle_battle_message(lote)

    assert await player.wait_for_background_failure() is failure


async def test_cancelar_un_vigilante_no_cancela_el_future_compartido():
    player = _player()
    first_waiter = asyncio.create_task(player.wait_for_background_failure())
    await asyncio.sleep(0)

    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    failure = TransientProviderError("later failure")
    second_waiter = asyncio.create_task(player.wait_for_background_failure())
    player._background_failure.set_result(failure)

    assert await second_waiter is failure


# --- D31 (MON-6): camino pre-lock ------------------------------------------
#
# Fixture FIEL: los 11 frames son el protocolo crudo real de
# `battle-gen6randombattle-397`, tal como los recibio el socket, reconstruidos
# por sus marcas `>battle-...`. El frame 9 es el `|request|` de una decision y
# el frame 10 la narracion que el servidor ya habia emitido cuando esa decision
# se tomo: Latias entra y queda al 76%.
#
# Se conducen por `PSClient._handle_message` —una task por frame, igual que
# `listen()`— y NO inyectados a mano en `_handle_battle_message`: el punto
# entero del arreglo es que la publicacion ocurra antes del lock.


def _frames_reales() -> list[list[str]]:
    import json
    from pathlib import Path

    ruta = Path(__file__).parent / "data" / "prelock_frames_397.json"
    return json.loads(ruta.read_text())


def _player_del_fixture(**kwargs) -> LudexPlayer:
    from poke_env import AccountConfiguration

    kwargs.setdefault("start_listening", False)
    return LudexPlayer(
        # Tiene que coincidir con `|player|p1|LudexBot3682|...` del fixture:
        # es asi como poke-env decide que somos p1.
        account_configuration=AccountConfiguration("LudexBot3682", None),
        battle_format="gen6randombattle",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
        **kwargs,
    )


async def _correr_fixture(player: LudexPlayer, frames: list[list[str]]) -> None:
    """Una task por frame, creadas en orden de llegada: la misma topologia que
    `PSClient.listen()`. Es lo que reproduce la carrera real —la narracion
    llega mientras la decision anterior tiene el lock— en vez de simularla."""
    client = player.ps_client
    tasks = []
    for frame in frames:
        tasks.append(asyncio.create_task(client._handle_message("\n".join(frame))))
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)


async def test_el_grafo_recibe_el_rival_revelado_por_la_narracion_ya_emitida():
    """LA regresion de F2-01.

    Con el protocolo real: cuando se decide desde el `|request|` del frame 9,
    poke-env todavia tiene a Ludicolo como activo rival, porque la narracion
    que mete a Latias esta en el frame 10 y su task quedo encolada detras del
    lock por batalla. El proveedor tiene que ver a Latias igual, porque esa
    narracion YA fue emitida por el servidor y no depende de nuestra respuesta.
    """
    vistas: list[dict] = []
    lo_que_ve_pokeenv: list[list[str]] = []

    class CapturingGraph:
        async def ainvoke(self, graph_input):
            estado = graph_input["raw_state"]
            vistas.append(estado)
            tag = graph_input["turn_id"].split(":")[0]
            batalla = player.battles[tag]
            # CANARIO: lo que poke-env tiene aplicado en ESTE instante. Si la
            # proyeccion no se aplicara, seria identico a `estado` y el test
            # no probaria nada.
            lo_que_ve_pokeenv.append(
                sorted(
                    m.species
                    for m in (batalla.opponent_team or {}).values()
                    if m.active
                )
            )
            return {
                "action": estado["legal_actions"][0],
                "action_path": "llm",
            }

    player = _player_del_fixture(decision_graph=CapturingGraph())
    enviados: list[str] = []

    async def fake_send(message, room="", message_2=None):
        enviados.append(message)

    player._send_message_original = fake_send
    await _correr_fixture(player, _frames_reales())

    assert vistas, "el grafo nunca se invoco: el fixture no ejercio nada"
    decision = vistas[-1]

    activos = [p for p in decision["opponent"]["pokemon"] if p["active"]]
    assert len(activos) == 1
    assert activos[0]["species"] == "latias", (
        "el proveedor tiene que ver el switch-in que la narracion previa ya "
        f"revelo, no {activos[0]['species']!r}"
    )
    assert activos[0]["hp_fraction"] == 0.76

    # CANARIO: poke-env seguia en Ludicolo cuando decidimos. Sin esta
    # diferencia el test podria pasar sin que la proyeccion hiciera nada.
    assert lo_que_ve_pokeenv[-1] == ["ludicolo"], (
        "si poke-env ya tuviera a Latias aplicada, este fixture no estaria "
        "ejerciendo el desfase que F2-01 arregla"
    )


async def test_la_proyeccion_no_filtra_informacion_oculta_del_rival():
    """Latias entra y recibe daño, pero la narracion no revela NINGUN
    movimiento, item ni habilidad suyos: el Sludge Bomb del frame 10 es
    NUESTRO. Nada de eso puede aparecer."""
    vistas: list[dict] = []

    class CapturingGraph:
        async def ainvoke(self, graph_input):
            vistas.append(graph_input["raw_state"])
            return {
                "action": graph_input["raw_state"]["legal_actions"][0],
                "action_path": "llm",
            }

    player = _player_del_fixture(decision_graph=CapturingGraph())

    async def fake_send(message, room="", message_2=None):
        return None

    player._send_message_original = fake_send
    await _correr_fixture(player, _frames_reales())

    decision = vistas[-1]
    latias = next(
        p for p in decision["opponent"]["pokemon"] if p["species"] == "latias"
    )
    assert latias["moves"] == [], "ningun movimiento de Latias fue revelado"
    assert latias["item"] == "unknown_item"
    # `levitate` NO es fuga: en gen 6 el dex le da a Latias exactamente UNA
    # ability posible, asi que saberla no requiere haberla visto. Es la misma
    # inferencia que hace poke-env (`pokemon.py:658-661`), y el criterio del
    # SKILL es que una inferencia legitima este anclada al dex. La fuga seria
    # afirmar una ability de una especie con varias posibles.
    abilities_posibles = list(
        GenData.from_gen(6).pokedex["latias"]["abilities"].values()
    )
    assert abilities_posibles == ["Levitate"], "cambio el dex: revisar la asercion"
    assert latias["ability"] == "levitate"

    # Lo que SI estaba revelado se conserva: Ludicolo uso Energy Ball antes.
    ludicolo = next(
        p for p in decision["opponent"]["pokemon"] if p["species"] == "ludicolo"
    )
    assert [m["id"] for m in ludicolo["moves"]] == ["energyball"]
    assert ludicolo["active"] is False


async def test_snapshot_del_proveedor_y_fila_persistida_son_el_mismo_objeto():
    """No pueden representar puntos distintos de la batalla: es el mismo dict."""
    vistas: list[dict] = []

    class CapturingGraph:
        async def ainvoke(self, graph_input):
            vistas.append(graph_input["raw_state"])
            return {
                "action": graph_input["raw_state"]["legal_actions"][0],
                "action_path": "llm",
            }

    player = _player_del_fixture(decision_graph=CapturingGraph())

    async def fake_send(message, room="", message_2=None):
        return None

    player._send_message_original = fake_send
    await _correr_fixture(player, _frames_reales())

    tag = "battle-gen6randombattle-397"
    pasos = [s for s in player.steps[tag] if s["state"] is not None]
    assert pasos
    assert pasos[-1]["state"] is vistas[-1]


async def test_la_mascara_capturada_no_cambia_por_la_espera():
    """`legal_actions` sale del `|request|` propio y la espera no la toca."""
    vistas: list[dict] = []

    class CapturingGraph:
        async def ainvoke(self, graph_input):
            vistas.append(graph_input["raw_state"])
            return {
                "action": graph_input["raw_state"]["legal_actions"][0],
                "action_path": "llm",
            }

    player = _player_del_fixture(decision_graph=CapturingGraph())

    async def fake_send(message, room="", message_2=None):
        return None

    player._send_message_original = fake_send
    await _correr_fixture(player, _frames_reales())

    tag = "battle-gen6randombattle-397"
    for paso in player.steps[tag]:
        if paso["state"] is None:
            continue
        assert paso["state"]["legal_actions"] == paso["legal_actions"]
        assert paso["action_taken"] in paso["legal_actions"]


# --- F2-08 (MON-13/D38): cableado de metadata del resultado del grafo al
# step canonico -----------------------------------------------------------

async def test_el_step_hereda_la_metadata_completa_del_resultado_del_grafo():
    """El caller real (`run_graph` dentro de `choose_move`) copia la metadata
    de la decision del resultado del grafo al step canonico: rationale,
    confidence, alternatives, target, provider/model efectivos, latencia y
    usage. Esa metadata viaja despues a `save_step` (cli._persist_one)."""
    tag = "battle-graph-metadata"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(turn=1, battle_tag=tag, available_moves=[move])

    class MetadataGraph:
        async def ainvoke(self, graph_input):
            return {
                "action": {"kind": "move", "id": "tackle"},
                "action_path": "llm",
                "reasoning": "breve y user-facing",
                "rationale": "breve y user-facing",
                "confidence": 0.87,
                "alternatives": [{"kind": "move", "id": "meteormash"}],
                "target": None,
                "provider": "open_code_zen",
                "model": "minimax-m2.7",
                "decision_latency_ms": 123.4,
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_input_tokens": 2,
                "reasoning_tokens": 1,
            }

    player = _player(decision_graph=MetadataGraph(), decision_budget_seconds=5)
    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "turn": 1, "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        },
    ):
        pending = player.choose_move(battle)
        await player.frame_inbox.publish(tag, ("|upkeep",))
        await pending

    step = player.steps[tag][0]
    assert step["action_taken"] == {"kind": "move", "id": "tackle"}
    assert step["action_path"] == "llm"
    assert step["rationale"] == "breve y user-facing"
    assert step["confidence"] == 0.87
    assert step["alternatives"] == [{"kind": "move", "id": "meteormash"}]
    assert step["target"] is None
    assert step["provider"] == "open_code_zen"
    assert step["model"] == "minimax-m2.7"
    assert step["decision_latency_ms"] == 123.4
    assert step["input_tokens"] == 10
    assert step["output_tokens"] == 5
    assert step["cached_input_tokens"] == 2
    assert step["reasoning_tokens"] == 1


async def test_el_step_de_fallback_no_atribuye_metadata_a_un_modelo():
    """Un fallback (dos respuestas invalidas) deja provider/model/confidence
    en None, alternatives en [] y un rationale determinista; usage y latencia
    pueden existir si hubo llamadas LLM."""
    tag = "battle-graph-fallback"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(turn=1, battle_tag=tag, available_moves=[move])

    class FallbackGraph:
        async def ainvoke(self, graph_input):
            return {
                "action": {"kind": "move", "id": "tackle"},
                "action_path": "fallback",
                "reasoning": "deterministic fallback after two invalid model responses",
                "rationale": "deterministic fallback after two invalid model responses",
                "confidence": None,
                "alternatives": [],
                "target": None,
                "provider": None,
                "model": None,
                "decision_latency_ms": 250.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            }

    player = _player(decision_graph=FallbackGraph(), decision_budget_seconds=5)
    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "turn": 1, "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        },
    ):
        pending = player.choose_move(battle)
        await player.frame_inbox.publish(tag, ("|upkeep",))
        await pending

    step = player.steps[tag][0]
    assert step["action_path"] == "fallback"
    assert step["provider"] is None
    assert step["model"] is None
    assert step["confidence"] is None
    assert step["alternatives"] == []
    assert step["target"] is None
    assert step["rationale"]
    assert step["decision_latency_ms"] == 250.0
    assert step["input_tokens"] == 0


def test_la_ruta_random_no_deja_metadata_en_el_step():
    """La ruta random no setea las claves de metadata: el step no las
    contiene y `_persist_one` (via `step.get`) persistira NULL, coherente
    con la historia."""
    player = _player()
    tag = "battle-x-1"
    battle = _fake_battle(battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")])

    with patch.object(
        client_module.RandomPlayer, "choose_move",
        lambda self, b: FakeOrder(mid="tackle"),
    ):
        _descartar(player.choose_move(battle))

    step = player.steps[tag][0]
    for key in (
        "rationale", "confidence", "alternatives", "target", "provider",
        "model", "decision_latency_ms", "input_tokens", "output_tokens",
        "cached_input_tokens", "reasoning_tokens",
    ):
        assert key not in step, f"la ruta random no debe setear {key!r}"
        assert step.get(key) is None
