"""Carga el .env de la raiz del repo antes de coleccionar tests.

Sin esto los tests que necesitan DATABASE_URL se saltean en silencio: verdes
para quien lo escribio, silenciosamente mas debiles para el resto. `setdefault`
para que una variable ya exportada en el entorno siempre gane.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    # apps/agent/tests/conftest.py -> parents[3] es la raiz del repo
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _expose_disposable_db_helpers() -> None:
    """`tests/db/_disposable.py` (el guardia de base descartable, MON-11) no
    es un paquete -- sin esto, `tests/integration/*.py` no puede reusarlo y
    terminaria reimplementando el mismo guardia dos veces. Se agrega una
    unica vez, al principio de `sys.path`, para todo el suite."""
    db_dir = str(Path(__file__).resolve().parent / "db")
    if db_dir not in sys.path:
        sys.path.insert(0, db_dir)


_load_dotenv()
_expose_disposable_db_helpers()
