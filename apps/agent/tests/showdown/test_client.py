import asyncio
import logging
import random
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from poke_env.concurrency import POKE_LOOP
from poke_env.data import GenData

from ludex_agent.graph.calc import CalcClient
from ludex_agent.graph.provider import (
    CompletionEnvelope,
    CompletionUsage,
    DecisionMetrics,
    ResolvedProvider,
    TransientProviderError,
)
from ludex_agent.graph.workflow import build_decision_graph
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


async def test_el_cableado_pre_applied_no_descuenta_pp_de_frames_de_gap():
    """MON-26 R3: el cliente deriva `pre_applied` comparando el seq de cada
    frame de la ventana con el cursor del request propio, y el proyector no
    reaplica el descuento de PP de las lineas de gap (poke-env ya llamo
    `Move.use()` al parsear esa narracion). End-to-end: ventana con gap
    real, snapshot post-narracion pp=15 -> proyectado pp=15, no 14."""
    class EchoGraph:
        async def ainvoke(self, graph_input):
            return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}

    player = _player(decision_graph=EchoGraph())
    tag = "battle-pre-applied"

    def serializado(b):
        return {
            "turn": 3, "player_role": "p1",
            "opponent": {"pokemon": [
                {
                    "species": "ludicolo", "hp_fraction": 0.0, "active": False,
                    "fainted": True, "status": "FNT", "level": 88,
                    "item": "unknown_item", "ability": None,
                    "types": ["WATER", "GRASS"],
                    "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                               "evasion": 0, "accuracy": 0},
                    "moves": [{"id": "energyball", "pp": 15, "max_pp": 16}],
                },
            ]},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        }

    # Frame de gap (llego ANTES del request): la narracion del rival.
    await player.frame_inbox.publish(
        tag, ("|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",)
    )
    # Request propio posterior al gap: el cursor queda DESPUES del frame.
    request = await player.frame_inbox.publish(tag, ("|request|{}",))
    battle = _fake_battle(
        battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
    )
    with patch.object(client_module, "serialize_battle", serializado):
        CURRENT_FRAME_SEQ.set(request.seq)
        pending = player.choose_move(battle)
    # Frame de cierre: el switch del reemplazo.
    await player.frame_inbox.publish(
        tag, ("|switch|p2a: Latias|Latias, L77, F|100/100",)
    )
    await pending

    estado = player._last_projection[tag]
    por_especie = {m["species"]: m for m in estado["opponent"]["pokemon"]}
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ], (
        "la linea de gap ya estaba en el snapshot: el PP no se descuenta "
        "dos veces (criterio R3: 15, no 14)"
    )
    assert por_especie["latias"]["moves"] == []


async def test_frame_move_antes_del_request_sin_switch_deriva_pre_aplicado():
    """MON-26 R3 (canario de cableado): la bandera `pre_applied` se deriva
    del ORDEN de frames (move ANTES del request -> poke-env ya lo proceso),
    aun SIN switch de por medio. Snapshot POST-narracion pp=15 -> 15."""
    class EchoGraph:
        async def ainvoke(self, graph_input):
            return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}

    player = _player(decision_graph=EchoGraph())
    tag = "battle-pre-aplicado-sin-switch"

    def serializado(b):
        return {
            "turn": 3, "player_role": "p1",
            "opponent": {"pokemon": [
                {
                    "species": "ludicolo", "hp_fraction": 0.5, "active": True,
                    "fainted": False, "status": None, "level": 88,
                    "item": "unknown_item", "ability": None,
                    "types": ["WATER", "GRASS"],
                    "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                               "evasion": 0, "accuracy": 0},
                    "moves": [{"id": "energyball", "pp": 15, "max_pp": 16}],
                },
            ]},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        }

    await player.frame_inbox.publish(
        tag, ("|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",)
    )
    request = await player.frame_inbox.publish(tag, ("|request|{}",))
    battle = _fake_battle(
        battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
    )
    with patch.object(client_module, "serialize_battle", serializado):
        CURRENT_FRAME_SEQ.set(request.seq)
        pending = player.choose_move(battle)
    await player.frame_inbox.publish(tag, ("|upkeep",))
    await pending

    estado = player._last_projection[tag]
    por_especie = {m["species"]: m for m in estado["opponent"]["pokemon"]}
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ], (
        "el frame de move llego ANTES del request: la bandera deriva "
        "pre_aplicado y el PP no se re-descuenta (celda 2 del oraculo)"
    )


