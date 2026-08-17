"""Captura del stream crudo de protocolo, agrupado por turno.

El protocolo es la FUENTE DE VERDAD del estado (ver D17): el estado derivado es
una vista materializada que se puede volver a calcular desde aca. Por eso se
guarda tal como llega, incluido el |request|, que trae el equipo propio.

IMPORTANTE: el stream es POR JUGADOR. El |request| de p1 contiene el equipo de
p1 y el de p2 el de p2. Un recorder por jugador, nunca compartido.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import re
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class ProtocolRecorder:
    def __init__(self) -> None:
        self._by_turn: dict[int, list[str]] = defaultdict(list)
        # (turno, linea) en el orden EXACTO de llegada. Ademas de `_by_turn`
        # (para lines_for_turn/turns), esto permite buscar una linea con un
        # cursor global que solo avanza (ver `entries_from`): dos decisiones
        # pueden mencionar la MISMA especie/movimiento (p.ej. Outrage dos
        # turnos seguidos por el bloqueo del movimiento), y sin un cursor que
        # nunca retrocede, la segunda busqueda podria reusar por error la
        # linea que ya le pertenecia a la primera.
        self._entries: list[tuple[int, str]] = []
        self._current_turn = 0

    def record(self, split_messages: list[list[str]]) -> None:
        """Recibe las lineas ya separadas por `|`, tal como las da poke-env."""
        for parts in split_messages:
            line = "|".join(parts)
            # `|turn|N` ABRE el turno N: la linea pertenece al turno nuevo.
            if len(parts) > 2 and parts[1] == "turn":
                try:
                    self._current_turn = int(parts[2])
                except ValueError:
                    pass
            self._by_turn[self._current_turn].append(line)
            self._entries.append((self._current_turn, line))

    def lines_for_turn(self, turn: int) -> list[str]:
        return list(self._by_turn.get(turn, []))

    def turns(self) -> list[int]:
        return sorted(self._by_turn)

    @property
    def all_lines(self) -> list[str]:
        return [line for _, line in self._entries]

    @property
    def line_count(self) -> int:
        """Cuantas lineas de protocolo se grabaron hasta ahora, para este tag.

        Barato de leer (a diferencia de `all_lines`, que copia la lista
        entera).
        """
        return len(self._entries)

    def entries_from(self, index: int) -> list[tuple[int, str]]:
        """Pares (turno, linea) desde una posicion GLOBAL en el orden de
        llegada. Usado por `LudexPlayer` para corregir la etiqueta de turno
        de cada decision con un cursor que solo avanza (ver D20)."""
        return list(self._entries[index:])


# ---------------------------------------------------------------------------
# Identidad de apertura (MON-10/F2-03, D36)
#
# `battle_tag` (`battle-<formato>-<N>`) NO es un identificador global: `N` es
# el contador del server de Showdown, que vive en `logs/lastbattle.txt`
# DENTRO del contenedor sin volumen. Un rebuild lo reinicia en 1 y reusa
# tags viejos para batallas completamente distintas. La identidad persistida
# tiene que salir de lo que el servidor narro, no de ese contador.
#
# Fuente: bloque de apertura PUBLICO del turno 0 (las lineas que ambos lados
# reciben). La unica asimetria real entre p1 y p2 en ese bloque es el HP del
# `|switch|` inicial -- Showdown manda el valor EXACTO al dueno del mon y el
# PORCENTUAL al rival (`getHealth.secret` vs `.shared`) -- y al arrancar la
# batalla las dos formas representan siempre el 100%, asi que normalizarlas
# al mismo sentinel es lo que le da paridad a la clave entre los dos lados.
# ---------------------------------------------------------------------------

OPENING_IDENTITY_VERSION = "ps-open-v1"

# Etiquetas allowlisted del bloque de apertura (DESIGN VERDICT #2). Cualquier
# otra linea (`>tag`, `|init|`, `|title|`, `|j|`, `|request|` privado, blancos)
# se descarta sola por no figurar aca: no hace falta enumerarla.
_OPENING_LABELS = frozenset({
    "t:", "gametype", "gen", "tier", "rule", "teamsize", "player", "start", "switch",
})

# Roles y activos por lado, UNO por gametype soportado (DESIGN VERDICT #3: la
# completitud se parametriza por jugadores/gametype, nunca se fija "Singles").
# Confirmado leyendo el simulador VENDORIZADO (LINEAR_VERDICT L-02, re-review):
# `pokemon-showdown@0.11.10` (la version pineada, D4) es explicito en
# `sim/side.ts` sobre que roles/activos admite cada gametype, y en
# `sim/pokemon.ts:504-507` (`Pokemon.getSlot`) sobre como se arma la letra:
#
#   positionOffset = floor(side.n / 2) * side.active.length
#   letra = 'abcdef'[posicion_del_mon + positionOffset]
#
# `side.n` es el indice 0-based del rol (p1=0 .. p4=3). Para singles/doubles/
# triples solo existen p1/p2 (n=0,1): floor(n/2) siempre da 0, asi que cada
# rol usa sus propias letras 'a'.."slots" sin pisar al otro. Para 'multi'
# (4 roles, 1 activo por lado) el offset SI depende del rol: p1/p2 caen en
# 'a' pero p3/p4 caen en 'b' -- p1a, p2a, p3b, p4b. No es "cada rol usa las
# mismas letras que los demas": _expected_switch_keys reproduce la formula
# tal cual, no una aproximacion por gametype.
_GAMETYPES: dict[str, tuple[tuple[str, ...], int]] = {
    "singles": (("p1", "p2"), 1),
    "doubles": (("p1", "p2"), 2),
    "triples": (("p1", "p2"), 3),
    "multi": (("p1", "p2", "p3", "p4"), 1),
}

_SLOT_LETTERS = "abcdef"


def _expected_switch_keys(required_roles: tuple[str, ...], slots_per_side: int) -> frozenset[tuple[str, str]]:
    """Espeja `Pokemon.getSlot()` (`sim/pokemon.ts:504-507`, ver comentario
    de `_GAMETYPES`), no una aproximacion: la letra depende del INDICE del
    rol y de cuantos activos tiene su lado, no solo de su propio rol."""
    keys: set[tuple[str, str]] = set()
    for role in required_roles:
        n = int(role[1:]) - 1
        offset = (n // 2) * slots_per_side
        for i in range(slots_per_side):
            keys.add((role, _SLOT_LETTERS[offset + i]))
    return frozenset(keys)

_TAG_DOMAIN_RE = re.compile(r"-\d+$")
_SWITCH_HP_RE = re.compile(r"^(\d+)/(\d+)$")


class OpeningIdentityError(RuntimeError):
    """La apertura publica no alcanza, o no cierra, para calcular una
    identidad segura. Fallo CERRADO a proposito: mejor no persistir nada que
    persistir con una clave que no representa de verdad a esta partida."""


def _opening_label(line: str) -> str | None:
    if not line.startswith("|"):
        return None
    parts = line.split("|")
    return parts[1] if len(parts) > 1 and parts[1] else None


def _normalize_switch_line(line: str) -> str:
    """Reemplaza el token de HP del switch inicial por el sentinel `FULL`.

    Falla cerrado si el switch NO esta al 100%: en el turno de apertura eso
    es siempre cierto por regla del juego, asi que lo contrario significa que
    esta funcion no entiende esta apertura, y forzar un sentinel de todos
    modos iria contra D17 (fallar antes que inventar).
    """
    parts = line.split("|")
    if len(parts) != 5:
        raise OpeningIdentityError(f"switch inicial con forma inesperada: {line!r}")
    match = _SWITCH_HP_RE.match(parts[4].strip())
    if match is None:
        raise OpeningIdentityError(f"switch inicial con HP no parseable: {line!r}")
    current, total = int(match.group(1)), int(match.group(2))
    if total == 0 or current != total:
        raise OpeningIdentityError(f"switch inicial no esta al 100%: {line!r}")
    parts[4] = "FULL"
    return "|".join(parts)


def _tag_domain(battle_tag: str) -> str:
    """El dominio del tag SIN el contador del servidor: es justo el contador
    el que se reutiliza tras un restart, asi que no puede formar parte de una
    identidad pensada para sobrevivirlo."""
    return _TAG_DOMAIN_RE.sub("", battle_tag.strip().lower())


def _gametype_of(by_label: dict[str, list[str]]) -> str:
    lines = by_label.get("gametype", [])
    if len(lines) != 1:
        raise OpeningIdentityError(
            f"apertura incompleta: se esperaba exactamente una linea 'gametype', "
            f"se vieron {len(lines)}"
        )
    parts = lines[0].split("|")
    if len(parts) < 3 or not parts[2]:
        raise OpeningIdentityError(f"linea 'gametype' sin valor: {lines[0]!r}")
    return parts[2].strip().lower()


_ROLE_RE = re.compile(r"^p[1-4]$")
_SWITCH_IDENT_RE = re.compile(r"^(p[1-4])([a-z])$")


def _player_roles(by_label: dict[str, list[str]], required_roles: tuple[str, ...]) -> frozenset[str]:
    """L-02 (re-review): no alcanza con "al menos 2 roles distintos" -- eso
    aceptaba singles con p1+p3, o multi con solo p1+p2. El CONJUNTO de roles
    declarados tiene que ser exactamente el que el gametype exige, ni un rol
    de mas ni de menos, y cada rol solo un valido `p1`..`p4`."""
    roles: list[str] = []
    for line in by_label.get("player", []):
        parts = line.split("|")
        role = parts[2] if len(parts) > 2 else ""
        if not _ROLE_RE.match(role):
            raise OpeningIdentityError(f"linea 'player' con rol invalido: {line!r}")
        roles.append(role)
    if len(set(roles)) != len(roles):
        raise OpeningIdentityError(
            f"apertura incompleta: roles 'player' repetidos en vez de un lado real: {roles}"
        )
    if set(roles) != set(required_roles):
        raise OpeningIdentityError(
            f"apertura incompleta: se esperaban exactamente los roles {sorted(required_roles)} "
            f"para este gametype; se vio {sorted(set(roles))}"
        )
    return frozenset(roles)


def _validate_teamsize(by_label: dict[str, list[str]], player_roles: frozenset[str]) -> None:
    """Exactamente un `teamsize` por rol declarado en `player` -- ni de mas
    (rol duplicado) ni de menos (un lado sin su teamsize)."""
    roles: list[str] = []
    for line in by_label.get("teamsize", []):
        parts = line.split("|")
        role = parts[2] if len(parts) > 2 else ""
        if not _ROLE_RE.match(role):
            raise OpeningIdentityError(f"linea 'teamsize' con rol invalido: {line!r}")
        roles.append(role)
    if len(set(roles)) != len(roles):
        raise OpeningIdentityError(f"apertura incompleta: roles 'teamsize' repetidos: {roles}")
    if set(roles) != player_roles:
        raise OpeningIdentityError(
            f"apertura incompleta: 'teamsize' no cubre exactamente los roles de 'player' "
            f"({sorted(player_roles)}); se vio {sorted(set(roles))}"
        )


def _validate_switches(
    by_label: dict[str, list[str]], player_roles: frozenset[str], slots: int,
) -> None:
    """L-02: cada rol necesita sus activos iniciales exactos segun
    `_expected_switch_keys` (que refleja `Pokemon.getSlot()` real, no una
    aproximacion), cada uno UNA sola vez. Un slot duplicado (p.ej. dos
    `p1a`) no puede sustituir al slot de un lado ausente (p.ej. `p2a`): se
    valida el CONJUNTO de (rol, slot), no la cantidad de lineas."""
    keys: list[tuple[str, str]] = []
    for line in by_label.get("switch", []):
        parts = line.split("|")
        ident = parts[2].split(":", 1)[0].strip() if len(parts) > 2 else ""
        match = _SWITCH_IDENT_RE.match(ident)
        if match is None:
            raise OpeningIdentityError(f"switch inicial con ident invalido: {line!r}")
        keys.append((match.group(1), match.group(2)))
    if len(set(keys)) != len(keys):
        raise OpeningIdentityError(
            f"apertura incompleta: slots 'switch' duplicados en vez de cubrir todos "
            f"los lados: {keys}"
        )
    expected = _expected_switch_keys(tuple(sorted(player_roles)), slots)
    if set(keys) != expected:
        raise OpeningIdentityError(
            f"apertura incompleta: los switches iniciales no cubren exactamente "
            f"{sorted(expected)}; se vio {sorted(set(keys))}"
        )


def compute_opening_identity(battle_tag: str, opening_lines: Sequence[str]) -> str:
    """`ps-open-v1:sha256:<64hex>` — la identidad persistida de una batalla.

    El payload es el `battle_tag` normalizado como dominio (sin el contador)
    mas las lineas allowlisted del bloque de apertura, normalizadas linea por
    linea, ORDENADAS canonicamente y sin deduplicar (dos rules identicas
    cuentan dos veces). Nunca se compara protocolo concatenado ni por
    substring: cada linea es un elemento propio antes de unirse con '\\n'.
    """
    by_label: dict[str, list[str]] = defaultdict(list)
    normalized: list[str] = []
    for raw in opening_lines:
        label = _opening_label(raw)
        if label not in _OPENING_LABELS:
            continue
        line = raw.strip()
        if label == "switch":
            line = _normalize_switch_line(line)
        by_label[label].append(line)
        normalized.append(line)

    # `t:` NO aparece una sola vez: medido contra batallas reales del corpus
    # (battle-gen6randombattle-257, -271, -386), Showdown manda un `|t:|` con
    # el intercambio inicial de info y OTRO justo antes de `|start|`. Exigir
    # "exactamente 1" hacia fallar cerrado el 100% de las aperturas reales.
    # `start` si es siempre exactamente 1 en la misma muestra.
    for required in ("t:", "gen", "tier", "start"):
        if len(by_label.get(required, [])) < 1:
            raise OpeningIdentityError(f"apertura incompleta: falta '{required}'")
    if len(by_label["start"]) != 1:
        raise OpeningIdentityError(
            "apertura incompleta: 'start' debe aparecer exactamente una vez"
        )

    gametype = _gametype_of(by_label)
    supported = _GAMETYPES.get(gametype)
    if supported is None:
        raise OpeningIdentityError(
            f"gametype desconocido o no soportado, no se puede validar completitud: {gametype!r}"
        )
    required_roles, slots = supported

    # L-02: la completitud es ESTRUCTURAL (roles y slots distintos y
    # completos segun EXACTAMENTE lo que el gametype exige), no una cuenta de
    # lineas -- un conteo se cumple con lineas de un solo lado duplicadas, sin
    # que el otro lado exista, o con roles que no corresponden al gametype.
    player_roles = _player_roles(by_label, required_roles)
    _validate_teamsize(by_label, player_roles)
    _validate_switches(by_label, player_roles, slots)

    payload = "\n".join([f"domain:{_tag_domain(battle_tag)}", *sorted(normalized)])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{OPENING_IDENTITY_VERSION}:sha256:{digest}"


# ---------------------------------------------------------------------------
# Camino pre-lock (D31, MON-6)
#
# Medido con una sonda causal contra Showdown local (ver
# docs/superpowers/specs/2026-07-29-f2-01-prelock-snapshot-design.md):
#
#   - Showdown emite el `|request|` privado en su PROPIO frame y ANTES de
#     narrar publicamente el turno que ese request ya resolvio.
#   - Esa narracion, `NARR(k)`, llega al socket 0.022-5.557 ms despues del
#     request y NO depende de nuestra respuesta: demorar la eleccion 500 ms
#     no la mueve (76/76 decisiones). La que SI depende es `NARR(k+1)`.
#   - Pero la task que la procesaria queda esperando el mismo lock por
#     batalla que la decision mantiene abierto, asi que `Battle` no la
#     aplica hasta despues de que elegimos.
#
# De ahi el inbox: se publica el frame CRUDO antes del lock, y la decision
# espera esa señal en vez de esperar a `Battle` o al recorder (prohibido:
# ambos avanzan solo dentro del lock).
# ---------------------------------------------------------------------------


class ProjectionFailure(RuntimeError):
    """Base comun: la proyeccion no puede completarse de forma segura para
    esta decision. `client.py` cierra las dos subclases con el mismo
    mecanismo (paso reservado descartado, sin invocar al proveedor)."""


class ProjectionTimeoutError(ProjectionFailure):
    """La narracion previa no llego dentro del presupuesto.

    Es fallo CERRADO a proposito: no se decide con estado stale ni se
    persiste una fila degradada. Si una fila existe, su proyeccion es
    valida por construccion.
    """


class ProjectionAmbiguityError(ProjectionFailure):
    """No se puede resolver, sin ambiguedad, quien es el dueño real de un
    dato que hay que copiar del lado PROPIO (Transform, Reflect Type,
    `-copyboost`).

    Fallo CERRADO a proposito: la alternativa era sustituir silenciosamente
    por "el activo actual" del snapshot -- que ya es post-resolucion y puede
    ser un pokemon que entro DESPUES del evento que se esta proyectando. Es
    exactamente el bug medido en `battle-gen6randombattle-1929`: un
    `-transform` que copiaba a Spinda terminaba copiando a Tentacruel,
    porque Tentacruel ya era el activo del snapshot por un switch posterior
    en la MISMA narracion.
    """


# Un frame completa la espera SOLO si trae al menos una de estas etiquetas.
# Es lista BLANCA, no lista negra: chat (`c`/`c:`), `inactive`, `j`/`l`/`n`,
# `t:` solo, `request`, `error`, `popup`, `raw`, `html` y `init` no pueden
# completarla ni entrar al prompt.
#
# `turn` NO es obligatorio y `upkeep`/`faint` estan incluidos a proposito: el
# frame de un cambio forzado cierra en el `|faint|` y nunca trae `|turn|`
# (medido: 7/76 decisiones con forceSwitch, todas con turn=None). Una regla
# que exigiera `|turn|` colgaria en cada cambio forzado.
RESOLUTION_TAGS = frozenset({
    "move", "switch", "drag", "replace", "detailschange", "-formechange",
    "faint", "cant", "-damage", "-heal", "-sethp", "-status", "-curestatus",
    "-boost", "-unboost", "-setboost", "-clearboost", "-clearallboost",
    "-clearnegativeboost", "-clearpositiveboost", "-copyboost",
    "-invertboost", "-swapboost",
    "-item", "-enditem", "-ability", "-endability",
    "-weather", "-fieldstart", "-fieldend", "-sidestart", "-sideend",
    "-activate", "-start", "-end", "-transform", "upkeep", "turn",
    "win", "tie",
})

_BOOST_KEYS = ("accuracy", "atk", "def", "evasion", "spa", "spd", "spe")


def normalize_id(text: str) -> str:
    """Mismo criterio que `to_id_str` de poke-env: se saca TODA la puntuacion.

    No alcanza con espacios y guiones: "Mr. Mime" lleva punto y "Farfetch'd"
    apostrofo.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _line_tag(line: str) -> str:
    parts = line.split("|")
    return parts[1] if len(parts) > 1 else ""


