import httpx
import pytest

from ludex_agent.graph.calc import CalcClient


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
