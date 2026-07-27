import os
import re

import pytest
from sqlalchemy import text

from ludex_agent.config import load_settings
from ludex_agent.cli import play
from ludex_agent.db.repository import BattleRepository
from ludex_agent.db.session import make_engine, session_factory
from ludex_agent.state.schema import STATE_SCHEMA_VERSION

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="necesita postgres y el server local de showdown",
)


def _normalizar(texto: str) -> str:
    """Normaliza para comparar especie (id de poke-env) contra protocolo crudo.

    poke-env normaliza `species` a un identificador sin espacios ni puntuacion
    (p.ej. "Mr. Mime" -> "mrmime", "Farfetch'd" -> "farfetchd"). El protocolo
    crudo conserva la puntuacion tal como la manda el server. Sin quitar TODA
    la puntuacion (no solo "-" y " "), especies como Mr. Mime dan un falso
    positivo de fuga: revisado empiricamente en battle-gen6randombattle-18,
    turno 9 del protocolo, donde "Mr. Mime" aparece pero el chequeo original
    (que solo sacaba "-" y " ") no lo encontraba por el punto.
    """
    return re.sub(r"[^a-z0-9]", "", texto.lower())


def _clave_de_busqueda(accion: str) -> str:
    """Reduce el id de Hidden Power a una clave que SI puede aparecer en el
    protocolo (fix-final, residual de C1, causa 2).

    poke-env distingue las 17 variantes de Hidden Power por tipo
    (`hiddenpowerground`, `hiddenpowergrass`, ...: son 17 ids de movimiento
    DISTINTOS, cada uno con su propio `.id`), pero Showdown narra la
    ejecucion siempre como `|move|...|Hidden Power|...`, sin el tipo — el
    tipo real es un dato oculto del set del pokemon (IVs), no algo que la
    narracion publica revele. Normalizado, `hiddenpowerground` nunca es
    substring de `hiddenpower`: comparar el id completo contra el protocolo
    no matchea NUNCA, para ninguna de las 17 variantes, aunque la accion se
    haya ejecutado. Verificado que esto es un defecto de esta comparacion y
    no un bug del agente (6 batallas frescas: 5 Hidden Power elegidas, 5
    ejecutadas segun el protocolo).

    Recorta especificamente el prefijo "hiddenpower" en vez de pelar
    cualquier sufijo de tipo en general: ya nos mordio una vez en el seed
    tratar estas 17 variantes como si compartieran un unico id base para
    otra cosa. Especifico a Hidden Power, no un pelado generico de sufijos
    que podria de nuevo matchear de mas contra otro movimiento.
    """
    if accion.startswith("hiddenpower"):
        return "hiddenpower"
    return accion


def _propio_no_actuo(lineas: list[str], side: str) -> bool:
    """Causas 1 y 1bis del residual de C1 (fix-final-report.md): el
    protocolo puede confirmar, sin narrar la accion elegida, que el juego
    impidio que se ejecutara. Evidencia corroborante contra el protocolo
    crudo (D17), no una excepcion a dedo.

    - `|cant|{side}a:`: el propio pokemon quiso actuar y el juego se lo
      impidio (dormido, paralizado, congelado). Es la causa mas frecuente
      del residual medido sobre 6 batallas frescas (316 filas: 296
      alineadas, 20 en el residual).
    - `|faint|{side}a:` que aparece ANTES que cualquier `|move|{side}a:` en
      el mismo bloque: el propio pokemon se debilito por un rival mas rapido
      antes de que le tocara actuar. Showdown no deja ningun otro rastro de
      esto (D20) — ni `|cant|`, ni `|-fail|`, nada — asi que la ausencia de
      un `|move|` propio ANTES del `|faint|` es la unica evidencia posible.
    """
    prefix_cant = f"|cant|{side}a:"
    prefix_move = f"|move|{side}a:"
    prefix_faint = f"|faint|{side}a:"
    if any(l.startswith(prefix_cant) for l in lineas):
        return True
    for l in lineas:
        if l.startswith(prefix_move):
            return False
        if l.startswith(prefix_faint):
            return True
    return False


# --- I4 (review de merge): la fuga se verifica en TODOS los campos que el
# serializador persiste del rival, no solo `species`. Helpers especificos por
# campo, porque cada uno se revela con una sintaxis de protocolo distinta y
# ninguno tolera un simple "aparece en algun lado del blob" sin arriesgar
# falsos positivos (ver docstrings de cada uno). ---

_BOOST_OPS = {
    "-boost", "-unboost", "-setboost", "-swapboost", "-clearboost",
    "-invertboost", "-copyboost", "-clearpositiveboost", "-clearnegativeboost",
}
_STATUS_OPS = {"status", "-status", "-curestatus"}


def _ident_normalizado(parts: list[str]) -> str | None:
    """Extrae y normaliza el nombre de pokemon de un campo `{side}a: Nombre`
    (segundo o tercer campo segun la linea, ya separado por `|`)."""
    if len(parts) < 3:
        return None
    return _normalizar(parts[2].split(": ", 1)[-1])


def _es_esa_especie(ident_normalizado: str | None, especie_normalizada: str) -> bool:
    """Compara el ident del protocolo (SIEMPRE nombre BASE, D22) contra la
    especie completa del estado (que SI incluye la forma, p.ej.
    `rotomwash`/`arceuspoison`). El ident base siempre es un prefijo de la
    especie con forma: `rotom` de `rotomwash`, `arceus` de `arceuspoison`."""
    if not ident_normalizado:
        return False
    return especie_normalizada.startswith(ident_normalizado)


