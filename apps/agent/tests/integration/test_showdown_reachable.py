"""D49 (MON-25): counterweight del preflight endurecido.

Contra el Showdown local REAL (sin proveedores, sin DB): el handshake de
protocolo debe pasar. Complementa los canarios de `test_cli.py`, que
prueban que un listener mudo o un endpoint HTTP invalido fallan cerrados.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from ludex_agent.cli import _check_showdown_reachable

_WS_URL = os.environ.get("SHOWDOWN_WS_URL", "ws://localhost:8100/showdown/websocket")


async def _showdown_up(ws_url: str) -> bool:
    try:
        await _check_showdown_reachable(ws_url)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not asyncio.run(_showdown_up(_WS_URL)),
    reason=f"necesita el Showdown local real arriba en {_WS_URL} "
    "(docker compose --profile local up -d showdown)",
)


async def test_check_showdown_reachable_pasa_contra_showdown_local_real():
    """No debe lanzar: el Showdown pinneado real habla el protocolo
    esperado dentro del timeout."""
    await _check_showdown_reachable(_WS_URL)
