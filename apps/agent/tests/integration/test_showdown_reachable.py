"""D49 (MON-25): counterweight del preflight endurecido.

Contra el Showdown local REAL (sin proveedores, sin DB): el handshake de
protocolo debe pasar. Complementa los canarios de `test_cli.py`, que
prueban que un listener mudo o un endpoint HTTP invalido fallan cerrados.
"""

from __future__ import annotations

import os

import pytest

from ludex_agent.cli import _check_showdown_reachable

_WS_URL = os.environ.get("SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket")

# MON-25 (changes requested, Latwan): el gate anterior llamaba a
# `_check_showdown_reachable` para decidir si saltaba el propio test de
# `_check_showdown_reachable` -- si el chequeo real se rompe, el gate lo
# clasifica como "no disponible" y el SKIP esconde la regresion en vez de
# fallarla. El opt-in de abajo no invoca ninguna funcion del producto: quien
# corre la suite declara a mano que el Showdown local real esta arriba,
# igual que TEST_DATABASE_URL/DATABASE_URL en test_play.py. Si se declara
# arriba y el handshake real falla, el test FALLA, nunca se salta.
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_SHOWDOWN_INTEGRATION"),
    reason="necesita RUN_SHOWDOWN_INTEGRATION=1 + el Showdown local real "
    f"arriba en {_WS_URL} (docker compose --profile local up -d showdown)",
)


async def test_check_showdown_reachable_pasa_contra_showdown_local_real():
    """No debe lanzar: el Showdown pinneado real habla el protocolo
    esperado dentro del timeout."""
    await _check_showdown_reachable(_WS_URL)