def _boost_revelado(lineas_crudas: list[str], side: str, especie_normalizada: str) -> bool:
    """Un boost/unboost de CUALQUIER stat sobre este pokemon es evidencia de
    que su estado de boosts no es un dato inventado: la MAGNITUD acumulada
    en `boosts` es una funcion deterministica de eventos ya publicos
    (`-boost`/`-unboost`/... pueden aplicarse varias veces), asi que pedir
    coincidencia de una linea con el valor exacto acumulado no tiene sentido
    -- lo que hay que confirmar es que ALGO le paso a ESTE pokemon en el
    canal publico, no re-derivar la aritmetica.

    No se usa substring sobre texto normalizado (los nombres de stat como
    "atk"/"spa" son demasiado cortos y aparecen sueltos en cualquier lado):
    se parsea la linea cruda por `|` y se exige el prefijo `{side}a:` mas el
    nombre de pokemon, igual que `_actor_matches` en `client.py`.
    """
    for linea in lineas_crudas:
        parts = linea.split("|")
        if len(parts) < 3 or parts[1] not in _BOOST_OPS:
            continue
        if not parts[2].startswith(f"{side}a:"):
            continue
        if _es_esa_especie(_ident_normalizado(parts), especie_normalizada):
            return True
    return False


def _status_revelado(
    lineas_crudas: list[str], side: str, especie_normalizada: str, status_abbr: str
) -> bool:
    """El status se revela via `|status|`/`|-status|`/`|-curestatus|`
    (`{side}a: Nombre|abr`) o, sin ninguna de esas lineas todavia, con el
    campo HP/condicion de `|switch|`/`|drag|` (`"331/331 slp"`, `"0 fnt"`):
    el ULTIMO token de esa condicion es el status abreviado cuando lo hay.

    `FNT` (fainted) es un caso aparte: poke-env lo persiste como el `status`
    del pokemon debilitado (ademas del booleano `fainted`, que ya cubre esto
    por su cuenta), pero Showdown lo narra con `|faint|{side}a: Nombre`, sin
    ningun campo de status -- nunca un `|status|FNT` ni un "0 fnt" si el
    debilitamiento no vino acompañado de un `|switch|`/`|drag|` posterior.

    No se usa substring sobre texto normalizado: los codigos de 3 letras
    (`par`, `brn`, `tox`) son sustrings comunes de palabras que no tienen
    nada que ver con el status (falso positivo de "revelado" garantizado).
    """
    abbr = status_abbr.lower()
    for linea in lineas_crudas:
        parts = linea.split("|")
        if len(parts) < 3:
            continue
        op = parts[1]
        if not parts[2].startswith(f"{side}a:"):
            continue
        if not _es_esa_especie(_ident_normalizado(parts), especie_normalizada):
            continue
        if op in _STATUS_OPS and len(parts) > 3 and parts[3].lower() == abbr:
            return True
        if op in ("switch", "drag") and len(parts) > 4:
            tokens = parts[4].split()
            if tokens and tokens[-1].lower() == abbr:
                return True
        if op == "faint" and abbr == "fnt":
            return True
    return False


def _nivel_revelado(lineas_crudas: list[str], nivel: int) -> bool:
    """El nivel aparece en el campo `details` de `|switch|`/`|drag|` como
    `L{nivel}` (p.ej. "Cloyster, L78, F"). Se busca con limite de palabra
    sobre la linea CRUDA (no normalizada): sin el limite, nivel 7 seria un
    substring de "l78" y daria un falso "revelado"."""
    patron = re.compile(rf"(?<![A-Za-z0-9])[Ll]{nivel}(?![0-9])")
    return any(patron.search(linea) for linea in lineas_crudas)


def _tipos_por_cambio_dinamico(
    lineas_crudas: list[str], side: str, especie_normalizada: str
) -> list[list[str]]:
    """TODOS los `|-start|{side}a: Nombre|typechange|Tipo1/Tipo2|...` para
    este pokemon hasta este turno, en orden, si los hay.

    Protean/Libero (cambian el tipo propio al del movimiento que se va a
    usar), Reflect Type, Soak y Camouflage narran TODOS con esta misma linea
    de protocolo -- Showdown no distingue el mecanismo en el nombre del
    evento, asi que no hace falta distinguirlo aca tampoco. Verificado sobre
    datos reales: Greninja (Protean) cambia de Agua/Siniestro a Agua puro
    (`|-start|p2a: Greninja|typechange|Water|[from] ability: Protean`) y
    despues a Veneno puro, ambos explicitamente narrados -- comparar contra
    el tipo ESTATICO del dex ahi habria marcado una fuga (o un "bug de
    forma") sobre un cambio de tipo perfectamente publico.

    Devuelve la lista COMPLETA de cambios, no solo el ultimo: el `state` de
    una fila puede quedar un turno atras del ultimo cambio (la misma clase de
    desfase de un turno que D20/D22 documentan para `turn_number` -- el
    cambio narrado en el bloque del turno N todavia no esta reflejado en el
    `state` de la decision de ESE mismo turno, y recien aparece en la
    siguiente). Verificado: Greninja cambia a "Water" puro en el turno 7,
    pero el `state` de la fila turno=7 todavia muestra Agua/Siniestro (el
    tipo de ANTES del cambio) y recien en turno=8 muestra "Water" solo. El
    tipo vigente ANTES de un cambio sigue siendo un candidato valido por eso.
    """
    resultado: list[list[str]] = []
    prefix = f"|-start|{side}a:"
    for linea in lineas_crudas:
        if not linea.startswith(prefix):
            continue
        parts = linea.split("|")
        if len(parts) < 5 or parts[3] != "typechange":
            continue
        if not _es_esa_especie(_ident_normalizado(parts), especie_normalizada):
            continue
        resultado.append([t.strip().lower() for t in parts[4].split("/") if t.strip()])
    return resultado


