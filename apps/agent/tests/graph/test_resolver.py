"""F2-09 (MON-14): resolucion de provider/model por decision.

El bloqueante del encargo: el MISMO grafo, invocado dos veces, usa el modelo
ACTIVO de cada invocacion -- cambiar la seleccion en la DB entre ambas debe
surtir efecto sin recompilar el grafo ni reiniciar la batalla.
"""

import time

import pytest

from ludex_agent.db.model_repository import ProviderModel, ProviderRow
from ludex_agent.graph.provider import (
    CompletionEnvelope,
    CompletionUsage,
    DecisionMetrics,
    PinnedResolver,
    ProviderMixError,
    ProviderResolver,
    ProviderSelectionError,
    ResolvedProvider,
)
from ludex_agent.graph.workflow import build_decision_graph


class ScriptedProvider:
    """Provider de test que reporta su (provider, model) en cada envelope."""

    def __init__(self, provider_name, model_id):
        self.provider_name = provider_name
        self.model_id = model_id
        self.calls = 0

    async def complete(self, prompt, *, deadline, turn_id):
        self.calls += 1
        return CompletionEnvelope(
            payload={
                "action": {"kind": "move", "id": "tackle"},
                "rationale": "breve",
                "confidence": 0.9,
                "alternatives": [],
            },
            provider=self.provider_name,
            model=self.model_id,
            usage=CompletionUsage(input_tokens=1, output_tokens=1),
            latency_ms=1.0,
        )


class FakeModelRepository:
    """La DB fake: la seleccion activa se puede cambiar entre invocaciones."""

    def __init__(self, selection):
        self.selection = selection
        self.selection_calls = 0
        self.provider_calls = 0

    async def active_selection(self):
        self.selection_calls += 1
        return self.selection

    async def provider(self, name):
        self.provider_calls += 1
        return ProviderRow(name=name, base_url=None, api_key_env="GEMINI_API_KEY", enabled=True)


def _fake_factory(providers):
    def factory(name, model_id, *, base_url, api_key_env, metrics,
                timeout_seconds, routes, environ):
        provider = ScriptedProvider(name, model_id)
        providers.append(provider)
        return provider
    return factory


class _SinContexto:
    async def load_battle_context(self, **kwargs):
        return {"generation": {"gen_number": 6, "label": "XY/ORAS"},
                "own": [], "opponent": []}

    async def load_moves(self, **kwargs):
        return {
            "tackle": {
                "showdown_id": "tackle", "name": "Tackle",
                "type": "Normal", "category": "Physical", "power": 50,
                "power_kind": "standard", "accuracy": 100,
                "never_misses": False, "pp": 35, "priority": 0,
                "target": "normal", "flags": {"contact": 1},
                "description": "Hits the target.",
            },
        }

    async def load_mega_forms(self, **kwargs):
        return {}


class _SinCalc:
    async def calculate(self, request):
        return {
            "damage_rolls": [[10]], "min_damage": 10, "max_damage": 10,
            "defender_hp": {"cur": 100, "max": 100},
        }


_RAW = {
    "gen": 6,
    "me": {"pokemon": [{
        "species": "pikachu", "active": True, "hp_fraction": 1,
        "moves": [{"id": "tackle"}],
    }]},
    "opponent": {"pokemon": [{
        "species": "eevee", "active": True, "hp_fraction": 1, "moves": [],
    }]},
    "field": {},
    "legal_actions": [{"kind": "move", "id": "tackle"}],
}


@pytest.mark.asyncio
async def test_el_mismo_grafo_cambia_de_modelo_entre_invocaciones():
    """BLOQUEANTE 1 (F2-09): el mismo grafo compilado UNA vez; entre las dos
    invocaciones cambia la seleccion activa en la DB; la segunda decision usa
    el modelo nuevo, sin recompilar el grafo."""
    repo = FakeModelRepository(ProviderModel("google", "modelo-a"))
    providers: list[ScriptedProvider] = []
    resolver = ProviderResolver(
        repo, provider_factory=_fake_factory(providers),
        metrics=DecisionMetrics(), environ={"GEMINI_API_KEY": "k"},
    )
    graph = build_decision_graph(
        _SinCalc(), resolver, DecisionMetrics(), _SinContexto(),
    )

    r1 = await graph.ainvoke({
        "raw_state": _RAW, "turn_id": "battle:1",
        "deadline": time.monotonic() + 5,
    })
    repo.selection = ProviderModel("google", "modelo-b")
    r2 = await graph.ainvoke({
        "raw_state": _RAW, "turn_id": "battle:2",
        "deadline": time.monotonic() + 5,
    })

    assert r1["provider"] == "google" and r1["model"] == "modelo-a"
    assert r2["provider"] == "google" and r2["model"] == "modelo-b"
    # Canario: la seleccion se consulto en CADA invocacion, nunca se cacheo.
    assert repo.selection_calls >= 2, (
        "el resolver tiene que consultar la DB en cada decision"
    )


