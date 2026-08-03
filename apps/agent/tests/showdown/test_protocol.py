from ludex_agent.showdown.protocol import ProtocolRecorder


def _split(raw: str) -> list[str]:
    return raw.split("|")


# --- MON-10/F2-03: identidad de apertura (D36) -------------------------

import pytest

from ludex_agent.showdown.protocol import OpeningIdentityError, compute_opening_identity

BATTLE_TAG = "battle-gen6randombattle-386"


def _p1_opening(**overrides: list[str]) -> list[str]:
    """Apertura tal como la ve p1: HP exacto de su propio activo, porcentual
    del rival. Refleja la forma real observada en el corpus (turno 0 de
    `battle-gen6randombattle-386`: rule/switch/switch mas cabecera)."""
    lines = {
        "t:": ["|t:|1785186819"],
        "gametype": ["|gametype|singles"],
        "player": ["|player|p1|LudexBot3682|101|", "|player|p2|Rival3682|102|"],
        "teamsize": ["|teamsize|p1|6", "|teamsize|p2|6"],
        "gen": ["|gen|6"],
        "tier": ["|tier|[Gen 6] Random Battle"],
        "rule": [
            "|rule|HP Percentage Mod: HP is shown in percentages",
            "|rule|Sleep Clause Mod: Limit one foe put to sleep",
        ],
        "start": ["|start"],
        "switch": [
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
        ],
    }
    lines.update(overrides)
    noise = [
        f">{BATTLE_TAG}",
        "|init|battle",
        "|title|LudexBot3682 vs. Rival3682",
        "|j|☆LudexBot3682",
        "",
        "|request|",
    ]
    out: list[str] = list(noise)
    for key in ("t:", "gametype", "player", "teamsize", "gen", "tier", "rule", "start", "switch"):
        out.extend(lines[key])
    return out


def _p2_opening(**overrides: list[str]) -> list[str]:
    """La MISMA batalla vista por p2: exacto/porcentual invertidos respecto
    de `_p1_opening`. El resto del bloque es identico (es publico)."""
    lines = {
        "switch": [
            "|switch|p1a: Furret|Furret, L93, F|100/100",
            "|switch|p2a: Lapras|Lapras, L88, M|248/248",
        ],
    }
    lines.update(overrides)
    return _p1_opening(**lines)


def test_paridad_p1_p2_hp_exacto_y_porcentual_invertidos_da_la_misma_clave():
    assert compute_opening_identity(BATTLE_TAG, _p1_opening()) == \
        compute_opening_identity(BATTLE_TAG, _p2_opening())


def test_una_sola_linea_distinta_cambia_la_clave():
    base = compute_opening_identity(BATTLE_TAG, _p1_opening())
    otro_lead = compute_opening_identity(BATTLE_TAG, _p1_opening(switch=[
        "|switch|p1a: Furret|Furret, L93, F|309/309",
        "|switch|p2a: Dusknoir|Dusknoir, L84, M|100/100",
    ]))
    assert base != otro_lead