def _dex_lookup(especie: str, dex: dict[str, dict]) -> dict | None:
    """Datos de dex (tipos, cantidad de habilidades posibles) para una
    especie, con fallback para formas puramente cosmeticas.

    Formas como los colores de Florges, las letras de Unown, los patrones de
    Vivillon, los peinados de Furfrou o las estaciones de Sawsbuck comparten
    stats/tipos/habilidad con su base y el seed (D6/D7) no les genera fila
    propia -- verificado contra la base: `florgesblue`, `unownc`,
    `vivillonsun`, `furfroukabuki`, `sawsbuckautumn`, `gastrodoneast` y
    `floetteeternal` son las unicas especies del dataset sin fila directa, y
    las 7 son cosmeticas. Se cae a la fila cuyo id sea el prefijo mas largo
    del id compuesto.
    """
    if especie in dex:
        return dex[especie]
    candidatos = [b for b in dex if especie.startswith(b) and b != especie]
    if not candidatos:
        return None
    return dex[max(candidatos, key=len)]


def _forma_mega_revelada(
    lineas_crudas: list[str], side: str, especie_normalizada: str
) -> str | None:
    """Si el protocolo muestra a ESTE pokemon mega-evolucionando
    (`|-mega|{side}a: Nombre|Especie|Piedra`), devuelve el id de dex de la
    forma MEGA (`scizormega`, `charizardmegax`/`megay` segun la piedra).

    Necesario porque `mon['species']` de poke-env no siempre se actualiza a
    la forma mega en el mismo instante en que `mon['ability']` ya refleja la
    habilidad mega (medido: `battle-gen6randombattle-?`, Scizor
    mega-evolucionado en el turno 18 del protocolo -- `|-mega|p2a:
    Scizor|Scizor|Scizorite` -- con `species` todavia en `"scizor"` pero
    `ability` ya en `"technician"`, la UNICA habilidad de Scizor-Mega). Sin
    esto, `_dex_lookup("scizor", ...)` encuentra las 3 habilidades de la
    forma BASE y la inferencia legitima se rechaza como si fuera una fuga.
    """
    prefix = f"|-mega|{side}a:"
    for linea in lineas_crudas:
        if not linea.startswith(prefix):
            continue
        parts = linea.split("|")
        if len(parts) < 5:
            continue
        if not _es_esa_especie(_ident_normalizado(parts), especie_normalizada):
            continue
        base = _normalizar(parts[3])
        piedra = parts[4].strip()
        if piedra.endswith(" X"):
            return f"{base}megax"
        if piedra.endswith(" Y"):
            return f"{base}megay"
        return f"{base}mega"
    return None


def _objetivos_de_transform(
    lineas_crudas: list[str], side: str, especie_normalizada: str
) -> list[str]:
    """TODOS los objetivos de Transform/Imposter de este pokemon hasta este
    turno (no solo el ultimo), como nombres normalizados.

    Cada `|-transform|{side}a: Nombre|{lado_del_objetivo}a: Objetivo|...`
    revela con QUIEN se copio. En singles el objetivo es SIEMPRE el pokemon
    activo del lado contrario en ese momento -- no hay otro al que copiar.
    Cuando el que transforma es del RIVAL, el objetivo es MI propio
    pokemon, cuyo moveset/ability/tipos/boosts ya me son conocidos (estan
    en `estado["me"]`, nunca ocultos): Ditto rival copiando a mi Espeon
    (`|-transform|p2a: Ditto|p1a: Espeon|[from] ability: Imposter`) revela
    `moves` con Freeze-Dry -- no es informacion nueva del rival, es mi
    propio equipo repetido bajo otro nombre.

    Se devuelven TODOS, no solo el ultimo, por la misma razon que
    `_tipos_por_cambio_dinamico` guarda todos los cambios de tipo: un
    pokemon con Imposter puede entrar y salir varias veces en la MISMA
    trayectoria, transformandose en un objetivo distinto cada vez, y el
    `state` de una fila puede quedar capturado ANTES de que el ULTIMO
    cambio de esta secuencia de switch-in/transform/faint termine de
    resolverse dentro del mismo bloque de turno (verificado sobre datos
    reales: Ditto se transforma en Vaporeon y se debilita por quemadura en
    el MISMO bloque del turno 44, pero el `state` de esa fila todavia
    muestra la ability `lightningrod` de un Sceptile-Mega copiado varios
    turnos antes). El objetivo vigente puede ser CUALQUIERA de los
    copiados hasta ahora, no necesariamente el cronologicamente ultimo.
    """
    prefix = f"|-transform|{side}a:"
    objetivos: list[str] = []
    for linea in lineas_crudas:
        if not linea.startswith(prefix):
            continue
        parts = linea.split("|")
        if len(parts) < 4:
            continue
        if not _es_esa_especie(_ident_normalizado(parts), especie_normalizada):
            continue
        objetivo = _normalizar(parts[3].split(": ", 1)[-1])
        if objetivo not in objetivos:
            objetivos.append(objetivo)
    return objetivos