@pytest.mark.asyncio
async def test_resolver_reusa_la_instancia_del_mismo_modelo():
    """El cooldown de claves (D30) vive en la instancia del provider: dos
    resoluciones del MISMO (provider, model) devuelven el MISMO objeto."""
    repo = FakeModelRepository(ProviderModel("google", "modelo-a"))
    providers: list[ScriptedProvider] = []
    resolver = ProviderResolver(
        repo, provider_factory=_fake_factory(providers),
        metrics=DecisionMetrics(), environ={"GEMINI_API_KEY": "k"},
    )

    a1 = await resolver.resolve()
    a2 = await resolver.resolve()
    repo.selection = ProviderModel("google", "modelo-b")
    b = await resolver.resolve()

    assert a1.provider is a2.provider
    assert a1.provider is not b.provider
    assert len(providers) == 2


@pytest.mark.asyncio
async def test_resolver_cae_al_bootstrap_sin_seleccion_en_db():
    repo = FakeModelRepository(None)
    resolver = ProviderResolver(
        repo, provider_factory=_fake_factory([]),
        metrics=DecisionMetrics(), environ={"GEMINI_API_KEY": "k"},
        bootstrap=ProviderModel("kimi", "kimi-k2.6"),
    )

    resolved = await resolver.resolve()

    assert resolved.provider_name == "kimi"
    assert resolved.model_id == "kimi-k2.6"


@pytest.mark.asyncio
async def test_resolver_sin_seleccion_ni_bootstrap_falla_ruidoso():
    repo = FakeModelRepository(None)
    resolver = ProviderResolver(
        repo, provider_factory=_fake_factory([]),
        metrics=DecisionMetrics(), environ={},
    )

    with pytest.raises(ProviderSelectionError):
        await resolver.resolve()


@pytest.mark.asyncio
async def test_resolver_sin_claves_env_falla_sin_exponer_secretos():
    """El valor de una API key nunca aparece en errores ni en la DB: el
    error menciona el NOMBRE de la env var, no el valor."""
    repo = FakeModelRepository(ProviderModel("google", "modelo-a"))
    secreto = "sk-12345678901234567890"
    resolver = ProviderResolver(
        repo, metrics=DecisionMetrics(),
        environ={},  # sin la clave
    )

    with pytest.raises(ProviderSelectionError) as exc:
        await resolver.resolve()

    assert "GEMINI_API_KEY" in str(exc.value)
    assert secreto not in str(exc.value)


@pytest.mark.asyncio
async def test_pinned_resolver_audita_y_aborta_cualquier_mezcla():
    """BLOQUEANTE 8 (F2-09): el benchmark fija provider/model al inicio y
    rechaza cualquier mezcla dentro de la corrida: si el envelope efectivo
    difiere del pin, aborta."""
    provider = ScriptedProvider("google", "otro-modelo")

    resolver = PinnedResolver(
        provider, "google", "gemini-2.5-flash", enforce_pin=True,
    )
    with pytest.raises(ProviderMixError, match="pin"):
        resolved = await resolver.resolve()
        await resolved.provider.complete(
            "p", deadline=time.monotonic() + 1, turn_id="b:1"
        )


@pytest.mark.asyncio
async def test_pinned_resolver_sin_mezcla_devuelve_el_envelope():
    provider = ScriptedProvider("google", "gemini-2.5-flash")
    resolver = PinnedResolver(
        provider, "google", "gemini-2.5-flash", enforce_pin=True,
    )

    resolved = await resolver.resolve()
    envelope = await resolved.provider.complete(
        "p", deadline=time.monotonic() + 1, turn_id="b:1"
    )

    assert envelope.model == "gemini-2.5-flash"


def test_resolved_provider_es_inmutable():
    from dataclasses import FrozenInstanceError

    resolved = ResolvedProvider("google", "m", object())
    with pytest.raises(FrozenInstanceError):
        resolved.provider = object()  # type: ignore[misc]
