import httpx
import pytest

from ludex_agent.graph.calc import CalcClient, calc_damage


@pytest.mark.asyncio
async def test_cliente_calc_coincide_con_respuesta_directa_gen_6():
    base_url = "http://127.0.0.1:8200"
    request = {
        "gen": 6,
        "attacker": {"species": "Pikachu", "level": 80},
        "defender": {"species": "Blastoise", "level": 80},
        "move": {"name": "Thunderbolt"},
    }
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2) as direct:
            health = await direct.get("/health")
            health.raise_for_status()
            expected_response = await direct.post("/calc", json=request)
            expected_response.raise_for_status()
            expected = expected_response.json()
    except httpx.HTTPError as exc:
        pytest.fail(f"calc real no responde en {base_url}: {exc}")

    async with CalcClient(base_url, timeout_seconds=2) as client:
        assert await client.health()
        assert await client.calculate(request) == expected


class _CaptureCalculator:
    """Envía al calc real y guarda los requests para comparación."""

    def __init__(self, base_url: str):
        self._client = CalcClient(base_url, timeout_seconds=3)
        self.requests = []

    async def calculate(self, request):
        self.requests.append(request)
        return await self._client.calculate(request)

    async def aclose(self):
        await self._client.aclose()


async def _assert_adapter_matches_direct(
    battle, context, base_url, *, action_index=0
):
    """Compara el resultado del adaptador contra /calc directo."""
    async with httpx.AsyncClient(base_url=base_url, timeout=3) as direct:
        calc_calc = _CaptureCalculator(base_url)
        try:
            await calc_damage(
                {"battle_state": battle, "context": context}, calc_calc
            )
            request = calc_calc.requests[action_index]
            expected_response = await direct.post("/calc", json=request)
            expected_response.raise_for_status()
            expected = expected_response.json()
        finally:
            await calc_calc.aclose()
        return calc_calc, request, expected


@pytest.mark.asyncio
async def test_adapter_rain_coincide_con_calc_directo():
    base_url = "http://127.0.0.1:8200"
    battle = {
        "gen": 6,
        "field": {
            "weather": {"RAINDANCE": 5}, "field_effects": {},
            "my_side": {}, "opponent_side": {},
        },
        "me": {"pokemon": [{
            "species": "pikachu", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Static", "status": None,
            "stats": {"hp": 180},
        }]},
        "opponent": {"pokemon": [{
            "species": "charizard", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Blaze", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "thunderbolt"}],
    }
    _, request, expected = await _assert_adapter_matches_direct(
        battle, {}, base_url
    )
    assert request["field"]["weather"] == "Rain"
    async with CalcClient(base_url, timeout_seconds=3) as client:
        result = await client.calculate(request)
    assert result == expected
    assert result["min_damage"] > 0


@pytest.mark.asyncio
async def test_adapter_reflect_y_boosts_coincide_con_calc_directo():
    base_url = "http://127.0.0.1:8200"
    battle = {
        "gen": 6,
        "field": {
            "weather": {}, "field_effects": {},
            "my_side": {"REFLECT": 3}, "opponent_side": {},
        },
        "me": {"pokemon": [{
            "species": "machamp", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {"atk": 2},
            "item": None, "ability": "No Guard", "status": None,
            "stats": {"hp": 200},
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {"def": 2},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "closecombat"}],
    }
    _, request, expected = await _assert_adapter_matches_direct(
        battle, {}, base_url
    )
    assert request["field"]["attackerSide"]["isReflect"] is True
    assert request["attacker"]["boosts"] == {"atk": 2}
    assert request["defender"]["boosts"] == {"def": 2}
    async with CalcClient(base_url, timeout_seconds=3) as client:
        result = await client.calculate(request)
    assert result == expected


@pytest.mark.asyncio
async def test_adapter_burn_coincide_con_calc_directo():
    base_url = "http://127.0.0.1:8200"
    battle = {
        "gen": 6,
        "field": {
            "weather": {}, "field_effects": {},
            "my_side": {}, "opponent_side": {},
        },
        "me": {"pokemon": [{
            "species": "machamp", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Guts", "status": "BRN",
            "stats": {"hp": 200},
        }]},
        "opponent": {"pokemon": [{
            "species": "blastoise", "level": 80, "active": True,
            "hp_fraction": 1.0, "moves": [], "boosts": {},
            "item": None, "ability": "Torrent", "status": None,
        }]},
        "legal_actions": [{"kind": "move", "id": "closecombat"}],
    }
    _, request, expected = await _assert_adapter_matches_direct(
        battle, {}, base_url
    )
    assert request["attacker"]["status"] == "brn"
    async with CalcClient(base_url, timeout_seconds=3) as client:
        result = await client.calculate(request)
    assert result == expected