@pytest.fixture(scope="module")
async def jugadas():
    # I6 (review final): esta fixture juega batallas REALES contra el server
    # local y las persiste en el MISMO Postgres que el dataset de
    # entrenamiento. `source="test"` las marca sinteticas (D19,
    # migracion 20260727000007) para que sean excluibles con
    # `source <> 'test'` en vez de mezclarse en silencio.
    return await play(2, "gen6randombattle", source="test")


async def test_persiste_batallas_turnos_y_pasos(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                bid = (await s.execute(text(
                    "SELECT id FROM battles WHERE battle_tag=:t"), {"t": tag})).scalar_one()
                turnos = (await s.execute(text(
                    "SELECT count(*) FROM battle_turns WHERE battle_id=:b"),
                    {"b": bid})).scalar_one()
                pasos = (await s.execute(text("""
                    SELECT count(*) FROM trajectory_steps ts
                    JOIN trajectories t ON t.id = ts.trajectory_id WHERE t.battle_id = :b
                """), {"b": bid})).scalar_one()
                assert turnos > 0 and pasos > 0
    finally:
        await engine.dispose()


async def test_el_reward_esta_propagado(jugadas):
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            sin_reward = (await s.execute(text("""
                SELECT count(*) FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags) AND ts.reward IS NULL
            """), {"tags": list(jugadas)})).scalar_one()
            assert sin_reward == 0
    finally:
        await engine.dispose()


async def test_no_hay_fuga_de_informacion_del_rival(jugadas):
    """LA propiedad de correccion de esta rebanada.

    Para cada turno N, ningun pokemon del rival puede estar en el estado si el
    protocolo no lo revelo hasta ese turno. El protocolo persistido es el juez.
    Si un modelo se entrena con informacion que un jugador no tiene, es inutil
    en batalla real.

    I4 (review de merge): la version anterior de este test solo miraba
    `species`. El serializador persiste ademas, del rival, `moves` (id),
    `item`, `ability`, `level`, `types`, `boosts` y `status` -- una fuga por
    cualquiera de esos siete pasaba en verde. Se cubren los ONCE campos de la
    lista blanca de `serializer.py` salvo `hp_fraction`/`active`/`fainted`
    (numericos/booleanos derivados de la simulacion, no un dato que Showdown
    "revele" en una linea) y `stats` (nunca se persiste del rival: cubierto
    aparte por la review anterior, D18).

    Dos campos NO se verifican contra el protocolo porque son INFERENCIAS
    LEGITIMAS, no fugas -- verificado a mano por la review anterior para
    `ability` (310 casos sin linea de protocolo, todos de especies con
    habilidad unica o megas ya reveladas) y generalizado aca con una regla
    unica respaldada por el dex (`pokemon.abilities`, D1): una especie con
    UNA sola habilidad posible (columna `abilities` con una unica clave) no
    necesita narracion -- y una mega evolucionada SIEMPRE tiene exactamente
    una habilidad posible en el dex, asi que la misma regla cubre el caso de
    Blastoise-Mega/Slowbro-Mega sin necesitar una lista de especies a mano.
    `types` es directamente 100% determinado por la especie (dato de dex, no
    de batalla): una vez que la especie esta revelada (ya verificado arriba),
    sus tipos no son un canal de fuga aparte, y se verifica por CONSISTENCIA
    contra el dex en vez de buscar una linea de protocolo que en general no
    existe.

    Invariante de TODO el dataset (review final, I1): antes filtraba por
    `battle_tag = ANY(:tags)`, es decir solo esta corrida. Corrido sin ese
    filtro habria encontrado I1 el dia que paso (un lote de protocolo perdido
    en silencio en una batalla vieja). `jugadas` sigue siendo un parametro
    para forzar que la fixture juegue algo antes de verificar.
    """
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            dex_rows = (await s.execute(text("""
                SELECT showdown_id, types, abilities FROM pokemon
                WHERE gen_id = (SELECT id FROM generations WHERE gen_number = 6)
            """))).all()
            dex = {
                r[0]: {"types": [t.lower() for t in r[1]], "abilities": r[2]}
                for r in dex_rows
            }
            assert dex, "el dex de gen 6 esta vacio: no se puede verificar types/ability"

            filas = (await s.execute(text("""
                SELECT ts.turn_number, ts.state, t.player_side, t.battle_id
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                ORDER BY t.battle_id, ts.turn_number
            """))).all()
            assert filas, "no hay pasos que verificar"

            revisados = {
                "species": 0, "moves": 0, "item": 0, "ability": 0,
                "level": 0, "types": 0, "boosts": 0, "status": 0,
            }
            for turno, estado, side, battle_id in filas:
                # Se comparan LINEAS sueltas, no el protocolo concatenado. Pegar
                # todo y sacarle los separadores crea un blob donde una especie
                # puede "aparecer" a caballo entre dos tokens sin relacion, y una
                # fuga real pasaria como revelada.
                lineas_crudas = (await s.execute(text("""
                    SELECT unnest(protocol_lines) FROM battle_turns
                    WHERE battle_id = :b AND player_side = :ps AND turn_number <= :t
                """), {"b": battle_id, "ps": side, "t": turno})).scalars().all()
                # `|request|` trae el equipo PROPIO completo, ability/item/
                # moves incluidos (D17, protocol.py): nunca es evidencia de
                # que algo del RIVAL se revelo. Sin excluirlo, una habilidad
                # o item que casualmente coincide con la de mi PROPIO
                # pokemon (p.ej. mi Breloom con Technician, mismo nombre que
                # el Scizor del rival) se cuenta como "revelada" por una
                # linea que no tiene nada que ver con el rival -- el mismo
                # riesgo de blob que motiva comparar linea por linea, pero
                # DENTRO de una sola linea gigante en vez de entre dos.
                lineas = [l for l in lineas_crudas if not l.startswith("|request|")]
                normalizadas = [_normalizar(l) for l in lineas]
                # El rival es SIEMPRE el lado contrario a `player_side`: si la
                # trayectoria es de p1, el rival narra sus acciones con el
                # prefijo `p2a:` (y viceversa). `_boost_revelado`/
                # `_status_revelado` necesitan el prefijo del RIVAL, no el
                # propio.
                rival_side = "p2" if side == "p1" else "p1"

                for mon in estado["opponent"]["pokemon"]:
                    especie = _normalizar(mon["species"])

                    # --- species ---
                    revisados["species"] += 1
                    visto = any(especie in linea for linea in normalizadas)
                    assert visto, (
                        f"FUGA: {mon['species']} aparece en el estado del turno "
                        f"{turno} pero el protocolo no lo revelo hasta ahi"
                    )

                    # Transform/Imposter: este pokemon puede haber copiado a
                    # MI activo (en singles, el UNICO objetivo posible es el
                    # pokemon activo del lado contrario), y puede haberlo
                    # hecho VARIAS veces (entra, se transforma, sale, entra
                    # de nuevo y se transforma en otro). Mi propio moveset/
                    # ability/tipos/boosts NUNCA son informacion oculta, asi
                    # que sirven como fuente legitima para lo que Transform
                    # copia (moves, ability, types, boosts -- nunca item, que
                    # Transform no toca). `mis_objetivos` son TODOS los
                    # dicts de mi equipo que fueron copiados hasta este
                    # turno -- no solo el ultimo (ver docstring de
                    # `_objetivos_de_transform`: el `state` puede quedar
                    # capturado antes de que el ultimo cambio del bloque
                    # termine de resolverse).
                    objetivos_transform = _objetivos_de_transform(lineas, rival_side, especie)
                    mis_objetivos = [
                        mio for mio in estado["me"]["pokemon"]
                        if any(
                            _es_esa_especie(obj, _normalizar(mio["species"]))
                            for obj in objetivos_transform
                        )
                    ]
                    moves_del_objetivo = (
                        {_normalizar(m["id"]) for mio in mis_objetivos for m in mio["moves"]}
                        if mis_objetivos else None
                    )

                    # --- moves[].id ---
                    for mv in mon["moves"]:
                        revisados["moves"] += 1
                        clave_normalizada = _normalizar(mv["id"])
                        clave = _clave_de_busqueda(clave_normalizada)
                        visto = any(clave in linea for linea in normalizadas)
                        if not visto and moves_del_objetivo is not None:
                            visto = clave_normalizada in moves_del_objetivo
                        assert visto, (
                            f"FUGA: el movimiento {mv['id']!r} de "
                            f"{mon['species']} aparece en el estado del turno "
                            f"{turno} pero el protocolo no lo revelo hasta ahi "
                            "y no es un Transform/Imposter de mi propio pokemon"
                        )

                    # --- item (unknown_item es el centinela de poke-env
                    # para "no revelado": lo opuesto de una fuga, Minor 4 de
                    # la review de merge) ---
                    if mon["item"] and mon["item"] != "unknown_item":
                        revisados["item"] += 1
                        clave = _normalizar(mon["item"])
                        visto = any(clave in linea for linea in normalizadas)
                        assert visto, (
                            f"FUGA: el item {mon['item']!r} de {mon['species']} "
                            f"aparece en el estado del turno {turno} pero el "
                            "protocolo no lo revelo hasta ahi"
                        )

                    # --- ability: revelado en protocolo, inferencia legitima
                    # (una sola habilidad posible en el dex -- cubre tanto
                    # especies de habilidad unica como CUALQUIER mega
                    # evolucionada, que en el dex siempre tiene una sola), o
                    # copiada por Transform/Imposter de MI pokemon ---
                    if mon["ability"]:
                        revisados["ability"] += 1
                        clave = _normalizar(mon["ability"])
                        visto = any(clave in linea for linea in normalizadas)
                        if not visto and mis_objetivos:
                            visto = any(
                                _normalizar(mio["ability"] or "") == clave
                                for mio in mis_objetivos
                            )
                        if not visto:
                            entrada = _dex_lookup(especie, dex)
                            es_inferencia = (
                                entrada is not None and len(entrada["abilities"]) == 1
                            )
                            if not es_inferencia:
                                # `mon['species']` puede no haberse actualizado
                                # todavia a la forma mega aunque `ability` ya
                                # la refleje (ver `_forma_mega_revelada`).
                                forma_mega = _forma_mega_revelada(
                                    lineas, rival_side, especie
                                )
                                if forma_mega is not None:
                                    entrada_mega = _dex_lookup(forma_mega, dex)
                                    es_inferencia = (
                                        entrada_mega is not None
                                        and len(entrada_mega["abilities"]) == 1
                                    )
                            assert es_inferencia, (
                                f"FUGA: la habilidad {mon['ability']!r} de "
                                f"{mon['species']} no aparece en el protocolo "
                                f"del turno {turno} y no es una inferencia "
                                "legitima (el dex le permite mas de una "
                                "habilidad posible, ni siquiera mega-evolucionada, "
                                "ni copiada por Transform/Imposter)"
                            )

                    # --- level: revelado en protocolo, o nivel 100 (Showdown
                    # omite "Lxx" en los detalles cuando el nivel es 100 --
                    # verificado contra la base: ni una sola linea "L100" en
                    # toda la tabla `battle_turns`, y SI hay filas con
                    # `level=100`, asi que la omision es el formato, no una
                    # casualidad del dataset) ---
                    revisados["level"] += 1
                    nivel = mon["level"]
                    visto = _nivel_revelado(lineas, nivel)
                    assert visto or nivel == 100, (
                        f"FUGA: el nivel {nivel} de {mon['species']} no "
                        f"aparece en el protocolo del turno {turno} y no es "
                        "el default 100 que Showdown omite"
                    )

                    # --- types: dato de dex, no de batalla (con dos
                    # excepciones, ambas explicitamente narradas). Una vez
                    # que la especie esta revelada (ya verificado arriba),
                    # sus tipos no agregan un canal de fuga propio -- se
                    # verifica CONSISTENCIA contra el dex, no presencia en
                    # protocolo. Excepcion 1: una mega evolucion puede
                    # cambiar el tipo (Altaria Dragon/Volador -> Dragon/Hada
                    # mega) antes de que `mon['species']` refleje la forma
                    # mega, igual que `ability`. Excepcion 2: Protean/Libero/
                    # Reflect Type/Soak/Camouflage narran un cambio de tipo
                    # DINAMICO con `-start|typechange` (ver
                    # `_tipos_por_cambio_dinamico`) -- ahi el tipo revelado
                    # explicitamente en el protocolo manda sobre el dex.
                    revisados["types"] += 1
                    tipos_estado = sorted(t.lower() for t in mon["types"])
                    candidatos: list[list[str]] = []
                    cambios = _tipos_por_cambio_dinamico(lineas, rival_side, especie)
                    for cambio in cambios:
                        candidatos.append(sorted(cambio))

                    entrada = _dex_lookup(especie, dex)
                    assert entrada is not None, (
                        f"sin fila de dex para {mon['species']} ({especie}): "
                        "no se puede verificar `types`"
                    )
                    candidatos.append(sorted(entrada["types"]))
                    forma_mega = _forma_mega_revelada(lineas, rival_side, especie)
                    entrada_mega = _dex_lookup(forma_mega, dex) if forma_mega else None
                    if entrada_mega is not None:
                        candidatos.append(sorted(entrada_mega["types"]))
                    for mio in mis_objetivos:
                        # Transform copia el tipo del objetivo (mi propio
                        # pokemon): tampoco es informacion oculta.
                        candidatos.append(sorted(t.lower() for t in mio["types"]))

                    # El tipo vigente puede ser el dex base, la forma mega, el
                    # copiado por Transform, o CUALQUIERA de los cambios
                    # dinamicos narrados hasta este turno -- no solo el
                    # ultimo: `state` puede quedar un turno atras del cambio
                    # mas reciente (ver docstring de `_tipos_por_cambio_
                    # dinamico`), asi que el tipo previo al ultimo cambio
                    # sigue siendo un candidato legitimo.
                    assert tipos_estado in candidatos, (
                        f"FUGA: los tipos {tipos_estado} de {mon['species']} en "
                        f"el turno {turno} no coinciden con ninguno de los "
                        f"candidatos revelados/de dex {candidatos}: o es un bug "
                        "de forma, o es una fuga de un tipo que la especie no "
                        "tiene"
                    )

                    # --- boosts: la MAGNITUD acumulada no es substring-
                    # comparable (ver docstring de `_boost_revelado`); se
                    # exige evidencia de que ALGO le cambio los stats a ESTE
                    # pokemon, no que el numero exacto aparezca en una linea.
                    # Transform tambien copia los boosts vigentes del
                    # objetivo (mi propio pokemon) al momento de copiarlo.
                    boosts_no_cero = {k: v for k, v in mon["boosts"].items() if v}
                    if boosts_no_cero:
                        revisados["boosts"] += 1
                        visto = _boost_revelado(lineas, rival_side, especie)
                        if not visto and mis_objetivos:
                            visto = any(
                                mio["boosts"].get(k) == v
                                for mio in mis_objetivos
                                for k, v in boosts_no_cero.items()
                            )
                        assert visto, (
                            f"FUGA: {mon['species']} tiene boosts "
                            f"{boosts_no_cero} en el turno {turno} sin "
                            "ninguna linea de protocolo (boost/unboost/...) "
                            "que le cambie los stats a este pokemon, ni "
                            "coinciden con un Transform de mi propio pokemon"
                        )

                    # --- status ---
                    if mon["status"]:
                        revisados["status"] += 1
                        visto = _status_revelado(lineas, rival_side, especie, mon["status"])
                        assert visto, (
                            f"FUGA: el status {mon['status']!r} de "
                            f"{mon['species']} aparece en el estado del turno "
                            f"{turno} pero el protocolo no lo revelo hasta ahi"
                        )

            # Canario: sin esto, un serializador que dejara `opponent.pokemon`
            # siempre vacio (o un campo siempre en blanco/cero) haria que el
            # loop correspondiente no verificara nada y el test pasara en
            # verde igual. Cada campo tiene su propio piso: `species`/
            # `moves`/`level`/`types` no dependen de que la partida haya
            # tenido items, boosts o estados alterados, pero SI dependen de
            # que la fixture haya jugado batallas reales.
            print(f"I4 - campos y valores del rival verificados: {revisados}")
            for campo, cuenta in revisados.items():
                assert cuenta > 0, (
                    f"'{campo}' del rival: 0 valores verificados, el test no "
                    "ejercio nada para este campo"
                )
    finally:
        await engine.dispose()


async def test_la_version_de_esquema_esta_en_todas_las_filas(jugadas):
    """Invariante de TODO el dataset (review final): sin filtro de tags."""
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            distintas = (await s.execute(text(
                "SELECT DISTINCT state_schema_version FROM trajectory_steps"
            ))).scalars().all()
            assert distintas == [STATE_SCHEMA_VERSION]
    finally:
        await engine.dispose()


async def test_repersistir_la_misma_batalla_no_duplica(jugadas):
    """Idempotencia REAL: se vuelve a guardar una batalla ya guardada.

    La version anterior de este test solo contaba filas de una unica corrida,
    donde cada battle_tag es unico por construccion del loop: pasaba en verde
    aunque el ON CONFLICT estuviera roto o ausente. Para ejercer la garantia
    hay que reescribir la MISMA batalla y verificar que no aparece una fila
    nueva y que devuelve el mismo id.
    """
    engine = make_engine(load_settings().database_url)
    try:
        factory = session_factory(engine)
        repo = BattleRepository(factory)
        tag = jugadas[0]
        async with factory() as s:
            antes = (await s.execute(
                text("SELECT count(*) FROM battles"))).scalar_one()
            # `winner` TIENE que venir en el SELECT y volver tal cual: el
            # ON CONFLICT hace `SET winner = EXCLUDED.winner`, asi que mandar
            # None aca borraria el ganador real de una batalla ya jugada. Este
            # test verifica idempotencia; no debe destruir el dato que verifica.
            fila = (await s.execute(text(
                "SELECT id, format, p1, p2, winner FROM battles WHERE battle_tag = :t"),
                {"t": tag})).one()

        # Canario: sin esto la asercion de abajo se cumple sola cuando el
        # ganador ya viene en NULL, que es justo el estado que este test
        # provocaba antes. Una batalla de `jugadas` termino: tiene ganador.
        assert fila[4] is not None, "la batalla recien jugada debe tener ganador"

        de_nuevo = await repo.save_battle(
            battle_tag=tag, fmt=fila[1], p1=fila[2], p2=fila[3],
            winner=fila[4], source="test", played_by="bot",
        )

        async with factory() as s:
            ganador = (await s.execute(text(
                "SELECT winner FROM battles WHERE battle_tag = :t"),
                {"t": tag})).scalar_one()
        assert ganador == fila[4], "re-persistir no debe alterar el ganador"

        async with factory() as s:
            despues = (await s.execute(
                text("SELECT count(*) FROM battles"))).scalar_one()

        assert de_nuevo == fila[0], "el mismo battle_tag debe devolver el mismo id"
        assert despues == antes, "re-persistir no debe crear una fila nueva"
    finally:
        await engine.dispose()


async def test_la_accion_de_la_fila_corresponde_a_su_propio_turno(jugadas):
    """C1: `action_taken` de la fila del turno N tiene que aparecer en el
    protocolo del turno N, no del N+1.

    Antes del arreglo, poke-env llama a `choose_move` en cuanto parsea el
    `|request|`, que Showdown manda ya resuelto y en su propio frame, antes de
    narrar el turno. La accion que se graba en la fila `turno=N` en realidad
    se ejecuta en el bloque de protocolo N+1. Ningun otro test lo detecta
    porque ninguno cruza `action_taken` contra el protocolo del MISMO turno.

    fix-final (residual medido sobre 6 batallas frescas, 316 filas: 296
    alineadas, 20 en el residual): de esas 20, 2 causas resultaron ser falsos
    positivos de ESTE chequeo, no del agente, y se excusan explicitamente
    (`_propio_no_actuo`, `_clave_de_busqueda`) con evidencia corroborante del
    protocolo (D17) — nunca a dedo, sin evidencia. Una tercera causa (fila
    corrida un turno, el mismo defecto de C1 pero residual: ver D20 y la
    entrada final de fix-final-report.md) NO se excusa: si sobrevive, este
    test tiene que seguir fallando por ella.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT ts.turn_number,
                       regexp_replace(
                           lower(coalesce(ts.action_taken->>'id', ts.action_taken->>'species')),
                           '[^a-z0-9]', '', 'g') AS accion,
                       t.player_side, t.battle_id
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE b.battle_tag = ANY(:tags) AND ts.action_taken IS NOT NULL
            """), {"tags": list(jugadas)})).all()
            assert filas, "no hay pasos con accion para verificar"

            revisados = 0
            for turno, accion, side, battle_id in filas:
                lineas = (await s.execute(text("""
                    SELECT unnest(protocol_lines) FROM battle_turns
                    WHERE battle_id = :b AND player_side = :ps AND turn_number = :t
                """), {"b": battle_id, "ps": side, "t": turno})).scalars().all()
                candidatas = [
                    _normalizar(l) for l in lineas
                    if l.startswith(f"|move|{side}a:") or l.startswith(f"|switch|{side}a:")
                ]
                revisados += 1
                clave = _clave_de_busqueda(accion)
                visto = any(clave in linea for linea in candidatas)
                excusado = not visto and _propio_no_actuo(lineas, side)
                assert visto or excusado, (
                    f"la accion '{accion}' de la fila turno={turno} no aparece "
                    f"en el protocolo de ESE turno (bloque {turno}), lado {side}"
                )

            assert revisados > 0, (
                "ninguna fila tenia accion para verificar: el test no verifico nada"
            )
    finally:
        await engine.dispose()