async def test_frame_move_despues_del_request_no_deriva_pre_aplicado():
    """MON-26 R3 (canario de cableado): un frame de move que llega DESPUES
    del request (frame de cierre) NO es pre-aplicado: el snapshot es PRE-
    narracion y el descuento SI corre (pp=10 -> 9, uso repetido)."""
    class EchoGraph:
        async def ainvoke(self, graph_input):
            return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}

    player = _player(decision_graph=EchoGraph())
    tag = "battle-no-pre-aplicado"

    def serializado(b):
        return {
            "turn": 3, "player_role": "p1",
            "opponent": {"pokemon": [
                {
                    "species": "ludicolo", "hp_fraction": 0.5, "active": True,
                    "fainted": False, "status": None, "level": 88,
                    "item": "unknown_item", "ability": None,
                    "types": ["WATER", "GRASS"],
                    "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                               "evasion": 0, "accuracy": 0},
                    "moves": [{"id": "energyball", "pp": 10, "max_pp": 16}],
                },
            ]},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        }

    request = await player.frame_inbox.publish(tag, ("|request|{}",))
    battle = _fake_battle(
        battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
    )
    with patch.object(client_module, "serialize_battle", serializado):
        CURRENT_FRAME_SEQ.set(request.seq)
        pending = player.choose_move(battle)
    await player.frame_inbox.publish(
        tag, ("|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",)
    )
    await pending

    estado = player._last_projection[tag]
    por_especie = {m["species"]: m for m in estado["opponent"]["pokemon"]}
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 9, "max_pp": 16}
    ], (
        "el frame de move llego DESPUES del request: no es pre-aplicado y "
        "el descuento corre una vez (uso repetido)"
    )


