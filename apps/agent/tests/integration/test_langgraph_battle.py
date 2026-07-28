import time

from poke_env import AccountConfiguration
from poke_env.player import RandomPlayer

from ludex_agent.config import load_settings
from ludex_agent.showdown.client import local_server_configuration


async def test_langgraph_y_poke_env_juegan_una_batalla_en_el_mismo_proceso():
    # Import local deliberado: el test prueba que cargar y ejecutar LangGraph
    # no rompe el websocket que poke-env usa inmediatamente después.
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(dict)
    graph.add_node("identity", lambda state: state)
    graph.add_edge(START, "identity")
    graph.add_edge("identity", END)
    assert graph.compile().invoke({"ok": True}) == {"ok": True}

    settings = load_settings()
    server = local_server_configuration(settings.showdown_ws_url)
    suffix = str(time.time_ns())[-8:]
    common = {
        "server_configuration": server,
        "battle_format": settings.showdown_battle_format,
        "log_level": 40,
    }
    first = RandomPlayer(
        account_configuration=AccountConfiguration(f"CompatA{suffix}", None),
        **common,
    )
    second = RandomPlayer(
        account_configuration=AccountConfiguration(f"CompatB{suffix}", None),
        **common,
    )

    await first.battle_against(second, n_battles=1)

    assert first.n_finished_battles == 1
    assert first.n_won_battles + first.n_lost_battles + first.n_tied_battles == 1
