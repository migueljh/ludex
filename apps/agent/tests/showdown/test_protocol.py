from ludex_agent.showdown.protocol import ProtocolRecorder


def _split(raw: str) -> list[str]:
    return raw.split("|")


def test_agrupa_las_lineas_por_turno():
    r = ProtocolRecorder()
    r.record([_split("|init|battle"), _split("|turn|1")])
    r.record([_split("|move|p1a: Beautifly|Bug Buzz"), _split("|turn|2")])
    r.record([_split("|switch|p2a: Magnezone")])

    assert r.turns() == [0, 1, 2]
    assert "|init|battle" in r.lines_for_turn(0)
    assert "|move|p1a: Beautifly|Bug Buzz" in r.lines_for_turn(1)
    assert "|switch|p2a: Magnezone" in r.lines_for_turn(2)


def test_la_linea_de_turno_abre_el_turno_nuevo():
    # |turn|N marca el comienzo del turno N: la linea pertenece a N, no a N-1.
    r = ProtocolRecorder()
    r.record([_split("|turn|1")])
    assert "|turn|1" in r.lines_for_turn(1)
    assert "|turn|1" not in r.lines_for_turn(0)


def test_conserva_el_orden_dentro_del_turno():
    r = ProtocolRecorder()
    r.record([_split("|turn|1"), _split("|a"), _split("|b"), _split("|c")])
    assert r.lines_for_turn(1) == ["|turn|1", "|a", "|b", "|c"]


def test_preserva_el_request_con_mi_equipo():
    # El |request| trae MI equipo y es lo que permite re-derivar el estado.
    # Perderlo romperia la re-derivacion.
    r = ProtocolRecorder()
    req = '|request|{"active":[{"moves":[{"id":"bugbuzz"}]}]}'
    r.record([_split(req)])
    assert req in r.lines_for_turn(0)


def test_un_turno_sin_lineas_devuelve_vacio():
    assert ProtocolRecorder().lines_for_turn(7) == []


def test_all_lines_devuelve_todo_en_orden():
    r = ProtocolRecorder()
    r.record([_split("|a"), _split("|turn|1"), _split("|b")])
    assert r.all_lines == ["|a", "|turn|1", "|b"]


# --- D31 (MON-6): camino pre-lock ------------------------------------------

import asyncio

import pytest

from ludex_agent.showdown.protocol import (
    MAX_RETAINED_FRAMES,
    ProjectionTimeoutError,
    RawFrameInbox,
    is_resolution_frame,
    project_observable_state,
)


class FakeVocabulary:
    """Doble del dex. Que `protocol.py` no importe poke-env es lo que permite
    testear el proyector sin levantar nada."""

    TIPOS = {
        "latias": ["DRAGON", "PSYCHIC"],
        "mandibuzz": ["DARK", "FLYING"],
        "zoroark": ["DARK"],
        "charizard": ["FIRE", "FLYING"],
        # Mega-X, no Mega-Y: Mega-Y es FIRE/FLYING igual que la base, asi que
        # con ella la asercion "los tipos SI cambian" pasaba sola.
        "charizardmegax": ["FIRE", "DRAGON"],
        "ditto": ["NORMAL"],
        "tentacruel": ["WATER", "POISON"],
        "ludicolo": ["WATER", "GRASS"],
    }

    def species_types(self, species_id):
        return list(self.TIPOS.get(species_id, []))

    def type_name(self, raw):
        return raw.strip().upper().replace(" ", "_")

    def weather_name(self, raw):
        return None if raw.lower() == "none" else raw.upper()

    def field_name(self, raw):
        return raw.replace("move: ", "").upper().replace(" ", "_")

    def side_condition_name(self, raw):
        return raw.replace("move: ", "").upper().replace(" ", "_")

    def side_condition_is_stackable(self, raw):
        return "spikes" in raw.lower()