async def test_el_watermark_de_proyeccion_es_por_batalla():
    """MON-26 (condicion del tech lead): el watermark que acota la ventana de
    frames de `_resolve_state` es POR TAG, nunca global. Con concurrencia > 1,
    un watermark compartido haria que una batalla salte frames que se
    publicaron mientras la otra decidia -- perdida SILENCIOSA de evidencia,
    peor que el defecto que este arreglo corrige. Un frame publicado antes
    de que la batalla B cierre su ventana tiene que sobrevivir para la
    SIGUIENTE decision de la batalla A."""
    class EchoGraph:
        async def ainvoke(self, graph_input):
            return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}

    player = _player(decision_graph=EchoGraph())
    CURRENT_FRAME_SEQ.set(None)

    def serializado(b):
        return {
            "turn": 3, "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        }

    async def decidir(tag, *, frames_a_publicar):
        # La decision anterior del MISMO tag se resuelve como lo haria el
        # request real del servidor: la orden ya fue enviada ("sent") y llega
        # el request nuevo.
        if tag in player._pending_choices:
            player._pending_choices[tag].outbound_phase = "sent"
            player._observe_request(tag, '{"rqid": 2}')
        battle = _fake_battle(
            battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
        )
        with patch.object(client_module, "serialize_battle", serializado):
            pending = player.choose_move(battle)
        for frame in frames_a_publicar:
            await player.frame_inbox.publish(tag, frame)
        await pending
        return player._last_projection[tag]

    await decidir(
        "battle-a",
        frames_a_publicar=[("|switch|p2a: Latias|Latias, L77, F|100/100",)],
    )
    watermark_a = player._projected_until["battle-a"]
    # Un frame de A publicado AHORA, antes de que B decida: A todavia no lo
    # consumio, y un watermark global lo perderia en la decision siguiente.
    await player.frame_inbox.publish(
        "battle-a", ("|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",)
    )
    await decidir(
        "battle-b",
        frames_a_publicar=[("|switch|p2a: Weezing|Weezing, L83, M|100/100",)],
    )
    watermark_b = player._projected_until["battle-b"]

    estado_a2 = await decidir(
        "battle-a",
        frames_a_publicar=[("|switch|p2a: Drapion|Drapion, L80, F|100/100",)],
    )
    especies = [m["species"] for m in estado_a2["opponent"]["pokemon"]]
    assert "mandibuzz" in especies, (
        "el frame de A publicado mientras B decida no puede perderse"
    )
    # Evidencia estructural: el watermark es un dict POR TAG y cada tag
    # avanza sobre SUS frames, no sobre los de la otra batalla.
    assert player._projected_until["battle-a"] > watermark_a
    assert player._projected_until["battle-b"] == watermark_b
    assert player._projected_until["battle-a"] != player._projected_until["battle-b"]


async def test_ventana_vacia_falla_cerrado_sin_dejar_fila():
    """MON-26 R2 (F4): el contrato de `wait_for_resolution` permite lista
    VACIA cuando el cursor de la decision quedo por detras del watermark
    (dos decisiones resolviendo al mismo `closing`; medido por Tasos con
    probe). `window[-1]` sin guarda lanzaria `IndexError`, que escapa de los
    dos `except` de fallo cerrado SIN `_drop_step` -- justo la propiedad que
    el resto del modulo cuida. La guarda lo convierte en
    `ProjectionTimeoutError` y el paso reservado se descarta."""
    class EchoGraph:
        async def ainvoke(self, graph_input):
            return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}

    player = _player(decision_graph=EchoGraph())
    tag = "battle-ventana-vacia"
    r1 = await player.frame_inbox.publish(tag, ("|request|1",))

    def serializado(b):
        return {
            "turn": 3, "opponent": {"pokemon": []},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        }

    async def decidir(cursor_ctx):
        battle = _fake_battle(
            battle_tag=tag, available_moves=[SimpleNamespace(id="tackle")]
        )
        with patch.object(client_module, "serialize_battle", serializado):
            CURRENT_FRAME_SEQ.set(cursor_ctx)
            return player.choose_move(battle)

    # Decision 1: cursor cae a last_seq (= r1) y consume la narracion n1;
    # el watermark queda en n1.seq.
    pending1 = await decidir(None)
    n1 = await player.frame_inbox.publish(tag, ("|upkeep",))
    order = await pending1
    assert order.order.id == "tackle"
    assert player._projected_until[tag] == n1.seq

    # La decision 1 se resuelve como lo haria el request real del servidor.
    player._pending_choices[tag].outbound_phase = "sent"
    player._observe_request(tag, '{"rqid": 2}')

    # Decision 2: el cursor (r1.seq) queda por DETRAS del watermark
    # (n1.seq). El frame de cierre es el de n1, ya consumido: la ventana es
    # vacia y tiene que fallar CERRADO, no con IndexError.
    with pytest.raises(client_module.ProjectionTimeoutError):
        await (await decidir(r1.seq))

    assert len(player.steps[tag]) == 1, (
        "la decision con ventana vacia no puede dejar un paso persistible: "
        "queda solo el paso de la decision 1"
    )
    assert player.projection_timeout_count == 1


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
    ('spikes') no aparece en NINGUN lado del protocolo.

    MON-21 (D45): el indice devuelto es DESPUES de la linea de repeticion
    (`|move|p1a: Skarmory|Stealth Rock||[still]`, offset 3), no despues del
    anuncio de Encore (offset 1). La version original de este test pineaba
    `(26, 2)` -- el cursor que dejaba esa linea de repeticion SIN CONSUMIR,
    exactamente el hueco que la decision siguiente, bajo Encore+trapping,
    terminaba robando (ver
    test_correct_step_turns_encore_mas_trapping_no_hereda_el_turno_de_la_decision_anterior)."""
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
    ) == (26, 4)


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


# --- MON-21: Encore + trapping puede duplicar `turn_number` entre dos
# decisiones que no son un reemplazo forzado legitimo (D21) -- ver
# docs/DECISIONS.md D45. Reproduccion minima de las dos capturas reales de
# MON-11 R4 (`battle-gen6randombattle-3349`, decisiones 26/turno23 y
# 38/turno35), mas los contrapesos que exige la aceptacion de MON-21. ---


def test_find_action_line_encore_sin_repeticion_en_su_turno_no_ancla_ahi():
    """Reproduccion minima de `battle-gen6randombattle-3349`, decision 26
    (Mamoswine): el Encore del rival aparece en un turno donde nuestra
    accion de ESE turno ya se resolvio por otro camino (aca: el cursor
    arranca DESPUES de esa resolucion, como lo dejaria la decision
    anterior) -- no hay ninguna repeticion propia que lo confirme dentro
    del mismo bloque. La decision real (forzada por Encore+trapping a
    repetir el mismo movimiento) se resuelve un turno mas adelante, y la
    busqueda tiene que llegar hasta ahi en vez de anclarse en el anuncio."""
    recorder = _recorder_con([
        [
            "|turn|23",
            "|-sidestart|p2: Rival|move: Stealth Rock",
            "|move|p2a: Wobbuffet|Encore|p1a: Mamoswine",
            "|-start|p1a: Mamoswine|Encore",
        ],
        [
            "|turn|24",
            "|move|p1a: Mamoswine|Stealth Rock||[still]",
            "|-fail|p1a: Mamoswine",
        ],
    ])
    assert _find_action_line(
        recorder, "p1", {"kind": "move", "id": "stealthrock"}, 0, max_turn=26,
        actor_species="mamoswine",
    ) == (24, 6), (
        "sin confirmacion en el turno 23, la busqueda tiene que seguir "
        "hasta la repeticion real del turno 24, no anclarse en el anuncio"
    )


def test_correct_step_turns_encore_mas_trapping_no_hereda_el_turno_de_la_decision_anterior():
    """Integracion (atraviesa `_correct_step_turns`, el llamador real):
    reproduccion de `battle-gen6randombattle-3349`, decisiones 25->26
    (Mamoswine). Decision 25 elige Stealth Rock libremente entre 4
    movimientos y se resuelve en el turno 23. El rival (Wobbuffet, Shadow
    Tag) lo encorea ahi mismo. Decision 26 -- Encore + trapping la fuerzan a
    ofrecer el UNICO movimiento legal, el mismo Stealth Rock -- se resuelve
    de verdad en el turno 24 (`|move|...||[still]`). ANTES de este fix,
    ambas quedaban con `turn=23`: un `turn_number` duplicado sin ninguna
    firma de reemplazo forzado legitima (D21) que lo explique."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        [
            "|turn|23",
            "|move|p1a: Mamoswine|Stealth Rock|p2a: Wobbuffet",
            "|-sidestart|p2: Rival|move: Stealth Rock",
            "|move|p2a: Wobbuffet|Encore|p1a: Mamoswine",
            "|-start|p1a: Mamoswine|Encore",
        ],
        [
            "|turn|24",
            "|move|p1a: Mamoswine|Stealth Rock||[still]",
            "|-fail|p1a: Mamoswine",
        ],
    ])
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 23, "decision_turn": 23, "state": {}, "actor_species": "mamoswine",
         "action_taken": {"kind": "move", "id": "stealthrock"}},
        {"turn": 23, "decision_turn": 23, "state": {}, "actor_species": "mamoswine",
         "action_taken": {"kind": "move", "id": "stealthrock"}},
    ]

    player._correct_step_turns(tag)

    assert [s["turn"] for s in player.steps[tag]] == [23, 24], (
        "la decision 26 (Encore+trapping) tiene que resolverse en SU propio "
        "turno (24), no heredar el turno de la decision 25 (23)"
    )