def is_resolution_frame(lines: tuple[str, ...]) -> bool:
    """¿Este frame narra la resolucion de un turno?"""
    return any(_line_tag(line) in RESOLUTION_TAGS for line in lines)


@dataclass(frozen=True)
class RawFrame:
    seq: int
    recv_ns: int
    lines: tuple[str, ...]


# `seq` del frame que esta procesando ESTA task. Lo publica el observador
# pre-lock y lo lee `choose_move` como cursor.
#
# Tiene que ser un ContextVar y no `inbox.last_seq(tag)`: poke-env crea una
# task por frame y todas publican apenas arrancan, pero solo una entra al
# lock por vez. Bajo carga, para cuando la decision del frame N llega a
# `choose_move`, los frames N+1, N+2... ya estan publicados, y `last_seq`
# devolveria uno de ELLOS — con lo que la decision esperaria una narracion
# posterior a la suya, o no encontraria ninguna. Cada task copia su propio
# contexto, asi que el ContextVar da el frame correcto sin importar cuantos
# se hayan encolado detras.
CURRENT_FRAME_SEQ: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "ludex_current_frame_seq", default=None
)


# Cuantos frames se retienen por batalla EN VUELO. Una decision mira unos
# pocos frames atras: desde el watermark de la proyeccion anterior hasta el
# primer frame de resolucion posterior a su request (MON-26 amplio la ventana
# de uno a "los que haya entre watermark y cierre", tipicamente 1-2), asi que
# 128 son dos ordenes de magnitud de margen; sin tope, una corrida de miles
# de batallas acumula cada frame de cada una para siempre. Desalojar nunca
# puede devolver una respuesta equivocada: si se desalojo cualquier frame
# posterior al watermark, `wait_for_resolution` falla CERRADO (ver ahi).
MAX_RETAINED_FRAMES = 128