async def test_los_switches_grabados_cierran_contra_el_protocolo(jugadas):
    """C2: la cantidad de decisiones de switch grabadas tiene que cerrar
    contra los `|switch|` propios del protocolo, descontando la entrada
    inicial del team preview (que es un `|switch|` en el protocolo pero no
    una decision de la politica).

    Antes del arreglo, `save_step` upsertea por `(trajectory_id, turn_number)`
    y un cambio forzado tras un debilitamiento no avanza el turno: la segunda
    decision pisa a la primera y el cambio forzado desaparece en silencio.
    """
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            for tag in jugadas:
                battle_id, side = (await s.execute(text("""
                    SELECT t.battle_id, t.player_side FROM trajectories t
                    JOIN battles b ON b.id = t.battle_id WHERE b.battle_tag = :tag
                """), {"tag": tag})).one()

                switches_protocolo = (await s.execute(text("""
                    SELECT count(*) FROM battle_turns bt, LATERAL unnest(bt.protocol_lines) l
                    WHERE bt.battle_id = :b AND bt.player_side = :ps
                      AND l LIKE '|switch|' || :ps || 'a:%'
                """), {"b": battle_id, "ps": side})).scalar_one()

                switches_grabados = (await s.execute(text("""
                    SELECT count(*) FROM trajectory_steps ts
                    JOIN trajectories t ON t.id = ts.trajectory_id
                    WHERE t.battle_id = :b AND t.player_side = :ps
                      AND ts.action_taken ->> 'kind' = 'switch'
                """), {"b": battle_id, "ps": side})).scalar_one()

                assert switches_grabados == switches_protocolo - 1, (
                    f"{tag}: {switches_protocolo} switches en el protocolo, "
                    f"{switches_grabados} grabados como decision (se espera "
                    f"{switches_protocolo - 1}, descontando la entrada inicial "
                    "del team preview)"
                )
    finally:
        await engine.dispose()