def test_el_orden_de_llegada_no_importa():
    p1 = _p1_opening()
    shuffled = p1[len(p1) // 2:] + p1[:len(p1) // 2]
    assert compute_opening_identity(BATTLE_TAG, p1) == \
        compute_opening_identity(BATTLE_TAG, shuffled)


def test_no_compara_por_substring_ni_concatenado():
    # Concatenadas, "|rule|A" + "|rule|BC" == "|rule|AB" + "|rule|C". Como
    # elementos de lista, con separador y ordenados, NO deben coincidir.
    a = compute_opening_identity(BATTLE_TAG, _p1_opening(rule=["|rule|A", "|rule|BC"]))
    b = compute_opening_identity(BATTLE_TAG, _p1_opening(rule=["|rule|AB", "|rule|C"]))
    assert a != b


def test_conserva_duplicados_no_los_deduplica():
    dos = compute_opening_identity(BATTLE_TAG, _p1_opening(
        rule=["|rule|HP Percentage Mod: HP is shown in percentages"] * 2,
    ))
    tres = compute_opening_identity(BATTLE_TAG, _p1_opening(
        rule=["|rule|HP Percentage Mod: HP is shown in percentages"] * 3,
    ))
    assert dos != tres


def test_el_contador_del_tag_no_participa_de_la_identidad():
    # Es justo el numero que Showdown reutiliza tras un restart.
    misma_apertura = _p1_opening()
    assert compute_opening_identity("battle-gen6randombattle-1", misma_apertura) == \
        compute_opening_identity("battle-gen6randombattle-9999", misma_apertura)


def test_apertura_incompleta_falla_cerrado():
    lines = [line for line in _p1_opening() if not line.startswith("|start")]
    with pytest.raises(OpeningIdentityError):
        compute_opening_identity(BATTLE_TAG, lines)


def test_switch_inicial_no_full_falla_cerrado():
    with pytest.raises(OpeningIdentityError):
        compute_opening_identity(BATTLE_TAG, _p1_opening(switch=[
            "|switch|p1a: Furret|Furret, L93, F|200/309",
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
        ]))


def test_faltan_switches_para_el_gametype_declarado():
    # Singles con 2 jugadores exige 2 switches iniciales, no 1.
    with pytest.raises(OpeningIdentityError):
        compute_opening_identity(BATTLE_TAG, _p1_opening(switch=[
            "|switch|p1a: Furret|Furret, L93, F|309/309",
        ]))


def test_switches_de_dobles_se_validan_contra_el_gametype():
    doubles = _p1_opening(gametype=["|gametype|doubles"], switch=[
        "|switch|p1a: Furret|Furret, L93, F|309/309",
        "|switch|p1b: Gengar|Gengar, L80, M|280/280",
        "|switch|p2a: Lapras|Lapras, L88, M|100/100",
        "|switch|p2b: Dusknoir|Dusknoir, L84, M|100/100",
    ])
    # No revienta: 2 jugadores x 2 activos de dobles = 4 switches, y hay 4.
    compute_opening_identity(BATTLE_TAG, doubles)


def test_gametype_desconocido_falla_cerrado():
    with pytest.raises(OpeningIdentityError):
        compute_opening_identity(BATTLE_TAG, _p1_opening(gametype=["|gametype|rotacion"]))


# --- L-02 (LINEAR_VERDICT): la completitud es ESTRUCTURAL, no un conteo ----
#
# Latwan reprodujo que duplicar las lineas de p1 (player/teamsize/switch) y
# no incluir NINGUNA de p2 producia una clave valida: los conteos cuadraban
# (2 'player', 2 'teamsize', 2 'switch' con singles) aunque el segundo lado
# nunca existio.

def test_p2_ausente_pero_player_duplicado_de_p1_falla_cerrado():
    with pytest.raises(OpeningIdentityError, match="roles 'player' repetidos"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(
            player=["|player|p1|LudexBot3682|101|", "|player|p1|LudexBot3682|101|"],
        ))


def test_p2_ausente_pero_teamsize_duplicado_de_p1_falla_cerrado():
    # player SI tiene p1/p2 reales; el ataque esta en teamsize: 'p1' dos
    # veces en vez de 'p1'+'p2'. Falla cerrado por rol repetido, que ya
    # implica que no puede cubrir los dos roles declarados por 'player'.
    with pytest.raises(OpeningIdentityError, match="roles 'teamsize' repetidos"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(
            teamsize=["|teamsize|p1|6", "|teamsize|p1|6"],
        ))


def test_p2_ausente_pero_switch_duplicado_de_p1_falla_cerrado():
    # player y teamsize reales; el ataque esta en los switches iniciales:
    # exactamente el repro de Latwan (dos 'p1a', cero 'p2a').
    with pytest.raises(OpeningIdentityError, match="slots 'switch' duplicados"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(switch=[
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p1a: Furret|Furret, L93, F|309/309",
        ]))


def test_switch_con_slot_repetido_aunque_el_otro_lado_exista_falla_cerrado():
    # 3 switches: p1a x2 + p2a. El CONTEO (3) no dice nada; el slot p1a esta
    # duplicado y ningun duplicado puede sustituir a un slot ausente.
    with pytest.raises(OpeningIdentityError, match="slots 'switch' duplicados"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(switch=[
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
        ]))


def test_switch_con_slot_mal_formado_falla_cerrado():
    with pytest.raises(OpeningIdentityError, match="ident invalido"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(switch=[
            "|switch|p1: Furret|Furret, L93, F|309/309",  # falta la letra de slot
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
        ]))


def test_player_con_rol_mal_formado_falla_cerrado():
    with pytest.raises(OpeningIdentityError, match="rol invalido"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(
            player=["|player|p1|LudexBot3682|101|", "|player|equipoB|Rival3682|102|"],
        ))


def test_teamsize_de_un_rol_que_player_no_declaro_falla_cerrado():
    with pytest.raises(OpeningIdentityError, match="'teamsize' no cubre"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(
            teamsize=["|teamsize|p1|6", "|teamsize|p3|6"],
        ))


# --- L-02, re-review de Latwan sobre 14df921 ---------------------------
#
# `compute_opening_identity` derivaba la topologia esperada de los roles
# RECIBIDOS en vez de los roles REALES del gametype: aceptaba singles con
# p1+p3, y 'multi' con solo p1+p2 (ambos con conteos que "cerraban" por
# casualidad). La topologia de slots de 'multi' esta confirmada contra el
# simulador VENDORIZADO `pokemon-showdown@0.11.10` (la version pineada,
# D4): `Pokemon.getSlot()` (`sim/pokemon.ts:504-507`) calcula la letra como
# `'abcdef'[posicion + floor(side.n/2)*activos_por_lado]`, y con 1 activo
# por lado en multi eso da p1a/p2a/p3b/p4b -- NO "cada rol usa las mismas
# letras", que es lo que la version anterior asumia.

def test_singles_con_p1_y_p3_falla_cerrado():
    with pytest.raises(OpeningIdentityError, match=r"roles \['p1', 'p2'\]"):
        compute_opening_identity(BATTLE_TAG, _p1_opening(
            player=["|player|p1|LudexBot3682|101|", "|player|p3|Rival3682|102|"],
            teamsize=["|teamsize|p1|6", "|teamsize|p3|6"],
            switch=[
                "|switch|p1a: Furret|Furret, L93, F|309/309",
                "|switch|p3a: Lapras|Lapras, L88, M|100/100",
            ],
        ))


MULTI_TAG = "battle-gen6multibattle-1"


def _multi_opening(**overrides: list[str]) -> list[str]:
    """Apertura real de 'multi': 4 roles, 1 activo por lado, topologia
    p1a/p2a/p3b/p4b (confirmada arriba contra el simulador vendorizado)."""
    lines = {
        "t:": ["|t:|1785186819"],
        "gametype": ["|gametype|multi"],
        "player": [
            "|player|p1|LudexBot3682|101|", "|player|p2|Rival3682|102|",
            "|player|p3|LudexAlly|103|", "|player|p4|RivalAlly|104|",
        ],
        "teamsize": [
            "|teamsize|p1|6", "|teamsize|p2|6", "|teamsize|p3|6", "|teamsize|p4|6",
        ],
        "gen": ["|gen|8"],
        "tier": ["|tier|[Gen 8] Multi Battle"],
        "rule": ["|rule|HP Percentage Mod: HP is shown in percentages"],
        "start": ["|start"],
        "switch": [
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
            "|switch|p3b: Gengar|Gengar, L80, M|280/280",
            "|switch|p4b: Dusknoir|Dusknoir, L84, M|100/100",
        ],
    }
    lines.update(overrides)
    noise = [f">{MULTI_TAG}", "|init|battle", ""]
    out: list[str] = list(noise)
    for key in ("t:", "gametype", "player", "teamsize", "gen", "tier", "rule", "start", "switch"):
        out.extend(lines[key])
    return out


def test_multi_real_completo_pasa():
    # No revienta: los 4 roles, sus 4 teamsize y la topologia p1a/p2a/p3b/p4b
    # estan completos y correctos.
    compute_opening_identity(MULTI_TAG, _multi_opening())


def test_multi_sin_p3_p4_falla_cerrado():
    with pytest.raises(OpeningIdentityError, match=r"roles \['p1', 'p2', 'p3', 'p4'\]"):
        compute_opening_identity(MULTI_TAG, _multi_opening(
            player=["|player|p1|LudexBot3682|101|", "|player|p2|Rival3682|102|"],
            teamsize=["|teamsize|p1|6", "|teamsize|p2|6"],
            switch=[
                "|switch|p1a: Furret|Furret, L93, F|309/309",
                "|switch|p2a: Lapras|Lapras, L88, M|100/100",
            ],
        ))


def test_multi_con_topologia_incorrecta_falla_cerrado():
    # Los 4 roles estan, pero p3/p4 salen con letra 'a' (como si multi no
    # compartiera semi-lado) en vez de 'b': la topologia real exige p3b/p4b.
    with pytest.raises(OpeningIdentityError, match="no cubren exactamente"):
        compute_opening_identity(MULTI_TAG, _multi_opening(switch=[
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
            "|switch|p3a: Gengar|Gengar, L80, M|280/280",
            "|switch|p4a: Dusknoir|Dusknoir, L84, M|100/100",
        ]))


def test_multi_con_slot_duplicado_falla_cerrado():
    # p3b aparece dos veces y p4b nunca: el conteo (4) cuadra pero el
    # conjunto no cubre el lado de p4.
    with pytest.raises(OpeningIdentityError, match="slots 'switch' duplicados"):
        compute_opening_identity(MULTI_TAG, _multi_opening(switch=[
            "|switch|p1a: Furret|Furret, L93, F|309/309",
            "|switch|p2a: Lapras|Lapras, L88, M|100/100",
            "|switch|p3b: Gengar|Gengar, L80, M|280/280",
            "|switch|p3b: Gengar|Gengar, L80, M|280/280",
        ]))


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
    ProjectionAmbiguityError,
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
        "drapion": ["POISON", "DARK"],
        "charizard": ["FIRE", "FLYING"],
        # Mega-X, no Mega-Y: Mega-Y es FIRE/FLYING igual que la base, asi que
        # con ella la asercion "los tipos SI cambian" pasaba sola.
        "charizardmegax": ["FIRE", "DRAGON"],
        "ditto": ["NORMAL"],
        "tentacruel": ["WATER", "POISON"],
        "spinda": ["NORMAL"],
        "ludicolo": ["WATER", "GRASS"],
        "camerupt": ["FIRE", "GROUND"],
        "cameruptmega": ["FIRE", "GROUND"],
        "weezing": ["POISON"],
        "xatu": ["PSYCHIC", "FLYING"],
    }
    # `baseSpecies` del dex, ya normalizado. Es lo que usa
    # `Pokemon.identifies_as` (`pokemon.py:435-438`) para decidir si dos
    # nombres son el MISMO pokemon.
    BASE = {"cameruptmega": "camerupt", "charizardmegax": "charizard"}
    # Ability cuando el dex lista exactamente una y la forma no es Mega/Primal
    # (`pokemon.py:658-661`, con `gen >= 3`).
    UNICA = {"zoroark": "illusion", "weezing": "levitate"}
    # `abilities["0"]` de una forma Mega/Primal (`pokemon.py:650-655`).
    FORMA = {"cameruptmega": "sheerforce", "charizardmegax": "toughclaws"}
    # `entry["pp"] * 8 // 5` (`move.py:476`).
    MAX_PP = {"energyball": 16, "scald": 24, "sludgebomb": 16,
              "transform": 16, "toxic": 16, "copycat": 32, "sleeptalk": 16}

    def species_types(self, species_id):
        return list(self.TIPOS.get(species_id, []))

    def base_species(self, species_id):
        return self.BASE.get(species_id, species_id)

    def unique_ability(self, species_id):
        return self.UNICA.get(species_id)

    def forme_change_ability(self, species_id):
        return self.FORMA.get(species_id)

    def move_max_pp(self, move_id):
        return self.MAX_PP.get(move_id)

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