def test_correct_step_turns_encore_intercepta_y_la_decision_siguiente_no_hereda_su_linea():
    """Integracion: reproduccion de `battle-gen6randombattle-3349`,
    decisiones 37->38 (Cresselia). Decision 37 elige Moonlight entre 6
    opciones, pero el Encore del rival (prioridad +2) intercepta ANTES de
    que se ejecute y fuerza la repeticion de Moonblast, el ultimo
    movimiento usado -- se resuelve en el turno 35 via el respaldo de
    Encore (D23). Decision 38 -- Encore + trapping la fuerzan a Moonblast
    unico -- se resuelve de verdad en el turno 36. ANTES de este fix, el
    respaldo de la decision 37 dejaba la linea de Moonblast SIN CONSUMIR y
    la decision 38 la robaba, quedando tambien en turno 35."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        [
            "|turn|35",
            "|move|p2a: Wobbuffet|Encore|p1a: Cresselia",
            "|-start|p1a: Cresselia|Encore",
            "|move|p1a: Cresselia|Moonblast|p2a: Wobbuffet",
        ],
        [
            "|turn|36",
            "|move|p1a: Cresselia|Moonblast|p2a: Wobbuffet",
        ],
    ])
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 35, "decision_turn": 35, "state": {}, "actor_species": "cresselia",
         "action_taken": {"kind": "move", "id": "moonlight"}},
        {"turn": 35, "decision_turn": 35, "state": {}, "actor_species": "cresselia",
         "action_taken": {"kind": "move", "id": "moonblast"}},
    ]

    player._correct_step_turns(tag)

    assert [s["turn"] for s in player.steps[tag]] == [35, 36], (
        "la decision 38 (Encore+trapping, forzada a Moonblast) tiene que "
        "encontrar SU PROPIA repeticion en el turno 36, no heredar la "
        "linea que ya resolvio a la decision 37 en el turno 35"
    )


def test_correct_step_turns_encore_ordinario_con_cambio_disponible_no_se_ve_afectado():
    """Contrapeso (aceptacion MON-21): Encore SIN trapping. El pokemon
    encoreado puede cambiarse libremente (D23) -- este mecanismo nuevo no
    se activa para una decision de CAMBIO (`accion_es_movimiento=False`),
    asi que un Encore ordinario con salida disponible tiene que seguir
    resolviendo cada decision en su propio turno, sin compartir nada."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        [
            "|turn|22",
            "|move|p2a: Shuckle|Encore|p1a: Gourgeist",
            "|-start|p1a: Gourgeist|Encore",
        ],
        ["|turn|23", "|switch|p1a: Kangaskhan|Kangaskhan-Mega, L71, F|267/267"],
        ["|turn|24", "|move|p1a: Kangaskhan|Return|p2a: Shuckle"],
    ])
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 22, "decision_turn": 22, "state": {}, "actor_species": "gourgeist",
         "action_taken": {"kind": "switch", "species": "kangaskhanmega"}},
        {"turn": 23, "decision_turn": 23, "state": {}, "actor_species": "kangaskhanmega",
         "action_taken": {"kind": "move", "id": "return"}},
    ]

    player._correct_step_turns(tag)

    assert [s["turn"] for s in player.steps[tag]] == [23, 24], (
        "un cambio bajo Encore sigue resolviendose en su propio turno, sin "
        "interaccion con el respaldo de repeticion forzada"
    )


