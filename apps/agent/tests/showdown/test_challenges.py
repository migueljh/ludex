"""RED tests para Task 7 (MON-37/F3-07, S5): aceptacion explicita de
challenges (D65 seccion 6.1, canarios 10-13).

`LudexPlayer` sobrescribe los dos productores de `_challenge_queue` de
poke-env (`_update_challenges` desde `|updatechallenges|`,
`_handle_challenge_request` desde PM `/challenge`): ninguno encola por su
cuenta, los dos publican al `LobbyInbox` TODOS los formatos (no solo
`self._format`), y solo `accept_incoming_challenge` inserta explicitamente
en `_challenge_queue`. No se abre socket ni se toca poke-env real: estos
tests construyen `LudexPlayer` con `start_listening=False` (mismo patron que
el resto de `tests/showdown/`) y llaman los handlers a mano.
"""

from __future__ import annotations

import random

import pytest
from poke_env import AccountConfiguration

from ludex_agent.showdown.client import LudexPlayer, UnknownChallengeError
from ludex_agent.showdown.client import local_server_configuration
from ludex_agent.showdown.lobby import LobbyInbox


def _player(**kwargs) -> LudexPlayer:
    # Mismo patron que `tests/showdown/test_client.py::_player`: sufijo
    # aleatorio para no chocar con `|nametaken|` si algo llegara a conectar.
    sufijo = random.randint(1000, 9999)
    kwargs.setdefault("start_listening", False)
    return LudexPlayer(
        account_configuration=AccountConfiguration(f"Foo{sufijo}", None),
        battle_format="gen6ou",
        log_level=50,
        server_configuration=local_server_configuration(
            "ws://localhost:8100/showdown/websocket"
        ),
        **kwargs,
    )


def _update_challenges_message(challenges: dict[str, str]) -> list[str]:
    import orjson

    return ["", "updatechallenges", orjson.dumps({"challengesFrom": challenges}).decode()]


@pytest.mark.asyncio
async def test_update_challenges_publishes_without_enqueuing() -> None:
    player = _player()

    await player._update_challenges(_update_challenges_message({"rival1": "gen6ou"}))

    assert player._challenge_queue.empty()
    events = player.lobby_inbox.snapshot()
    assert len(events) == 1
    assert events[0]["type"] == "challenge"
    assert events[0]["direction"] == "incoming"
    assert events[0]["user"] == "rival1"
    assert events[0]["format"] == "gen6ou"


@pytest.mark.asyncio
async def test_handle_challenge_request_publishes_without_enqueuing() -> None:
    player = _player()

    # PM `/challenge`: split_message[2] es el retador, split_message[5] el
    # formato (mismo indice que usa poke-env, ver player.py).
    await player._handle_challenge_request(
        ["", "pm", "rival2", "Ludex", "/challenge", "gen6ou"]
    )

    assert player._challenge_queue.empty()
    events = player.lobby_inbox.snapshot()
    assert events[0]["user"] == "rival2"
    assert events[0]["format"] == "gen6ou"


@pytest.mark.asyncio
async def test_challenges_of_other_formats_stay_visible_in_lobby() -> None:
    """Canario 12: un challenge de otro formato NUNCA se filtra del lobby,
    a diferencia del `_challenge_queue` original de poke-env (que solo
    encola si `format_ == self._format`)."""
    player = _player()

    await player._update_challenges(
        _update_challenges_message({"rival1": "gen6ou", "rival2": "gen9ou"})
    )

    users = {event["user"]: event["format"] for event in player.lobby_inbox.snapshot()}
    assert users == {"rival1": "gen6ou", "rival2": "gen9ou"}
    assert set(player.incoming_challenges) == {"rival1", "rival2"}


@pytest.mark.asyncio
async def test_updatechallenges_snapshot_publishes_withdrawal() -> None:
    """`|updatechallenges|` manda el mapa COMPLETO vigente cada vez: un
    usuario ausente en el snapshot nuevo retiro su challenge."""
    player = _player()
    await player._update_challenges(_update_challenges_message({"rival1": "gen6ou"}))

    await player._update_challenges(_update_challenges_message({}))

    assert "rival1" not in player.incoming_challenges
    events = player.lobby_inbox.snapshot()
    assert events[-1]["type"] == "challenge_withdrawn"
    assert events[-1]["user"] == "rival1"


@pytest.mark.asyncio
async def test_accept_incoming_challenge_rejects_unknown_user() -> None:
    player = _player()

    with pytest.raises(UnknownChallengeError):
        await player.accept_incoming_challenge("ghost")

    assert player._challenge_queue.empty()


@pytest.mark.asyncio
async def test_accept_incoming_challenge_enqueues_known_user() -> None:
    """Canario 13: solo el accept explicito encola. `PSClient.accept_
    challenge` nunca se llama desde aca (D65: eso rompe la contabilidad de
    poke-env)."""
    player = _player()
    await player._update_challenges(_update_challenges_message({"rival1": "gen6ou"}))

    await player.accept_incoming_challenge("rival1")

    assert player._challenge_queue.qsize() == 1
    queued = await player._challenge_queue.get()
    assert queued == "rival1"
    assert "rival1" not in player.incoming_challenges
    events = player.lobby_inbox.snapshot()
    assert events[-1]["type"] == "challenge_accepted"
    assert events[-1]["user"] == "rival1"


@pytest.mark.asyncio
async def test_accept_incoming_challenge_normalizes_username() -> None:
    player = _player()
    await player._update_challenges(_update_challenges_message({"Rival One": "gen6ou"}))

    await player.accept_incoming_challenge("rival one")

    assert player._challenge_queue.qsize() == 1


@pytest.mark.asyncio
async def test_reject_incoming_challenge_removes_without_enqueuing() -> None:
    player = _player()
    await player._update_challenges(_update_challenges_message({"rival1": "gen6ou"}))

    await player.reject_incoming_challenge("rival1")

    assert player._challenge_queue.empty()
    assert "rival1" not in player.incoming_challenges
    events = player.lobby_inbox.snapshot()
    assert events[-1]["type"] == "challenge_rejected"


@pytest.mark.asyncio
async def test_reject_incoming_challenge_rejects_unknown_user() -> None:
    player = _player()

    with pytest.raises(UnknownChallengeError):
        await player.reject_incoming_challenge("ghost")


@pytest.mark.asyncio
async def test_custom_lobby_inbox_is_used_when_injected() -> None:
    inbox = LobbyInbox()
    player = _player(lobby_inbox=inbox)

    await player._update_challenges(_update_challenges_message({"rival1": "gen6ou"}))

    assert inbox.snapshot()
    assert player.lobby_inbox is inbox