def _proyectar(lines, snapshot=None, *, persistent_state=None):
    return project_observable_state(
        snapshot or _snapshot(), tuple(lines),
        opponent_side="p2", vocabulary=FakeVocabulary(),
        persistent_state=persistent_state,
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
    assert real["moves"] == []
    # No hereda `overcoat`. Su ability es `illusion` por la regla del dex
    # (Zoroark solo puede tener esa en gen 6), no por herencia.
    assert real["ability"] == "illusion"


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


# --- Paridad medida contra poke-env 0.15.0 (TECH LEAD REVIEW de 6af10da) ---
#
# Los valores esperados de este bloque NO estan de memoria: salieron de
# alimentar un `Battle` real de poke-env con las MISMAS lineas y serializarlo
# con `state/serializer.py`. Cada docstring cita la salida medida.


def _vacio():
    """Snapshot con el equipo rival vacio: el frame lo revela entero."""
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"] = []
    return snapshot


def _por_especie(out):
    return {p["species"]: p for p in out["opponent"]["pokemon"]}


def test_una_mega_que_sale_y_vuelve_no_duplica_el_miembro():
    """Finding 1. Medido con poke-env sobre estas cuatro lineas:

        p2: Camerupt  species=cameruptmega active=True  lvl=79 types=FIRE/GROUND
        p2: Weezing   species=weezing     active=False lvl=83

    Dos entradas, no tres. poke-env resuelve la identidad por `base_species`
    (`Pokemon.identifies_as`, `pokemon.py:435-438`), no por igualdad exacta de
    `species`, y `switch_in` SI escribe la especie (`store_species=True`),
    asi que al volver la Mega la entrada pasa a decir `cameruptmega`.

    La integracion de `6af10da` produjo un snapshot con SIETE rivales por
    exactamente esto (`battle-gen6randombattle-1917`, decision 32).
    """
    out = _proyectar([
        "|switch|p2a: Camerupt|Camerupt, L79, M|100/100",
        "|detailschange|p2a: Camerupt|Camerupt-Mega, L79, M",
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
        "|switch|p2a: Camerupt|Camerupt-Mega, L79, M|100/100",
    ], _vacio())
    equipo = out["opponent"]["pokemon"]
    assert len(equipo) == 2, f"un solo Camerupt, no dos: {[p['species'] for p in equipo]}"
    por = _por_especie(out)
    assert por["cameruptmega"]["active"] is True
    assert por["cameruptmega"]["level"] == 79
    assert por["weezing"]["active"] is False


def test_el_detailschange_solo_no_cambia_la_especie_de_la_entrada():
    """Medido: tras `switch Camerupt` + `detailschange Camerupt-Mega`,
    poke-env sigue diciendo `species=camerupt` (store_species=False)."""
    out = _proyectar([
        "|switch|p2a: Camerupt|Camerupt, L79, M|100/100",
        "|detailschange|p2a: Camerupt|Camerupt-Mega, L79, M",
    ], _vacio())
    assert [p["species"] for p in out["opponent"]["pokemon"]] == ["camerupt"]


def test_ninguna_identidad_canonica_se_repite_en_el_equipo():
    """Canario de la propiedad, no de un caso: dos entradas con el mismo
    `base_species` son siempre el mismo pokemon contado dos veces."""
    out = _proyectar([
        "|switch|p2a: Charizard|Charizard, L79, M|100/100",
        "|detailschange|p2a: Charizard|Charizard-Mega-X, L79, M",
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
        "|switch|p2a: Charizard|Charizard-Mega-X, L79, M|100/100",
    ], _vacio())
    vocab = FakeVocabulary()
    canonicas = [vocab.base_species(p["species"]) for p in out["opponent"]["pokemon"]]
    assert len(canonicas) == len(set(canonicas)), canonicas
    assert len(out["opponent"]["pokemon"]) <= 6


def test_la_forma_mega_reporta_la_ability_de_la_forma():
    """Medido: `cameruptmega` -> `ability=sheerforce`. poke-env lo guarda en
    `forme_change_ability` y la property `ability` la prefiere
    (`pokemon.py:650-655` y `861-871`)."""
    out = _proyectar([
        "|switch|p2a: Camerupt|Camerupt, L79, M|100/100",
        "|detailschange|p2a: Camerupt|Camerupt-Mega, L79, M",
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
        "|switch|p2a: Camerupt|Camerupt-Mega, L79, M|100/100",
    ], _vacio())
    assert _por_especie(out)["cameruptmega"]["ability"] == "sheerforce"


def test_una_especie_con_una_sola_ability_la_revela_desde_el_dex():
    """Medido: `weezing` -> `ability=levitate` sin ninguna linea `-ability`.
    poke-env lo deduce del dex cuando hay UNA sola ability posible
    (`pokemon.py:658-661`). Es inferencia anclada al dex, no lista a mano:
    Camerupt tiene tres abilities y queda en None."""
    out = _proyectar([
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
        "|switch|p2a: Camerupt|Camerupt, L79, M|100/100",
    ], _vacio())
    por = _por_especie(out)
    assert por["weezing"]["ability"] == "levitate"
    assert por["camerupt"]["ability"] is None, "Camerupt tiene 3 abilities"


def test_illusion_registra_la_ability_publica_del_imitador():
    """Finding 2. Medido con `replace` + `-end Illusion`:

        p2: Drapion  species=drapion active=False lvl=83 hp=0   ability=None
        p2: Zoroark  species=zoroark active=True  lvl=81 hp=0.6 ability=illusion

    Drapion queda en None porque tiene tres abilities posibles; Zoroark en
    `illusion` porque el dex de gen 6 le da exactamente una. Misma regla del
    dex, sin lista de especies.
    """
    out = _proyectar([
        "|switch|p2a: Drapion|Drapion, L83, M|100/100",
        "|-damage|p2a: Drapion|60/100",
        "|replace|p2a: Zoroark|Zoroark, L81, M",
        "|-end|p2a: Zoroark|Illusion",
    ], _vacio())
    por = _por_especie(out)
    assert por["drapion"]["active"] is False
    assert por["drapion"]["level"] == 83
    assert por["drapion"]["hp_fraction"] == 0.0
    assert por["drapion"]["ability"] is None
    assert por["zoroark"]["active"] is True
    assert por["zoroark"]["level"] == 81
    assert por["zoroark"]["hp_fraction"] == 0.6
    assert por["zoroark"]["ability"] == "illusion"
    assert por["zoroark"]["types"] == ["DARK"]


def test_end_illusion_solo_tambien_registra_la_ability():
    """La linea `|-end|...|Illusion` se procesa por si misma: si la ventana de
    frames arranca despues del `|replace|`, sigue siendo evidencia publica."""
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"] = [{
        **snapshot["opponent"]["pokemon"][0], "species": "zoroark",
        "types": ["DARK"], "ability": None,
    }]
    out = _proyectar(["|-end|p2a: Zoroark|Illusion"], snapshot)
    assert out["opponent"]["pokemon"][0]["ability"] == "illusion"