class RawFrameInbox:
    """Ventana movil de frames crudos por `battle_tag`.

    Deliberadamente NO es el `ProtocolRecorder`: el recorder se graba dentro
    de `_handle_battle_message`, o sea bajo el lock por batalla, y esperarlo
    desde una decision que TIENE ese lock es un punto muerto. El inbox se
    publica antes del lock, que es lo unico que se puede esperar.

    Retencion acotada por los dos extremos: `MAX_RETAINED_FRAMES` durante la
    batalla y `close()` al terminarla. Lo unico que sobrevive a una batalla
    cerrada es su tag en `_closed` y su `Condition`, para que un waiter tardio
    falle cerrado en vez de colgarse; `forget()` limpia tambien eso.
    """

    def __init__(self, *, max_frames: int = MAX_RETAINED_FRAMES) -> None:
        self._max_frames = max_frames
        self._frames: dict[str, deque[RawFrame]] = defaultdict(
            lambda: deque(maxlen=max_frames)
        )
        self._conds: dict[str, asyncio.Condition] = {}
        self._closed: set[str] = set()
        # Mayor `seq` DESALOJADO por tag. Es por tag y no global porque `_seq`
        # es global (una task por frame, todas las batallas comparten el
        # contador), asi que los seq de un mismo tag no son contiguos y no se
        # puede razonar con `after_seq + 1`.
        self._evicted: dict[str, int] = {}
        self._seq = 0

    def _cond(self, tag: str) -> asyncio.Condition:
        cond = self._conds.get(tag)
        if cond is None:
            cond = asyncio.Condition()
            self._conds[tag] = cond
        return cond

    async def publish(self, tag: str, lines: tuple[str, ...]) -> RawFrame:
        self._seq += 1
        frame = RawFrame(self._seq, time.monotonic_ns(), tuple(lines))
        cond = self._cond(tag)
        async with cond:
            frames = self._frames[tag]
            if len(frames) == self._max_frames:
                self._evicted[tag] = frames[0].seq
            frames.append(frame)
            cond.notify_all()
        return frame

    def last_seq(self, tag: str) -> int:
        frames = self._frames.get(tag)
        return frames[-1].seq if frames else 0

    def retained(self, tag: str) -> int:
        """Cuantos frames hay retenidos hoy para ese tag (para los tests y
        para poder verificar el tope sin mirar atributos privados)."""
        return len(self._frames.get(tag, ()))

    async def close(self, tag: str) -> None:
        """La batalla termino: despierta a cualquiera que siga esperando y
        libera sus frames.

        Los frames se sueltan aca y no en `forget()` porque despues de un
        `|win|`/`|tie|` no hay decision nueva posible en ese tag: cualquier
        waiter que quede tiene que fallar cerrado igual, con o sin frames.
        """
        cond = self._cond(tag)
        async with cond:
            self._closed.add(tag)
            self._frames.pop(tag, None)
            self._evicted.pop(tag, None)
            cond.notify_all()

    def forget(self, tag: str) -> None:
        self._frames.pop(tag, None)
        self._conds.pop(tag, None)
        self._closed.discard(tag)
        self._evicted.pop(tag, None)

    def _first_resolution_after(self, tag: str, after_seq: int) -> RawFrame | None:
        for frame in self._frames.get(tag, ()):
            if frame.seq > after_seq and is_resolution_frame(frame.lines):
                return frame
        return None

    def _lost_frames_after(self, tag: str, after_seq: int) -> bool:
        """¿Se desalojo algun frame posterior al cursor?

        Solo se desaloja por el frente, asi que si el mayor `seq` desalojado
        supera al cursor, todo lo que hubiera entre medio ya no esta y el
        primer frame de resolucion retenido NO es demostrablemente el de esta
        decision.
        """
        return self._evicted.get(tag, 0) > after_seq

    async def wait_for_resolution(
        self,
        tag: str,
        *,
        after_seq: int,
        until_seq: int,
        timeout: float,
    ) -> list[RawFrame]:
        """Ventana de frames de resolucion de una decision (MON-26).

        `after_seq` (watermark) es el seq del ultimo frame entregado a la
        proyeccion ANTERIOR de este tag; `until_seq` es el seq del frame del
        request PROPIO de esta decision. Devuelve, en orden, todos los frames
        de resolucion con `after_seq < seq <= closing.seq`, donde `closing`
        es el PRIMER frame de resolucion con `seq > until_seq`.

        La API anterior devolvia solo `closing`. Eso asumia "[request] seguido
        de [narracion]" y dejaba HUERFANA cualquier narracion que un request
        `wait:true` interpusiera antes del request activo: medido en
        `battle-gen6randombattle-67`, turno 2 -- el frame con `-enditem`
        llego entre el request de espera y el request activo, ninguna decision
        lo proyecto, y la memoria de item (D40) reafirmo el item consumido
        durante el resto de la batalla (21 violaciones de hidden_information).
        La ventana cierra ese gap: sin request interpuesto devuelve
        exactamente el mismo frame unico de siempre.

        El watermark es POR TAG (el seq es global; ver el docstring de
        `publish`): dos batallas concurrentes no pueden pisarse la ventana.
        Falla cerrado si se desalojo cualquier frame posterior al watermark
        (la ventana no seria demostrablemente la de esta decision), y espera
        hasta que `closing` exista o la batalla cierre.
        """
        cond = self._cond(tag)
        try:
            async with asyncio.timeout(timeout):
                async with cond:
                    await cond.wait_for(
                        lambda: (
                            self._lost_frames_after(tag, after_seq)
                            or self._first_resolution_after(tag, until_seq) is not None
                            or tag in self._closed
                        )
                    )
        except TimeoutError as exc:
            raise ProjectionTimeoutError(
                f"la narracion previa de {tag} no llego en {timeout}s "
                f"(watermark={after_seq}, request={until_seq}): no se decide "
                "con estado stale"
            ) from exc
        if self._lost_frames_after(tag, after_seq):
            raise ProjectionTimeoutError(
                f"{tag} desalojo frames posteriores a watermark={after_seq} "
                f"(tope {self._max_frames}): no se puede demostrar cual es la "
                "narracion de esta decision"
            )
        closing = self._first_resolution_after(tag, until_seq)
        if closing is None:
            raise ProjectionTimeoutError(
                f"{tag} cerro sin narracion de resolucion despues de "
                f"request={until_seq}"
            )
        return [
            frame
            for frame in self._frames.get(tag, ())
            if after_seq < frame.seq <= closing.seq
            and is_resolution_frame(frame.lines)
        ]


class ObservableVocabulary(Protocol):
    """Traduce nombres del protocolo a la representacion de poke-env.

    Esta INYECTADA para que este modulo no importe poke-env: el proyector se
    testea sin levantar nada. Y porque las inferencias legitimas tienen que
    quedar ancladas al dex o a los enums de la libreria, nunca a una lista a
    mano (ver .claude/agent-recording/SKILL.md).
    """

    def species_types(self, species_id: str) -> list[str]: ...
    def base_species(self, species_id: str) -> str: ...
    def unique_ability(self, species_id: str) -> str | None: ...
    def forme_change_ability(self, species_id: str) -> str | None: ...
    def move_max_pp(self, move_id: str) -> int | None: ...
    def type_name(self, raw: str) -> str | None: ...
    def weather_name(self, raw: str) -> str | None: ...
    def field_name(self, raw: str) -> str | None: ...
    def side_condition_name(self, raw: str) -> str | None: ...
    def side_condition_is_stackable(self, raw: str) -> bool: ...


def _parse_hp(field: str) -> tuple[float | None, str | None, bool]:
    """`76/100`, `0 fnt`, `76/100 brn` -> (fraccion, status, fainted)."""
    field = field.strip()
    if not field:
        return None, None, False
    chunks = field.split(" ")
    hp_part = chunks[0]
    status = chunks[1].upper() if len(chunks) > 1 and chunks[1] else None
    if "/" in hp_part:
        cur_s, max_s = hp_part.split("/", 1)
        try:
            cur, mx = int(cur_s), int(max_s)
        except ValueError:
            return None, status, status == "FNT"
        if mx == 0:
            return None, status, status == "FNT"
        return round(cur / mx, 4), status, cur == 0
    if hp_part == "0":
        return 0.0, status, True
    return None, status, status == "FNT"


def _blank_boosts() -> dict[str, int]:
    return {key: 0 for key in _BOOST_KEYS}


def _dex_ability(
    species: str, current: str | None, vocabulary: ObservableVocabulary
) -> str | None:
    """La ability que reporta poke-env para esa especie, sin lista a mano.

    Replica `Pokemon.ability` (`pokemon.py:861-871`) sobre lo que escribe
    `_update_from_pokedex` (`pokemon.py:650-661`):

      - si la forma es Mega/Primal, gana su `abilities["0"]`, que la property
        prefiere por `forme_change_ability`;
      - si no, lo ya revelado por una linea publica;
      - si tampoco, y el dex lista EXACTAMENTE una ability posible (gen >= 3),
        esa ability es conocimiento publico: Zoroark solo puede tener Illusion,
        Weezing solo Levitate. Camerupt tiene tres y queda en None.
    """
    forme = vocabulary.forme_change_ability(species)
    if forme is not None:
        return forme
    if current is not None:
        return current
    return vocabulary.unique_ability(species)


def _new_mon(species: str, vocabulary: ObservableVocabulary) -> dict[str, Any]:
    """Un rival recien revelado.

    `item` y `moves` quedan desconocidos a proposito: el protocolo no los trae
    y completarlos seria inventar informacion que el jugador no tiene. Los
    tipos y la ability salen del dex de la generacion (ver `_dex_ability`), que
    es una inferencia legitima y anclada, no una lista a mano.
    """
    return {
        "species": species,
        "hp_fraction": 1.0,
        "active": False,
        "fainted": False,
        "status": None,
        "level": None,
        "item": "unknown_item",
        "ability": _dex_ability(species, None, vocabulary),
        "types": vocabulary.species_types(species),
        "boosts": _blank_boosts(),
        "moves": [],
    }


# Movimientos que llaman a OTRO movimiento que no es del actor: el eco no es
# evidencia de pertenencia. Es la misma lista que codifica poke-env en
# `abstract_battle.py:625-633`, citada de ahi y no de datos de juego a mano.
_CALLERS_NOT_OWNED = frozenset({"Copycat", "Metronome", "Nature Power", "Round"})
# Los Pledge combinan y poke-env descarta el override por completo.
_PLEDGES = frozenset({"Grass Pledge", "Water Pledge", "Fire Pledge"})
# Sufijos que poke-env limpia sin cambiar la atribucion del movimiento.
_MOVE_NOISE = frozenset({
    "[miss]", "[still]", "[notarget]", "[zeffect]", "null", "",
    "[from] Pursuit", "[from]Pursuit",
})
# Sufijos que significan "este movimiento no se ejecuto ni pertenece al actor".
_MOVE_NOT_OWNED = frozenset({
    "[from] lockedmove", "[from]lockedmove", "[from] Sky Attack",
    "[from] Magic Coat", "[from] Mirror Move",
})

# `-item` con estos sufijos narra una TRANSFERENCIA, no una revelacion simple:
# el `ident` (parts[2]) es quien RECIBE el item, y `[of]` nombra a quien lo
# PIERDE -- Showdown nunca manda una linea separada para el que pierde (D40,
# T-01). Thief/Covet son movimientos; Pickpocket/Magician son abilities.
_ITEM_TRANSFER_MOVES = frozenset({"Thief", "Covet"})
_ITEM_TRANSFER_ABILITIES = frozenset({"Pickpocket", "Magician"})

