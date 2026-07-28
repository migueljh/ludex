import json

from ludex_agent.graph.state import allowlisted_state


def test_allowlist_elimina_chat_y_claves_desconocidas_anidadas():
    raw = {
        "schema_version": 1,
        "turn": 7,
        "player_role": "p1",
        "format": "gen6randombattle",
        "gen": 6,
        "field": {
            "weather": {"RAIN": 2},
            "field_effects": {},
            "my_side": {},
            "opponent_side": {},
            "raw_protocol": "rendite",
        },
        "me": {"pokemon": [{
            "species": "ninetalesalola",
            "hp_fraction": 0.5,
            "active": True,
            "fainted": False,
            "status": None,
            "level": 80,
            "item": "leftovers",
            "ability": "snowwarning",
            "types": ["ICE", "FAIRY"],
            "boosts": {},
            "moves": [{"id": "moonblast", "pp": 10, "max_pp": 15, "secret": "rendite"}],
            "stats": {"spa": 200},
            "private": "rendite",
        }]},
        "opponent": {"pokemon": [{
            "species": "garchomp",
            "hp_fraction": 1,
            "active": True,
            "fainted": False,
            "status": None,
            "level": 80,
            "item": None,
            "ability": None,
            "types": ["DRAGON", "GROUND"],
            "boosts": {},
            "moves": [],
            "stats": {"atk": 999},
        }]},
        "legal_actions": [{"kind": "move", "id": "moonblast"}],
        "chat": "rendite",
    }

    result = allowlisted_state(raw)

    assert set(result) == {
        "schema_version", "turn", "player_role", "format", "gen",
        "field", "me", "opponent", "legal_actions",
    }
    assert "rendite" not in json.dumps(result)
    assert "stats" in result["me"]["pokemon"][0]
    assert "stats" not in result["opponent"]["pokemon"][0]


def test_allowlist_copia_acciones_solo_con_campos_conocidos():
    raw = {
        "schema_version": 1, "turn": 1, "player_role": "p1",
        "format": "gen6randombattle", "gen": 6,
        "field": {}, "me": {"pokemon": []}, "opponent": {"pokemon": []},
        "legal_actions": [
            {"kind": "move", "id": "tackle", "mega": True, "prompt_injection": "x"},
            {"kind": "switch", "species": "pikachu", "garbage": True},
        ],
    }

    assert allowlisted_state(raw)["legal_actions"] == [
        {"kind": "move", "id": "tackle", "mega": True},
        {"kind": "switch", "species": "pikachu"},
    ]