def _snapshot(**overrides):
    base = {
        "schema_version": 2,
        "turn": 3,
        "player_role": "p1",
        "legal_actions": [{"kind": "move", "id": "sludgebomb"}],
        "me": {"pokemon": [{"species": "tentacruel", "active": True}]},
        "opponent": {"pokemon": [{
            "species": "ludicolo", "hp_fraction": 1.0, "active": True,
            "fainted": False, "status": None, "level": 88,
            "item": "unknown_item", "ability": None,
            "types": ["WATER", "GRASS"],
            "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                       "evasion": 0, "accuracy": 0},
            "moves": [{"id": "energyball", "pp": 15, "max_pp": 16}],
        }]},
        "field": {"weather": {}, "field_effects": {},
                  "my_side": {}, "opponent_side": {}},
    }
    base.update(overrides)
    return base


def _proyectar(lines, snapshot=None):
    return project_observable_state(
        snapshot or _snapshot(), tuple(lines),
        opponent_side="p2", vocabulary=FakeVocabulary(),
    )


# --- is_resolution_frame ---


def test_un_frame_con_narracion_completa_la_espera():
    assert is_resolution_frame((
        ">battle-x", "|", "|t:|123",
        "|switch|p2a: Latias|Latias, L77, F|100/100", "|upkeep", "|turn|3",
    ))


def test_el_frame_de_un_cambio_forzado_completa_sin_turn():
    """Medido: las 7 decisiones con forceSwitch de la sonda tienen su frame
    narrativo con turn=None — el bloque cierra en el `|faint|`. Una regla que
    exigiera `|turn|` colgaria en cada cambio forzado."""
    frame = (
        ">battle-x", "|", "|t:|123",
        "|move|p2a: Galvantula|Giga Drain|p1a: Torkoal",
        "|-damage|p1a: Torkoal|0 fnt",
        "|faint|p1a: Torkoal",
    )
    assert not any(l.startswith("|turn|") for l in frame)
    assert is_resolution_frame(frame)


@pytest.mark.parametrize("frame", [
    (">battle-x", "|c:|1785|☆Rival|hola"),
    (">battle-x", "|inactive|Rival has 30 seconds left."),
    (">battle-x", "|inactiveoff|Battle timer is OFF."),
    (">battle-x", "|j|☆Rival"),
    (">battle-x", "|l|☆Rival"),
    (">battle-x", "|t:|1785186829"),
    (">battle-x", "|request|{\"active\":[]}"),
    (">battle-x", "|error|[Invalid choice] Blah"),
    (">battle-x", "|popup|algo"),
    (">battle-x", "|init|battle"),
])
def test_chat_y_ruido_nunca_completan_la_espera(frame):
    """Lista BLANCA, no lista negra: si un frame de chat pudiera completar la
    espera, la decision se tomaria sin la narracion y volveria el desfase."""
    assert not is_resolution_frame(frame)


# --- RawFrameInbox ---


async def test_el_inbox_devuelve_la_primera_narracion_posterior_al_cursor():
    inbox = RawFrameInbox()
    tag = "battle-x"
    req = await inbox.publish(tag, ("|request|{}",))
    await inbox.publish(tag, ("|c:|1785|☆Rival|hola",))
    await inbox.publish(tag, ("|switch|p2a: Latias|Latias, L77, F|100/100",))

    frame = await inbox.wait_for_resolution(tag, after_seq=req.seq, timeout=1)
    assert frame.lines[0].startswith("|switch|")


async def test_dos_decisiones_no_consumen_el_mismo_frame():
    inbox = RawFrameInbox()
    tag = "battle-x"
    r1 = await inbox.publish(tag, ("|request|1",))
    n1 = await inbox.publish(tag, ("|move|p2a: A|Tackle|p1a: B",))
    r2 = await inbox.publish(tag, ("|request|2",))
    n2 = await inbox.publish(tag, ("|move|p2a: A|Surf|p1a: B",))

    assert (await inbox.wait_for_resolution(tag, after_seq=r1.seq, timeout=1)).seq == n1.seq
    assert (await inbox.wait_for_resolution(tag, after_seq=r2.seq, timeout=1)).seq == n2.seq


async def test_el_inbox_espera_a_una_narracion_que_todavia_no_llego():
    inbox = RawFrameInbox()
    tag = "battle-x"

    async def publicar_tarde():
        await asyncio.sleep(0.02)
        await inbox.publish(tag, ("|faint|p2a: Latias",))

    asyncio.create_task(publicar_tarde())
    frame = await inbox.wait_for_resolution(tag, after_seq=0, timeout=2)
    assert frame.lines[0].startswith("|faint|")