def _snapshot_transform():
    snapshot = _snapshot(gen=6, me={"pokemon": [{
        "species": "tentacruel", "active": True,
        "types": ["WATER", "POISON"], "ability": "liquidooze",
        "boosts": {"spa": 2, "atk": 0, "def": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 20, "max_pp": 24}],
    }]})
    snapshot["opponent"]["pokemon"] = []
    return snapshot


def test_al_salir_del_transform_se_limpia_todo_el_estado_temporal():
    """Finding 3. Medido tras `Imposter -> Transform -> switch out`:

        p2: Ditto species=ditto active=False types=['NORMAL']
                  ability=imposter moves=[{'id':'transform','pp':16,'max_pp':16}]

    `switch_out` limpia `_temporary_types`, `temporary_ability`,
    `_transform_moves` y los boosts (`pokemon.py:600-612`), asi que queda a la
    vista solo lo PERSISTENTE: la ability que revelo Imposter y el movimiento
    `transform` que poke-env le agrega al moveset base, con PP completo.
    """
    out = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
    ], _snapshot_transform())
    ditto = _por_especie(out)["ditto"]
    assert ditto["active"] is False
    assert ditto["types"] == ["NORMAL"], "los tipos copiados no sobreviven"
    assert ditto["ability"] == "imposter", "la ability copiada tampoco"
    assert ditto["moves"] == [{"id": "transform", "pp": 16, "max_pp": 16}]
    assert ditto["boosts"]["spa"] == 0, "los boosts copiados tampoco"


def test_durante_el_transform_si_se_ve_lo_copiado():
    """El contrapeso del test anterior: mientras esta transformado, lo copiado
    SI es lo observable. Sin este, "limpiar al salir" podria pasar no
    copiando nunca nada."""
    out = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], _snapshot_transform())
    ditto = _por_especie(out)["ditto"]
    assert ditto["types"] == ["WATER", "POISON"]
    assert ditto["ability"] == "liquidooze"
    assert ditto["boosts"]["spa"] == 2
    assert [m["id"] for m in ditto["moves"]] == ["scald"]


# --- Finding 1 (TECH LEAD REVIEW sobre `b784bcc`): la memoria dura un frame,
# pero el protocolo la necesita entre DECISIONES ---
#
# `project_observable_state` se llama UNA VEZ POR DECISION, siempre con un
# snapshot fresco de `serialize_battle(battle)`. Un test de una sola llamada
# con las tres lineas juntas (switch/transform/switch-out) no ejerce nada de
# esto: el dict local `transformed` de la version anterior sobrevivia dentro
# de esa unica llamada por pura coincidencia. En una batalla real, Transform
# puede resolverse en la decision N y el switch-out en la N+k: cada una es
# una llamada DISTINTA a `project_observable_state`. Estos tests pasan el
# MISMO `persistent_state` a dos llamadas separadas, como hace `client.py`
# con `self._temporary_state[tag]`.


