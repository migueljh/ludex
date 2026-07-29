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


@pytest.mark.asyncio
async def test_retrieve_context_excluye_rival_no_revelado():
    from ludex_agent.graph.context import retrieve_context

    expected_context = {
        "generation": {"gen_number": 6, "label": "XY/ORAS"},
        "own": [],
        "opponent": [],
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
            return expected_context

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

    assert result == {"context": expected_context}
    assert repository.calls == [{
        "gen_number": 6,
        "own_species": ("pikachu",),
        "opponent_species": ("garchomp",),
    }]
