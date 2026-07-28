from typing import TypedDict

import poke_env
import websockets
from langgraph.graph import END, START, StateGraph


class _State(TypedDict):
    value: int


def test_poke_env_y_langgraph_local_conviven_con_websockets_16():
    assert poke_env is not None
    assert websockets.__version__.startswith("16.")

    graph = StateGraph(_State)
    graph.add_node("increment", lambda state: {"value": state["value"] + 1})
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)

    assert graph.compile().invoke({"value": 1}) == {"value": 2}