def test_transform_sobrevive_a_la_decision_y_se_limpia_en_otra_llamada():
    memoria: dict[str, dict] = {}
    tras_transform = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], _snapshot_transform(), persistent_state=memoria)
    ditto = _por_especie(tras_transform)["ditto"]
    assert ditto["ability"] == "liquidooze"
    assert [m["id"] for m in ditto["moves"]] == ["scald"]

    # SEGUNDA llamada: el snapshot de la decision siguiente ya viene con a
    # Ditto transformado (asi lo serializaria poke-env, que procesa el
    # Transform con su propio mecanismo en cuanto el lock se libera). Esta
    # llamada solo aplica la evidencia NUEVA: el switch de Weezing.
    tras_switch = _proyectar([
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
    ], tras_transform, persistent_state=memoria)
    ditto2 = _por_especie(tras_switch)["ditto"]
    assert ditto2["active"] is False
    assert ditto2["ability"] == "imposter", (
        "sin memoria entre llamadas, esto queda en 'liquidooze' -- el bug "
        "medido en la revision: la ability copiada sobrevive al switch-out "
        "porque la segunda llamada no sabe que hubo un Transform"
    )
    assert ditto2["moves"] == [{"id": "transform", "pp": 16, "max_pp": 16}]
    assert ditto2["types"] == ["NORMAL"]
    # `types`/`moves` se consumieron (evento puntual); `ability` PERSISTE a
    # proposito -- es la base para el PROXIMO override, igual que `_ability`
    # nunca se olvida en poke-env aunque el pokemon salga del campo.
    assert memoria == {"ditto": {"ability": "imposter"}}


def test_una_mega_conserva_sus_tipos_al_salir_en_otra_llamada():
    """El mismo defecto de memoria, con Mega en vez de Transform: los tipos
    de una forma son PERSISTENTES (poke-env nunca resetea `_type_1`/
    `_type_2` en `switch_out`, pokemon.py:600-612), no un typechange
    temporal. La version anterior los reseteaba al dex de `species`
    (`charizard`, que sigue siendo la especie ya que `detailschange` no la
    cambia) y perdia los tipos de la Mega."""
    memoria: dict[str, dict] = {}
    tras_mega = _proyectar([
        "|switch|p2a: Charizard|Charizard, L79, M|100/100",
        "|detailschange|p2a: Charizard|Charizard-Mega-X, L79, M",
    ], _vacio(), persistent_state=memoria)
    charizard = _por_especie(tras_mega)["charizard"]
    assert charizard["types"] == ["FIRE", "DRAGON"]

    tras_switch = _proyectar([
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
    ], tras_mega, persistent_state=memoria)
    charizard2 = _por_especie(tras_switch)["charizard"]
    assert charizard2["active"] is False
    assert charizard2["types"] == ["FIRE", "DRAGON"], (
        "los tipos de la Mega son persistentes: switch_out no los resetea "
        "al dex de la especie base"
    )
    # Sin ningun typechange/Transform de por medio, no hay nada que
    # restaurar: la memoria no tiene entrada para Charizard.
    assert memoria == {}


def test_un_typechange_temporal_si_se_revierte_al_salir_en_otra_llamada():
    """El contrapeso del test anterior: un typechange autentico (Protean) SI
    es temporal y SI tiene que revertirse, a diferencia de los tipos de una
    Mega. Sin este contrapeso, "no tocar los tipos en switch_out" podria
    pasar sin revertir NUNCA un typechange real."""
    memoria: dict[str, dict] = {}
    tras_typechange = _proyectar([
        "|-start|p2a: Ludicolo|typechange|Water",
    ], _snapshot(), persistent_state=memoria)
    ludicolo = _por_especie(tras_typechange)["ludicolo"]
    assert ludicolo["types"] == ["WATER"]

    tras_switch = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
    ], tras_typechange, persistent_state=memoria)
    ludicolo2 = _por_especie(tras_switch)["ludicolo"]
    assert ludicolo2["types"] == ["WATER", "GRASS"], (
        "el typechange de Protean SI se revierte: es temporal, a diferencia "
        "de los tipos de una Mega"
    )
    assert memoria == {}


