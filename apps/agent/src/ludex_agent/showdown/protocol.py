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
import re
import time
from collections import defaultdict, deque
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


class ProjectionTimeoutError(RuntimeError):
    """La narracion previa no llego dentro del presupuesto.

    Es fallo CERRADO a proposito: no se decide con estado stale ni se
    persiste una fila degradada. Si una fila existe, su proyeccion es
    valida por construccion.
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
    "-item", "-enditem", "-ability", "-endability",
    "-weather", "-fieldstart", "-fieldend", "-sidestart", "-sideend",
    "-activate", "upkeep", "turn", "win", "tie",
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


# Cuantos frames se retienen por batalla EN VUELO. Una decision mira uno o
# dos frames mas alla de su cursor (el `|request|` y la narracion que lo
# sigue), asi que 128 son dos ordenes de magnitud de margen; sin tope, una
# corrida de miles de batallas acumula cada frame de cada una para siempre.
# Desalojar nunca puede devolver una respuesta equivocada: si el frame que
# seguia al cursor ya se fue, `wait_for_resolution` falla CERRADO (ver ahi).
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
        self, tag: str, *, after_seq: int, timeout: float
    ) -> RawFrame:
        """Primer frame de resolucion con `seq > after_seq`.

        `after_seq` es el cursor capturado SINCRONICAMENTE al empezar la
        decision, asi que dos decisiones nunca pueden consumir el mismo
        frame: cada una espera mas alla del suyo.
        """
        cond = self._cond(tag)
        try:
            async with asyncio.timeout(timeout):
                async with cond:
                    await cond.wait_for(
                        lambda: (
                            self._lost_frames_after(tag, after_seq)
                            or self._first_resolution_after(tag, after_seq) is not None
                            or tag in self._closed
                        )
                    )
        except TimeoutError as exc:
            raise ProjectionTimeoutError(
                f"la narracion previa de {tag} no llego en {timeout}s "
                f"(cursor={after_seq}): no se decide con estado stale"
            ) from exc
        if self._lost_frames_after(tag, after_seq):
            raise ProjectionTimeoutError(
                f"{tag} desalojo frames posteriores a cursor={after_seq} "
                f"(tope {self._max_frames}): no se puede demostrar cual es la "
                "narracion de esta decision"
            )
        frame = self._first_resolution_after(tag, after_seq)
        if frame is None:
            raise ProjectionTimeoutError(
                f"{tag} cerro sin narracion de resolucion despues de "
                f"cursor={after_seq}"
            )
        return frame


class ObservableVocabulary(Protocol):
    """Traduce nombres del protocolo a la representacion de poke-env.

    Esta INYECTADA para que este modulo no importe poke-env: el proyector se
    testea sin levantar nada. Y porque las inferencias legitimas tienen que
    quedar ancladas al dex o a los enums de la libreria, nunca a una lista a
    mano (ver .claude/agent-recording/SKILL.md).
    """

    def species_types(self, species_id: str) -> list[str]: ...
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


def _new_mon(species: str, vocabulary: ObservableVocabulary) -> dict[str, Any]:
    """Un rival recien revelado.

    `item`/`ability`/`moves` quedan desconocidos a proposito: el protocolo no
    los trae y completarlos seria inventar informacion que el jugador no
    tiene. Los tipos SI salen del dex de la generacion, que es una inferencia
    legitima y anclada, no una lista a mano.
    """
    return {
        "species": species,
        "hp_fraction": 1.0,
        "active": False,
        "fainted": False,
        "status": None,
        "level": None,
        "item": "unknown_item",
        "ability": None,
        "types": vocabulary.species_types(species),
        "boosts": _blank_boosts(),
        "moves": [],
    }


def project_observable_state(
    snapshot: dict,
    lines: tuple[str, ...],
    *,
    opponent_side: str,
    vocabulary: ObservableVocabulary,
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
    """
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
        """El activo de NUESTRO lado, leido del snapshot propio.

        Es informacion que ya tenemos: se usa solo para las inferencias que
        copian de un pokemon nuestro (Reflect Type con `[of]`, Transform).
        """
        for mon in (snapshot.get("me") or {}).get("pokemon", []):
            if mon.get("active"):
                return mon
        return None

    def mon_for_ident(ident: str) -> dict | None:
        """Singles: el sufijo `a` ES la ranura activa de ese lado."""
        if ident.startswith(active_prefix):
            return active()
        return own_active()

    def switch_in(species: str, hp_field: str, details: str) -> None:
        for mon in team:
            mon["active"] = False
        existing = next(
            (m for m in team if normalize_id(m.get("species") or "") == species), None
        )
        mon = existing if existing is not None else _new_mon(species, vocabulary)
        if existing is None:
            team.append(mon)
        mon["active"] = True
        # Un cambio resetea los boosts: no arrastrar los del anterior.
        mon["boosts"] = _blank_boosts()
        # Y resetea los tipos a los del dex: `switch_out` limpia
        # `_temporary_types` en poke-env (`pokemon.py:612`), asi que un
        # typechange (Protean, Reflect Type, Transform) no sobrevive al cambio.
        mon["types"] = vocabulary.species_types(species)
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
        """
        illusioned = active()
        if illusioned is None:
            return
        if normalize_id(illusioned.get("species") or "") == species:
            # poke-env corta en seco: `if illusionist_mon is illusioned`.
            return
        real = next(
            (m for m in team if normalize_id(m.get("species") or "") == species), None
        )
        if real is None:
            real = _new_mon(species, vocabulary)
            team.append(real)
        real["active"] = True
        real["types"] = vocabulary.species_types(species)
        level = _level_from_details(details)
        if level is not None:
            real["level"] = level
        real["hp_fraction"] = illusioned.get("hp_fraction")
        real["status"] = illusioned.get("status")
        real["fainted"] = illusioned.get("fainted", False)

        illusioned["active"] = False
        illusioned["hp_fraction"] = 0.0
        illusioned["status"] = None
        illusioned["fainted"] = False
        illusioned["boosts"] = _blank_boosts()

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
        if any(part.strip() == "[from] ability: Imposter" for part in parts[4:]):
            mon["ability"] = "imposter"
        source_species = normalize_id(source.get("species") or "")
        types = vocabulary.species_types(source_species)
        if types:
            mon["types"] = types
        mon["boosts"] = {**_blank_boosts(), **(source.get("boosts") or {})}
        source_ability = source.get("ability")
        if source_ability:
            mon["ability"] = source_ability
        gen = snapshot.get("gen")
        mon["moves"] = [
            _transformed_move(move, gen) for move in (source.get("moves") or [])
        ]

    def forme_change(forme: str) -> None:
        """`detailschange` (Mega/Primal) y `-formechange`.

        Cambia los TIPOS pero NO `species`, porque es exactamente lo que hace
        poke-env: `Pokemon.forme_change()` llama a `_update_from_pokedex(...,
        store_species=False)` (`pokemon.py:431-433`), asi que `mon.species`
        sigue siendo la forma base tras una Mega evolucion.

        Verificado sobre datos reales: en `battle-gen6randombattle-1896` T3
        llega `|detailschange|p2a: Slowbro|Slowbro-Mega, L81, M` y las filas
        posteriores de poke-env siguen diciendo `slowbro`. Si aca se escribiera
        `slowbromega`, la proyeccion contradiria al resto del dataset dentro de
        la MISMA batalla.
        """
        mon = active()
        if mon is None:
            return
        mon["types"] = vocabulary.species_types(forme)

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
        elif tag in ("detailschange", "-formechange") and len(parts) > 3:
            forme_change(normalize_id(parts[3].split(",", 1)[0]))
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
        elif tag in ("-clearboost", "-clearallboost"):
            mon = active()
            if mon is not None:
                mon["boosts"] = _blank_boosts()
        elif tag == "move" and len(parts) > 3:
            mon = active()
            if mon is not None:
                move_id = normalize_id(parts[3])
                # Hidden Power: Showdown narra siempre "Hidden Power", nunca
                # el tipo. El tipo real es dato oculto (IVs).
                if move_id.startswith("hiddenpower"):
                    move_id = "hiddenpower"
                if move_id and not any(m.get("id") == move_id for m in mon["moves"]):
                    # pp/max_pp en null: "no derivable de esta evidencia
                    # publica" (schema v2). No es cero ni un PP faltante por
                    # error.
                    mon["moves"].append(
                        {"id": move_id, "pp": None, "max_pp": None}
                    )
        elif tag == "-item" and len(parts) > 3:
            mon = active()
            if mon is not None:
                mon["item"] = normalize_id(parts[3])
        elif tag == "-enditem":
            mon = active()
            if mon is not None:
                mon["item"] = None
        elif tag == "-ability" and len(parts) > 3:
            mon = active()
            if mon is not None:
                mon["ability"] = normalize_id(parts[3])
        elif tag == "-endability":
            mon = active()
            if mon is not None:
                mon["ability"] = None

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
