from copy import deepcopy

import pytest


def test_extrae_ids_allowlisted_deduplicados_en_orden():
    from ludex_agent.graph.context import extract_species_ids

    state = {
        "raw_state": {
            "opponent": {
                "private_team": [{"species": "mewtwo"}],
            },
        },
        "battle_state": {
            "gen": 6,
            "me": {
                "pokemon": [
                    {"species": "pikachu"},
                    {"species": "charizard"},
                    {"species": "pikachu"},
                    {"species": ""},
                    "slot-invalido",
                ],
            },
            "opponent": {
                "pokemon": [
                    {"species": "garchomp"},
                    {"species": "garchomp"},
                    {},
                ],
            },
        },
    }

    assert extract_species_ids(state) == (
        ("pikachu", "charizard"),
        ("garchomp",),
    )


def test_extrae_movimientos_observados_sin_leer_raw_state():
    from ludex_agent.graph.context import extract_observed_move_ids

    state = {
        "raw_state": {
            "opponent": {
                "pokemon": [{
                    "species": "zoroark",
                    "moves": [{"id": "nightdaze"}],
                }],
            },
        },
        "battle_state": {
            "gen": 6,
            "me": {
                "pokemon": [{
                    "species": "pikachu",
                    "moves": [
                        {"id": "volttackle"},
                        {"id": "volttackle"},
                        {"id": ""},
                    ],
                }],
            },
            "opponent": {
                "pokemon": [{
                    "species": "ursaring",
                    "moves": [{"id": "sludgebomb"}],
                }],
            },
        },
    }

    assert extract_observed_move_ids(state) == (
        "volttackle",
        "sludgebomb",
    )


def _move(
    showdown_id,
    *,
    move_type,
    category,
    power,
    power_kind,
    accuracy,
    description,
    flags,
    learn_methods=None,
):
    return {
        "showdown_id": showdown_id,
        "name": showdown_id,
        "type": move_type,
        "category": category,
        "power": power,
        "power_kind": power_kind,
        "accuracy": accuracy,
        "never_misses": accuracy is None,
        "pp": 10,
        "priority": 0,
        "target": "normal",
        "flags": flags,
        "description": description,
        **(
            {"learn_methods": learn_methods}
            if learn_methods is not None else {}
        ),
    }


def test_prompt_context_separa_observados_enriquecidos_de_posibles_compactos():
    from ludex_agent.graph.context import project_prompt_context

    own_learnset_move = _move(
        "thunderbolt",
        move_type="Electric",
        category="Special",
        power=90,
        power_kind="standard",
        accuracy=100,
        description="rich-only own learnset",
        flags={"protect": 1},
        learn_methods=[{
            "gen": 6,
            "method": "machine",
            "sourceSpecies": "pikachu",
        }],
    )
    possible_only = _move(
        "facade",
        move_type="Normal",
        category="Physical",
        power=70,
        power_kind="standard",
        accuracy=100,
        description="rich-only possible description",
        flags={"contact": 1, "protect": 1},
        learn_methods=[{
            "gen": 6,
            "method": "machine",
            "sourceSpecies": "ursaring",
        }],
    )
    nasty_plot = _move(
        "nastyplot",
        move_type="Dark",
        category="Status",
        power=0,
        power_kind="status",
        accuracy=None,
        description="Raises the user's Special Attack by 2 stages.",
        flags={"snatch": 1},
    )
    sludge_bomb = _move(
        "sludgebomb",
        move_type="Poison",
        category="Special",
        power=90,
        power_kind="standard",
        accuracy=100,
        description="Has a 30% chance to poison the target.",
        flags={"bullet": 1, "protect": 1},
    )
    rich_context = {
        "generation": {"gen_number": 6, "label": "XY/ORAS"},
        "own": [{
            "showdown_id": "pikachu",
            "types": ["Electric"],
            "base_stats": {
                "hp": 35, "atk": 55, "def": 40,
                "spa": 50, "spd": 50, "spe": 90,
            },
            "abilities": {"0": "Static", "H": "Lightning Rod"},
            "moves": [own_learnset_move],
        }],
        "opponent": [{
            "showdown_id": "ursaring",
            "types": ["Normal"],
            "base_stats": {
                "hp": 90, "atk": 130, "def": 75,
                "spa": 75, "spd": 75, "spe": 55,
            },
            "abilities": {"0": "Guts", "1": "Quick Feet", "H": "Unnerve"},
            "moves": [possible_only],
        }],
        "observed_moves": {
            "nastyplot": nasty_plot,
            "sludgebomb": sludge_bomb,
        },
    }
    battle_state = {
        "gen": 6,
        "me": {
            "pokemon": [{
                "species": "pikachu",
                "moves": [{"id": "nastyplot"}],
            }],
        },
        "opponent": {
            "pokemon": [{
                "species": "ursaring",
                "moves": [{"id": "sludgebomb"}],
            }],
        },
    }
    before_rich = deepcopy(rich_context)
    before_battle = deepcopy(battle_state)

    projected = project_prompt_context(battle_state, rich_context)

    assert projected == {
        "generation": {"gen_number": 6, "label": "XY/ORAS"},
        "own": [{
            "species": "pikachu",
            "known_moves": ["nastyplot"],
        }],
        "opponent": [{
            "species": "ursaring",
            "base_types": ["Normal"],
            "base_stats": {
                "hp": 90, "atk": 130, "def": 75,
                "spa": 75, "spd": 75, "spe": 55,
            },
            "possible_abilities": ["guts", "quickfeet", "unnerve"],
            "revealed_moves": ["sludgebomb"],
            "possible_moves": ["facade"],
        }],
        "moves": {
            "nastyplot": {
                "type": "Dark",
                "category": "Status",
                "power": 0,
                "power_kind": "status",
                "accuracy": "never_misses",
                "priority": 0,
                "target": "normal",
                "description": "Raises the user's Special Attack by 2 stages.",
                "flags": {"snatch": 1},
            },
            "sludgebomb": {
                "type": "Poison",
                "category": "Special",
                "power": 90,
                "power_kind": "standard",
                "accuracy": 100,
                "priority": 0,
                "target": "normal",
                "description": "Has a 30% chance to poison the target.",
                "flags": {"bullet": 1, "protect": 1},
            },
            "facade": {
                "type": "Normal",
                "category": "Physical",
                "power": 70,
                "power_kind": "standard",
                "accuracy": 100,
                "priority": 0,
                "target": "normal",
            },
        },
    }
    assert "thunderbolt" not in projected["moves"]
    assert "description" not in projected["moves"]["facade"]
    assert "flags" not in projected["moves"]["facade"]
    assert rich_context == before_rich
    assert battle_state == before_battle