async def test_el_inbox_falla_cerrado_al_vencer_el_timeout():
    inbox = RawFrameInbox()
    with pytest.raises(ProjectionTimeoutError):
        await inbox.wait_for_resolution("battle-x", after_seq=0, timeout=0.01)


async def test_cerrar_la_batalla_despierta_al_que_espera():
    """Sin esto, la ultima decision de una batalla que termina quedaria
    esperando hasta el timeout."""
    inbox = RawFrameInbox()
    tag = "battle-x"

    async def cerrar():
        await asyncio.sleep(0.02)
        await inbox.close(tag)

    asyncio.create_task(cerrar())
    with pytest.raises(ProjectionTimeoutError):
        await inbox.wait_for_resolution(tag, after_seq=0, timeout=2)


async def test_el_inbox_acota_cuantos_frames_retiene_por_batalla():
    """Sin tope, una corrida larga acumula cada frame de cada batalla en
    memoria para siempre: la espera solo mira uno o dos frames hacia atras."""
    inbox = RawFrameInbox(max_frames=4)
    for i in range(20):
        await inbox.publish("battle-x", (f"|turn|{i}",))
    assert inbox.retained("battle-x") == 4


async def test_el_inbox_falla_cerrado_si_desalojo_el_frame_del_cursor():
    """El tope no puede volverse una respuesta EQUIVOCADA en silencio.

    Si el frame que seguia al cursor ya se desalojo, el primer frame de
    resolucion que queda retenido NO es necesariamente el que le corresponde a
    esta decision. Devolverlo seria exactamente el defecto que D31 arregla, con
    el tope como causa nueva. Falla cerrado, igual que el timeout.
    """
    inbox = RawFrameInbox(max_frames=2)
    cursor = (await inbox.publish("battle-x", ("|t:|1",))).seq
    for i in range(3):
        await inbox.publish("battle-x", (f"|turn|{i + 2}",))
    with pytest.raises(ProjectionTimeoutError, match="desaloj"):
        await inbox.wait_for_resolution("battle-x", after_seq=cursor, timeout=0.01)


async def test_cerrar_la_batalla_libera_los_frames_retenidos():
    inbox = RawFrameInbox()
    await inbox.publish("battle-x", ("|turn|1",))
    await inbox.close("battle-x")
    assert inbox.retained("battle-x") == 0


async def test_el_tope_por_defecto_no_estorba_a_una_batalla_normal():
    """Canario: el tope tiene que estar dos ordenes de magnitud por encima de
    lo que una decision real necesita mirar (1-2 frames)."""
    inbox = RawFrameInbox()
    cursor = (await inbox.publish("battle-x", ("|t:|1",))).seq
    for i in range(64):
        await inbox.publish("battle-x", (f"|turn|{i + 2}",))
    frame = await inbox.wait_for_resolution(
        "battle-x", after_seq=cursor, timeout=0.5
    )
    # La relacion (el frame del cursor sigue alcanzable) Y el valor exacto: si
    # el tope bajara a 64 el `retained` cambia y este test lo dice, en vez de
    # quedar del lado correcto de una desigualdad.
    assert frame.lines == ("|turn|2",)
    assert inbox.retained("battle-x") == 65
    assert MAX_RETAINED_FRAMES == 128, "poke-env 0.15.0, gen6randombattle"


# --- project_observable_state ---


def test_proyecta_el_switch_in_rival_con_hp_y_nivel():
    out = _proyectar([
        "|switch|p2a: Latias|Latias, L77, F|100/100",
        "|-damage|p2a: Latias|76/100",
    ])
    activos = [p for p in out["opponent"]["pokemon"] if p["active"]]
    assert len(activos) == 1
    latias = activos[0]
    assert latias["species"] == "latias"
    assert latias["hp_fraction"] == 0.76
    assert latias["level"] == 77
    assert latias["types"] == ["DRAGON", "PSYCHIC"]
    # El anterior deja de estar activo pero no se pierde.
    ludicolo = next(p for p in out["opponent"]["pokemon"] if p["species"] == "ludicolo")
    assert ludicolo["active"] is False