async def test_cada_paso_de_estado_tiene_su_protocolo(jugadas):
    """La propiedad que hace REVERSIBLE al serializador.

    Si manana se descubre que el serializador tenia un defecto, el historico se
    re-deriva desde el protocolo crudo en vez de descartarse. Eso solo vale si
    CADA paso de estado tiene su protocolo, del mismo jugador y del mismo turno.
    Contar filas de cada lado por separado no alcanza: hay que verificar la
    correspondencia turno a turno.

    Invariante de TODO el dataset (review final, I1): antes filtraba por
    `battle_tag = ANY(:tags)` y solo habria detectado I1 si el lote perdido
    hubiera sido de esta misma corrida. Corrido sin el filtro es la unica
    manera de que este test hubiera atrapado I1 el dia que paso.
    """
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            huerfanos = (await s.execute(text("""
                SELECT count(*) FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                WHERE NOT EXISTS (
                      SELECT 1 FROM battle_turns bt
                      WHERE bt.battle_id = t.battle_id
                        AND bt.player_side = t.player_side
                        AND bt.turn_number = ts.turn_number)
            """))).scalar_one()
        assert huerfanos == 0, (
            f"{huerfanos} pasos de estado sin su protocolo crudo: el historico "
            "de esas batallas no se podria re-derivar"
        )
    finally:
        await engine.dispose()