# Sentinel: "la clave 'item' de `persistent_state` estaba AUSENTE" antes de
# la primera mutacion de la estancia activa (D40, T-02) -- distinto de que
# estuviera PRESENTE con `None`. Un objeto propio, nunca `None` ni un string,
# para que no colisione con ningun valor real de item.
_NO_PRIOR_ITEM = object()


def project_observable_state(
    snapshot: dict,
    lines: tuple[str, ...],
    *,
    opponent_side: str,
    vocabulary: ObservableVocabulary,
    persistent_state: dict[str, dict] | None = None,
) -> dict:
    """Aplica al snapshot inmutable la evidencia PUBLICA de un frame crudo.

    Devuelve un dict NUEVO; no muta la entrada.

    Nunca lee una linea `|request|`: el request es privado, y el del rival no
    existe. Nunca escribe `legal_actions`, `me` ni `player_role`: eso viene
    del request propio, ya esta fresco, y tocarlo introduciria acciones fuera
    de la mascara capturada.

    Singles: en `p2a:` el sufijo `a` ES la ranura activa, asi que las lineas
    de HP/status/boost se aplican al activo proyectado sin depender del
    apodo (que puede no ser la especie).

    `persistent_state` es la UNICA memoria que sobrevive entre llamadas
    (por eso es un parametro explicito, mutado in-place, y no un cache
    interno oculto): tipos/ability/moves persistentes que una forma o un
    Transform tapan temporalmente pueden quedar tapados en UNA decision y
    recien restaurarse en otra. `switch_out` (`pokemon.py:600-612`) en
    poke-env NUNCA resetea `_type_1`/`_type_2`; solo limpia
    `_temporary_types`/`temporary_ability`/`_transform_moves`. Sin esta
    memoria, la version anterior confundia "tapado temporalmente" con
    "hay que recalcular del dex", y perdia los tipos de una Mega o la
    ability/moveset propios de un Transform tan pronto la decision que los
    aplico terminaba. El caller (`client.py`) le pasa un dict por
    `battle_tag`, vivo mientras dura la batalla.

    `unknown_pp_moves`/`transform_unknown_pp_moves` (D37) son la misma idea
    aplicada al PP bajo Pressure: `register_move` marca ahi el `move_id`
    cuando pone `pp=None`, y esta funcion lo reaplica al principio de CADA
    llamada, porque el `snapshot` de entrada es fresco (el PP que cuenta
    poke-env, ciego a Pressure) y sin la marca ese numero pisaria el `None`
    sin que ninguna linea nueva lo pidiera. `unknown_pp_moves` sobrevive
    cualquier cantidad de switches (como `ability`); `transform_unknown_pp_
    moves` es tan temporal como el moveset copiado y se descarta junto con
    el en `switch_out`, para no filtrarse a un Transform distinto.

    Las dos marcas son MUTUAMENTE EXCLUYENTES en la reaplicacion, nunca se
    unen (LINEAR_VERDICT MON-18 R1, L-01): mientras `"moves" in entry`
    indique un Transform activo, el moveset VISIBLE es el copiado, no el
    base, asi que solo `transform_unknown_pp_moves` puede gobernarlo -- una
    marca permanente del base nombra un `move_id` que puede coincidir por
    casualidad con el copiado, pero son instancias de PP distintas. Sin
    Transform activo, solo `unknown_pp_moves` gobierna.

    `item` (D40) es la misma idea aplicada al item del rival: poke-env
    corrompe `battle.opponent_team[...].item` entre una decision y la
    siguiente tras un intercambio por Trick (confirmado en vivo, ver
    ROOT-CAUSE CHECKPOINT R3), y sin memoria propia ese valor corrupto pisa
    evidencia ya establecida. A diferencia de `types`/`moves`, no tiene
    version temporal: Transform no copia item, asi que la marca es siempre
    permanente para la identidad del propio tenedor. `value=None` (item
    consumido/removido) es tan significativo como cualquier item real -- la
    clave `"item"` queda presente con ese valor, nunca ausente.

    `canonical_types` (D41) es la misma idea aplicada a los tipos BASE del
    rival: `Pokemon._update_from_details` de poke-env corta en seco si
    `details` no cambio desde la ultima vez, asi que tras un `-formechange`
    TEMPORAL (Relic Song) su propio snapshot puede seguir stale en el
    PROXIMO switch-in con el mismo `details` base -- `switch_in()` ya
    recalcula sin condicion en la llamada donde el switch ocurre, pero esa
    correccion no sobrevive sola a la llamada SIGUIENTE si nada vuelve a
    nombrar a esa identidad (ROOT-CAUSE CHECKPOINT, MON-19). A diferencia de
    `item`, `types` YA tenia una clave `"types"` en `persistent_state`
    -- el backup de un override TEMPORAL activo (typechange, Transform, y
    ahora tambien `-formechange`). `canonical_types` es una clave DISTINTA
    y estrictamente permanente (`switch_in`, `detailschange`/Mega); las dos
    son mutuamente excluyentes en la reaplicacion: mientras `"types"` este
    presente, el override manda y `canonical_types` no se toca.
    """
    persistent_state = {} if persistent_state is None else persistent_state
    projected = {
        **snapshot,
        "opponent": {
            "pokemon": [
                {**mon, "boosts": dict(mon.get("boosts") or {}),
                 "moves": [dict(m) for m in (mon.get("moves") or [])]}
                for mon in (snapshot.get("opponent") or {}).get("pokemon", [])
            ]
        },
        "field": {
            key: dict(value)
            for key, value in (snapshot.get("field") or {}).items()
        },
    }
    team: list[dict] = projected["opponent"]["pokemon"]
    active_prefix = f"{opponent_side}a:"
    side_prefix = f"{opponent_side}:"

    def active() -> dict | None:
        for mon in team:
            if mon.get("active"):
                return mon
        return None

    def own_active() -> dict | None:
        """El activo AHORA de nuestro lado, leido del snapshot propio.

        Uso restringido: solo para lo que de verdad depende de "quien esta
        en cancha en este instante" (Pressure sobre un movimiento rival que
        se resuelve ahora). NUNCA para copiar de un pokemon nombrado por un
        evento pasado (Transform, Reflect Type, `-copyboost`): para eso esta
        `own_mon_named`, mas abajo, precisamente porque el activo AHORA
        puede no ser el mismo que estaba en cancha cuando el evento paso.
        """
        for mon in (snapshot.get("me") or {}).get("pokemon", []):
            if mon.get("active"):
                return mon
        return None

    def own_mon_named(ident: str) -> dict:
        """El miembro de NUESTRO equipo que NOMBRA este ident de evento --
        nunca "el que esta activo ahora" en el snapshot.

        `snapshot["me"]` viene fresco del `|request|` propio y ya post-
        resolucion de TODO el turno: si el evento que se esta proyectando
        (Transform, Reflect Type, `-copyboost`) nombra a un pokemon que
        DESPUES salio del campo dentro de la MISMA narracion, `active()`
        apunta al que entro despues, no al nombrado. Medido en
        `battle-gen6randombattle-1929`: `|-transform|p2a: Ditto|p1a:
        Spinda|[from] ability: Imposter` seguido, en el mismo NARR, por
        `|switch|p1a: Tentacruel|...` -- el snapshot ya tenia a Tentacruel
        activo, y Ditto terminaba copiando a Tentacruel en vez de a Spinda.

        La correspondencia es por identidad canonica (`base_species`), igual
        que D22 usa para reconocer al actor pese a un nickname o una forma:
        `snapshot["me"]["pokemon"]` trae el equipo COMPLETO (poke-env conoce
        los seis desde el team preview y el propio `|request|`), asi que el
        nombrado casi siempre esta ahi sin importar cual este activo ahora.

        Fallo CERRADO si no hay match: `ProjectionAmbiguityError`, nunca un
        `own_active()` "por las dudas" -- eso seria repetir el mismo bug.
        """
        nombre = ident.split(":", 1)[-1].strip() if ":" in ident else ""
        objetivo = vocabulary.base_species(normalize_id(nombre)) if nombre else None
        if objetivo:
            for mon in (snapshot.get("me") or {}).get("pokemon", []):
                especie = normalize_id(mon.get("species") or "")
                if vocabulary.base_species(especie) == objetivo:
                    return mon
        raise ProjectionAmbiguityError(
            f"no se pudo resolver el ident propio {ident!r} contra ningun "
            "miembro conocido del equipo: fallar cerrado antes que copiar "
            "del activo equivocado"
        )

    def mon_for_ident(ident: str) -> dict | None:
        """Singles: el sufijo `a` ES la ranura activa de ese lado para el
        RIVAL (puede no haber ninguno activo todavia; se devuelve `None` y
        el llamador se abstiene, igual que antes). Del lado propio, resuelve
        por nombre (`own_mon_named`), nunca por "quien esta activo ahora" --
        ver su docstring -- y ese camino falla CERRADO en vez de devolver
        `None`."""
        if ident.startswith(active_prefix):
            return active()
        return own_mon_named(ident)

    def canon(species: str) -> str:
        """Identidad canonica de un miembro del equipo: su `base_species`.

        Es exactamente el criterio de `Pokemon.identifies_as`
        (`pokemon.py:435-438`). Comparar `species` a secas contaba `camerupt` y
        `cameruptmega` como DOS miembros distintos, y una Mega que sale y
        vuelve producia un equipo rival de siete (medido en
        `battle-gen6randombattle-1917`, decision 32).
        """
        return vocabulary.base_species(species)

    def find(species: str) -> dict | None:
        objetivo = canon(species)
        for mon in team:
            if canon(normalize_id(mon.get("species") or "")) == objetivo:
                return mon
        return None

    def named_target(ident: str) -> dict | None:
        """El miembro del equipo rival que NOMBRA este ident de evento
        (MON-26 R2; antes `enditem_target`).

        Resuelve por identidad canonica (`find`, por `base_species`), NO por
        "quien esta activo ahora": en una ventana con gap (request
        `wait:true`, ver `wait_for_resolution`) la narracion puede traer
        lineas de identidad persistente (`-item`, `-enditem`, `-ability`,
        `-endability`) seguidas de `switch` en el MISMO frame, y el
        snapshot -- post-narracion -- ya tiene al nombrado fuera de cancha.
        Resolver por `active()` le escribe el dato al REEMPLAZO y envenena
        la memoria D40 (misatribucion medida, LINEAR_VERDICT MON-26 R1 F1).
        Bajo Illusion el disfraz ES la entrada del equipo, asi que `find`
        devuelve el mismo objeto que `active()`.

        Si el nombre no esta en el equipo (mon jamas revelado -- la linea
        tambien quedo en un gap sin su revelacion previa), se conserva el
        fallback al activo actual: es el comportamiento previo, y en ese
        caso la ranura activa es lo unico publicamente resoluble. Un ident
        que no sea la ranura activa del rival (`pXa:`) no resuelve a nadie:
        en singles las lineas de identidad siempre nombran al activo.
        """
        if not ident.startswith(active_prefix):
            return None
        nombre = ident.split(":", 1)[-1].strip() if ":" in ident else ""
        especie = normalize_id(nombre)
        if especie:
            target = find(especie)
            if target is not None:
                return target
        return active()

    def switch_out(mon: dict) -> None:
        """Saca del campo, replicando `Pokemon.switch_out` (`pokemon.py:
        600-612`): limpia boosts y CUALQUIER override temporal (typechange,
        Transform, ability copiada) que este registrado para esta identidad.

        Lo que NO hace: resetear tipos/ability/moves "por las dudas". poke-env
        nunca toca `_type_1`/`_type_2` en `switch_out` -- solo
        `_temporary_types`/`temporary_ability`/`_transform_moves`, que son
        campos DISTINTOS. Si no hay ningun override registrado para esta
        identidad, tipos/ability/moves ya son los persistentes correctos
        (los que puso `switch_in` o `forme_change`) y no se tocan: resetearlos
        al dex de `species` es exactamente el bug que producia tipos
        equivocados en un Pokemon Mega que sale del campo (su `species` sigue
        siendo la forma base, pero sus tipos persistentes son los de la Mega).

        `ability` es la excepcion a "se consume una sola vez": `types` y
        `moves` se descartan (`pop`) porque un Transform es un evento
        puntual, pero la ability PERSISTENTE (equivalente a `_ability` en
        poke-env) tiene que sobrevivir para el PROXIMO override (otra
        Entrainment, otro Transform) -- por eso se lee (`peek`), nunca se
        descarta, aunque no haya ningun override activo ahora mismo.

        `item_backup` (D40, T-02) se descarta SIN restaurar nada: un switch
        ordinario (a diferencia de `|replace|`, ver `end_illusion`) confirma
        que la identidad aparente era real, asi que cualquier item revelado
        durante esta estancia queda permanente.
        """
        mon["active"] = False
        mon["boosts"] = _blank_boosts()
        identidad = canon(normalize_id(mon.get("species") or ""))
        entry = persistent_state.get(identidad)
        if entry is not None:
            entry.pop("item_backup", None)
            if "types" in entry:
                mon["types"] = list(entry.pop("types"))
            if "moves" in entry:
                mon["moves"] = [dict(m) for m in entry.pop("moves")]
                # D37: la marca de PP desconocido de un Transform es tan
                # temporal como el moveset que restaura -- se descarta con
                # el, para no filtrarse al PROXIMO Transform de esta misma
                # identidad. `unknown_pp_moves` (Pressure sobre un
                # movimiento propio del pokemon, no copiado) es la
                # excepcion: esa SI sobrevive cualquier cantidad de
                # switches, por eso no se toca aca.
                entry.pop("transform_unknown_pp_moves", None)
            if "ability" in entry:
                mon["ability"] = entry["ability"]
            if not entry:
                persistent_state.pop(identidad, None)

    def switch_in(species: str, hp_field: str, details: str) -> None:
        for mon in team:
            if mon.get("active"):
                switch_out(mon)
        existing = find(species)
        mon = existing if existing is not None else _new_mon(species, vocabulary)
        if existing is None:
            team.append(mon)
        # `switch_in` SI escribe la especie: `_update_from_details` llama a
        # `_update_from_pokedex` con `store_species=True` (a diferencia de
        # `forme_change`). Medido: cuando una Mega vuelve a entrar, poke-env
        # pasa a decir `cameruptmega` en la MISMA entrada.
        mon["species"] = species
        mon["active"] = True
        mon["boosts"] = _blank_boosts()
        mon["types"] = vocabulary.species_types(species)
        mon["ability"] = _dex_ability(species, mon.get("ability"), vocabulary)
        # D41 (MON-19): `switch_in` es evidencia publica DIRECTA del tipo
        # canonico -- poke-env corta en seco en `_update_from_details` si
        # `details` no cambio desde la ultima vez, asi que tras un
        # `-formechange` temporal (Relic Song) su propio snapshot puede
        # seguir stale en el PROXIMO switch-in con el mismo `details` base.
        # Esta linea SIEMPRE recalcula del dex sin importar el cache de
        # poke-env, pero esa correccion no sobrevive sola a la llamada
        # SIGUIENTE si nada vuelve a nombrar a esta identidad -- por eso se
        # memoriza en `canonical_types` y se reaplica mas abajo (mismo
        # patron que `item`, D40). Tambien termina cualquier override
        # temporal colgado de esta identidad: en curso normal `switch_out`
        # ya lo limpia, pero `switch_in` no puede depender de eso.
        identidad = canon(normalize_id(mon.get("species") or ""))
        entry = persistent_state.setdefault(identidad, {})
        entry.pop("types", None)
        entry["canonical_types"] = list(mon["types"])
        level = _level_from_details(details)
        if level is not None:
            mon["level"] = level
        fraction, status, fainted = _parse_hp(hp_field)
        if fraction is not None:
            mon["hp_fraction"] = fraction
        mon["status"] = status
        mon["fainted"] = fainted

    def end_illusion(species: str, details: str) -> None:
        """`replace`: la Illusion se rompio y el activo era OTRO pokemon.

        Son DOS entradas del equipo, no un renombre. Paridad exacta con
        `AbstractBattle._end_illusion_on` (`abstract_battle.py:409-427`):

          - el imitado NO desaparece (su `|switch|` es evidencia publica de
            que el rival lo tiene); sale del campo via `was_illusioned` +
            `switch_out`, o sea `active=False`, boosts limpios y hp/status a
            None. `current_hp_fraction` devuelve 0 cuando `_current_hp` es
            None (`pokemon.py:988-995`), asi que la fila de poke-env para el
            imitado dice `hp_fraction=0.0` con `fainted=False`; se replica
            para no contradecir a las filas de la MISMA batalla que se
            serializan desde poke-env.
          - el real entra con `switch_in(details)` (especie, nivel y tipos del
            `details` del propio `|replace|`) y hereda el HP y el status que
            recibio mientras estaba disfrazado, porque el que estaba en el
            campo era el.
          - lo que no viaja: item, ability y movimientos. Renombrar la entrada
            activa —lo que hacia la version anterior— le regalaba a Zoroark el
            item y la ability revelados del imitado, y borraba al imitado del
            equipo rival.

        **Provenance del item (D40, T-02).** Cualquier item revelado
        DURANTE esta estancia le pertenece al disfraz, no al imitado: antes
        de que `switch_out` descarte el backup sin restaurar (lo que haria
        si el disfraz nunca se hubiera roto), se restaura la memoria de
        item que el imitado tenia ANTES de la primera mutacion de esta
        estancia -- ausente, `None`, o un item -- y se limpia el sentinel
        de `item_backup`. No hace falta nada simetrico para Zoroark (`real`):
        nunca se le escribio item a partir del imitado.
        """
        illusioned = active()
        if illusioned is None:
            return
        if canon(normalize_id(illusioned.get("species") or "")) == canon(species):
            # poke-env corta en seco: `if illusionist_mon is illusioned`.
            return
        identidad_imitado = canon(normalize_id(illusioned.get("species") or ""))
        entry_imitado = persistent_state.get(identidad_imitado)
        if entry_imitado is not None and "item_backup" in entry_imitado:
            backup = entry_imitado.pop("item_backup")
            if backup is _NO_PRIOR_ITEM:
                entry_imitado.pop("item", None)
                illusioned["item"] = "unknown_item"
            else:
                entry_imitado["item"] = backup
                illusioned["item"] = backup
            if not entry_imitado:
                persistent_state.pop(identidad_imitado, None)
        real = find(species)
        if real is None:
            real = _new_mon(species, vocabulary)
            team.append(real)
        real["species"] = species
        real["active"] = True
        real["types"] = vocabulary.species_types(species)
        real["ability"] = _dex_ability(species, real.get("ability"), vocabulary)
        level = _level_from_details(details)
        if level is not None:
            real["level"] = level
        real["hp_fraction"] = illusioned.get("hp_fraction")
        real["status"] = illusioned.get("status")
        real["fainted"] = illusioned.get("fainted", False)

        switch_out(illusioned)
        illusioned["hp_fraction"] = 0.0
        illusioned["status"] = None
        illusioned["fainted"] = False

    def apply_typechange(parts: list[str]) -> None:
        """`|-start|{side}a: X|typechange|...`.

        Misma regla que `abstract_battle.py:802-809`, que mira exactamente
        `event[5]`: si ahi viene un `[of] ...`, los tipos se copian del pokemon
        citado (Reflect Type); si no, vienen narrados en claro, separados por
        `/` (Protean, Libero, Camouflage).
        """
        mon = active()
        if mon is None:
            return
        # Se recuerda el tipo PERSISTENTE (el que hay ANTES de este
        # typechange) una sola vez por identidad: si ya hay un override en
        # curso (p.ej. un segundo typechange antes de salir del campo),
        # `setdefault` no lo vuelve a pisar con el valor YA temporal.
        entry = persistent_state.setdefault(canon(normalize_id(mon.get("species") or "")), {})
        entry.setdefault("types", list(mon["types"]))
        if len(parts) > 5 and parts[5].startswith("[of] "):
            source = mon_for_ident(parts[5][5:].strip())
            if source is not None:
                mon["types"] = list(source.get("types") or [])
            return
        if len(parts) > 4 and parts[4]:
            names = [vocabulary.type_name(chunk) for chunk in parts[4].split("/")]
            resolved = [name for name in names if name]
            if resolved:
                mon["types"] = resolved

    def apply_transform(parts: list[str]) -> None:
        """`|-transform|{side}a: Ditto|{otro}a: X|[from] ability: Imposter`.

        Copiar un pokemon NUESTRO no es fuga: es informacion que ya tenemos
        (ver .claude/agent-recording/SKILL.md, inferencias legitimas).

        Paridad con `Pokemon.transform()` (`pokemon.py:625-636`): tipos del DEX
        de la especie copiada (no sus tipos ACTUALES), boosts copiados, moveset
        del objetivo, ability del objetivo si se conoce, y la especie intacta.
        El PP de un movimiento copiado es `min(5, max_pp)` en gen >= 5
        (`move.py:114` y `move.py:477-478`): es una regla fija de la
        generacion, no informacion oculta. Sin generacion en el snapshot el PP
        no es derivable y va en null (schema v2).
        """
        mon = active()
        if mon is None or len(parts) < 4:
            return
        source = mon_for_ident(parts[3].strip())
        if source is None:
            return
        gen = snapshot.get("gen")
        # Imposter va por `reveal_ability`, no por escritura directa: usa la
        # MISMA regla persistente-vs-temporal que poke-env aplica en su
        # setter (`pokemon.py:873-878`), en vez de asumir que "todavia no
        # conocida" es el unico caso posible. `_add_move("transform")` al
        # moveset base es siempre persistente (`abstract_battle.py:
        # 1059-1065`), asi que eso si se escribe directo.
        if any(part.strip() == "[from] ability: Imposter" for part in parts[4:]):
            reveal_ability(mon, "imposter")
            if not any(m.get("id") == "transform" for m in mon["moves"]):
                max_pp = vocabulary.move_max_pp("transform")
                mon["moves"].append(
                    {"id": "transform", "pp": max_pp, "max_pp": max_pp}
                )
        # Se guarda el estado base de TIPOS y MOVES antes de taparlos, para
        # restaurarlos al switch-out (evento puntual, se consume una sola
        # vez). La ability NO entra aca: `reveal_ability` mas abajo ya
        # decide sola si el copiado es persistente o temporal.
        especie = normalize_id(mon.get("species") or "")
        entry = persistent_state.setdefault(canon(especie), {})
        entry.setdefault("moves", [dict(m) for m in mon["moves"]])
        entry.setdefault("types", list(mon["types"]))
        source_species = normalize_id(source.get("species") or "")
        types = vocabulary.species_types(source_species)
        if types:
            mon["types"] = types
        mon["boosts"] = {**_blank_boosts(), **(source.get("boosts") or {})}
        source_ability = source.get("ability")
        if source_ability:
            reveal_ability(mon, source_ability)
        mon["moves"] = [
            _transformed_move(move, gen) for move in (source.get("moves") or [])
        ]

    def reveal_ability(mon: dict, raw: str) -> None:
        """Ability revelada por un evento publico. Espejo EXACTO del setter
        de poke-env (`pokemon.py:873-878`): si TODAVIA no hay ability
        conocida para este pokemon, este valor la fija como PERSISTENTE (no
        hay nada que restaurar despues -- por eso NO se siembra
        `persistent_state`, a diferencia de si ya habia una). Si YA habia
        una, este valor es un override TEMPORAL: se registra la persistente
        (solo la PRIMERA vez que se tapa, `setdefault`) para que
        `switch_out` o `-endability` puedan revertir.

        Se usa en TODOS los caminos que revelan una ability -- `-ability`,
        Trace, Magic Bounce/Dancer (via `apply_move`), y la copiada por
        Transform -- para que la regla persistente-vs-temporal sea una sola,
        nunca reinventada por cada camino.
        """
        ability = normalize_id(raw)
        if not ability:
            return
        actual = mon.get("ability")
        if actual is not None:
            identidad = canon(normalize_id(mon.get("species") or ""))
            persistent_state.setdefault(identidad, {}).setdefault("ability", actual)
        mon["ability"] = ability

    def remember_item(mon: dict, value: str | None) -> None:
        """Fija el item PUBLICAMENTE evidenciado del rival y lo memoriza en
        `persistent_state` (D40): el `snapshot` de entrada es SIEMPRE fresco
        (`serialize_battle`, nunca la proyeccion anterior) y poke-env
        corrompe `battle.opponent_team[...].item` entre una decision y la
        siguiente tras un intercambio por Trick -- confirmado en vivo
        (`battle-gen6randombattle-2746`, ROOT-CAUSE CHECKPOINT R3). Sin esta
        memoria, ese valor corrupto pisaria evidencia ya establecida sin que
        ninguna linea nueva la pidiera; mismo patron que D37 ya resuelve
        para el PP bajo Pressure.

        `value=None` es tan significativo como cualquier item real (item
        consumido o removido, `-enditem`): la clave `"item"` queda
        PRESENTE con ese valor, nunca ausente -- de eso depende no
        reescribir un `None` ya confirmado con un numero stale. Nunca se
        llama con el sentinel inicial `unknown_item`: eso no es evidencia,
        es su ausencia (D40, requisito 2).

        **Provenance bajo Illusion (D40, T-02).** Esta identidad puede ser
        el disfraz de un Zoroark: si la mutacion resulta ser evidencia
        observada DURANTE un disfraz, `end_illusion` la revierte cuando el
        `|replace|` la desenmascara. Por eso, en la PRIMERA mutacion de
        `item` desde el ultimo switch-in de esta identidad (marcada por la
        ausencia de `"item_backup"` en la entrada), se guarda el valor
        ANTERIOR -- presente con un item, presente con `None`, o
        `_NO_PRIOR_ITEM` si la clave no existia -- antes de pisarlo.
        `switch_out` descarta ese backup sin restaurar nada (un switch
        ordinario confirma que la identidad aparente era real); `end_
        illusion` lo restaura antes de llamar a `switch_out`. Mutaciones
        SIGUIENTES en la misma estancia no vuelven a tocar el backup: sólo
        importa el estado anterior a la PRIMERA.
        """
        identidad = canon(normalize_id(mon.get("species") or ""))
        entry = persistent_state.setdefault(identidad, {})
        if "item_backup" not in entry:
            entry["item_backup"] = entry.get("item", _NO_PRIOR_ITEM)
        mon["item"] = value
        entry["item"] = value

    def register_move(mon: dict, raw_move: str, *, use: bool) -> None:
        """Revela un movimiento del rival y descuenta su PP.

        `use=False` revela sin consumir: es lo que hace poke-env para el
        movimiento que llama Sleep Talk, que se revela con PP completo porque
        el PP lo paga Sleep Talk.

        El PP se descuenta desde el valor que ya trae el snapshot (que sale de
        la contabilidad de poke-env) y para un movimiento nuevo desde el
        `max_pp` del dex. Cuando no es derivable con exactitud va en `null`
        (schema v2): dejar el numero anterior seria afirmar un PP stale.
        """
        move_id = normalize_id(raw_move)
        # Hidden Power: Showdown narra siempre "Hidden Power", nunca el tipo.
        # El tipo real es dato oculto (IVs).
        if move_id.startswith("hiddenpower"):
            move_id = "hiddenpower"
        if not move_id:
            return
        existing = next((m for m in mon["moves"] if m.get("id") == move_id), None)
        if existing is None:
            max_pp = vocabulary.move_max_pp(move_id)
            existing = {"id": move_id, "pp": max_pp, "max_pp": max_pp}
            mon["moves"].append(existing)
        if not use:
            return
        if pressure_on_us():
            # `Move.use` descuenta 2 con Pressure (`move.py:123-127`) y la regla
            # exacta depende del objetivo. Antes que errar por uno, null.
            existing["pp"] = None
            # D37: sin esta marca, la proxima llamada reconstruye
            # `mon["moves"]` desde un snapshot fresco (el PP que cuenta
            # poke-env, ciego a Pressure) y ese numero pisaria el `None` sin
            # que ninguna linea nueva lo pidiera. `persistent_state` es lo
            # unico que sobrevive entre llamadas (ver docstring del modulo).
            identidad = canon(normalize_id(mon.get("species") or ""))
            entry = persistent_state.setdefault(identidad, {})
            if "moves" in entry:
                # Transformado ahora mismo: la marca es tan temporal como el
                # moveset copiado -- se descarta junto con el en
                # `switch_out`, para no filtrarse a un Transform distinto.
                entry.setdefault("transform_unknown_pp_moves", set()).add(move_id)
            else:
                entry.setdefault("unknown_pp_moves", set()).add(move_id)
            return
        pp = existing.get("pp")
        existing["pp"] = pp - 1 if isinstance(pp, int) and pp > 0 else None

    def pressure_on_us() -> bool:
        own = own_active()
        return bool(own) and normalize_id(own.get("ability") or "") == "pressure"

    def _owner_of(ident: str) -> dict | None:
        """El pokemon nombrado por un `ident` de protocolo, si es el rival.

        A diferencia de `mon_for_ident`, devuelve `None` (no `own_active()`)
        cuando el ident es nuestro: nuestro lado ya llega fresco por el
        `|request|` y no hay nada que proyectar sobre `me`.
        """
        ident = ident.strip()
        if ident.startswith(active_prefix):
            return active()
        return None

    def apply_damage_or_heal_ownership(parts: list[str], *, heal: bool) -> None:
        """Item/ability revelados por el sufijo de una linea `-damage`/`-heal`.

        Reproduce los cuatro helpers que poke-env 0.15.0 ya tiene para esto
        (`abstract_battle.py:333-403`), con su misma semantica de `[of]`:

          - daño por item PROPIO (sin `[of]`): el item es de quien lo recibe;
          - daño por item/ability AJENOS (`[of] X`): pertenecen a X, que
            puede ser CUALQUIERA de los dos lados -- por eso esto corre antes
            del filtro por ident;
          - heal por item propio: mismo item, salvo que ya sea `None`
            (consumido) o sea una berry/herb, para no reescribir un item que
            Showdown ya vacio al consumirlo (medido: la narracion de heal
            llega DESPUES de que la berry ya se gasto);
          - heal por ability propia: el `[of]` de esta linea es enganoso y
            NO indica de quien es la ability (salvo el caso especial
            Hospitality, que si la trae de otro).
        """
        if heal:
            if len(parts) == 5 and parts[4].startswith("[from] item:"):
                mon = _owner_of(parts[2])
                if mon is None:
                    return
                item = normalize_id(parts[4].split("item:", 1)[-1])
                current = mon.get("item")
                if current is not None and "berry" not in item and "herb" not in item:
                    remember_item(mon, item)
            elif len(parts) == 6 and parts[4].startswith("[from] ability:"):
                ability = normalize_id(parts[4].split("ability:", 1)[-1])
                if ability == "hospitality":
                    mon = _owner_of(parts[5].replace("[of]", "").strip())
                else:
                    mon = _owner_of(parts[2])
                if mon is not None:
                    mon["ability"] = ability
            return
        if (
            len(parts) == 6
            and parts[4].startswith("[from] item:")
            and parts[5].startswith("[of]")
        ):
            mon = _owner_of(parts[5].split("[of]", 1)[-1].strip())
            if mon is not None:
                remember_item(mon, normalize_id(parts[4].split("item:", 1)[-1]))
        elif len(parts) == 5 and parts[4].startswith("[from] item:"):
            mon = _owner_of(parts[2])
            if mon is not None:
                remember_item(mon, normalize_id(parts[4].split("item:", 1)[-1]))
        elif (
            len(parts) == 6
            and parts[4].startswith("[from] ability:")
            and parts[5].startswith("[of]")
        ):
            mon = _owner_of(parts[5].split("[of]", 1)[-1].strip())
            if mon is not None:
                mon["ability"] = normalize_id(parts[4].split("ability:", 1)[-1])

    def apply_item_transfer_ownership(parts: list[str]) -> None:
        """`-item|{receptor}|{item}|[from] move: Thief|[of] {victima}` (D40,
        T-01): Thief/Covet (movimientos) y Pickpocket/Magician (abilities)
        narran la transferencia en UNA sola linea, con `ident` (parts[2]) =
        quien RECIBE el item. Showdown nunca manda una linea separada para
        quien lo PIERDE -- esta es la unica evidencia. Se procesa ANTES del
        filtro generico de ident, igual que `apply_damage_or_heal_
        ownership`: si el receptor es nuestro lado, ese filtro descartaria
        la linea completa antes de que nadie notara que el rival nombrado
        por `[of]` se quedo sin item.

        Cuando el receptor es el RIVAL (nos robo a nosotros), esta funcion
        no hace nada: `_owner_of` sobre el `[of]` (que aca nombra a nuestro
        lado) no resuelve a ningun mon, y el handler normal de `-item` --
        alcanzable porque el ident YA es del rival -- cubre esa direccion
        sin cambios (D40, T-01, requisito 4).

        Symbiosis (la otra ability que transfiere items) exige un ALIADO
        del mismo lado: es estructuralmente inalcanzable en singles, el
        unico gametype que este proyector modela (`active_prefix` asume una
        sola ranura activa por lado) y el unico que `apps/agent` juega en
        la practica (`SHOWDOWN_BATTLE_FORMAT` por defecto es
        `gen6randombattle`). No se implementa: seria logica muerta.
        """
        if len(parts) != 6 or not parts[5].startswith("[of]"):
            return
        prefix = parts[4]
        if prefix.startswith("[from] move:"):
            causa = prefix.split(":", 1)[-1].strip()
            if causa not in _ITEM_TRANSFER_MOVES:
                return
        elif prefix.startswith("[from] ability:"):
            causa = prefix.split(":", 1)[-1].strip()
            if causa not in _ITEM_TRANSFER_ABILITIES:
                return
        else:
            return
        victima = _owner_of(parts[5].split("[of]", 1)[-1].strip())
        if victima is not None:
            remember_item(victima, None)

    def apply_move(parts: list[str]) -> None:
        """`|move|{side}a: X|Nombre|objetivo|sufijos...`.

        Reproduce las excepciones de pertenencia que poke-env 0.15.0 ya
        codifica (`abstract_battle.py:582-700`), ancladas a los sufijos
        publicos. Sin esto, la rama `move` de D31 afirmaba que TODO movimiento
        narrado pertenece al actor: en el corpus hay 39 lineas `|move|` con
        `[from] ability:`, casi todas Magic Bounce, donde el movimiento
        reflejado era del rival de ese actor.
        """
        mon = active()
        if mon is None:
            return
        event = list(parts)
        use = True
        reveal = True
        while len(event) > 4:
            tail = event[-1].strip()
            if tail in _MOVE_NOISE or tail.startswith(("[spread]", "[anim]")):
                event.pop()
                continue
            if tail in _MOVE_NOT_OWNED:
                use = reveal = False
                event.pop()
                continue
            if tail == "[from] Sleep Talk":
                event[-1] = "[from] move: Sleep Talk"
                continue
            if tail.startswith(("[from] move: ", "[from]move: ")):
                called = tail.split(": ", 1)[-1].strip()
                event.pop()
                if called in _PLEDGES:
                    continue
                # El PP lo paga el movimiento que llamo, no el eco.
                use = False
                if called in _CALLERS_NOT_OWNED:
                    reveal = False
                continue
            if tail.startswith(("[from] ability: ", "[from]ability: ")):
                ability = tail.split(": ", 1)[-1].strip()
                event.pop()
                # Orden exacto de poke-env (`abstract_battle.py:650-656`): la
                # ability se asigna PRIMERO, incondicionalmente, y RECIEN
                # despues viene el `return` de Dancer. Dancer revela su
                # propia ability publica; lo que NO se revela es el
                # movimiento que bailo (por eso el return, que salta el
                # `register_move` de mas abajo).
                reveal_ability(mon, ability)
                if ability == "Dancer":
                    return
                if ability == "Magic Bounce":
                    use = reveal = False
                continue
            break
        if reveal and len(event) > 3:
            register_move(mon, event[3], use=use)

    def forme_change(forme: str, *, permanent: bool) -> None:
        """`detailschange` (Mega/Primal, `permanent=True`) y `-formechange`
        (Meloetta y demas formas transitorias, `permanent=False`).

        Cambia los TIPOS pero NO `species`, porque es exactamente lo que hace
        poke-env: `Pokemon.forme_change()` llama a `_update_from_pokedex(...,
        store_species=False)` (`pokemon.py:431-433`), asi que `mon.species`
        sigue siendo la forma base tras una Mega evolucion.

        Verificado sobre datos reales: en `battle-gen6randombattle-1896` T3
        llega `|detailschange|p2a: Slowbro|Slowbro-Mega, L81, M` y las filas
        posteriores de poke-env siguen diciendo `slowbro`. Si aca se escribiera
        `slowbromega`, la proyeccion contradiria al resto del dataset dentro de
        la MISMA batalla.

        D41 (MON-19): las dos etiquetas comparten esta funcion pero NO el
        mismo destino en `persistent_state` -- son semanticas opuestas:

          - `detailschange` (Mega/Primal) es PERMANENTE: no revierte al
            salir del campo (`switch_out` nunca toca `_type_1`/`_type_2`),
            asi que estos tipos SON el nuevo canonico. Actualiza
            `canonical_types` directo, sin backup temporal.
          - `-formechange` (Relic Song y demas formas que SI revierten al
            salir) es TEMPORAL: entra al mismo ciclo backup/restauracion
            que ya usan `apply_typechange`/`apply_transform` (`entry.
            setdefault("types", ...)`, restaurado en `switch_out`). NUNCA
            toca `canonical_types` -- si lo hiciera, `canonical_types`
            quedaria con la forma temporal en vez de la base, y la
            reaplicacion de mas abajo la sostendria stale para siempre.
        """
        mon = active()
        if mon is None:
            return
        identidad = canon(normalize_id(mon.get("species") or ""))
        if permanent:
            mon["types"] = vocabulary.species_types(forme)
            persistent_state.setdefault(identidad, {})["canonical_types"] = list(mon["types"])
        else:
            entry = persistent_state.setdefault(identidad, {})
            entry.setdefault("types", list(mon["types"]))
            mon["types"] = vocabulary.species_types(forme)
        # La ability SI cambia: `_update_from_pokedex` la guarda en
        # `forme_change_ability` para una forma Mega/Primal y la property la
        # prefiere (`pokemon.py:650-655`, `861-871`). Medido: tras el
        # detailschange a Camerupt-Mega, poke-env reporta `sheerforce`.
        # Meloetta-Pirouette no es Mega/Primal (`forme_change_ability`
        # devuelve `None` para ella), asi que esta rama es un no-op para
        # `-formechange` en la practica -- compartida por simetria, no
        # porque Relic Song cambie la ability.
        forme_ability = vocabulary.forme_change_ability(forme)
        if forme_ability is not None:
            mon["ability"] = forme_ability

    # D37: `persistent_state` es la UNICA memoria entre llamadas; el
    # `snapshot` de ESTA llamada llega fresco de `serialize_battle` (nunca
    # es la proyeccion anterior encadenada) y puede traer un PP numerico
    # para un movimiento que ya marcamos como indeterminable por Pressure
    # -- poke-env cuenta su propio PP sin saber de Pressure. Se reaplica
    # ANTES de procesar ninguna linea nueva de este frame, para que un uso
    # real dentro de esta misma llamada (`register_move`) pueda seguir
    # recalculando sobre la base correcta.
    #
    # `"moves" in entry` es la MISMA senal que ya usa `switch_out` para
    # saber si hay un Transform activo ahora mismo: cuando existe, el
    # moveset VISIBLE en `mon["moves"]` es el COPIADO, no el base, y una
    # marca permanente del base (de antes del Transform) nombra un `move_id`
    # que puede coincidir por casualidad con el copiado -- son instancias
    # de PP distintas (LINEAR_VERDICT MON-18 R1, L-01). Mientras el
    # Transform siga activo, solo se reaplica `transform_unknown_pp_moves`;
    # sin Transform activo, solo `unknown_pp_moves` gobierna.
    for mon in team:
        identidad = canon(normalize_id(mon.get("species") or ""))
        entry = persistent_state.get(identidad)
        if not entry:
            continue
        # D40: mismo mecanismo que el PP bajo Pressure, pero permanente (no
        # hay analogo "temporal por Transform" -- Transform no copia item).
        # Reaplicado ANTES de procesar ninguna linea nueva del frame para
        # que una revelacion real dentro de esta misma llamada pueda seguir
        # reemplazandolo (D40, requisito 5).
        if "item" in entry:
            mon["item"] = entry["item"]
        # D41: `canonical_types` es tipos permanentes (switch_in,
        # `detailschange`/Mega). La clave `"types"` sigue significando
        # exclusivamente "backup de un override temporal activo"
        # (typechange, Transform, `-formechange`/Relic Song) -- mientras
        # este presente, el override es lo que se ve, y reaplicar el
        # canonico por encima lo pisaria mal. Mutuamente excluyentes, mismo
        # patron que D37 exige entre `unknown_pp_moves`/`transform_
        # unknown_pp_moves`.
        if "types" not in entry and "canonical_types" in entry:
            mon["types"] = list(entry["canonical_types"])
        if "moves" in entry:
            desconocidos = entry.get("transform_unknown_pp_moves") or ()
        else:
            desconocidos = entry.get("unknown_pp_moves") or ()
        if not desconocidos:
            continue
        for move in mon["moves"]:
            if move.get("id") in desconocidos:
                move["pp"] = None

    for line in lines:
        parts = line.split("|")
        tag = parts[1] if len(parts) > 1 else ""
        if tag == "turn" and len(parts) > 2:
            try:
                projected["turn"] = int(parts[2])
            except ValueError:
                pass
            continue
        if tag == "-weather" and len(parts) > 2:
            name = vocabulary.weather_name(parts[2])
            projected["field"]["weather"] = (
                {} if name is None else {name: projected.get("turn")}
            )
            continue
        if tag in ("-fieldstart", "-fieldend") and len(parts) > 2:
            name = vocabulary.field_name(parts[2])
            if name is not None:
                effects = projected["field"]["field_effects"]
                if tag == "-fieldstart":
                    effects.setdefault(name, projected.get("turn"))
                else:
                    effects.pop(name, None)
            continue
        if tag in ("-sidestart", "-sideend") and len(parts) > 3:
            _apply_side(projected, parts, opponent_side, vocabulary, tag)
            continue
        if tag == "-clearallboost":
            # `|-clearallboost` no trae ident: Showdown lo emite asi porque
            # limpia LOS DOS activos a la vez (`clear_all_boosts()`,
            # `abstract_battle.py:598-601`). El guard `len(parts) < 3` de mas
            # abajo lo volvia inalcanzable (94 lineas reales en el corpus de
            # test, cero ejercidas). Nuestro lado ya llega fresco por el
            # `|request|`; solo hace falta limpiar el rival.
            mon = active()
            if mon is not None:
                mon["boosts"] = _blank_boosts()
            continue
        if tag == "-copyboost" and len(parts) > 3:
            # `|-copyboost|fuente|objetivo|...`: el OBJETIVO (parts[3], no
            # parts[2]) se queda con los boosts de la fuente; la fuente no
            # cambia. Se procesa antes del filtro generico porque ese filtro
            # mira `parts[2]` (la fuente), y el caso que importa es
            # exactamente el opuesto: fuente nuestra, objetivo rival.
            target = parts[3].strip()
            source = parts[2].strip()
            if target.startswith(active_prefix) and not source.startswith(active_prefix):
                mon = active()
                # `own_mon_named`, no `own_active()`: la fuente es quien el
                # evento NOMBRA, no necesariamente quien esta activo ahora
                # en el snapshot post-resolucion (mismo bug que Transform).
                own = own_mon_named(source)
                if mon is not None:
                    mon["boosts"] = dict(own.get("boosts") or _blank_boosts())
            continue
        if tag == "-swapboost":
            # `-swapboost` (Guard Swap, Heart Swap, Power Swap) intercambia
            # boosts ENTRE los dos lados. En singles, con un solo activo por
            # lado, todo `-swapboost` real cruza los dos lados: SIEMPRE toca
            # al rival, sin importar cual de los dos nombra primero -- por
            # eso se chequea aca, ANTES del filtro generico por `parts[2]`.
            # Escribirle el valor correcto al rival exigiria el boost PROPIO
            # de ANTES del intercambio, y este proyector nunca lo tiene: "me"
            # llega siempre POST resolucion via el `|request|` propio (el
            # mismo valor fresco que ya se uso para decidir, no el de
            # "antes"). La revision anterior acepto documentar esto como
            # limite y conservar el boost stale del rival; el veredicto
            # siguiente lo rechazo explicitamente ("nunca dejar un numero
            # stale"). Ahora falla CERRADO: ni proveedor con un boost rival
            # conocido como incorrecto, ni fila persistida.
            raise ProjectionAmbiguityError(
                "-swapboost no es representable sin el boost propio de "
                "antes del intercambio: fallar cerrado antes que persistir "
                "un boost del rival sabidamente stale"
            )
        if tag in ("-damage", "-heal") and len(parts) > 3:
            # Item/ability revelados por un sufijo de daño o cura pueden
            # nombrar como `ident` (parts[2]) a CUALQUIERA de los dos lados,
            # y el dato relevante puede pertenecer al OTRO lado via `[of]`.
            # Se procesa ANTES del filtro por ident de mas abajo: si el
            # daño lo recibio nuestro propio activo pero el item/ability
            # revelado es del rival (`[of] {opponent}a: X`), saltarse el
            # filtro dejaria esa revelacion sin proyectar -- exactamente el
            # retraso-en-uno que F2-01 existe para eliminar, ahora en
            # `item`/`ability` en vez de en la identidad del activo.
            apply_damage_or_heal_ownership(parts, heal=(tag == "-heal"))
        if tag == "-item" and len(parts) > 3:
            # D40 (T-01): mismo motivo que arriba -- una transferencia por
            # Thief/Covet/Pickpocket/Magician nombra como `ident` a quien
            # RECIBE el item, que puede ser nuestro propio lado. El filtro
            # generico de mas abajo descartaria la linea antes de que nadie
            # notara que el rival, nombrado por `[of]`, se quedo sin item.
            apply_item_transfer_ownership(parts)
        if len(parts) < 3:
            continue
        ident = parts[2]
        if not ident.startswith(active_prefix) and not ident.startswith(side_prefix):
            continue
        if tag in ("switch", "drag") and len(parts) > 4:
            switch_in(normalize_id(parts[3].split(",", 1)[0]), parts[4], parts[3])
        elif tag == "replace" and len(parts) > 3:
            end_illusion(normalize_id(parts[3].split(",", 1)[0]), parts[3])
        elif tag == "-start" and len(parts) > 3 and parts[3] == "typechange":
            apply_typechange(parts)
        elif tag == "-transform":
            apply_transform(parts)
        elif tag == "detailschange" and len(parts) > 3:
            forme_change(normalize_id(parts[3].split(",", 1)[0]), permanent=True)
        elif tag == "-formechange" and len(parts) > 3:
            forme_change(normalize_id(parts[3].split(",", 1)[0]), permanent=False)
        elif tag in ("-damage", "-heal", "-sethp") and len(parts) > 3:
            mon = active()
            if mon is not None:
                fraction, status, fainted = _parse_hp(parts[3])
                if fraction is not None:
                    mon["hp_fraction"] = fraction
                if status is not None:
                    mon["status"] = status
                if fainted:
                    mon["fainted"] = True
        elif tag == "-status" and len(parts) > 3:
            mon = active()
            if mon is not None:
                mon["status"] = parts[3].strip().upper()
        elif tag == "-curestatus":
            mon = active()
            if mon is not None:
                mon["status"] = None
        elif tag == "faint":
            mon = active()
            if mon is not None:
                mon["hp_fraction"] = 0.0
                mon["fainted"] = True
                mon["status"] = "FNT"
        elif tag in ("-boost", "-unboost") and len(parts) > 4:
            mon = active()
            if mon is not None:
                try:
                    amount = int(parts[4])
                except ValueError:
                    amount = 0
                if tag == "-unboost":
                    amount = -amount
                stat = parts[3]
                current = mon["boosts"].get(stat, 0)
                mon["boosts"][stat] = max(-6, min(6, current + amount))
        elif tag == "-setboost" and len(parts) > 4:
            mon = active()
            if mon is not None:
                try:
                    mon["boosts"][parts[3]] = int(parts[4])
                except ValueError:
                    pass
        elif tag == "-clearboost":
            mon = active()
            if mon is not None:
                mon["boosts"] = _blank_boosts()
        elif tag == "-clearnegativeboost":
            mon = active()
            if mon is not None:
                mon["boosts"] = {
                    k: (0 if v < 0 else v) for k, v in mon["boosts"].items()
                }
        elif tag == "-clearpositiveboost":
            mon = active()
            if mon is not None:
                mon["boosts"] = {
                    k: (0 if v > 0 else v) for k, v in mon["boosts"].items()
                }
        elif tag == "-invertboost":
            mon = active()
            if mon is not None:
                mon["boosts"] = {k: -v for k, v in mon["boosts"].items()}
        elif tag == "move" and len(parts) > 3:
            apply_move(parts)
        elif tag == "-end" and len(parts) > 3 and normalize_id(parts[3]) == "illusion":
            # `|-end|{side}a: Zoroark|Illusion` es la linea publica que declara
            # que el activo tenia Illusion. poke-env llega al mismo valor por
            # la regla del dex (Zoroark solo puede tener esa ability), pero la
            # linea es evidencia por si misma: si la ventana de frames arranca
            # despues del `|replace|`, esto sigue alcanzando.
            mon = active()
            if mon is not None:
                reveal_ability(mon, "illusion")
        elif tag == "-item" and len(parts) > 3:
            mon = named_target(parts[2])
            if mon is not None:
                remember_item(mon, normalize_id(parts[3]))
        elif tag == "-enditem":
            mon = named_target(parts[2])
            if mon is not None:
                remember_item(mon, None)
        elif tag == "-ability" and len(parts) > 3:
            mon = named_target(parts[2])
            if mon is not None:
                # Trace: poke-env corrige la base a "trace" ANTES de aplicar
                # el override temporal con la ability copiada
                # (`abstract_battle.py:781-792`, "correcting for bad PS
                # ordering of logs"). 170 lineas reales en el corpus con
                # este sufijo, de 3323 lineas `-ability` en total.
                if any(
                    p.strip().startswith("[from] ability: Trace")
                    for p in parts[4:]
                ):
                    if normalize_id(mon.get("ability") or "") != "trace":
                        identidad = canon(normalize_id(mon.get("species") or ""))
                        entry = persistent_state.get(identidad)
                        if entry is not None:
                            entry.pop("ability", None)
                        mon["ability"] = None
                        reveal_ability(mon, "trace")
                reveal_ability(mon, parts[3])
        elif tag == "-endability":
            mon = named_target(parts[2])
            if mon is not None:
                # Restaura la base persistente si habia un override activo;
                # si no lo hay, no toca nada -- `temporary_ability = None`
                # en un pokemon sin override es un no-op tambien en poke-env.
                identidad = canon(normalize_id(mon.get("species") or ""))
                entry = persistent_state.get(identidad)
                if entry is not None and "ability" in entry:
                    mon["ability"] = entry["ability"]

    return projected