def test_no_muta_el_snapshot_de_entrada():
    snapshot = _snapshot()
    _proyectar(["|switch|p2a: Latias|Latias, L77, F|100/100"], snapshot)
    assert snapshot["opponent"]["pokemon"][0]["species"] == "ludicolo"
    assert snapshot["opponent"]["pokemon"][0]["active"] is True
    assert len(snapshot["opponent"]["pokemon"]) == 1


def test_no_toca_la_mascara_ni_mi_lado():
    out = _proyectar([
        "|switch|p2a: Latias|Latias, L77, F|100/100",
        "|-damage|p1a: Tentacruel|10/268",
    ])
    assert out["legal_actions"] == [{"kind": "move", "id": "sludgebomb"}]
    assert out["me"]["pokemon"][0]["species"] == "tentacruel"
    assert out["player_role"] == "p1"


def test_nunca_lee_una_linea_de_request():
    """El request es privado. Si el proyector lo consumiera, el rival podria
    completarse con informacion que el jugador no tiene."""
    import json

    request = json.dumps({
        "side": {"id": "p2", "pokemon": [
            {"ident": "p2: Salamence", "active": True,
             "moves": ["outrage", "roost"], "item": "lifeorb",
             "ability": "intimidate"},
        ]},
    })
    out = _proyectar([f"|request|{request}"])
    especies = [p["species"] for p in out["opponent"]["pokemon"]]
    assert especies == ["ludicolo"]
    assert "salamence" not in str(out)


def test_revela_movimiento_rival_con_pp_desconocido():
    """Schema v2: `null` significa "no derivable de esta evidencia publica",
    no cero ni PP faltante por error."""
    out = _proyectar(["|move|p2a: Ludicolo|Giga Drain|p1a: Tentacruel"])
    ludicolo = out["opponent"]["pokemon"][0]
    assert {"id": "gigadrain", "pp": None, "max_pp": None} in ludicolo["moves"]
    # Lo ya revelado con PP real se conserva.
    assert {"id": "energyball", "pp": 15, "max_pp": 16} in ludicolo["moves"]


def test_hidden_power_se_revela_sin_el_tipo():
    """Showdown narra "Hidden Power" sin el tipo: el tipo sale de los IVs y es
    dato oculto. Guardar `hiddenpowerice` seria inventar informacion."""
    out = _proyectar(["|move|p2a: Ludicolo|Hidden Power|p1a: Tentacruel"])
    ids = [m["id"] for m in out["opponent"]["pokemon"][0]["moves"]]
    assert "hiddenpower" in ids
    assert not any(i.startswith("hiddenpower") and i != "hiddenpower" for i in ids)


def test_no_duplica_un_movimiento_ya_revelado():
    out = _proyectar([
        "|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
        "|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
    ])
    ids = [m["id"] for m in out["opponent"]["pokemon"][0]["moves"]]
    assert ids.count("energyball") == 1


def test_proyecta_status_boosts_item_y_ability():
    out = _proyectar([
        "|-status|p2a: Ludicolo|brn",
        "|-boost|p2a: Ludicolo|spa|2",
        "|-unboost|p2a: Ludicolo|def|1",
        "|-item|p2a: Ludicolo|Life Orb",
        "|-ability|p2a: Ludicolo|Rain Dish",
    ])
    mon = out["opponent"]["pokemon"][0]
    assert mon["status"] == "BRN"
    assert mon["boosts"]["spa"] == 2
    assert mon["boosts"]["def"] == -1
    assert mon["item"] == "lifeorb"
    assert mon["ability"] == "raindish"


def test_los_boosts_se_topean_en_seis():
    out = _proyectar(["|-boost|p2a: Ludicolo|spa|5", "|-boost|p2a: Ludicolo|spa|5"])
    assert out["opponent"]["pokemon"][0]["boosts"]["spa"] == 6