async def test_la_accion_tomada_esta_dentro_de_su_propia_mascara(jugadas):
    """C-1 (review de merge): `action_taken` tiene que estar SIEMPRE dentro de
    `legal_actions` de su PROPIA fila. Es una asercion de una linea sobre la
    base, y es la que faltaba para que la materializacion diferida de C1
    (commit `3ea7caf`) no hubiera llegado a mergear: esa task de fondo puede
    fotografiar el punto de decision SIGUIENTE (`battle` ya avanzo cuando la
    task por fin corre), y la fila queda con la accion de una decision y la
    mascara de otra. No es ruido: es una etiqueta imposible de entrenar.

    Invariante de TODO el dataset (mismo patron que
    `test_cada_paso_de_estado_tiene_su_protocolo`/`test_no_hay_fuga...`): sin
    filtro de `battle_tag`, porque el defecto que esto tiene que atrapar ya
    vive en filas de corridas anteriores a esta, y filtrar por `tags` de la
    corrida actual las dejaria pasar en silencio (asi es como escaparon del
    filtro `WHERE battle_tag = ANY(:tags)` de otros tests la primera vez).
    """
    assert jugadas, "la fixture no jugo nada"
    engine = make_engine(load_settings().database_url)
    try:
        async with session_factory(engine)() as s:
            filas = (await s.execute(text("""
                SELECT b.battle_tag, ts.decision_index, ts.turn_number, ts.action_taken
                FROM trajectory_steps ts
                JOIN trajectories t ON t.id = ts.trajectory_id
                JOIN battles b ON b.id = t.battle_id
                WHERE ts.action_taken IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements(ts.legal_actions) la
                      WHERE la->>'kind' = ts.action_taken->>'kind'
                        AND coalesce(la->>'id', '') = coalesce(ts.action_taken->>'id', '')
                        AND coalesce(la->>'species', '') = coalesce(ts.action_taken->>'species', '')
                  )
            """))).all()
        assert filas == [], (
            f"{len(filas)} fila(s) con action_taken fuera de su propia "
            f"legal_actions: {filas}"
        )
    finally:
        await engine.dispose()