def test_correct_step_turns_trapped_sin_encore_no_dispara_el_mecanismo_nuevo():
    """Contrapeso (aceptacion MON-21): trapping SIN Encore. Con varios
    movimientos legales (nunca colapsados a uno solo) y ninguna linea
    `|move|{opp}a:...|Encore|...` en el protocolo, el respaldo de Encore
    nunca se activa -- cada decision matchea por su propio `|move|` real,
    sin ninguna interaccion con `encore_rival_turno`."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        ["|turn|9", "|move|p1a: Wobbuffet|Counter|p2a: Y"],
        ["|turn|10", "|move|p1a: Wobbuffet|Mirror Coat|p2a: Y"],
    ])
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 9, "decision_turn": 9, "state": {}, "actor_species": "wobbuffet",
         "action_taken": {"kind": "move", "id": "counter"}},
        {"turn": 9, "decision_turn": 9, "state": {}, "actor_species": "wobbuffet",
         "action_taken": {"kind": "move", "id": "mirrorcoat"}},
    ]

    player._correct_step_turns(tag)

    assert [s["turn"] for s in player.steps[tag]] == [9, 10]


def test_correct_step_turns_reemplazo_forzado_real_tras_debilitamiento_sigue_compartiendo_turno():
    """Contrapeso (aceptacion MON-21): un reemplazo forzado REAL (D21, tras
    un debilitamiento) sigue compartiendo `turn_number` con la decision
    anterior -- eso es LEGITIMO (D21) y este fix no lo toca: no hay ningun
    Encore de por medio, `encore_rival_turno` nunca se setea."""
    player = _player()
    tag = "battle-x-1"
    player.recorders[tag] = _recorder_con([
        [
            "|turn|12",
            "|move|p2a: Y|Earthquake|p1a: Garchomp",
            "|-damage|p1a: Garchomp|0 fnt",
            "|faint|p1a: Garchomp",
            "|switch|p1a: Latios|Latios, L84, M|278/278",
        ],
    ])
    player._sides[tag] = "p1"
    player.steps[tag] = [
        {"turn": 12, "decision_turn": 12, "state": {}, "actor_species": "garchomp",
         "action_taken": {"kind": "move", "id": "earthquake"}},
        {"turn": 12, "decision_turn": 12, "state": {}, "actor_species": None,
         "action_taken": {"kind": "switch", "species": "latios"}},
    ]

    player._correct_step_turns(tag)

    assert [s["turn"] for s in player.steps[tag]] == [12, 12], (
        "el reemplazo forzado tras un debilitamiento SI comparte turno "
        "legitimamente (D21) -- este fix no debe alterar ese caso"
    )


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
    rechazado["rationale"] = "REJECTED_RATIONALE"
    rechazado["reasoning"] = "REJECTED_RATIONALE"
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
                "rationale": "FRESH_RATIONALE",
                "reasoning": "FRESH_RATIONALE",
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
    assert final["rationale"] == "FRESH_RATIONALE"
    assert final["reasoning"] == "FRESH_RATIONALE"
    assert "REJECTED_PATH" not in repr(final)
    assert "REJECTED_RATIONALE" not in repr(final)
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
    # Alias interno derivado del rationale validado (L-01): el step lo lleva
    # como `reasoning` solo para los consumidores que ya lo leian.
    assert step["reasoning"] == "breve y user-facing"
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
# --- F2-09 (MON-14): adapter de ejecucion cableado y metadata por turno ---


async def test_el_caller_ejecuta_despues_de_decide_en_orden():
    """Correspondencia end-to-end (F2-09/D39): el grafo corre
    resolve_provider → parse_state → retrieve_context → calc_damage → decide
    y el adapter de ejecucion (`execute_action`, desde graph/execute.py) se
    aplica DESPUES, dentro de `run_graph`, sobre el resultado del grafo.

    Canario del cableado: el evento "execute" solo puede salir de la llamada
    real a `execute_action` dentro de `run_graph`. Si el cableado se
    desconecta (se vuelve a la traduccion inline de la base), el patch no
    intercepta nada y el orden queda sin "execute" -- el test se pone en
    rojo exactamente cuando el adapter deja de estar conectado."""
    tag = "battle-graph-orden"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(turn=1, battle_tag=tag, available_moves=[move])
    events: list[str] = []

    class Calculator:
        async def calculate(self, request):
            events.append("calc_damage")
            return {
                "damage_rolls": [[10]], "min_damage": 10, "max_damage": 10,
                "defender_hp": {"cur": 100, "max": 100},
            }

    class Repository:
        async def load_battle_context(self, **kwargs):
            events.append("retrieve_context")
            return {
                "generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [], "opponent": [],
            }

        async def load_moves(self, **kwargs):
            return {}

        async def load_mega_forms(self, **kwargs):
            return {}

    class Provider:
        async def complete(self, prompt, *, deadline, turn_id):
            events.append("decide")
            return CompletionEnvelope(
                payload={
                    "action": {"kind": "move", "id": "tackle"},
                    "rationale": "breve",
                    "confidence": 0.9,
                    "alternatives": [],
                },
                provider="google", model="gemini-2.5-flash",
                usage=CompletionUsage(input_tokens=1, output_tokens=1),
                latency_ms=1.0,
            )

    class Resolver:
        async def resolve(self):
            events.append("resolve_provider")
            return ResolvedProvider("google", "gemini-2.5-flash", Provider())

    def parser(raw):
        events.append("parse_state")
        return raw

    graph = build_decision_graph(
        Calculator(), Resolver(), DecisionMetrics(), Repository(), parser=parser,
    )

    real_execute = client_module.execute_action

    def recording_execute(action, orders):
        events.append("execute")
        return real_execute(action, orders)

    player = _player(decision_graph=graph, decision_budget_seconds=5)
    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "gen": 6, "turn": 1, "player_role": "p1",
            "format": "gen6randombattle",
            "me": {"pokemon": [{
                "species": "pikachu", "active": True, "hp_fraction": 1,
                "moves": [],
            }]},
            "opponent": {"pokemon": [{
                "species": "eevee", "active": True, "hp_fraction": 1,
                "moves": [],
            }]},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        },
    ), patch.object(client_module, "execute_action", recording_execute):
        pending = player.choose_move(battle)
        await player.frame_inbox.publish(tag, ("|upkeep",))
        order = await pending

    assert order.order.id == "tackle"
    assert events == [
        "resolve_provider", "parse_state", "retrieve_context",
        "calc_damage", "decide", "execute",
    ]


async def test_la_metadata_efectiva_cambia_por_turno_hasta_el_step():
    """BLOQUEANTE (F2-09): provider/model efectivos se resuelven POR TURNO a
    traves del flujo real (`choose_move` → grafo → step) y las claves de
    metadata llegan al step desde el resultado de CADA decision.

    Tres decisiones del mismo tag, sin recompilar el grafo: dos exito LLM con
    modelos distintos y una tercera que cae al fallback determinista. Detecta
    las regresiones del bloqueante:
    - resolucion cacheada al inicio: si la seleccion se cacheara, la decision
      2 seguiria con el modelo de la 1 (paso 2 con model equivocado);
    - reutilizacion tipo `last_*` (patron D38): si la metadata saliera de
      estado compartido de una decision anterior, cada paso heredaria la del
      anterior;
    - fuente equivocada de metadata: los 11 campos salen del resultado del
      grafo (envelope de D38), nunca de otra parte;
    - exito y fallback usan la MISMA frontera: el paso del fallback no
      atribuye provider/model, confidence ni alternatives a ningun modelo."""
    tag = "battle-graph-per-turn"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(turn=1, battle_tag=tag, available_moves=[move])

    class Calculator:
        async def calculate(self, request):
            return {
                "damage_rolls": [[10]], "min_damage": 10, "max_damage": 10,
                "defender_hp": {"cur": 100, "max": 100},
            }

    class Repository:
        async def load_battle_context(self, **kwargs):
            return {
                "generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [], "opponent": [],
            }

        async def load_moves(self, **kwargs):
            return {}

        async def load_mega_forms(self, **kwargs):
            return {}

    class TurnProvider:
        def __init__(self, provider_name, model_id, *, valid=True,
                     rationale="breve", confidence=0.7):
            self.provider_name = provider_name
            self.model_id = model_id
            self.valid = valid
            self.rationale = rationale
            self.confidence = confidence

        async def complete(self, prompt, *, deadline, turn_id):
            if not self.valid:
                # Dos respuestas semanticamente invalidas: decide cae al
                # fallback determinista.
                payload = {
                    "action": {"kind": "move", "id": "fuera-de-la-mascara"},
                    "rationale": "invalida",
                    "confidence": 0.5,
                    "alternatives": [],
                }
            else:
                payload = {
                    "action": {"kind": "move", "id": "tackle"},
                    "rationale": self.rationale,
                    "confidence": self.confidence,
                    "alternatives": [],
                }
            return CompletionEnvelope(
                payload=payload,
                provider=self.provider_name, model=self.model_id,
                usage=CompletionUsage(input_tokens=1, output_tokens=1),
                latency_ms=1.0,
            )

    class TurnResolver:
        def __init__(self):
            self.turnos = [
                ResolvedProvider(
                    "google", "gemini-2.5-flash",
                    TurnProvider(
                        "google", "gemini-2.5-flash",
                        rationale="breve-gemini-2.5-flash", confidence=0.7,
                    ),
                ),
                ResolvedProvider(
                    "kimi", "kimi-k2.6",
                    TurnProvider(
                        "kimi", "kimi-k2.6",
                        rationale="breve-kimi-k2.6", confidence=0.9,
                    ),
                ),
                ResolvedProvider(
                    "anthropic", "claude-sonnet-4",
                    TurnProvider("anthropic", "claude-sonnet-4", valid=False),
                ),
            ]
            self.index = 0

        async def resolve(self):
            resolved = self.turnos[self.index]
            self.index += 1
            return resolved

    graph = build_decision_graph(
        Calculator(), TurnResolver(), DecisionMetrics(), Repository(),
    )
    player = _player(decision_graph=graph, decision_budget_seconds=5)
    with patch.object(
        client_module, "serialize_battle",
        lambda b: {
            "gen": 6, "turn": 1, "player_role": "p1",
            "format": "gen6randombattle",
            "me": {"pokemon": [{
                "species": "pikachu", "active": True, "hp_fraction": 1,
                "moves": [],
            }]},
            "opponent": {"pokemon": [{
                "species": "eevee", "active": True, "hp_fraction": 1,
                "moves": [],
            }]},
            "field": {"weather": {}, "field_effects": {},
                      "my_side": {}, "opponent_side": {}},
            "legal_actions": [{"kind": "move", "id": "tackle"}],
        },
    ):
        for _ in range(3):
            pending = player.choose_move(battle)
            await player.frame_inbox.publish(tag, ("|upkeep",))
            await pending
            player._resolve_pending_choice(tag)

    primer_paso = player.steps[tag][0]
    segundo_paso = player.steps[tag][1]
    tercer_paso = player.steps[tag][2]

    assert (primer_paso["provider"], primer_paso["model"]) == (
        "google", "gemini-2.5-flash",
    )
    assert primer_paso["rationale"] == "breve-gemini-2.5-flash"
    assert primer_paso["confidence"] == 0.7
    assert (segundo_paso["provider"], segundo_paso["model"]) == (
        "kimi", "kimi-k2.6",
    )
    assert segundo_paso["rationale"] == "breve-kimi-k2.6"
    assert segundo_paso["confidence"] == 0.9
    # Las 11 claves de metadata llegan al step desde el resultado del grafo
    # en ambas decisiones, sin cruzarse entre si.
    for paso in (primer_paso, segundo_paso):
        for key in (
            "rationale", "confidence", "alternatives", "target", "provider",
            "model", "decision_latency_ms", "input_tokens", "output_tokens",
            "cached_input_tokens", "reasoning_tokens",
        ):
            assert key in paso, f"el step debe llevar la metadata {key!r}"
    assert segundo_paso["provider"] != primer_paso["provider"]
    assert segundo_paso["model"] != primer_paso["model"]
    # Fallback: la misma frontera que el exito. Nada de la decision 2 puede
    # filtrarse a la 3.
    assert tercer_paso["action_path"] == "fallback"
    assert tercer_paso["provider"] is None
    assert tercer_paso["model"] is None
    assert tercer_paso["confidence"] is None
    assert tercer_paso["alternatives"] == []
    assert tercer_paso["target"] is None
    assert tercer_paso["rationale"] == (
        "deterministic fallback after two invalid model responses"
    )


# --- D46/MON-23: barrera terminal de lifecycle para decisiones en vuelo ---
#
# `choose_move` corre como task fire-and-forget de `listen()` en
# `ps_client.loop` (POKE_LOOP en produccion), HERMANA -- no hija -- de la
# task de `battle_against`/`run_benchmark` (ver `poke_env/concurrency.py` y
# `player.py:battle_against`). Cancelar o timeoutear ese wrapper nunca
# cancela una decision (proyeccion + grafo/calc + ejecucion) que siga en
# vuelo: son tasks independientes en el mismo loop. `drain_inflight_decisions`
# cierra la admision, cancela y espera toda decision registrada; los
# callers que comparten `CalcClient`/context repository con el grafo deben
# invocarla ANTES de cerrarlos.


def _run_on_pokeloop(coro):
    """Agenda `coro` en `POKE_LOOP` (el loop real de poke-env, en su propio
    thread) y devuelve el `concurrent.futures.Future`. Mismo mecanismo que
    `poke_env.concurrency.handle_threaded_coroutines`, pero reteniendo el
    future para poder inspeccionar el resultado de una decision huerfana."""
    return asyncio.run_coroutine_threadsafe(coro, POKE_LOOP)


async def _await_on_pokeloop(coro) -> None:
    await asyncio.wrap_future(_run_on_pokeloop(coro))


def _serialize_stub(battle):
    return {
        "turn": battle.turn,
        "opponent": {"pokemon": []},
        "field": {
            "weather": {}, "field_effects": {},
            "my_side": {}, "opponent_side": {},
        },
        "legal_actions": [
            {"kind": "move", "id": move.id} for move in battle.available_moves
        ],
    }


class _SlowGraph:
    """Simula `calc_damage` bajo carga: tarda, y solo termina "normal" si
    nadie la cancela antes."""

    def __init__(self, hold_seconds: float = 5.0) -> None:
        self.hold_seconds = hold_seconds
        self.started = threading.Event()
        self.finished_normally = False

    async def ainvoke(self, graph_input):
        self.started.set()
        await asyncio.sleep(self.hold_seconds)
        self.finished_normally = True
        return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}


async def test_drenaje_cancela_una_decision_en_vuelo_con_topologia_de_tasks_hermanas():
    """Reproduccion roja de la topologia real: sin la barrera, esta decision
    queda huerfana. La task de `choose_move` corre en POKE_LOOP, hermana de
    un wrapper tipo `battle_against` que se cancela por timeout SIN
    tocarla. Solo `drain_inflight_decisions` la cancela y la espera."""
    tag = "battle-drain-sibling-1"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(battle_tag=tag, available_moves=[move])
    graph = _SlowGraph(hold_seconds=5.0)
    player = _player(decision_graph=graph, decision_budget_seconds=60)

    with patch.object(client_module, "serialize_battle", _serialize_stub):
        pending = player.choose_move(battle)
        # Topologia real: la coroutine corre como task en ps_client.loop,
        # NUNCA en el loop del test (que aqui hace de "caller").
        decision_future = _run_on_pokeloop(pending)
        await _await_on_pokeloop(player.frame_inbox.publish(tag, ("|upkeep",)))
        assert graph.started.wait(timeout=2), "la decision nunca arranco"

        # Un wrapper hermano tipo `battle_against`, sin relacion con la
        # decision, se cancela por timeout -- como en el test real
        # (`asyncio.timeout(45)` sobre `agent.battle_against(...)`).
        async def sibling_wrapper():
            await asyncio.sleep(30)

        sibling_future = _run_on_pokeloop(sibling_wrapper())
        try:
            async with asyncio.timeout(0.1):
                await asyncio.wrap_future(sibling_future)
        except TimeoutError:
            pass
        finally:
            sibling_future.cancel()

        # Cancelar al hermano NO toca a la decision: sigue viva. Esto es
        # exactamente la causa raiz del CHECKPOINT: si este assert fallara
        # (la decision se cancelara sola), no habria bug que arreglar.
        await asyncio.sleep(0.05)
        assert not decision_future.done(), (
            "cancelar la task hermana no deberia cancelar la decision; si "
            "esto falla, algo mas -- no la barrera -- la esta deteniendo"
        )

        t0 = time.monotonic()
        await player.drain_inflight_decisions()
        elapsed = time.monotonic() - t0
        # La barrera tiene que retornar SOLO despues de que la decision
        # cancelada termino de verdad, no apenas dispararle `.cancel()`.
        assert decision_future.done(), (
            "drain_inflight_decisions() retorno sin esperar a que la "
            "decision cancelada terminara"
        )

    assert elapsed < 2.0, (
        f"la barrera tardo {elapsed:.2f}s: deberia cancelar, no esperar los "
        f"{graph.hold_seconds}s completos del grafo lento"
    )
    assert graph.finished_normally is False
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wrap_future(decision_future)


class _CalcTouchingGraph:
    """Grafo minimo que llama `CalcClient.calculate` DE VERDAD -- no un
    doble de la carrera, para ejercitar httpx/asyncio reales, igual que
    `calc_damage` en produccion."""

    def __init__(self, calculator: CalcClient) -> None:
        self.calculator = calculator
        self.started = threading.Event()

    async def ainvoke(self, graph_input):
        self.started.set()
        await self.calculator.calculate({
            "gen": 6,
            "attacker": {"species": "eevee", "level": 100},
            "defender": {"species": "eevee", "level": 100},
            "move": {"name": "tackle"},
        })
        return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}


async def _slow_http_handler(reader, writer, *, delay: float) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        pass
    await asyncio.sleep(delay)
    body = b'{"status":"ok"}'
    resp = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    try:
        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def test_drenaje_evita_la_carrera_real_de_calcclient_contra_una_decision_en_vuelo():
    """Repite el mecanismo EXACTO del sintoma verificado en MON-23: una
    llamada real de `CalcClient` a mitad de vuelo, en POKE_LOOP, cuando el
    caller ya se rindio. Con la barrera ANTES de `aclose`, la decision
    termina LIMPIO (`CancelledError`) y nunca ve `RuntimeError: ... client
    has been closed` ni `httpx.ReadError` -- los dos sintomas reportados."""
    tag = "battle-drain-calc-1"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(battle_tag=tag, available_moves=[move])

    # Servidor propio, controlado: responde recien a los 3s, para que la
    # request quede en vuelo con certeza cuando llamamos a la barrera.
    server = await asyncio.start_server(
        lambda r, w: _slow_http_handler(r, w, delay=3.0), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]

    calculator = CalcClient(f"http://127.0.0.1:{port}", timeout_seconds=30)
    graph = _CalcTouchingGraph(calculator)
    player = _player(decision_graph=graph, decision_budget_seconds=60)

    async with server:
        asyncio.create_task(server.serve_forever())
        with patch.object(client_module, "serialize_battle", _serialize_stub):
            pending = player.choose_move(battle)
            decision_future = _run_on_pokeloop(pending)
            await _await_on_pokeloop(player.frame_inbox.publish(tag, ("|upkeep",)))
            assert graph.started.wait(timeout=2), "la decision nunca arranco"
            # Tiempo real para que la request HTTP salga al socket: la
            # carrera que reproduce el issue es contra una request YA EN
            # VUELO, no una que todavia no se mando.
            await asyncio.sleep(0.2)

            await player.drain_inflight_decisions()
            # La garantia que importa: cuando la barrera retorna, la
            # decision YA terminó -- no simplemente fue marcada para
            # cancelarse. Si esto no vale, `aclose()` de abajo puede correr
            # en paralelo con la request todavia en vuelo.
            assert decision_future.done(), (
                "drain_inflight_decisions() retorno sin esperar a que la "
                "decision terminara"
            )
            await calculator.aclose()

        surfaced: BaseException | None = None
        if decision_future.done() and not decision_future.cancelled():
            surfaced = decision_future.exception()

    assert surfaced is None, (
        f"la decision huerfana no debe surgir con otra excepcion que no sea "
        f"cancelacion limpia: {type(surfaced).__name__}: {surfaced}"
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wrap_future(decision_future)


class _NeverCalledGraph:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, graph_input):
        self.calls += 1  # pragma: no cover -- nunca deberia correr
        return {"action": {"kind": "move", "id": "tackle"}, "action_path": "llm"}


async def test_drenaje_conjunto_vacio_es_idempotente_y_bloquea_admision_tardia():
    """Tres requisitos del DESIGN VERDICT en un solo flujo:

    1. drenar sin ninguna decision en vuelo no falla (conjunto vacio);
    2. una decision cuya coroutine arranca DESPUES del drenaje jamas llega
       al grafo/calc, y falla ruidosamente con `DecisionsClosedError`
       (canario de admision);
    3. llamar la barrera una segunda vez es un no-op seguro (idempotencia,
       terminal para la instancia).
    """
    tag = "battle-drain-late-1"
    move = SimpleNamespace(id="tackle")
    battle = _fake_battle(battle_tag=tag, available_moves=[move])
    graph = _NeverCalledGraph()
    player = _player(decision_graph=graph)

    # 1. Conjunto vacio.
    await player.drain_inflight_decisions()
    assert player._decisions_closed is True

    # 2. Admision tardia: la coroutine arranca DESPUES del drenaje.
    pending = player.choose_move(battle)
    with pytest.raises(client_module.DecisionsClosedError):
        await pending
    assert graph.calls == 0, "una decision tardia no puede alcanzar el grafo/calc"

    # 3. Idempotencia.
    await player.drain_inflight_decisions()
    await player.drain_inflight_decisions()
