"""Contrato con el seam PRIVADO de poke-env que usa el camino pre-lock (D31).

`LudexPlayer` envuelve `PSClient._handle_message`, que es privado. poke-env
0.15.0 no expone ningun hook publico que corra antes del lock por batalla, y
`Player` instancia `PSClient` directamente, asi que no hay forma de inyectar
uno.

Estos tests existen para que un bump de poke-env falle ACA, ruidosamente y con
un mensaje que explique que mirar, en vez de degradar en silencio la frescura
del dataset.

Diseño completo:
`docs/superpowers/specs/2026-07-29-f2-01-prelock-snapshot-design.md`
"""

import asyncio
import inspect
import random

import pytest
from poke_env import AccountConfiguration
from poke_env.ps_client.ps_client import PSClient

from ludex_agent.showdown.client import LudexPlayer, local_server_configuration


VERSION_VERIFICADA = "0.15.0"
PISTA = (
    f"verificado contra poke-env=={VERSION_VERIFICADA}. Si esto falla tras un "
    "bump, revisar docs/superpowers/specs/"
    "2026-07-29-f2-01-prelock-snapshot-design.md seccion 6.11 antes de tocar "
    "showdown/client.py"
)


def _player(**kwargs) -> LudexPlayer:
    kwargs.setdefault("start_listening", False)
    return LudexPlayer(
        account_configuration=AccountConfiguration(
            f"Contract{random.randint(1000, 9999)}", None
        ),
        battle_format="gen6randombattle",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
        **kwargs,
    )


def test_la_version_de_pokeenv_es_la_verificada():
    from importlib.metadata import version

    assert version("poke-env") == VERSION_VERIFICADA, (
        f"el camino pre-lock depende de internals de poke-env. {PISTA}"
    )


def test_handle_message_sigue_siendo_el_punto_de_enganche():
    assert hasattr(PSClient, "_handle_message"), PISTA
    assert inspect.iscoroutinefunction(PSClient._handle_message), PISTA
    parametros = list(inspect.signature(PSClient._handle_message).parameters)
    assert parametros == ["self", "message"], (
        f"cambio la firma del seam que envolvemos. {PISTA}"
    )


def test_start_listening_false_no_arranca_el_listener():
    """De esto depende poder instalar el observador ANTES del primer frame."""
    player = _player(start_listening=False)
    assert getattr(player.ps_client, "_listening_coroutine", None) is None, (
        f"poke-env arranco el listener pese a start_listening=False. {PISTA}"
    )
    assert player._listener_started is False


def test_start_listening_es_idempotente_y_respeta_al_caller():
    player = _player(start_listening=False)
    player.start_listening()
    primera = player.ps_client._listening_coroutine
    player.start_listening()
    assert player.ps_client._listening_coroutine is primera, (
        "start_listening() no puede arrancar el listener dos veces"
    )
    primera.cancel()


async def test_el_inbox_se_puebla_con_el_lock_de_la_batalla_TOMADO():
    """LA aserción del contrato.

    Es lo que hace viable a F2-01: el frame crudo queda publicado aunque el
    lock por batalla este ocupado por una decision en curso. Si poke-env
    moviera el lock por encima de `_handle_message`, este test cae y hay que
    rediseñar antes de tocar nada.
    """
    player = _player()
    tag = "battle-contract-1"
    entrantes: list = []

    async def espia(split_messages):
        entrantes.append(split_messages)

    player.ps_client._on_battle_message = espia

    # Simula una decision en curso: el lock de esta batalla esta tomado.
    lock = asyncio.Lock()
    player.ps_client._battle_locks[tag] = lock
    await lock.acquire()

    tarea = asyncio.create_task(
        player.ps_client._handle_message(f">{tag}\n|switch|p2a: Latias|Latias, L77, F|100/100")
    )
    await asyncio.sleep(0.05)

    assert player.frame_inbox.last_seq(tag) > 0, (
        f"el frame no se publico antes del lock: el camino pre-lock no "
        f"funciona. {PISTA}"
    )
    assert entrantes == [], (
        f"poke-env proceso el frame pese al lock tomado: el lock ya no "
        f"serializa `_handle_battle_message`. {PISTA}"
    )

    lock.release()
    await asyncio.wait_for(tarea, timeout=2)
    assert entrantes, "al liberar el lock el frame tiene que procesarse igual"


async def test_el_observador_delega_sin_alterar_el_mensaje():
    """Observa y delega: no consume, no filtra, no reordena."""
    player = _player()
    recibidos: list = []

    async def espia(split_messages):
        recibidos.append(split_messages)

    player.ps_client._on_battle_message = espia
    mensaje = ">battle-contract-2\n|turn|4\n|upkeep"
    await player.ps_client._handle_message(mensaje)

    assert recibidos == [[m.split("|") for m in mensaje.split("\n")]]


async def test_los_frames_que_no_son_de_batalla_pasan_intactos():
    player = _player()
    await player.ps_client._handle_message("|popup|algo")  # no debe reventar
    assert player.frame_inbox.last_seq("") == 0