def test_la_primera_ability_revelada_se_persiste_sin_sembrar_memoria():
    """El caso `_ability is None` del setter de poke-env (`pokemon.py:
    873-878`): la PRIMERA ability revelada de un pokemon se fija como
    persistente, no hay nada que restaurar despues, y por eso NO se siembra
    ninguna entrada en `persistent_state`."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["ability"] = None
    tras_ability = _proyectar([
        "|-ability|p2a: Ludicolo|Swift Swim",
    ], snapshot, persistent_state=memoria)
    assert _por_especie(tras_ability)["ludicolo"]["ability"] == "swiftswim"

    tras_switch = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
    ], tras_ability, persistent_state=memoria)
    assert _por_especie(tras_switch)["ludicolo"]["ability"] == "swiftswim"
    assert memoria == {}, "primera revelacion: nada que restaurar, sin seed"


def _snapshot_weezing():
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"] = [{
        "species": "weezing", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 83,
        "item": "unknown_item", "ability": "levitate", "types": ["POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }]
    return snapshot


def test_finding2_ability_ya_conocida_es_temporal_y_se_restaura_en_otra_llamada():
    """Finding 2 (TECH LEAD REVIEW sobre `410eabb`). Weezing con ability YA
    conocida (`levitate`, unica en el dex -- persistente por construccion,
    no un `None` de partida como el test anterior). Medido con `Battle`
    real: `|-ability|...|Truant|[from] move: Entrainment` sobre un Weezing
    con `levitate` conocida dej ability=truant mientras esta activo y
    `ability=levitate` tras el switch-out.

    Dos llamadas separadas, memoria compartida: exactamente lo que pediste.
    """
    memoria: dict[str, dict] = {}
    tras_entrainment = _proyectar([
        "|-ability|p2a: Weezing|Truant|[from] move: Entrainment",
    ], _snapshot_weezing(), persistent_state=memoria)
    assert _por_especie(tras_entrainment)["weezing"]["ability"] == "truant"
    assert memoria == {"weezing": {"ability": "levitate"}}

    tras_switch = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
    ], tras_entrainment, persistent_state=memoria)
    weezing = _por_especie(tras_switch)["weezing"]
    assert weezing["active"] is False
    assert weezing["ability"] == "levitate", "vuelve a la base tras el switch-out"
    # `ability` PERSISTE en la memoria (a diferencia de types/moves): es la
    # base para el PROXIMO override, no un registro de un solo uso.
    assert memoria == {"weezing": {"ability": "levitate"}}


def test_finding2_trace_real_copia_temporal_y_restaura_trace_como_base():
    """El camino REAL de Trace, medido con `Battle`: Gardevoir (rival, sin
    ability conocida) traza `Levitate` de nuestro Weezing. Durante el
    Transform... digo, durante el Trace, `ability=levitate` (temporal);
    tras el switch-out, `ability=trace` -- NO `levitate` y NO `None`: Trace
    establece su PROPIA ability como la base persistente
    (`abstract_battle.py:781-792`), la ability copiada es solo temporal.
    """
    snapshot = _snapshot(me={"pokemon": [{
        "species": "weezing", "active": True, "ability": "levitate",
    }]})
    snapshot["opponent"]["pokemon"] = [{
        "species": "gardevoir", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 80,
        "item": "unknown_item", "ability": None, "types": ["PSYCHIC"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }]
    memoria: dict[str, dict] = {}
    durante = _proyectar([
        "|-ability|p2a: Gardevoir|Levitate|[from] ability: Trace|[of] p1a: Weezing",
    ], snapshot, persistent_state=memoria)
    assert _por_especie(durante)["gardevoir"]["ability"] == "levitate"
    assert memoria == {"gardevoir": {"ability": "trace"}}

    tras_switch = _proyectar([
        "|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100",
    ], durante, persistent_state=memoria)
    gardevoir = _por_especie(tras_switch)["gardevoir"]
    assert gardevoir["ability"] == "trace", "Trace es su propia base, no Levitate"


def test_finding2_endability_restaura_la_base_sin_esperar_al_switch():
    """`-endability` tiene que restaurar la base YA, sin esperar un switch:
    medido con `Battle` real, Weezing vuelve a `levitate` en la MISMA
    decision en la que termina Entrainment."""
    out = _proyectar([
        "|-ability|p2a: Weezing|Truant|[from] move: Entrainment",
        "|-endability|p2a: Weezing",
    ], _snapshot_weezing())
    assert _por_especie(out)["weezing"]["ability"] == "levitate"


def test_finding2_endability_sin_override_activo_no_hace_nada():
    """Contrapeso: `-endability` sobre un pokemon SIN ningun override activo
    (su ability persistente ya es la que se ve) no debe forzar `None` --
    `temporary_ability = None` sobre un pokemon que nunca tuvo una es un
    no-op tambien en poke-env."""
    out = _proyectar(["|-endability|p2a: Ludicolo"])
    assert _por_especie(out)["ludicolo"]["ability"] is None


def test_magic_bounce_revela_la_ability_y_no_el_movimiento():
    """Finding 4. Medido:

        p2: Xatu ability=magicbounce moves=[]

    El movimiento reflejado NO es del actor. poke-env pone `use=False` y
    `reveal=False` para Magic Bounce (`abstract_battle.py:650-654`) pero SI
    registra la ability que trae el sufijo.
    """
    out = _proyectar([
        "|switch|p2a: Xatu|Xatu, L83, M|100/100",
        "|move|p2a: Xatu|Toxic|p1a: Tentacruel|[from] ability: Magic Bounce",
    ], _vacio())
    xatu = _por_especie(out)["xatu"]
    assert xatu["ability"] == "magicbounce"
    assert xatu["moves"] == [], "Toxic era nuestro, no de Xatu"


def test_copycat_no_atribuye_el_movimiento_eco_al_actor():
    """Medido: `moves=[{'id':'copycat','pp':31,'max_pp':32}]`. El movimiento
    disparado por Copycat no se revela y no vuelve a descontar PP
    (`abstract_battle.py:625-633` y `move.py:123-130` con
    `overridden=True`)."""
    out = _proyectar([
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
        "|move|p2a: Ludicolo|Copycat|p1a: Tentacruel",
        "|move|p2a: Ludicolo|Scald|p1a: Tentacruel|[from] move: Copycat",
    ], _vacio())
    ludicolo = _por_especie(out)["ludicolo"]
    assert ludicolo["moves"] == [{"id": "copycat", "pp": 31, "max_pp": 32}]


def test_sleep_talk_si_revela_el_movimiento_llamado():
    """Medido: `sleeptalk pp=15` y `scald pp=24` (completo). Sleep Talk llama
    movimientos PROPIOS, asi que el eco si es evidencia de pertenencia — pero
    no consume PP del movimiento llamado."""
    out = _proyectar([
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
        "|move|p2a: Ludicolo|Sleep Talk|p2a: Ludicolo",
        "|move|p2a: Ludicolo|Scald|p1a: Tentacruel|[from] move: Sleep Talk",
    ], _vacio())
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "sleeptalk", "pp": 15, "max_pp": 16},
        {"id": "scald", "pp": 24, "max_pp": 24},
    ]


def test_un_movimiento_repetido_descuenta_pp_en_vez_de_quedar_stale():
    """Medido: dos `|move|Energy Ball|` -> `pp=14, max_pp=16`. La version
    anterior dejaba el PP que ya traia el snapshot, afirmando un numero
    stale."""
    out = _proyectar([
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
        "|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
        "|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
    ], _vacio())
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 14, "max_pp": 16}]


def test_un_movimiento_ya_conocido_descuenta_desde_el_pp_del_snapshot():
    """El PP que trae el snapshot viene de la contabilidad de poke-env; la
    proyeccion continua desde ahi, no lo recalcula."""
    out = _proyectar([
        "|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
    ])
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 14, "max_pp": 16}]


def test_un_movimiento_fallado_igual_descuenta():
    """Medido con `[miss]`: `pp=15`. `failed` no evita el uso."""
    out = _proyectar([
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
        "|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel|[miss]",
    ], _vacio())
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 15, "max_pp": 16}]


def test_sin_max_pp_conocido_el_pp_va_en_null():
    """Schema v2: cuando el PP no es derivable con exactitud se escribe
    `null`, nunca un numero inventado ni uno stale."""
    out = _proyectar([
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
        "|move|p2a: Ludicolo|Leaf Storm|p1a: Tentacruel",
    ], _vacio())
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "leafstorm", "pp": None, "max_pp": None}]


def test_con_pressure_propio_el_pp_va_en_null():
    """`Move.use` descuenta 2 con Pressure (`move.py:123-127`) y la regla
    exacta depende del objetivo del movimiento. Antes que afirmar un numero
    que puede estar mal por uno, se escribe `null`."""
    snapshot = _snapshot(me={"pokemon": [{
        "species": "dusknoir", "active": True, "ability": "pressure"}]})
    out = _proyectar(["|move|p2a: Ludicolo|Energy Ball|p1a: Dusknoir"], snapshot)
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}]


def test_dancer_revela_su_ability_pero_no_el_movimiento():
    """Finding 4 (TECH LEAD REVIEW sobre `b784bcc`). Medido con un `Battle`
    real: `{'ability': 'dancer', 'moves': []}`. poke-env asigna la ability
    ANTES del `return` (`abstract_battle.py:650-656`): Dancer SI revela su
    propia ability publica; lo unico que se omite es el movimiento que
    bailo (por eso el `return`, que corta antes de `mon.moved(...)`)."""
    out = _proyectar([
        "|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100",
        "|move|p2a: Ludicolo|Scald|p1a: Tentacruel|[from] ability: Dancer",
    ], _vacio())
    ludicolo = _por_especie(out)["ludicolo"]
    assert ludicolo["moves"] == [], "el movimiento que bailo no se revela"
    assert ludicolo["ability"] == "dancer", "pero la ability SI"


# --- Finding 2 (TECH LEAD REVIEW sobre `b784bcc`): item/ability revelados
# por el sufijo de una linea -damage/-heal ---
#
# Medido con un `Battle` real (`abstract_battle.py:333-403`). El caso real
# del corpus: `|-damage|p2a: Houndoom|85/100|[from] item: Life Orb` sin la
# linea `-item`, que la version anterior ignoraba por completo.


def test_damage_por_item_propio_lo_revela():
    out = _proyectar([
        "|switch|p2a: Houndoom|Houndoom, L80, M|100/100",
        "|-damage|p2a: Houndoom|85/100|[from] item: Life Orb",
    ], _vacio())
    assert _por_especie(out)["houndoom"]["item"] == "lifeorb"


def test_damage_por_item_ajeno_via_of_se_lo_atribuye_al_dueño():
    """`|-damage|{nuestro}a: X|...|[from] item: Rocky Helmet|[of] {rival}a: Y`:
    el item es de Y (el rival), no de X (nuestro activo, que ni se
    proyecta). Corre ANTES del filtro por ident: si se filtrara por el
    `ident` de la linea (nuestro propio activo), esta revelacion del rival
    se perderia enterita."""
    out = _proyectar([
        "|switch|p2a: Ferrothorn|Ferrothorn, L80, M|100/100",
        "|-damage|p1a: Archeops|88/100|[from] item: Rocky Helmet"
        "|[of] p2a: Ferrothorn",
    ], _vacio())
    assert _por_especie(out)["ferrothorn"]["item"] == "rockyhelmet"


def test_damage_por_ability_ajena_via_of_se_la_atribuye_al_dueño():
    out = _proyectar([
        "|switch|p2a: Ferrothorn|Ferrothorn, L80, M|100/100",
        "|-damage|p1a: Archeops|88/100|[from] ability: Iron Barbs"
        "|[of] p2a: Ferrothorn",
    ], _vacio())
    assert _por_especie(out)["ferrothorn"]["ability"] == "ironbarbs"


def test_heal_por_item_propio_lo_revela():
    """Sin ninguna linea `-item` previa: el item se revela SOLO por el
    sufijo del heal. poke-env exige `item is not None` antes de reasignar
    (`abstract_battle.py:377-387`), y nuestro sentinel de "no revelado" es
    `unknown_item`, no `None` -- por eso el snapshot base ya trae
    `unknown_item` (nunca `None`) para que este camino pueda escribir."""
    out = _proyectar([
        "|switch|p2a: Quagsire|Quagsire, L80, M|90/100",
        "|-heal|p2a: Quagsire|100/100|[from] item: Leftovers",
    ], _vacio())
    assert _por_especie(out)["quagsire"]["item"] == "leftovers"


def test_heal_por_berry_ya_consumida_no_reescribe_el_item():
    """poke-env exige `item is not None` ANTES de reasignar (`abstract_
    battle.py:377-387`): la narracion de heal llega DESPUES de que la berry
    ya se gasto y el item ya quedo en `None` via `-enditem`. Sin este guard
    se reescribiria un item que ya no esta."""
    out = _proyectar([
        "|switch|p2a: Quagsire|Quagsire, L80, M|50/100",
        "|-enditem|p2a: Quagsire|Sitrus Berry",
        "|-heal|p2a: Quagsire|100/100|[from] item: Sitrus Berry",
    ], _vacio())
    assert _por_especie(out)["quagsire"]["item"] is None


def test_heal_por_ability_propia_ignora_el_of_enganoso():
    """El `[of]` de un heal por ability NO indica de quien es (salvo
    Hospitality): la ability es de quien se cura (`abstract_battle.py:
    389-403`). Medido: Water Absorb cura a Quagsire, no a quien aparece
    tras `[of]`."""
    out = _proyectar([
        "|switch|p2a: Quagsire|Quagsire, L80, M|80/100",
        "|-heal|p2a: Quagsire|100/100|[from] ability: Water Absorb"
        "|[of] p1a: Genesect",
    ], _vacio())
    assert _por_especie(out)["quagsire"]["ability"] == "waterabsorb"


def test_la_frescura_es_inmediata_no_una_decision_despues():
    """Canario de retraso-en-uno especifico de item/ability: la revelacion
    tiene que quedar en la MISMA proyeccion que la trajo, no en la
    siguiente."""
    out = _proyectar([
        "|switch|p2a: Houndoom|Houndoom, L80, M|100/100",
        "|-damage|p2a: Houndoom|85/100|[from] item: Life Orb",
        "|upkeep",
    ], _vacio())
    assert _por_especie(out)["houndoom"]["item"] == "lifeorb", (
        "el item tiene que estar revelado en ESTA proyeccion, no en la "
        "decision siguiente"
    )


# --- Finding 3 (TECH LEAD REVIEW sobre `b784bcc`): -clearallboost sin
# ident, y el resto de los eventos de boost ---


def test_clearallboost_sin_ident_limpia_al_rival():
    """`|-clearallboost` llega SIN ident (limpia los dos activos a la vez,
    `abstract_battle.py:901-902`). El guard `len(parts) < 3` de la version
    anterior lo volvia inalcanzable: 94 lineas reales en el corpus de test,
    cero ejercidas."""
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["boosts"]["spa"] = 3
    out = _proyectar(["|-clearallboost"], snapshot)
    assert out["opponent"]["pokemon"][0]["boosts"]["spa"] == 0


def test_clearnegativeboost_solo_limpia_los_negativos():
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["boosts"]["spe"] = -2
    snapshot["opponent"]["pokemon"][0]["boosts"]["spa"] = 1
    out = _proyectar(["|-clearnegativeboost|p2a: Ludicolo"], snapshot)
    boosts = out["opponent"]["pokemon"][0]["boosts"]
    assert boosts["spe"] == 0
    assert boosts["spa"] == 1


def test_clearpositiveboost_solo_limpia_los_positivos():
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["boosts"]["spe"] = -2
    snapshot["opponent"]["pokemon"][0]["boosts"]["spa"] = 1
    out = _proyectar(["|-clearpositiveboost|p2a: Ludicolo"], snapshot)
    boosts = out["opponent"]["pokemon"][0]["boosts"]
    assert boosts["spe"] == -2
    assert boosts["spa"] == 0


def test_invertboost_niega_todos_los_boosts_del_rival():
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["boosts"]["spe"] = -2
    snapshot["opponent"]["pokemon"][0]["boosts"]["spa"] = 1
    out = _proyectar(["|-invertboost|p2a: Ludicolo"], snapshot)
    boosts = out["opponent"]["pokemon"][0]["boosts"]
    assert boosts["spe"] == 2
    assert boosts["spa"] == -1


def test_copyboost_copia_nuestros_boosts_al_rival():
    """`|-copyboost|fuente|objetivo|...`: el objetivo (parts[3]) se queda con
    los boosts de la fuente (parts[2]). Cuando la fuente somos nosotros, ya
    es informacion fresca por el `|request|`."""
    snapshot = _snapshot(me={"pokemon": [{
        "species": "tentacruel", "active": True,
        "boosts": {"atk": 0, "def": 0, "spa": 2, "spd": 0, "spe": 1,
                   "evasion": 0, "accuracy": 0},
    }]})
    out = _proyectar(
        ["|-copyboost|p1a: Tentacruel|p2a: Ludicolo"], snapshot,
    )
    boosts = out["opponent"]["pokemon"][0]["boosts"]
    assert boosts["spa"] == 2
    assert boosts["spe"] == 1


def test_copyboost_hacia_nuestro_lado_no_toca_al_rival():
    """Si el OBJETIVO somos nosotros, no hay nada que proyectar: nuestro
    lado ya llega fresco."""
    out = _proyectar([
        "|-copyboost|p2a: Ludicolo|p1a: Tentacruel",
    ])
    assert out["opponent"]["pokemon"][0]["boosts"]["spa"] == 0


def test_swapboost_falla_cerrado_en_vez_de_dejar_un_boost_stale():
    """Finding 3 (TECH LEAD REVIEW sobre `410eabb`). La ronda anterior
    documentaba `-swapboost` como limite y conservaba el boost stale del
    rival; el veredicto lo rechazo explicitamente ("nunca dejar un numero
    stale"). Sin el boost PROPIO de antes del intercambio (que este
    proyector nunca tiene: "me" llega post-resolucion), no hay forma
    correcta de escribirle el valor al rival -- falla CERRADO, mismo
    mecanismo que la ambiguedad de fuente propia."""
    assert is_resolution_frame((">battle-x", "|-swapboost|p1a: A|p2a: B|atk"))
    snapshot = _snapshot()
    snapshot["opponent"]["pokemon"][0]["boosts"]["spa"] = 1
    with pytest.raises(ProjectionAmbiguityError):
        _proyectar(["|-swapboost|p1a: Tentacruel|p2a: Ludicolo|spa"], snapshot)


def test_swapboost_falla_cerrado_sin_importar_que_lado_nombra_primero():
    """El chequeo no puede depender de `parts[2]`: un `-swapboost` con el
    RIVAL nombrado primero tiene que fallar cerrado igual."""
    with pytest.raises(ProjectionAmbiguityError):
        _proyectar(["|-swapboost|p2a: Ludicolo|p1a: Tentacruel|spa"])


# --- Finding 1 (TECH LEAD REVIEW sobre `410eabb`): la fuente propia se
# resuelve por el NOMBRE del evento, no por "quien esta activo ahora" ---
#
# `snapshot["me"]` viene fresco del `|request|` propio, YA post-resolucion de
# TODO el turno. Si un evento (Transform, Reflect Type, -copyboost) nombra a
# un pokemon propio que DESPUES salio del campo dentro de la MISMA
# narracion, "el activo ahora" del snapshot es el que entro despues, no el
# nombrado. Medido en `battle-gen6randombattle-1929`.


def _snapshot_multi_own(*, activo, vacio=True):
    """Snapshot con Tentacruel Y Spinda en el equipo propio; `activo` decide
    cual de los dos figura como `active: True` -- exactamente lo que el
    snapshot post-resolucion trae. `vacio=False` conserva al Ludicolo rival
    por defecto (para los casos que necesitan un activo rival ya en cancha)."""
    tentacruel = {
        "species": "tentacruel", "active": activo == "tentacruel",
        "types": ["WATER", "POISON"], "ability": "liquidooze",
        "boosts": {"spa": 2, "atk": 0, "def": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 20, "max_pp": 24}],
    }
    spinda = {
        "species": "spinda", "active": activo == "spinda",
        "types": ["NORMAL"], "ability": "owntempo",
        "boosts": {"spa": 0, "atk": 1, "def": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "tackle", "pp": 30, "max_pp": 35}],
    }
    snapshot = _snapshot(gen=6, me={"pokemon": [tentacruel, spinda]})
    if vacio:
        snapshot["opponent"]["pokemon"] = []
    return snapshot


def test_transform_copia_al_nombrado_por_el_evento_no_al_activo_del_snapshot():
    """Reproduccion real: `|-transform|p2a: Ditto|p1a: Spinda|...` con el
    snapshot YA mostrando a Tentacruel activo (porque un switch posterior en
    la MISMA narracion volvio a cambiar el activo antes de que armaramos el
    snapshot). Ditto tiene que copiar a SPINDA, el nombrado, no a Tentacruel."""
    snapshot = _snapshot_multi_own(activo="tentacruel")
    out = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Spinda|[from] ability: Imposter",
    ], snapshot)
    ditto = _por_especie(out)["ditto"]
    assert ditto["types"] == ["NORMAL"], "tiene que copiar a Spinda, no a Tentacruel"
    assert ditto["boosts"]["atk"] == 1
    assert ditto["ability"] == "owntempo"
    assert [m["id"] for m in ditto["moves"]] == ["tackle"]


def test_reflect_type_copia_al_nombrado_por_el_of_no_al_activo_del_snapshot():
    snapshot = _snapshot_multi_own(activo="tentacruel", vacio=False)
    out = _proyectar([
        "|-start|p2a: Ludicolo|typechange|[from] move: Reflect Type|[of] p1a: Spinda",
    ], snapshot)
    activo = next(p for p in out["opponent"]["pokemon"] if p["active"])
    assert activo["types"] == ["NORMAL"], "tiene que copiar a Spinda, no a Tentacruel"


def test_copyboost_copia_al_nombrado_por_la_fuente_no_al_activo_del_snapshot():
    snapshot = _snapshot_multi_own(activo="tentacruel", vacio=False)
    out = _proyectar(["|-copyboost|p1a: Spinda|p2a: Ludicolo"], snapshot)
    boosts = out["opponent"]["pokemon"][0]["boosts"]
    assert boosts["atk"] == 1, "tiene que copiar los boosts de Spinda"
    assert boosts["spa"] == 0


def test_transform_de_un_nombre_no_resoluble_falla_cerrado():
    """Fallo CERRADO, no `own_active()` "por las dudas": si el nombre del
    evento no corresponde a NINGUN miembro conocido del equipo propio, no se
    inventa una fuente."""
    snapshot = _snapshot_multi_own(activo="tentacruel")
    with pytest.raises(ProjectionAmbiguityError):
        _proyectar([
            "|switch|p2a: Ditto|Ditto, L84|100/100",
            "|-transform|p2a: Ditto|p1a: NoExiste|[from] ability: Imposter",
        ], snapshot)