def _transformed_move(move: dict, gen: Any) -> dict:
    """Un movimiento copiado por Transform, con el PP que le da poke-env.

    `Move(..., from_transform=True)` topea `max_pp` en 5 desde gen 5
    (`move.py:477-478`) y arranca `current_pp` en `min(5, max_pp)`
    (`move.py:114`). El `max_pp` del objetivo que llega aca es el sin topear,
    asi que aplicar el min reproduce exactamente los dos valores.
    """
    max_pp = move.get("max_pp")
    if isinstance(gen, int) and gen >= 5 and isinstance(max_pp, int):
        capped = min(5, max_pp)
        return {"id": move.get("id"), "pp": capped, "max_pp": capped}
    return {"id": move.get("id"), "pp": None, "max_pp": None}


def _level_from_details(details: str) -> int | None:
    for chunk in details.split(","):
        chunk = chunk.strip()
        if chunk.startswith("L") and chunk[1:].isdigit():
            return int(chunk[1:])
    # Showdown omite el nivel cuando es 100.
    return 100 if details.strip() else None


def _apply_side(
    projected: dict,
    parts: list[str],
    opponent_side: str,
    vocabulary: ObservableVocabulary,
    tag: str,
) -> None:
    """`|-sidestart|p2: Nombre|move: Spikes`.

    Replica exactamente `AbstractBattle._side_start`: las condiciones
    apilables cuentan capas, el resto guarda el turno en que empezaron.
    """
    owner = parts[2].split(":", 1)[0]
    key = "opponent_side" if owner == opponent_side else "my_side"
    name = vocabulary.side_condition_name(parts[3])
    if name is None:
        return
    conditions = projected["field"][key]
    if tag == "-sideend":
        conditions.pop(name, None)
    elif vocabulary.side_condition_is_stackable(parts[3]):
        conditions[name] = conditions.get(name, 0) + 1
    elif name not in conditions:
        conditions[name] = projected.get("turn")