def test_un_cambio_resetea_los_boosts():
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["boosts"]["spa"] = 4
    out = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
    ], snapshot)
    ludicolo = next(p for p in out["opponent"]["pokemon"] if p["species"] == "ludicolo")
    assert ludicolo["boosts"]["spa"] == 0


def test_faint_marca_debilitado():
    out = _proyectar(["|-damage|p2a: Ludicolo|0 fnt", "|faint|p2a: Ludicolo"])
    mon = out["opponent"]["pokemon"][0]
    assert mon["hp_fraction"] == 0.0
    assert mon["fainted"] is True


def test_illusion_replace_corrige_la_identidad_sin_perder_el_daño():
    """Sin `|replace|`, toda batalla con Zoroark cuenta mal: el `|switch|`
    original miente la especie."""
    out = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|-damage|p2a: Mandibuzz|60/100",
        "|replace|p2a: Zoroark|Zoroark, L84, M",
    ])
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["species"] == "zoroark"
    assert activo["types"] == ["DARK"]
    assert activo["hp_fraction"] == 0.6


def test_illusion_conserva_al_imitado_como_miembro_inactivo():
    """Paridad con `AbstractBattle._end_illusion_on` (`abstract_battle.py:
    409-427`): el imitado NO desaparece del equipo, sale del campo.

    Renombrar la entrada activa (lo que hacia la version anterior) borraba a
    Mandibuzz del equipo rival: el `|switch|` que la revelo como miembro es
    evidencia publica y la fila pasaba a decir que el rival tiene un pokemon
    menos. poke-env llama a `illusioned.was_illusioned(...)` -> hp/status a
    None y `switch_out` -> `active=False` y boosts limpios.
    """
    snapshot = _snapshot()
    out = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|-boost|p2a: Mandibuzz|atk|2",
        "|-damage|p2a: Mandibuzz|60/100",
        "|-status|p2a: Mandibuzz|brn",
        "|replace|p2a: Zoroark|Zoroark, L84, M",
    ], snapshot)
    especies = [p["species"] for p in out["opponent"]["pokemon"]]
    assert "mandibuzz" in especies, "el imitado sigue siendo miembro revelado"
    assert "zoroark" in especies
    imitado = next(p for p in out["opponent"]["pokemon"] if p["species"] == "mandibuzz")
    assert imitado["active"] is False
    assert imitado["status"] is None, "el quemado fue a Zoroark, no a Mandibuzz"
    assert imitado["boosts"]["atk"] == 0, "switch_out limpia los boosts"
    # `current_hp_fraction` devuelve 0 cuando `_current_hp` es None
    # (`pokemon.py:988-995`), asi que la fila de poke-env para el imitado dice
    # 0.0 con `fainted` en False. Se replica para no contradecir a las filas
    # que esa misma batalla serializa desde poke-env.
    assert imitado["hp_fraction"] == 0.0
    assert imitado["fainted"] is False


def test_illusion_activa_al_real_con_nivel_hp_y_status_del_campo():
    out = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|-damage|p2a: Mandibuzz|60/100",
        "|-status|p2a: Mandibuzz|brn",
        "|replace|p2a: Zoroark|Zoroark, L77, M",
    ])
    real = next(p for p in out["opponent"]["pokemon"] if p["species"] == "zoroark")
    assert real["active"] is True
    assert real["level"] == 77, "el nivel sale del `details` del |replace|"
    assert real["hp_fraction"] == 0.6, "el daño lo recibio Zoroark"
    assert real["status"] == "BRN"
    assert real["types"] == ["DARK"]


def test_illusion_no_hereda_item_ni_ability_del_imitado():
    """Lo que el `|switch|` disfrazado dejo atribuido al imitado no puede
    viajar a Zoroark por el solo hecho de romperse la Illusion: son dos
    entradas distintas del equipo, no un renombre."""
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"] = [{
        **snapshot["opponent"]["pokemon"][0],
        "species": "mandibuzz", "types": ["DARK", "FLYING"],
        "item": "leftovers", "ability": "overcoat",
        "moves": [{"id": "foulplay", "pp": None, "max_pp": None}],
    }]
    out = _proyectar(["|replace|p2a: Zoroark|Zoroark, L84, M"], snapshot)
    real = next(p for p in out["opponent"]["pokemon"] if p["species"] == "zoroark")
    assert real["item"] == "unknown_item"
    assert real["ability"] is None
    assert real["moves"] == []