@pytest.mark.asyncio
async def test_retrieve_context_excluye_rival_no_revelado():
    from ludex_agent.graph.context import retrieve_context

    battle_context = {
        "generation": {"gen_number": 6, "label": "XY/ORAS"},
        "own": [{
            "showdown_id": "pikachu",
            "types": ["Electric"],
            "base_stats": {},
            "abilities": {},
            "moves": [],
        }],
        "opponent": [{
            "showdown_id": "garchomp",
            "types": ["Dragon", "Ground"],
            "base_stats": {},
            "abilities": {},
            "moves": [],
        }],
    }

    class RecordingRepository:
        def __init__(self):
            self.calls = []

        async def load_battle_context(
            self, *, gen_number, own_species, opponent_species
        ):
            self.calls.append({
                "gen_number": gen_number,
                "own_species": own_species,
                "opponent_species": opponent_species,
            })
            return battle_context

        async def load_moves(self, *, gen_number, move_ids):
            self.calls.append({
                "gen_number": gen_number,
                "move_ids": move_ids,
            })
            return {}

    repository = RecordingRepository()
    state = {
        "raw_state": {
            "opponent": {
                "pokemon": [
                    {"species": "garchomp"},
                    {"species": "mewtwo"},
                ],
            },
        },
        "battle_state": {
            "gen": 6,
            "me": {"pokemon": [{"species": "pikachu"}]},
            "opponent": {
                "pokemon": [
                    {
                        "species": "garchomp",
                        "unrevealed_species": "mewtwo",
                    },
                    {"species": None},
                    {},
                ],
            },
        },
    }

    result = await retrieve_context(state, repository)

    assert result["context"] == {
        **battle_context,
        "observed_moves": {},
    }
    assert result["prompt_context"]["own"] == [{
        "species": "pikachu",
        "known_moves": [],
    }]
    assert [
        opponent["species"]
        for opponent in result["prompt_context"]["opponent"]
    ] == ["garchomp"]
    assert repository.calls == [
        {
            "gen_number": 6,
            "own_species": ("pikachu",),
            "opponent_species": ("garchomp",),
        },
        {
            "gen_number": 6,
            "move_ids": (),
        },
    ]


@pytest.mark.asyncio
async def test_retrieve_context_falla_si_falta_un_movimiento_observado():
    from ludex_agent.graph.context import retrieve_context

    class IncompleteRepository:
        async def load_battle_context(
            self, *, gen_number, own_species, opponent_species
        ):
            return {
                "generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [],
                "opponent": [],
            }

        async def load_moves(self, *, gen_number, move_ids):
            return {}

    state = {
        "battle_state": {
            "gen": 6,
            "me": {
                "pokemon": [{
                    "species": "pikachu",
                    "moves": [{"id": "movimientoausente"}],
                }],
            },
            "opponent": {"pokemon": []},
        },
    }

    with pytest.raises(LookupError, match="movimientoausente"):
        await retrieve_context(state, IncompleteRepository())