def test_replace_del_mismo_pokemon_no_duplica_la_entrada():
    """poke-env corta en seco cuando `illusionist_mon is illusioned`
    (`abstract_battle.py:418-419`)."""
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"] = [{
        **snapshot["opponent"]["pokemon"][0], "species": "zoroark",
        "types": ["DARK"], "hp_fraction": 0.4,
    }]
    out = _proyectar(["|replace|p2a: Zoroark|Zoroark, L84, M"], snapshot)
    assert [p["species"] for p in out["opponent"]["pokemon"]] == ["zoroark"]
    assert out["opponent"]["pokemon"][0]["hp_fraction"] == 0.4
    assert out["opponent"]["pokemon"][0]["active"] is True


def test_detailschange_de_mega_cambia_los_tipos_pero_no_la_especie():
    """poke-env NO guarda la especie en un cambio de forma:
    `Pokemon.forme_change()` llama a `_update_from_pokedex(...,
    store_species=False)`. Escribir `charizardmegax` aca haria que la
    proyeccion contradiga al resto del dataset dentro de la misma batalla
    (medido en `battle-gen6randombattle-1896`: tras el detailschange a
    Slowbro-Mega, poke-env sigue diciendo `slowbro`).

    Mega-X y no Mega-Y a proposito: Charizard-Mega-Y es FIRE/FLYING, igual
    que la base, asi que la mitad "los tipos SI cambian" de este test pasaba
    sin ejercer nada. Mega-X es FIRE/DRAGON.
    """
    out = _proyectar([
        "|switch|p2a: Charizard|Charizard, L79, M|100/100",
        "|detailschange|p2a: Charizard|Charizard-Mega-X, L79, M",
    ])
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["species"] == "charizard", "species NO cambia en una Mega"
    assert activo["types"] == ["FIRE", "DRAGON"], "los tipos SI cambian"


# --- typechange y Transform: inferencias publicas (D31) ---


def test_typechange_proyecta_los_tipos_narrados():
    """Protean/Libero/Camouflage: Showdown narra el tipo nuevo en claro."""
    out = _proyectar(["|-start|p2a: Ludicolo|typechange|Water"])
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["types"] == ["WATER"]


def test_typechange_dual_separa_por_barra():
    out = _proyectar(["|-start|p2a: Ludicolo|typechange|Ghost/Flying"])
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["types"] == ["GHOST", "FLYING"]


def test_typechange_con_of_copia_los_tipos_del_pokemon_citado():
    """Reflect Type: `[of]` nombra de quien se copian. Misma regla que
    `abstract_battle.py:802-809`, que mira exactamente `event[5]`."""
    snapshot = _snapshot(me={"pokemon": [{
        "species": "tentacruel", "active": True, "types": ["WATER", "POISON"],
    }]})
    out = _proyectar([
        "|-start|p2a: Ludicolo|typechange|[from] move: Reflect Type"
        "|[of] p1a: Tentacruel",
    ], snapshot)
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["types"] == ["WATER", "POISON"]


def test_un_cambio_borra_el_typechange():
    """Los tipos temporales no sobreviven al switch: `switch_out` limpia
    `_temporary_types` (`pokemon.py:612`)."""
    out = _proyectar([
        "|-start|p2a: Ludicolo|typechange|Water",
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
    ])
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["species"] == "ludicolo"
    assert activo["types"] == ["WATER", "GRASS"]


def _snapshot_ditto(**mio):
    propio = {
        "species": "tentacruel", "active": True,
        "types": ["WATER", "POISON"], "ability": "liquidooze",
        "boosts": {"spa": 2, "atk": 0, "def": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "sludgebomb", "pp": 12, "max_pp": 16},
                  {"id": "scald", "pp": 3, "max_pp": 24}],
    }
    propio.update(mio)
    snapshot = _snapshot(gen=6, me={"pokemon": [propio]})
    snapshot["opponent"]["pokemon"] = [{
        **snapshot["opponent"]["pokemon"][0], "species": "ditto",
        "types": ["NORMAL"], "moves": [],
    }]
    return snapshot


def test_transform_copia_tipos_boosts_y_movimientos_del_objetivo():
    """Copiar un pokemon PROPIO no es fuga: es informacion que ya tenemos
    (ver .claude/agent-recording/SKILL.md, inferencias legitimas).

    Paridad con `Pokemon.transform()` (`pokemon.py:625-636`): tipos del DEX de
    la especie copiada (no sus tipos actuales), boosts copiados, moveset del
    objetivo, y la especie intacta. El PP de un movimiento copiado es
    `min(5, max_pp)` en gen >= 5 (`move.py:114` y `move.py:477-478`): sale de
    una regla fija, no de informacion oculta.
    """
    out = _proyectar([
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], _snapshot_ditto())
    ditto = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert ditto["species"] == "ditto", "Transform NO cambia la especie"
    assert ditto["types"] == ["WATER", "POISON"]
    assert ditto["boosts"]["spa"] == 2
    assert ditto["moves"] == [
        {"id": "sludgebomb", "pp": 5, "max_pp": 5},
        {"id": "scald", "pp": 5, "max_pp": 5},
    ]


def test_imposter_copia_la_ability_del_objetivo_si_se_conoce():
    out = _proyectar([
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], _snapshot_ditto())
    ditto = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert ditto["ability"] == "liquidooze"


def test_imposter_sin_ability_conocida_revela_imposter():
    """`|[from] ability: Imposter` es la revelacion publica de la ability del
    transformador; poke-env la escribe antes de copiar
    (`abstract_battle.py:1059-1065`)."""
    out = _proyectar([
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], _snapshot_ditto(ability=None))
    ditto = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert ditto["ability"] == "imposter"


def test_transform_sin_gen_conocida_deja_el_pp_en_null():
    """Antes de gen 5 el tope de 5 PP no aplica; sin generacion en el
    snapshot el PP no es derivable y va en null (schema v2)."""
    snapshot = _snapshot_ditto()
    snapshot.pop("gen")
    out = _proyectar([
        "|-transform|p2a: Ditto|p1a: Tentacruel",
    ], snapshot)
    ditto = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert [m["pp"] for m in ditto["moves"]] == [None, None]


def test_transform_de_nuestro_pokemon_no_toca_al_rival():
    """El filtro por lado ya existente tiene que seguir valiendo: un
    `-transform` de NUESTRO Ditto no proyecta nada del rival."""
    snapshot = _snapshot_ditto()
    out = _proyectar([
        "|-transform|p1a: Tentacruel|p2a: Ditto",
    ], snapshot)
    assert out["opponent"]["pokemon"][0]["types"] == ["NORMAL"]
    assert out["opponent"]["pokemon"][0]["moves"] == []


def test_el_turno_sale_del_turn_de_la_narracion():
    out = _proyectar(["|upkeep", "|turn|8"])
    assert out["turn"] == 8


def test_sin_turn_conserva_el_decision_turn_sincronico():
    """Cambio forzado: el bloque cierra en el `|faint|` y nunca trae `|turn|`."""
    out = _proyectar([
        "|move|p2a: Ludicolo|Giga Drain|p1a: Tentacruel",
        "|-damage|p1a: Tentacruel|0 fnt",
        "|faint|p1a: Tentacruel",
    ])
    assert out["turn"] == 3


def test_proyecta_clima_y_hazards_apilables():
    out = _proyectar([
        "|turn|9",
        "|-weather|RainDance",
        "|-sidestart|p2: Rival|move: Spikes",
        "|-sidestart|p2: Rival|move: Spikes",
        "|-sidestart|p1: Ludex|move: Stealth Rock",
    ])
    assert out["field"]["weather"] == {"RAINDANCE": 9}
    # Apilable: cuenta capas, igual que `AbstractBattle._side_start`.
    assert out["field"]["opponent_side"] == {"SPIKES": 2}
    # No apilable: guarda el turno.
    assert out["field"]["my_side"] == {"STEALTH_ROCK": 9}
