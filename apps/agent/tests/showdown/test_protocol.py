from ludex_agent.showdown.protocol import (
    ProtocolRecorder,
    elo_bucket_from_rating,
    extract_replay_url,
)


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
        "purugly": ["NORMAL"],
        "meloetta": ["NORMAL", "PSYCHIC"],
        # Relic Song: forma temporal, revierte al salir del campo (MON-19,
        # D41) -- a diferencia de charizardmegax, `mon["species"]` NUNCA
        # pasa a decir "meloettapirouette" (`-formechange` no escribe
        # especie), asi que no hace falta una entrada en BASE para ella.
        "meloettapirouette": ["NORMAL", "FIGHTING"],
        # MON-26: las especies de la batalla real battle-gen6randombattle-67.
        "probopass": ["ROCK", "STEEL"],
        "malamar": ["DARK", "PSYCHIC"],
        # MON-27: battle-gen6randombattle-120. Mawile NO cambia de tipos al
        # mega evolucionar (a diferencia de Charizard-X) -- a proposito, para
        # que el canario de ability no dependa de una diferencia de tipos.
        "mawile": ["STEEL", "FAIRY"],
        "mawilemega": ["STEEL", "FAIRY"],
    }
    # `baseSpecies` del dex, ya normalizado. Es lo que usa
    # `Pokemon.identifies_as` (`pokemon.py:435-438`) para decidir si dos
    # nombres son el MISMO pokemon.
    BASE = {
        "cameruptmega": "camerupt", "charizardmegax": "charizard",
        "mawilemega": "mawile",
    }
    # Ability cuando el dex lista exactamente una y la forma no es Mega/Primal
    # (`pokemon.py:658-661`, con `gen >= 3`).
    UNICA = {"zoroark": "illusion", "weezing": "levitate"}
    # `abilities["0"]` de una forma Mega/Primal (`pokemon.py:650-655`).
    FORMA = {
        "cameruptmega": "sheerforce", "charizardmegax": "toughclaws",
        "mawilemega": "hugepower",
    }
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


def _proyectar(lines, snapshot=None, *, persistent_state=None, pre_applied=0):
    return project_observable_state(
        snapshot or _snapshot(), tuple(lines),
        opponent_side="p2", vocabulary=FakeVocabulary(),
        persistent_state=persistent_state,
        pre_applied=pre_applied,
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
    narr = await inbox.publish(tag, ("|switch|p2a: Latias|Latias, L77, F|100/100",))

    window = await inbox.wait_for_resolution(
        tag, after_seq=0, until_seq=req.seq, timeout=1
    )
    assert [f.seq for f in window] == [narr.seq]
    assert window[0].lines[0].startswith("|switch|")


async def test_dos_decisiones_no_consumen_el_mismo_frame():
    inbox = RawFrameInbox()
    tag = "battle-x"
    r1 = await inbox.publish(tag, ("|request|1",))
    n1 = await inbox.publish(tag, ("|move|p2a: A|Tackle|p1a: B",))
    r2 = await inbox.publish(tag, ("|request|2",))
    n2 = await inbox.publish(tag, ("|move|p2a: A|Surf|p1a: B",))

    d1 = await inbox.wait_for_resolution(tag, after_seq=0, until_seq=r1.seq, timeout=1)
    assert [f.seq for f in d1] == [n1.seq]
    # El watermark de la decision siguiente es la ventana anterior: sin ese
    # tope, dos decisiones consumirian el mismo frame de cierre.
    d2 = await inbox.wait_for_resolution(tag, after_seq=n1.seq, until_seq=r2.seq, timeout=1)
    assert [f.seq for f in d2] == [n2.seq]


async def test_el_inbox_espera_a_una_narracion_que_todavia_no_llego():
    inbox = RawFrameInbox()
    tag = "battle-x"

    async def publicar_tarde():
        await asyncio.sleep(0.02)
        await inbox.publish(tag, ("|faint|p2a: Latias",))

    asyncio.create_task(publicar_tarde())
    window = await inbox.wait_for_resolution(tag, after_seq=0, until_seq=0, timeout=2)
    assert [f.lines for f in window] == [("|faint|p2a: Latias",)]


async def test_el_inbox_falla_cerrado_al_vencer_el_timeout():
    inbox = RawFrameInbox()
    with pytest.raises(ProjectionTimeoutError):
        await inbox.wait_for_resolution("battle-x", after_seq=0, until_seq=0, timeout=0.01)


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
        await inbox.wait_for_resolution(tag, after_seq=0, until_seq=0, timeout=2)


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
        await inbox.wait_for_resolution(
            "battle-x", after_seq=cursor, until_seq=cursor, timeout=0.01
        )


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
    window = await inbox.wait_for_resolution(
        "battle-x", after_seq=cursor, until_seq=cursor, timeout=0.5
    )
    # La relacion (el frame del cursor sigue alcanzable) Y el valor exacto: si
    # el tope bajara a 64 el `retained` cambia y este test lo dice, en vez de
    # quedar del lado correcto de una desigualdad.
    assert [f.lines for f in window] == [("|turn|2",)]
    assert inbox.retained("battle-x") == 65
    assert MAX_RETAINED_FRAMES == 128, "poke-env 0.15.0, gen6randombattle"


# --- MON-26: ventana de frames por decision ---


async def test_la_ventana_incluye_la_narracion_anterior_al_request_wait():
    """MON-26 (medido en battle-gen6randombattle-67, turno 2): un request
    `wait:true` entre la narracion de la decision anterior y el request
    activo dejaba esa narracion HUERFANA -- la espera solo miraba hacia
    adelante del request propio y el frame con `-enditem` nunca se
    proyectaba. La ventana tiene que entregar los frames de resolucion entre
    el watermark (ultima proyeccion) y el frame de cierre, incluidos los que
    llegaron ANTES del request activo."""
    inbox = RawFrameInbox()
    tag = "battle-x"
    r1 = await inbox.publish(tag, ("|request|1",))
    n1 = await inbox.publish(tag, ("|move|p2a: A|Tackle|p1a: B",))
    await inbox.publish(tag, ("|request|2",))  # wait:true
    n2 = await inbox.publish(tag, (
        "|-damage|p2a: A|0 fnt",
        "|-enditem|p2a: A|Air Balloon",
        "|faint|p2a: A",
    ))
    r2 = await inbox.publish(tag, ("|request|3",))  # request activo
    n3 = await inbox.publish(tag, ("|switch|p2a: C|C, L80|100/100",))

    d1 = await inbox.wait_for_resolution(tag, after_seq=0, until_seq=r1.seq, timeout=1)
    assert [f.seq for f in d1] == [n1.seq]

    d2 = await inbox.wait_for_resolution(tag, after_seq=n1.seq, until_seq=r2.seq, timeout=1)
    assert [f.seq for f in d2] == [n2.seq, n3.seq], (
        "la narracion anterior al request activo no puede quedar huerfana"
    )
    assert any(l.startswith("|-enditem|") for f in d2 for l in f.lines)


async def test_sin_request_wait_la_ventana_es_el_frame_unico_de_siempre():
    """MON-26 (condicion del tech lead, no-regresion del flujo normal): sin un
    request `wait:true` interpuesto, la ventana devuelve EXACTAMENTE el mismo
    frame unico que devolvia la API anterior. Esta es la garantia de la que
    depende todo el arreglo."""
    inbox = RawFrameInbox()
    tag = "battle-x"
    r1 = await inbox.publish(tag, ("|request|1",))
    n1 = await inbox.publish(tag, ("|move|p2a: A|Tackle|p1a: B",))
    r2 = await inbox.publish(tag, ("|request|2",))
    n2 = await inbox.publish(tag, ("|move|p2a: A|Surf|p1a: B",))

    d1 = await inbox.wait_for_resolution(tag, after_seq=0, until_seq=r1.seq, timeout=1)
    d2 = await inbox.wait_for_resolution(tag, after_seq=n1.seq, until_seq=r2.seq, timeout=1)
    assert [(f.seq, f.lines) for f in d1] == [(n1.seq, n1.lines)]
    assert [(f.seq, f.lines) for f in d2] == [(n2.seq, n2.lines)]
    assert len(d1) == len(d2) == 1


async def test_el_inbox_aisla_los_frames_por_batalla():
    """MON-26 R2 (F3): el inbox entrega a cada batalla SOLO sus frames,
    aunque otra batalla publique entre medio. NOTA de honestidad (medido por
    Tasos): este test NO verifica el watermark compartido -- el inbox no
    guarda watermark alguno y aca se pasa `after_seq=0` a mano. La evidencia
    comportamental del watermark por tag la da UNICAMENTE
    `test_el_watermark_de_proyeccion_es_por_batalla` (client)."""
    inbox = RawFrameInbox()
    ra1 = await inbox.publish("battle-a", ("|request|1",))
    rb1 = await inbox.publish("battle-b", ("|request|1",))
    na1 = await inbox.publish("battle-a", ("|move|p2a: A|Tackle|p1a: B",))
    nb1 = await inbox.publish("battle-b", ("|move|p2a: X|Surf|p1a: Y",))

    da = await inbox.wait_for_resolution("battle-a", after_seq=0, until_seq=ra1.seq, timeout=1)
    db = await inbox.wait_for_resolution("battle-b", after_seq=0, until_seq=rb1.seq, timeout=1)
    assert [f.seq for f in da] == [na1.seq]
    assert [f.seq for f in db] == [nb1.seq]


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
    # `canonical_types` (D41) queda sembrada para ambas identidades: cada
    # una paso por `switch_in` en algun momento de esta secuencia.
    assert memoria == {
        "ditto": {"ability": "imposter", "canonical_types": ["NORMAL"]},
        "weezing": {"canonical_types": ["POISON"]},
    }


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
    # Sin ningun typechange/Transform de por medio, no hay backup temporal
    # que restaurar. `canonical_types` (D41) SI queda sembrada para las dos
    # identidades que pasaron por `switch_in`.
    assert memoria == {
        "charizard": {"canonical_types": ["FIRE", "DRAGON"]},
        "weezing": {"canonical_types": ["POISON"]},
    }


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
    # El backup temporal se consumio al restaurar (switch_out de Ludicolo,
    # disparado por el switch de Mandibuzz); `canonical_types` (D41) queda
    # sembrada para las dos identidades que volvieron a pasar por
    # `switch_in` en esta secuencia.
    assert memoria == {
        "ludicolo": {"canonical_types": ["WATER", "GRASS"]},
        "mandibuzz": {"canonical_types": ["DARK", "FLYING"]},
    }


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
    # La ability no siembra memoria (nada que restaurar), pero los dos
    # switch_in de esta secuencia si escriben `canonical_types` (D41) para
    # sus respectivas identidades.
    assert memoria == {
        "ludicolo": {"canonical_types": ["WATER", "GRASS"]},
        "mandibuzz": {"canonical_types": ["DARK", "FLYING"]},
    }


def test_segunda_revelacion_identica_de_ability_no_siembra_backup():
    """MON-27 causa raiz (aislada de Mega): revelar la MISMA ability dos
    veces (p.ej. Intimidate re-anunciandose en cada switch-in ordinario,
    medido en `battle-gen6randombattle-120` turnos 12 y 14) no es un
    override -- es la misma evidencia otra vez. `reveal_ability` no puede
    sembrar backup en `persistent_state` para un valor que no cambio: sin
    este guard, cualquier `switch_out` posterior restauraria ese backup
    espurio sobre lo que sea que la identidad tenga en ese momento,
    incluida una Mega evolution que ya volvio la ability permanente."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot()
    snapshot1["opponent"]["pokemon"][0]["ability"] = None
    tras_primera = _proyectar(
        ["|-ability|p2a: Ludicolo|Swift Swim"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_primera)["ludicolo"]["ability"] == "swiftswim"
    assert "ludicolo" not in memoria

    # Snapshot fresco INDEPENDIENTE: la ability ya conocida llega en el
    # snapshot (asi la reportaria poke-env), y la MISMA linea la re-anuncia.
    snapshot2 = _snapshot()
    snapshot2["opponent"]["pokemon"][0]["ability"] = "swiftswim"
    tras_segunda = _proyectar(
        ["|-ability|p2a: Ludicolo|Swift Swim"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(tras_segunda)["ludicolo"]["ability"] == "swiftswim"
    assert "ability" not in memoria.get("ludicolo", {}), (
        "revelar la MISMA ability otra vez no es un override: no debe "
        "sembrar ningun backup en persistent_state"
    )


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
    # base para el PROXIMO override, no un registro de un solo uso. El
    # switch_in de Mandibuzz en esta segunda llamada tambien siembra su
    # propio `canonical_types` (D41).
    assert memoria == {
        "weezing": {"ability": "levitate"},
        "mandibuzz": {"canonical_types": ["DARK", "FLYING"]},
    }


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


# --- MON-18/D37: el `None` de Pressure tiene que sobrevivir a la SIGUIENTE
# decision, no solo a la linea que lo produjo. `client.py` construye el
# `snapshot` de CADA llamada fresco desde `serialize_battle(battle)`
# (nunca encadena la proyeccion anterior); poke-env cuenta su propio PP sin
# saber de Pressure, asi que ese snapshot fresco puede traer un numero
# donde nosotros ya sabiamos que el PP real es indeterminable. Sin memoria
# explicita en `persistent_state`, ese numero pisa el `None` en la proxima
# llamada. Estos tests NUNCA encadenan `_proyectar(..., salida_anterior)`
# para el snapshot que sigue al uso bajo Pressure: construyen un snapshot
# fresco a mano, como haria `serialize_battle`, para no enmascarar el bug.


def test_pp_desconocido_por_pressure_se_reaplica_con_snapshot_fresco():
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(me={"pokemon": [{
        "species": "dusknoir", "active": True, "ability": "pressure"}]})
    tras_uso = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Dusknoir"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_uso)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}]
    assert memoria == {"ludicolo": {"unknown_pp_moves": {"energyball"}}}

    # Snapshot FRESCO e independiente (no la salida de la llamada anterior):
    # asi luciria si poke-env ya recontó Energy Ball sin saber de Pressure.
    snapshot2 = _snapshot()
    snapshot2["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 14, "max_pp": 16}]
    tras_segunda_decision = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_segunda_decision)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}], (
        "el snapshot fresco trae pp=14; persistent_state tiene que forzarlo "
        "de nuevo a None"
    )


def test_pressure_propio_con_linea_pre_aplicada_va_en_null_y_marca():
    """MON-26 R4 (T-01): `pre_applied` salta SOLO el `pp - 1`, nunca la rama
    D37 de Pressure. En una ventana con gap el snapshot POST-narracion trae
    el numero de poke-env (ciego a Pressure, por eso existe D37): con
    Pressure propio, el PP del rival NOMBRADO tiene que quedar `None` y la
    marca `unknown_pp_moves` persistir -- tambien cuando la linea es
    pre-aplicada. Y la marca tiene que sobrevivir al snapshot fresco de la
    decision siguiente (mismo patron que
    `test_pp_desconocido_por_pressure_se_reaplica_con_snapshot_fresco`)."""
    memoria: dict[str, dict] = {}
    snap = _snapshot_con_gap()
    snap["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ]
    snap["me"]["pokemon"] = [{
        "species": "dusknoir", "active": True, "ability": "pressure",
    }]
    tras_uso = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Dusknoir",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        snap, persistent_state=memoria, pre_applied=1,
    )
    assert _por_especie(tras_uso)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}
    ], (
        "Pressure propio + linea pre-aplicada: el PP NO es derivable y va "
        "en None, no en el numero de poke-env"
    )
    assert memoria.get("ludicolo", {}).get("unknown_pp_moves") == {"energyball"}, (
        "la marca D37 se siembra tambien en lineas pre-aplicadas"
    )

    # Snapshot FRESCO de la decision siguiente (poke-env ya recontó sin
    # saber de Pressure): la marca tiene que forzar None de nuevo.
    snap2 = _snapshot_con_gap()
    snap2["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 14, "max_pp": 16}
    ]
    tras_segunda = _proyectar([], snap2, persistent_state=memoria)
    assert _por_especie(tras_segunda)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}
    ], (
        "el snapshot fresco trae pp=14; la marca persistida lo fuerza de "
        "nuevo a None"
    )


def test_pp_desconocido_sobrevive_un_switch_ordinario():
    """A diferencia de `types`/`moves`/`ability` copiados por Transform, la
    marca de Pressure NO es un evento puntual: tiene que sobrevivir un
    switch-out Y un switch-in posterior, cada uno en su propia decision con
    snapshot fresco."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(me={"pokemon": [{
        "species": "dusknoir", "active": True, "ability": "pressure"}]})
    tras_uso = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Dusknoir"],
        snapshot1, persistent_state=memoria,
    )
    assert memoria == {"ludicolo": {"unknown_pp_moves": {"energyball"}}}

    snapshot2 = _snapshot()
    snapshot2["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 14, "max_pp": 16}]
    tras_switch_out = _proyectar(
        ["|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100"],
        snapshot2, persistent_state=memoria,
    )
    ludicolo_fuera = _por_especie(tras_switch_out)["ludicolo"]
    assert ludicolo_fuera["active"] is False
    assert ludicolo_fuera["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}]

    snapshot3 = _snapshot()
    snapshot3["opponent"]["pokemon"][0]["active"] = False
    snapshot3["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 14, "max_pp": 16}]
    snapshot3["opponent"]["pokemon"].append({
        "species": "mandibuzz", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 84,
        "item": "unknown_item", "ability": None, "types": ["DARK", "FLYING"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    })
    tras_switch_in = _proyectar(
        ["|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100"],
        snapshot3, persistent_state=memoria,
    )
    ludicolo_dentro = _por_especie(tras_switch_in)["ludicolo"]
    assert ludicolo_dentro["active"] is True
    assert ludicolo_dentro["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}], (
        "la marca sobrevive el switch-out Y el switch-in: no es un evento "
        "puntual como Transform"
    )


def test_dos_rivales_con_el_mismo_movimiento_no_se_contaminan():
    """Aislamiento por identidad canonica: que Ludicolo tenga Energy Ball
    con PP desconocido no puede afectar el Energy Ball de un Weezing
    distinto, aunque compartan `move_id`."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(me={"pokemon": [{
        "species": "dusknoir", "active": True, "ability": "pressure"}]})
    snapshot["opponent"]["pokemon"].append({
        "species": "weezing", "hp_fraction": 1.0, "active": False,
        "fainted": False, "status": None, "level": 83,
        "item": "unknown_item", "ability": "levitate", "types": ["POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "energyball", "pp": 16, "max_pp": 16}],
    })
    tras_uso = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Dusknoir"],
        snapshot, persistent_state=memoria,
    )
    por_especie = _por_especie(tras_uso)
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}]
    assert por_especie["weezing"]["moves"] == [
        {"id": "energyball", "pp": 16, "max_pp": 16}], (
        "el Energy Ball de Weezing no debe verse afectado: identidad "
        "distinta, marca distinta"
    )
    assert memoria == {"ludicolo": {"unknown_pp_moves": {"energyball"}}}


def test_pp_desconocido_de_un_transform_es_temporal_y_no_se_filtra_a_otro():
    """Un movimiento copiado por Transform con PP desconocido por Pressure
    necesita una marca SEPARADA y descartable: no puede quedar pegada a la
    identidad base (Ditto) para siempre, ni filtrarse a un Transform
    DISTINTO que despues copie un movimiento con el mismo id.

    Reescrito tras LINEAR_VERDICT MON-18 R1 (L-02): CADA decision construye
    su propio snapshot fresco e independiente a mano -- ninguna reutiliza
    la salida de la llamada anterior como entrada de la siguiente. Solo
    `memoria` (el mismo dict de `persistent_state`) cruza las cuatro
    llamadas, exactamente como `client.py` reusa `self._temporary_state[tag]`."""
    memoria: dict[str, dict] = {}
    equipo_propio = [
        {"species": "tentacruel", "types": ["WATER", "POISON"],
         "ability": "liquidooze",
         "boosts": {"spa": 2, "atk": 0, "def": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0},
         "moves": [{"id": "scald", "pp": 20, "max_pp": 24}]},
        {"species": "golbat", "types": ["POISON", "FLYING"],
         "ability": "infiltrator",
         "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0},
         "moves": [{"id": "scald", "pp": 24, "max_pp": 24}]},
        {"species": "dusknoir", "ability": "pressure"},
    ]

    # Decision 1 (fresca): Ditto entra y se transforma en Tentacruel en la
    # MISMA linea de narracion.
    snapshot1 = _snapshot(gen=6, me={"pokemon": [
        {**equipo_propio[0], "active": True},
        equipo_propio[1], {**equipo_propio[2], "active": False},
    ]})
    snapshot1["opponent"]["pokemon"] = []
    tras_transform = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], snapshot1, persistent_state=memoria)
    # `_transformed_move` topea pp/max_pp a min(5, max_pp) desde gen 5
    # (`move.py:477-478`): un Scald copiado (max_pp real 24) queda en 5/5.
    assert _por_especie(tras_transform)["ditto"]["moves"] == [
        {"id": "scald", "pp": 5, "max_pp": 5}]

    # Decision 2 (snapshot FRESCO e independiente, no `tras_transform`):
    # asi luciria `serialize_battle` con Ditto YA transformado (poke-env
    # trackea Imposter con su propio mecanismo) y Dusknoir activo con
    # Pressure. Ditto usa el Scald COPIADO bajo Pressure propio.
    snapshot2 = _snapshot(gen=6, me={"pokemon": [
        equipo_propio[0], equipo_propio[1],
        {**equipo_propio[2], "active": True},
    ]})
    snapshot2["opponent"]["pokemon"] = [{
        "species": "ditto", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 84,
        "item": "unknown_item", "ability": "liquidooze",
        "types": ["WATER", "POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 2, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 5, "max_pp": 5}],
    }]
    tras_uso = _proyectar(
        ["|move|p2a: Ditto|Scald|p1a: Dusknoir"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(tras_uso)["ditto"]["moves"] == [
        {"id": "scald", "pp": None, "max_pp": 5}]
    assert memoria["ditto"]["transform_unknown_pp_moves"] == {"scald"}

    # Decision 3 (fresca): Ditto sale del campo -- el switch_out restaura
    # el moveset BASE ("transform") desde `persistent_state`, sin importar
    # que el snapshot fresco todavia muestre a Ditto transformado (poke-env
    # puede no haber revertido su propio Move de `from_transform` a
    # tiempo).
    snapshot3 = _snapshot(gen=6, me={"pokemon": [
        equipo_propio[0], equipo_propio[1],
        {**equipo_propio[2], "active": True},
    ]})
    snapshot3["opponent"]["pokemon"] = [{
        "species": "ditto", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 84,
        "item": "unknown_item", "ability": "liquidooze",
        "types": ["WATER", "POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 2, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 5, "max_pp": 5}],
    }]
    tras_switch_out = _proyectar([
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
    ], snapshot3, persistent_state=memoria)
    ditto2 = _por_especie(tras_switch_out)["ditto"]
    assert ditto2["moves"] == [{"id": "transform", "pp": 16, "max_pp": 16}]
    assert "transform_unknown_pp_moves" not in memoria.get("ditto", {}), (
        "la marca temporal del Transform anterior tiene que desaparecer al "
        "restaurar el moveset base"
    )

    # Decision 4 (fresca): Ditto vuelve a entrar y se transforma en Golbat
    # (tambien tiene Scald): no puede heredar la marca del Transform
    # anterior sobre Tentacruel.
    snapshot4 = _snapshot(gen=6, me={"pokemon": equipo_propio})
    snapshot4["opponent"]["pokemon"] = [{
        "species": "ditto", "hp_fraction": 1.0, "active": False,
        "fainted": False, "status": None, "level": 84,
        "item": "unknown_item", "ability": None, "types": ["NORMAL"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "transform", "pp": 16, "max_pp": 16}],
    }]
    tras_transform2 = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Golbat|[from] ability: Imposter",
    ], snapshot4, persistent_state=memoria)
    assert _por_especie(tras_transform2)["ditto"]["moves"] == [
        {"id": "scald", "pp": 5, "max_pp": 5}], (
        "Scald copiado de Golbat no arranca en None: la marca del "
        "Transform anterior (sobre Tentacruel) ya se descarto"
    )


def test_transform_copia_scald_independiente_y_el_base_vuelve_a_null_al_salir():
    """Regresion exacta del blocker L-01 (LINEAR_VERDICT MON-18 R1): un
    Scald marcado permanentemente desconocido en el moveset BASE de Mew no
    puede contaminar un Scald COPIADO por Transform -- son instancias de PP
    distintas, aunque compartan `move_id` y el rival sea el mismo pokemon.
    Cada decision usa un snapshot fresco e independiente."""
    memoria: dict[str, dict] = {}

    # Decision 1 (fresca): Mew BASE (sin transformar) usa Scald bajo
    # Pressure propio -> marca permanente en `unknown_pp_moves`.
    snapshot1 = _snapshot(gen=6, me={"pokemon": [
        {"species": "dusknoir", "active": True, "ability": "pressure"},
    ]})
    snapshot1["opponent"]["pokemon"] = [{
        "species": "mew", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 80,
        "item": "unknown_item", "ability": None, "types": ["PSYCHIC"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }]
    tras_uso = _proyectar(
        ["|move|p2a: Mew|Scald|p1a: Dusknoir"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_uso)["mew"]["moves"] == [
        {"id": "scald", "pp": None, "max_pp": 24}]
    assert memoria == {"mew": {"unknown_pp_moves": {"scald"}}}

    # Decision 2 (snapshot FRESCO e independiente, no `tras_uso`): Mew
    # TODAVIA base -- poke-env ya "conto" su propio Scald sin saber de
    # Pressure (14/24) -- y esta misma linea narra el Transform. La
    # reaplicacion corre ANTES del loop de lineas: el moveset que
    # `apply_transform` guarda para restaurar despues tiene que ser el 14
    # YA corregido a None, no el numero crudo.
    snapshot2 = _snapshot(gen=6, me={"pokemon": [
        {"species": "tentacruel", "active": True,
         "types": ["WATER", "POISON"], "ability": "liquidooze",
         "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0},
         "moves": [{"id": "scald", "pp": 20, "max_pp": 24}]},
    ]})
    snapshot2["opponent"]["pokemon"] = [{
        "species": "mew", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 80,
        "item": "unknown_item", "ability": None, "types": ["PSYCHIC"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 14, "max_pp": 24}],
    }]
    # Sin sufijo Imposter a proposito: ese camino ademas agrega un move
    # "transform" al moveset ANTES de guardarlo como base a restaurar
    # (paridad con Ditto), lo que ensuciaria la aserción de abajo con una
    # entrada ajena al PP que este test verifica.
    tras_transform = _proyectar([
        "|-transform|p2a: Mew|p1a: Tentacruel",
    ], snapshot2, persistent_state=memoria)
    mew_transformado = _por_especie(tras_transform)["mew"]
    assert mew_transformado["moves"] == [
        {"id": "scald", "pp": 5, "max_pp": 5}], (
        "el Scald COPIADO (min(5,max_pp) por regla de Transform) es una "
        "instancia distinta: no arranca en None por la marca permanente "
        "del base"
    )
    assert memoria["mew"]["moves"] == [
        {"id": "scald", "pp": None, "max_pp": 24}], (
        "el moveset base guardado para restaurar tiene que ser el YA "
        "corregido a None, no el 14 crudo del snapshot fresco"
    )

    # Decision 3 (fresca): Mew sigue transformado, sin ninguna linea nueva.
    # Sin marca temporal todavia sobre el copiado -- tiene que seguir en
    # 5/5, NUNCA en None por la marca permanente del base.
    snapshot3 = _snapshot(gen=6, me={"pokemon": [
        {"species": "tentacruel", "active": True,
         "types": ["WATER", "POISON"], "ability": "liquidooze",
         "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0},
         "moves": [{"id": "scald", "pp": 20, "max_pp": 24}]},
    ]})
    snapshot3["opponent"]["pokemon"] = [{
        "species": "mew", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 80,
        "item": "unknown_item", "ability": "liquidooze",
        "types": ["WATER", "POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 5, "max_pp": 5}],
    }]
    tras_espera = _proyectar([], snapshot3, persistent_state=memoria)
    assert _por_especie(tras_espera)["mew"]["moves"] == [
        {"id": "scald", "pp": 5, "max_pp": 5}], (
        "sigue transformado: la marca permanente del base NO gobierna el "
        "moveset copiado"
    )

    # Decision 4 (fresca): Mew sale del campo. El switch_out restaura el
    # moveset BASE ya corregido a None -- la marca permanente vuelve a
    # gobernar apenas el Transform termina.
    snapshot4 = _snapshot(gen=6, me={"pokemon": [
        {"species": "tentacruel", "active": True,
         "types": ["WATER", "POISON"], "ability": "liquidooze",
         "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0},
         "moves": [{"id": "scald", "pp": 20, "max_pp": 24}]},
    ]})
    snapshot4["opponent"]["pokemon"] = [{
        "species": "mew", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 80,
        "item": "unknown_item", "ability": "liquidooze",
        "types": ["WATER", "POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 5, "max_pp": 5}],
    }]
    tras_switch_out = _proyectar([
        "|switch|p2a: Weezing|Weezing, L83, F|100/100",
    ], snapshot4, persistent_state=memoria)
    mew_fuera = _por_especie(tras_switch_out)["mew"]
    assert mew_fuera["active"] is False
    assert mew_fuera["moves"] == [
        {"id": "scald", "pp": None, "max_pp": 24}], (
        "al terminar el Transform, el Scald BASE vuelve a null: la marca "
        "permanente vuelve a gobernar"
    )
    assert "moves" not in memoria.get("mew", {})
    assert memoria["mew"]["unknown_pp_moves"] == {"scald"}


def test_el_transform_guarda_el_moveset_base_ya_corregido_a_null():
    """Canario de orden (LINEAR_VERDICT MON-18 R1, L-02 punto 2): la
    reaplicacion tiene que correr ANTES de procesar lineas. Si un Transform
    ocurre en la MISMA llamada que trae un snapshot fresco numerico para un
    movimiento ya marcado permanentemente, `apply_transform` tiene que
    guardar en `persistent_state[identidad]["moves"]` el moveset YA
    corregido a `None` -- no el numero crudo del snapshot. Mover la
    reaplicacion a DESPUES del loop de lineas pone este test rojo."""
    memoria: dict[str, dict] = {"mew": {"unknown_pp_moves": {"scald"}}}
    snapshot = _snapshot(gen=6, me={"pokemon": [
        {"species": "tentacruel", "active": True,
         "types": ["WATER", "POISON"], "ability": "liquidooze",
         "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0},
         "moves": [{"id": "scald", "pp": 20, "max_pp": 24}]},
    ]})
    snapshot["opponent"]["pokemon"] = [{
        "species": "mew", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 80,
        "item": "unknown_item", "ability": None, "types": ["PSYCHIC"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        # snapshot fresco numerico: poke-env "conto" 14, ciego a Pressure.
        "moves": [{"id": "scald", "pp": 14, "max_pp": 24}],
    }]
    # Sin sufijo Imposter: ese camino agrega un move "transform" antes de
    # guardar la base, lo que ensuciaria esta aserción con una entrada
    # ajena al PP que el canario verifica.
    _proyectar([
        "|-transform|p2a: Mew|p1a: Tentacruel",
    ], snapshot, persistent_state=memoria)
    assert memoria["mew"]["moves"] == [
        {"id": "scald", "pp": None, "max_pp": 24}], (
        "el moveset base guardado para restaurar despues tiene que ser el "
        "YA corregido (None), no el numero crudo del snapshot fresco"
    )


def test_sin_reusar_el_persistent_state_por_battle_tag_se_pierde_la_marca():
    """Contrapeso: si el caller NO reutiliza el mismo `persistent_state` por
    `battle_tag` entre decisiones (como hace `client.py` con
    `self._temporary_state.setdefault(tag, {})`), la marca se pierde y el
    PP numerico del snapshot fresco pasa sin corregir. Confirma que la
    correccion depende de verdad de compartir el dict, no de una memoria
    oculta en el modulo."""
    memoria1: dict[str, dict] = {}
    snapshot1 = _snapshot(me={"pokemon": [{
        "species": "dusknoir", "active": True, "ability": "pressure"}]})
    tras_uso = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Dusknoir"],
        snapshot1, persistent_state=memoria1,
    )
    assert _por_especie(tras_uso)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": None, "max_pp": 16}]

    snapshot2 = _snapshot()
    snapshot2["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 14, "max_pp": 16}]
    # persistent_state DISTINTO: simula un caller que no reusa el dict del
    # battle_tag.
    tras_segunda_decision = _proyectar([], snapshot2, persistent_state={})
    assert _por_especie(tras_segunda_decision)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 14, "max_pp": 16}], (
        "sin compartir persistent_state no hay marca que reaplicar -- esto "
        "confirma que la correccion depende de que el caller reuse el "
        "mismo dict por battle_tag, exactamente como hace client.py"
    )


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


# ---------------------------------------------------------------------------
# D40 (MON-18 R3): item del rival, persistente entre decisiones.
#
# ROOT-CAUSE CHECKPOINT: poke-env corrompe `battle.opponent_team[...].item`
# entre una decision y la siguiente cuando el item vino de un intercambio por
# Trick -- confirmado en vivo (`battle-gen6randombattle-2746`), y medido en
# produccion sobre `battles.id=2782/2787`. El `snapshot` que entra a cada
# llamada es SIEMPRE fresco (`serialize_battle`, nunca la proyeccion
# anterior), asi que sin memoria propia ese valor corrupto pisa la evidencia
# ya establecida sin que ninguna linea nueva la pida -- el mismo patron
# arquitectonico que D37 ya resuelve para el PP bajo Pressure.
#
# Cada prueba construye el "snapshot fresco" de la SEGUNDA decision A MANO,
# nunca encadenando la salida de la primera: es exactamente asi como luce en
# produccion (D37 ya establecio este patron) y es lo que prueba que la
# memoria, no una casualidad de la linea, es lo que sostiene el valor.
# ---------------------------------------------------------------------------


def _snapshot_purugly():
    """Estado real persistido para `battles.id=2782`
    (`battle-gen6randombattle-2714`), decision_index=0 -- antes de que el
    turno 1 (el Trick) se procese. Verificado por `SELECT` contra Postgres,
    no inventado."""
    snapshot = _snapshot(gen=6)
    snapshot["me"] = {"pokemon": [{"species": "gothitelle", "active": True}]}
    snapshot["opponent"]["pokemon"] = [{
        "species": "purugly", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 88,
        "item": "unknown_item", "ability": None, "types": ["NORMAL"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }]
    return snapshot


def test_item_revelado_por_trick_sobrevive_snapshot_fresco_corrupto():
    """Reproduccion determinista de `battle-gen6randombattle-2714`
    (`battles.id=2782`) con las lineas REALES de sus turnos 1 y 2
    (`battle_turns.protocol_lines`, ambas preservadas en Postgres). Es el
    canario de boundary: cruza dos llamadas independientes de
    `project_observable_state` con SOLO `persistent_state` en comun -- la
    misma frontera que atraviesa `client.py` entre dos decisiones reales.
    """
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot_purugly()
    turno1 = [
        "|move|p1a: Gothitelle|Trick|p2a: Purugly",
        "|-activate|p1a: Gothitelle|move: Trick|[of] p2a: Purugly",
        "|-item|p2a: Purugly|Choice Scarf|[from] move: Trick",
        "|-item|p1a: Gothitelle|Silk Scarf|[from] move: Trick",
        "|move|p2a: Purugly|Return|p1a: Gothitelle",
        "|-damage|p1a: Gothitelle|179/269",
    ]
    tras_trick = _proyectar(turno1, snapshot1, persistent_state=memoria)
    assert _por_especie(tras_trick)["purugly"]["item"] == "choicescarf"
    assert memoria["purugly"]["item"] == "choicescarf"

    # Snapshot FRESCO e independiente para el turno 2, tal como lo entregaria
    # `serialize_battle` en produccion: SIN ninguna linea `-item` nueva,
    # poke-env ya trae "silkscarf" -- el item que Gothitelle recibio en el
    # MISMO Trick (valor medido en vivo, no inventado; ver CHECKPOINT).
    snapshot2 = _snapshot_purugly()
    snapshot2["opponent"]["pokemon"][0]["item"] = "silkscarf"
    snapshot2["opponent"]["pokemon"][0]["moves"] = [
        {"id": "return", "pp": 31, "max_pp": 32}]
    turno2 = [
        "|switch|p1a: Noctowl|Noctowl, L95, M|344/344",
        "|move|p2a: Purugly|Return|p1a: Noctowl",
        "|-damage|p1a: Noctowl|194/344",
        "|-heal|p1a: Noctowl|215/344|[from] item: Leftovers",
    ]
    tras_snapshot_corrupto = _proyectar(turno2, snapshot2, persistent_state=memoria)
    assert _por_especie(tras_snapshot_corrupto)["purugly"]["item"] == "choicescarf", (
        "el snapshot fresco trae silkscarf sin ninguna linea -item nueva; "
        "persistent_state tiene que forzarlo de nuevo a choicescarf"
    )


def test_reveal_pasivo_de_item_actualiza_la_memoria_y_tambien_sobrevive():
    """Contrapeso Life Orb: aunque el item verdadero SI genere una linea
    pasiva (daño propio), esa confirmacion tiene que escribir en
    `persistent_state` -- no solo en el dict de esta llamada -- para que la
    PROXIMA decision, con snapshot fresco y SIN esa linea, siga sosteniendo
    el valor solo con memoria."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"][0]["item"] = "unknown_item"
    tras_dano = _proyectar(
        ["|-damage|p2a: Ludicolo|91/100|[from] item: Life Orb"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_dano)["ludicolo"]["item"] == "lifeorb"
    assert memoria["ludicolo"]["item"] == "lifeorb"

    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"][0]["item"] = "choicescarf"
    tras_snapshot_corrupto = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_snapshot_corrupto)["ludicolo"]["item"] == "lifeorb"


def test_enditem_persiste_none_y_sobrevive_snapshot_fresco():
    """`None` es un valor SIGNIFICATIVO (requisito 3 del DESIGN VERDICT): la
    clave tiene que quedar PRESENTE con valor `None`, no ausente."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"][0]["item"] = "sitrusberry"
    tras_consumo = _proyectar(
        ["|-enditem|p2a: Ludicolo|Sitrus Berry"], snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_consumo)["ludicolo"]["item"] is None
    assert memoria["ludicolo"]["item"] is None
    assert "ludicolo" in memoria and "item" in memoria["ludicolo"], (
        "la clave 'item' tiene que estar PRESENTE, no ausente"
    )

    # Snapshot fresco que trae de vuelta un item numerico -- no puede pisar
    # el `None` ya confirmado.
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"][0]["item"] = "sitrusberry"
    tras_snapshot_corrupto = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_snapshot_corrupto)["ludicolo"]["item"] is None


def test_enditem_limpia_al_nombrado_aunque_ya_no_este_activo():
    """MON-26 (pieza B): `-enditem` NOMBRA al mon (`p2a: Ludicolo`). En un
    frame de gap (ver tests del inbox) la narracion puede traer `-enditem`
    seguido de `switch` en el MISMO frame, y el snapshot -- post-narracion --
    ya tiene al nombrado fuera de cancha. Resolver por `active()` limpiaria
    el item del REEMPLAZO y dejaria la memoria del nombrado con el item
    stale para siempre."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"][0]["active"] = False
    snapshot["opponent"]["pokemon"][0]["fainted"] = True
    snapshot["opponent"]["pokemon"][0]["status"] = "FNT"
    snapshot["opponent"]["pokemon"][0]["hp_fraction"] = 0.0
    snapshot["opponent"]["pokemon"][0]["item"] = "airballoon"
    snapshot["opponent"]["pokemon"].append({
        "species": "latias", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 77,
        "item": "unknown_item", "ability": None, "types": ["DRAGON", "PSYCHIC"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    })
    out = _proyectar(
        ["|-enditem|p2a: Ludicolo|Air Balloon",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        snapshot, persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["item"] is None, (
        "el item del NOMBRADO se limpia aunque ya no este activo"
    )
    assert por_especie["latias"]["item"] == "unknown_item", (
        "el item del reemplazo no se toca"
    )
    assert memoria["ludicolo"]["item"] is None


# --- MON-26 R2: la clase completa es "identidad persistente resuelta por
# quien la linea NOMBRA". La pieza A concatena la narracion huerfana con el
# switch de cierre sobre un snapshot POST-narracion: cualquier handler que
# resuelva por `active()` le escribe al REEMPLAZO (misatribucion, envenena la
# memoria D40). Medido por el tech lead y por Tasos: los volatiles se
# autocorrigen con el switch de cierre; los persistentes no.
# ---------------------------------------------------------------------------


def _snapshot_con_gap(*, item="unknown_item", ability=None, fainted=True):
    """Snapshot POST-narracion de una ventana con gap: ludicolo salio del
    campo (o se desmayo) y latias ya esta activa. `active()` resuelve al mon
    EQUIVOCADO para las lineas que nombran a ludicolo."""
    snapshot = _snapshot(gen=6)
    ludicolo = snapshot["opponent"]["pokemon"][0]
    ludicolo["active"] = False
    ludicolo["fainted"] = fainted
    ludicolo["status"] = "FNT" if fainted else None
    ludicolo["hp_fraction"] = 0.0 if fainted else 1.0
    ludicolo["item"] = item
    ludicolo["ability"] = ability
    snapshot["opponent"]["pokemon"].append({
        "species": "latias", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 77,
        "item": "unknown_item", "ability": None, "types": ["DRAGON", "PSYCHIC"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    })
    return snapshot


def test_item_revelado_en_gap_se_le_escribe_al_nombrado_no_al_reemplazo():
    """MON-26 R2 (F1): `-item` escribe informacion de identidad persistente
    (`remember_item`) y resuelve por `active()` -- en una ventana con gap eso
    le escribe el item del desmayado al REEMPLAZO y envenena la memoria D40
    (reproduccion del tech lead). Resolver por el ident NOMBRADO lo arregla."""
    memoria: dict[str, dict] = {}
    out = _proyectar(
        ["|-item|p2a: Ludicolo|Air Balloon",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(), persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["item"] == "airballoon", (
        "el item se le escribe al NOMBRADO, no al activo post-narracion"
    )
    assert por_especie["latias"]["item"] == "unknown_item", (
        "el reemplazo no es nombrado por la linea y no recibe el item"
    )
    assert memoria.get("ludicolo", {}).get("item") == "airballoon"
    assert "item" not in memoria.get("latias", {}), (
        "la memoria D40 no puede sembrar un item con la identidad equivocada"
    )


def test_ability_revelada_en_gap_se_le_escribe_al_nombrado_no_al_reemplazo():
    """MON-26 R2 (F1): `-ability` resuelve por `active()` y escribe via
    `reveal_ability`. En una ventana con gap la ability revelada del que
    salio se le escribe al REEMPLAZO."""
    out = _proyectar(
        ["|-ability|p2a: Ludicolo|Drizzle",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(), persistent_state={},
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["ability"] == "drizzle", (
        "la ability se le escribe al NOMBRADO"
    )
    assert por_especie["latias"]["ability"] is None, (
        "el reemplazo no es nombrado por la linea y no recibe la ability"
    )


def test_endability_en_gap_restaura_al_nombrado_no_al_reemplazo():
    """MON-26 R2 (F1): `-endability` restaura la base persistente de la
    identidad que la linea NOMBRA. Con `active()` (post-narracion) leería la
    entrada del REEMPLAZO y el override temporal del nombrado quedaría
    colgado para siempre."""
    memoria: dict[str, dict] = {"ludicolo": {"ability": "swiftswim"}}
    out = _proyectar(
        ["|-endability|p2a: Ludicolo",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(ability="intimidate"), persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["ability"] == "swiftswim", (
        "el override temporal del NOMBRADO termina y vuelve su base"
    )
    assert por_especie["latias"]["ability"] is None, (
        "la entrada del reemplazo no se toca"
    )


# --- MON-26 R3: los cuatro miembros vivos que el escaner de R2 no veia
# (delegan en helpers). Misma regla que R2: quien recibe el dato, nunca que.
# ---------------------------------------------------------------------------


def test_move_en_gap_se_registra_al_actor_nombrado_no_al_reemplazo():
    """MON-26 R3: `apply_move` resolvia por `active()`. En una ventana con
    gap el movimiento del que se desmayo se le registraba al REEMPLAZO
    (medido contra el corpus: 2817 turnos con move + switch del mismo lado,
    ~7%). La linea nombra al actor (`p2a: Ludicolo`)."""
    snap = _snapshot_con_gap()
    snap["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ]
    out = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        snap, pre_applied=1,
    )
    por_especie = _por_especie(out)
    assert por_especie["latias"]["moves"] == [], (
        "el movimiento NO es del reemplazo: la linea nombra a ludicolo"
    )
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ], (
        "la linea es PRE-aplicada (el snapshot ya trae el uso): el PP del "
        "nombrado no se vuelve a descontar (criterio R3: 15, no 14)"
    )


def test_pp_no_se_descuenta_dos_veces_con_snapshot_post_narracion():
    """MON-26 R3: el doble descuento. poke-env ya descontó el PP al parsear
    la narracion del gap (`mon.moved(..., use=True)`), asi que el snapshot
    post-narracion trae pp=15; el proyector no puede volver a descontar la
    MISMA linea (quedaria 14 -- la firma de las 4 violaciones de
    hidden_information/moves de battle-gen6randombattle-120)."""
    snap = _snapshot_con_gap()
    snap["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ]
    out = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        snap, pre_applied=1,
    )
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ]

    # Flujo NORMAL (linea del frame de cierre, snapshot PRE-narracion): el
    # descuento SÍ se aplica, exactamente una vez.
    snap2 = _snapshot_con_gap()
    snap2["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": 16, "max_pp": 16}
    ]
    out2 = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        snap2, pre_applied=0,
    )
    assert _por_especie(out2)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 15, "max_pp": 16}
    ]


def test_transform_en_gap_copia_desde_el_nombrado_no_al_reemplazo():
    """MON-26 R3: `apply_transform` resolvia el transformer por `active()`.
    En una ventana con gap el reemplazo recibia moves/ability ajenos (y el
    transformer quedaba intacto). La linea nombra al transformer."""
    snap = _snapshot_con_gap()
    snap["me"]["pokemon"][0]["moves"] = [
        {"id": "sludgebomb", "pp": 16, "max_pp": 16}
    ]
    out = _proyectar(
        ["|-transform|p2a: Ludicolo|p1a: Tentacruel|[from] ability: Imposter",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        snap,
    )
    por_especie = _por_especie(out)
    assert por_especie["latias"]["moves"] == [], (
        "el moveset copiado NO es del reemplazo"
    )
    assert por_especie["latias"]["ability"] is None, (
        "la ability copiada NO es del reemplazo"
    )
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "sludgebomb", "pp": 5, "max_pp": 5}
    ], (
        "el transformer NOMBRADO recibe el moveset copiado (tope 5 de "
        "Transform en gen >= 5)"
    )
    assert por_especie["ludicolo"]["ability"] == "imposter"


def test_damage_con_of_revela_la_ability_del_nombrado_no_del_reemplazo():
    """MON-26 R3: la ruta `[of]` de `apply_damage_or_heal_ownership` que
    revela ability resolvia por `active()`. En una ventana con gap la
    ability del nombrado se le escribia al REEMPLAZO."""
    out = _proyectar(
        ["|-damage|p1a: Tentacruel|91/100|[from] ability: Drizzle|[of] p2a: Ludicolo",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(),
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["ability"] == "drizzle", (
        "la ability se escribe al NOMBRADO por el [of]"
    )
    assert por_especie["latias"]["ability"] is None, (
        "el reemplazo no recibe la ability ajena"
    )


def test_item_transferido_en_gap_se_le_quita_a_la_victima_nombrada():
    """MON-26 R3: `apply_item_transfer_ownership` resolvia la victima del
    Thief/Covet/Pickpocket por `active()`. En una ventana con gap el item
    se le quitaba al REEMPLAZO y la memoria D40 quedaba con la identidad
    equivocada."""
    memoria: dict[str, dict] = {}
    out = _proyectar(
        ["|-item|p1a: Tentacruel|Leftovers|[from] move: Thief|[of] p2a: Ludicolo",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(item="leftovers"), persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["item"] is None, (
        "el item se le quita a la VICTIMA nombrada por el [of]"
    )
    assert por_especie["latias"]["item"] == "unknown_item", (
        "el reemplazo no es la victima y conserva su item"
    )
    assert "item" not in memoria.get("latias", {}), (
        "la memoria D40 no puede sembrarse con la identidad equivocada"
    )


def test_pickpocket_en_gap_revela_la_ability_del_receptor_nombrado_no_del_reemplazo():
    """MON-27 + MON-26 R3 (canario pedido en la prep adversarial de Tasos,
    `/tmp/ludex-coordination/tasos-mon27-r1-prep.md` T-PP-5): el RECEPTOR de
    Pickpocket/Magician (`ident`, parts[2]) se resuelve por `named_target`,
    igual que el resto de la clase -- nunca por `active()`. En una ventana
    con gap, resolver por el activo post-narracion le escribiria la ability
    al REEMPLAZO en vez de al que de verdad la tiene."""
    memoria: dict[str, dict] = {}
    out = _proyectar(
        ["|-item|p2a: Ludicolo|Leftovers|[from] ability: Pickpocket|[of] p1a: Tentacruel",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(), persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["item"] == "leftovers", (
        "el item robado se le escribe al RECEPTOR nombrado"
    )
    assert por_especie["ludicolo"]["ability"] == "pickpocket", (
        "la ability se revela en el RECEPTOR nombrado, no en el reemplazo"
    )
    assert por_especie["latias"]["ability"] is None, (
        "el reemplazo no es nombrado por la linea y no recibe la ability"
    )
    assert "ability" not in memoria.get("latias", {}), (
        "la memoria no puede sembrarse con la identidad equivocada"
    )


# --- MON-26 R3: canarios pedidos por la revision (Tasos) ---
# Magic Bounce y Rocky Helmet en gap: las rutas con sufijos [from] ability/
# [of] item no pueden salpicar al reemplazo. Y los canarios de `-heal`, que
# fijan que `_owner_of -> named_target` NO cambia la salida final de las
# rutas de heal (ramas medidas como no-miembros).
# ---------------------------------------------------------------------------


def test_magic_bounce_en_gap_revela_la_ability_del_actor_nombrado():
    """`|move|p2a: X|...|[from] ability: Magic Bounce` en ventana con gap: la
    ability se revela del ACTOR nombrado (use/reveal=False para el eco), el
    reemplazo no recibe ni el movimiento ni la ability."""
    out = _proyectar(
        ["|move|p2a: Ludicolo|Toxic|p1a: Tentacruel|[from] ability: Magic Bounce",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(), pre_applied=1,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["ability"] == "magicbounce", (
        "la ability del sufijo es del actor NOMBRADO"
    )
    assert por_especie["latias"]["ability"] is None
    assert por_especie["latias"]["moves"] == [], (
        "el eco reflejado no es evidencia de pertenencia (ni para el "
        "reemplazo ni para el actor)"
    )


def test_rocky_helmet_en_gap_se_le_atribuye_al_dueño_nombrado():
    """`-damage|p1a: X|...|[from] item: Rocky Helmet|[of] p2a: Y` en gap: el
    item es de Y (el rival NOMBRADO por el [of]), el reemplazo no lo recibe
    y la memoria no se siembra con la identidad equivocada."""
    memoria: dict[str, dict] = {}
    out = _proyectar(
        ["|-damage|p1a: Tentacruel|88/100|[from] item: Rocky Helmet|[of] p2a: Ludicolo",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(), persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["item"] == "rockyhelmet", (
        "el item es del dueno NOMBRADO por el [of]"
    )
    assert por_especie["latias"]["item"] == "unknown_item"
    assert "item" not in memoria.get("latias", {})


def test_heal_por_item_propio_en_gap_no_cambia_la_salida_del_reemplazo():
    """Canario de `_owner_of -> named_target` (adjudicacion R3): la ruta de
    heal por item propio (`-heal|p2a: X|...|[from] item: Leftovers`) es una
    rama medida como NO-miembro; con la resolucion por nombre el reemplazo
    queda intacto y el item se le revela al NOMBRADO (que es a quien la
    linea se lo atribuye)."""
    memoria: dict[str, dict] = {}
    out = _proyectar(
        ["|-heal|p2a: Ludicolo|78/100|[from] item: Leftovers",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(), persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["latias"]["item"] == "unknown_item", (
        "la salida final del reemplazo no cambia"
    )
    assert por_especie["ludicolo"]["item"] == "leftovers"
    assert memoria.get("ludicolo", {}).get("item") == "leftovers"


def test_heal_con_of_en_gap_no_cambia_la_salida_del_reemplazo():
    """Canario de `_owner_of -> named_target`: la ruta `-heal` con `[of]`
    (Hospitality) resuelve al NOMBRADO y el reemplazo queda intacto."""
    out = _proyectar(
        ["|-heal|p2a: Latias|100/100|[from] ability: Hospitality|[of] p2a: Ludicolo",
         "|switch|p2a: Latias|Latias, L77, F|100/100"],
        _snapshot_con_gap(),
    )
    por_especie = _por_especie(out)
    assert por_especie["latias"]["ability"] is None, (
        "la salida final del reemplazo no cambia"
    )
    assert por_especie["ludicolo"]["ability"] == "hospitality"


def _snapshot_con_pp(pp: int) -> dict:
    snap = _snapshot_con_gap()
    snap["opponent"]["pokemon"][0]["moves"] = [
        {"id": "energyball", "pp": pp, "max_pp": 16}
    ]
    return snap


# ORACULO PP (MON-26 R3, las CUATRO celdas exactas adjudicadas):
# `pre_applied` significa estrictamente "linea procesada por poke-env ANTES
# del snapshot" (derivada del orden de frames: seq < seq del request), NO
# "hay un switch despues". Celdas 1/2 sin switch; celdas 3/4 con switch de
# cierre y ademas "reemplazo limpio".
@pytest.mark.parametrize("pp_snapshot,pre_applied,con_switch,esperado_nombrado", [
    (16, 0, False, 15),  # celda 1: PRE16 sin gap -> un descuento
    (15, 1, False, 15),  # celda 2: POST15 sin gap -> no re-descuenta (battle-120)
    (16, 0, True, 15),   # celda 3: PRE16 con gap -> un descuento
    (15, 1, True, 15),   # celda 4: POST15 con gap -> no re-descuenta
])
def test_oraculo_pp_cuatro_celdas(pp_snapshot, pre_applied, con_switch, esperado_nombrado):
    lineas = ["|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel"]
    if con_switch:
        lineas.append("|switch|p2a: Latias|Latias, L77, F|100/100")
    out = _proyectar(lineas, _snapshot_con_pp(pp_snapshot), pre_applied=pre_applied)
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": esperado_nombrado, "max_pp": 16}
    ]
    if con_switch:
        assert por_especie["latias"]["moves"] == [], (
            "el reemplazo no recibe el movimiento del nombrado"
        )


def test_pin_de_uso_repetido_sin_gap_sigue_descontando():
    """La bandera es el UNICO eje del descuento: un uso repetido del flujo
    normal (snapshot pp=10, linea del frame de cierre) descuenta igual. Una
    regla por-estado (p.ej. `pp == max_pp`) romperia este caso y queda
    prohibida por adjudicacion."""
    out = _proyectar(
        ["|move|p2a: Ludicolo|Energy Ball|p1a: Tentacruel"],
        _snapshot_con_pp(10), pre_applied=0,
    )
    assert _por_especie(out)["ludicolo"]["moves"] == [
        {"id": "energyball", "pp": 9, "max_pp": 16}
    ]


# ---------------------------------------------------------------------------
# MON-26 R2/R3: invariante ejecutable que cierra la CLASE.
#
# La clase adjudicada: "rama del despacho que escribe identidad persistente
# (llama a `remember_item` o `reveal_ability`, o lee `persistent_state`)
# resolviendo por `active()` en vez de por `named_target`". Sin este escaneo,
# la proxima linea de protocolo que agregue alguien reabre la clase en
# silencio (lo que paso ocho rondas seguidas en MON-20).
#
# R3: el escaneo es TRANSITIVO -- sigue las llamadas a los helpers anidados
# de `project_observable_state` e incluye sus cuerpos. La frontera de que
# helpers se siguen se DERIVA (alcance por llamadas), no se declara. Sin
# esto, los miembros que delegan en un helper (move/apply_move, -transform/
# apply_transform, -damage/-heal y -item-transferencia por `_owner_of`)
# quedaban invisibles (medido: 8 ramas C, 4 miembros vivos).
# ---------------------------------------------------------------------------

import ast
from collections import defaultdict
from pathlib import Path

# Allowlist JUSTIFICADA por escrito (medicion del tech lead, reproducida en
# R3 con sonda propia sobre el escenario de gap: narracion huerfana + switch
# de cierre). Son las ramas que escriben identidad resolviendo por active()
# pero cuyo efecto el switch de cierre DESCARTA, asi que no son miembros:
#   - `-start` (typechange): tipos temporales; el switch de cierre re-deriva.
#   - `-formechange` / `detailschange` (forme_change): idem.
#   - `replace` (end_illusion): la linea ES el desenmascaramiento; el
#     `|switch|` posterior confirma la identidad real.
#   - `-end` (Illusion): fuera del alcance adjudicado (R2). La linea
#     `|-end|p2a: X|Illusion` nombra al activo cuyo disfraz ACABA de
#     romperse; en la misma ventana viaja el `|replace|` que lo desenmascara,
#     y la ability que escribe ("illusion") es la del Zoroark real detras del
#     disfraz. Patron latente, adjudicado a otro issue si hace falta.
_ALLOWLIST_R3 = frozenset({"-end", "-start", "-formechange", "detailschange", "replace"})


def _ramas_del_despacho(src: str):
    """Las ramas `if/elif tag ...` del despacho de `project_observable_state`,
    como pares (tags, nodo If)."""
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "project_observable_state"
    )
    ramas = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        tags = set()
        for test in ([node.test] if not isinstance(node.test, ast.BoolOp)
                     else node.test.values):
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "tag"):
                for cmp_ in test.comparators:
                    if isinstance(cmp_, ast.Constant) and isinstance(cmp_.value, str):
                        tags.add(cmp_.value)
                    elif isinstance(cmp_, ast.Tuple):
                        tags.update(
                            el.value for el in cmp_.elts
                            if isinstance(el, ast.Constant) and isinstance(el.value, str)
                        )
        if tags:
            ramas.append((frozenset(tags), node))
    return ramas


def _subarbol_propio(node: ast.If):
    """Los nodos del CUERPO de la rama, sin bajar por el `orelse`.

    El despacho es una cadena de `elif`: cada rama vive en el `orelse` de la
    anterior. Si el escaneo bajara por `orelse`, cada rama acumularia las
    llamadas de TODAS las siguientes y la violacion de una quedaria tapada
    por el `named_target` de las demas (vacuidad medida en R2). Los `if`
    ANIDADOS dentro del cuerpo (p.ej. la logica de Trace) si se recorren
    completos: son logica propia de la rama."""
    out: list[ast.AST] = []
    pila = list(node.body)
    while pila:
        actual = pila.pop()
        out.append(actual)
        pila.extend(ast.iter_child_nodes(actual))
    return out


def _helpers_del_proyector(src: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "project_observable_state"
    )
    return {n.name: n for n in fn.body if isinstance(n, ast.FunctionDef)}


def _clausura(node: ast.If, helpers: dict[str, ast.FunctionDef]) -> list[ast.AST]:
    """Nodos de la rama MAS los de todos los helpers anidados que invoca, de
    forma transitiva. La frontera se DERIVA: solo se siguen funciones
    definidas dentro del proyector y alcanzables por llamada."""
    vistos: set[str] = set()
    nodos = _subarbol_propio(node)
    while True:
        nuevas = []
        for actual in nodos:
            if (isinstance(actual, ast.Call) and isinstance(actual.func, ast.Name)
                    and actual.func.id in helpers
                    and actual.func.id not in vistos):
                vistos.add(actual.func.id)
                nuevas.extend(_subarbol_propio(helpers[actual.func.id]))
        if not nuevas:
            return nodos
        nodos.extend(nuevas)


def _nodos_del_resolver_ordenado(helpers: dict[str, ast.FunctionDef]) -> set[int]:
    """Los nodos DENTRO de `named_target`, el resolver que la clase exige.

    `named_target` degrada documentadamente a `active()` cuando el nombre no
    esta en el equipo. Ese `active()` no puede contar como violacion, o el
    invariante se autodemanda: el mandato es que las ramas USEN el resolver
    por identidad, no que el resolver reimplemente la busqueda. Un
    `named_target` que resolviera mal es otro defecto, cubierto por los
    tests de gap."""
    if "named_target" not in helpers:
        return set()
    return {id(n) for n in ast.walk(helpers["named_target"])}


# Campos de IDENTIDAD persistente: los que el auditor audita como
# `UnresolvedField` (packages/dataset-audit/src/projection.ts:57) y que
# sobreviven entre decisiones. `hp`, `status` y `boosts` son volatiles y
# quedan fuera a proposito: los reescribe el switch de cierre.
_CAMPOS_IDENTIDAD = frozenset({"species", "ability", "item", "moves", "types"})


def _asignacion_de_identidad(nodo: ast.AST) -> bool:
    """`mon["ability"] = ...` / `mon["item"] = ...` / etc.: escritura directa
    de un campo de identidad sin pasar por remember_item/reveal_ability."""
    if not isinstance(nodo, ast.Assign):
        return False
    for target in nodo.targets:
        if (isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "mon"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in _CAMPOS_IDENTIDAD):
            return True
    return False


def test_el_despacho_resuelve_identidad_persistente_por_named_target():
    """MON-26 R2/R3: ninguna rama del despacho que escriba identidad
    persistente (llama a `remember_item`, `reveal_ability` o `register_move`,
    lee `persistent_state`, o ASIGNA un campo de identidad sobre `mon`) puede
    resolver su objetivo por `active()` en vez de `named_target`, ni
    directamente NI a traves de un helper anidado. Allowlist escrita arriba
    con justificacion medida.

    Canarios de no-vacuidad: (1) el escaneo vio el despacho real (>= 25
    ramas y >= 15 helpers derivados); (2) POR RAMA, no por tag: cada rama de
    un miembro tiene `named_target` en su clausura -- una rama DUPLICADA del
    mismo tag no puede quedar tapada por otra que si resuelve bien (medido
    con la mutacion de allowlist). Mutaciones medidas (ver D62-R3): volver
    cualquiera de los miembros a `active()` pone ESTE test en rojo nombrando
    la rama, y una rama NUEVA que escriba identidad por active() a traves de
    un helper NUEVO tambien (canario obligatorio de R3)."""
    src = Path(__file__).resolve().parents[2] / "src" / "ludex_agent" / "showdown" / "protocol.py"
    helpers = _helpers_del_proyector(src.read_text())
    ramas = _ramas_del_despacho(src.read_text())

    assert len(ramas) >= 25, (
        "el escaneo no vio el despacho real: sin ramas que revisar seria vacuo"
    )
    assert len(helpers) >= 15, (
        "la frontera derivada no vio los helpers del proyector: el escaneo "
        "transitivo no tendria nada que seguir"
    )
    excluidos = _nodos_del_resolver_ordenado(helpers)
    violaciones = []
    por_tag: dict[str, list[bool]] = defaultdict(list)
    for tags, node in ramas:
        clausura = _clausura(node, helpers)
        llamadas = {
            c.func.id for c in clausura
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and id(c) not in excluidos
        }
        nombres = {
            n.id for n in clausura
            if isinstance(n, ast.Name) and id(n) not in excluidos
        }
        escribe_identidad = (
            "remember_item" in llamadas
            or "reveal_ability" in llamadas
            or "register_move" in llamadas
            or "persistent_state" in nombres
            or any(_asignacion_de_identidad(n) for n in clausura)
        )
        if not escribe_identidad:
            continue
        resuelve_por_nombre = "named_target" in llamadas
        for tag in tags:
            por_tag[tag].append(resuelve_por_nombre)
        # Regla FUERTE (R3): la clausura de una rama que escribe identidad
        # no puede contener `active` EN ABSOLUTO (fuera del resolver
        # ordenado) -- ni siquiera junto a un `named_target` de otro dato:
        # medido con la mutacion M-transform (el mon del transformer a
        # `active()` quedaba tapado por el `named_target` de la fuente).
        if "active" in llamadas and not tags <= _ALLOWLIST_R3:
            violaciones.append(sorted(tags))
    assert violaciones == [], (
        f"ramas que escriben identidad persistente resolviendo por active(): "
        f"{violaciones} -- la clase adjudicada exige named_target (R3: "
        f"incluso a traves de helpers anidados, register_move y asignaciones)"
    )
    miembros = {
        "-item", "-enditem", "-ability", "-endability",
        "move", "-transform", "-damage", "-heal",
    }
    for tag in sorted(miembros):
        assert por_tag.get(tag) and all(por_tag[tag]), (
            f"CADA rama del miembro {tag!r} tiene que resolver por "
            f"named_target en su clausura -- una rama duplicada del mismo "
            f"tag no puede quedar tapada por otra (y la allowlist no puede "
            f"ocultar un miembro)"
        )


def test_secuencia_completa_de_la_batalla_67_limpia_el_item():
    """CARACTERIZACION del proyector, no prueba del arreglo (MON-26 R2, F2):
    este test PASA en la base 093296c y queda verde bajo las dos mutaciones
    de R1 (verificado por Tasos). Su valor es dejar escrita, con las lineas
    exactas de battle-gen6randombattle-67, la refutacion de la hipotesis del
    `-damage` (el proyector nunca fue el problema) y pinar el resultado
    correcto de la ventana completa.

    La ventana de la decision del turno 3 trae la narracion del turno 2
    (`-damage|0 fnt`, `-enditem`, `faint`) seguida del `switch` del
    reemplazo, sobre el snapshot post-narracion (fainted=True, active=True,
    item=None en poke-env) con la memoria D40 en airballoon. El item del
    desmayado queda None y el reemplazo intacto.

    La linea `-damage` PRECEDE a `-enditem` (hipotesis original del tech
    lead): medido, `active` NO se limpia ni ahi ni en `faint`, y la
    resolucion por identidad no depende de eso."""
    memoria: dict[str, dict] = {"probopass": {"item": "airballoon"}}
    probopass = {
        "species": "probopass", "hp_fraction": 0.0, "active": True,
        "fainted": True, "status": "FNT", "level": 91,
        "item": None, "ability": None, "types": ["ROCK", "STEEL"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "flashcannon", "pp": 15, "max_pp": 16}],
    }
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"] = [probopass]
    out = _proyectar(
        ["|move|p1a: Hariyama|Close Combat|p2a: Probopass",
         "|-supereffective|p2a: Probopass",
         "|-damage|p2a: Probopass|0 fnt",
         "|-unboost|p1a: Hariyama|def|1",
         "|-unboost|p1a: Hariyama|spd|1",
         "|-enditem|p2a: Probopass|Air Balloon",
         "|faint|p2a: Probopass",
         "|switch|p2a: Malamar|Malamar, L81, F|100/100"],
        snapshot, persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["probopass"]["item"] is None
    assert por_especie["probopass"]["fainted"] is True
    assert por_especie["probopass"]["active"] is False
    assert por_especie["malamar"]["item"] == "unknown_item"
    assert memoria["probopass"]["item"] is None


def test_adquisicion_posterior_reemplaza_la_memoria_anterior():
    """Requisito 5 del DESIGN VERDICT: evidencia nueva del frame actual
    SIEMPRE reemplaza lo que la memoria tenia antes, sea `None` (consumido) o
    un item distinto."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"][0]["item"] = "sitrusberry"
    _proyectar(
        ["|-enditem|p2a: Ludicolo|Sitrus Berry"], snapshot1, persistent_state=memoria,
    )
    assert memoria["ludicolo"]["item"] is None

    snapshot2 = _snapshot(gen=6)
    tras_adquisicion = _proyectar(
        ["|-item|p2a: Ludicolo|Leftovers|[from] move: Trick"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(tras_adquisicion)["ludicolo"]["item"] == "leftovers"
    assert memoria["ludicolo"]["item"] == "leftovers"


def test_item_del_rival_sobrevive_un_switch():
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"][0]["item"] = "unknown_item"
    _proyectar(
        ["|-item|p2a: Ludicolo|Life Orb|[from] move: Trick"],
        snapshot1, persistent_state=memoria,
    )
    assert memoria["ludicolo"]["item"] == "lifeorb"

    mandibuzz_entrando = {
        "species": "mandibuzz", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 84,
        "item": "unknown_item", "ability": None, "types": ["DARK", "FLYING"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"][0]["active"] = False
    snapshot2["opponent"]["pokemon"][0]["item"] = "choicescarf"  # snapshot fresco corrupto
    snapshot2["opponent"]["pokemon"].append(dict(mandibuzz_entrando))
    tras_switch_out = _proyectar(
        ["|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100"],
        snapshot2, persistent_state=memoria,
    )
    ludicolo_fuera = _por_especie(tras_switch_out)["ludicolo"]
    assert ludicolo_fuera["active"] is False
    assert ludicolo_fuera["item"] == "lifeorb"

    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"][0]["active"] = False
    snapshot3["opponent"]["pokemon"][0]["item"] = "choicescarf"
    snapshot3["opponent"]["pokemon"].append({**mandibuzz_entrando, "active": True})
    tras_switch_in = _proyectar(
        ["|switch|p2a: Ludicolo|Ludicolo, L88, F|100/100"],
        snapshot3, persistent_state=memoria,
    )
    ludicolo_dentro = _por_especie(tras_switch_in)["ludicolo"]
    assert ludicolo_dentro["active"] is True
    assert ludicolo_dentro["item"] == "lifeorb", (
        "la marca sobrevive el switch-out Y el switch-in"
    )


def test_dos_identidades_de_item_no_se_contaminan():
    """Aislamiento por identidad canonica: que Ludicolo tenga Life Orb
    conocido no puede afectar el item de un Weezing distinto."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"][0]["item"] = "unknown_item"
    snapshot["opponent"]["pokemon"].append({
        "species": "weezing", "hp_fraction": 1.0, "active": False,
        "fainted": False, "status": None, "level": 83,
        "item": "unknown_item", "ability": "levitate", "types": ["POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    })
    out = _proyectar(
        ["|-item|p2a: Ludicolo|Life Orb|[from] move: Trick"],
        snapshot, persistent_state=memoria,
    )
    por_especie = _por_especie(out)
    assert por_especie["ludicolo"]["item"] == "lifeorb"
    assert por_especie["weezing"]["item"] == "unknown_item", (
        "el item de Weezing no debe verse afectado: identidad distinta"
    )
    assert memoria["ludicolo"]["item"] == "lifeorb"


def test_una_linea_item_del_lado_propio_no_contamina_la_memoria_rival():
    """Guarda de mutacion: una linea `-item` que nombra a NUESTRO activo
    (`p1a:`) nunca puede sembrar `persistent_state` de ningun rival. Ya pasa
    hoy por el filtro generico de ident (mismo patron que el contrapeso
    negativo de D37, `test_sin_reusar_el_persistent_state_por_battle_tag_
    se_pierde_la_marca`: sirve de sentinela para la mutacion 3."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(gen=6)
    out = _proyectar(
        ["|-item|p1a: Tentacruel|Choice Scarf|[from] move: Trick"],
        snapshot, persistent_state=memoria,
    )
    assert _por_especie(out)["ludicolo"]["item"] == "unknown_item"
    assert memoria == {}, "una linea del lado propio no puede sembrar memoria del rival"


# ---------------------------------------------------------------------------
# D40 T-01 (MON-18 R4): transferencia de item (Thief/Covet/Pickpocket/
# Magician) hacia nuestro lado deja al rival sin item.
#
# `-item|p1a: Tentacruel|Leftovers|[from] move: Thief|[of] p2a: Ludicolo`:
# el `ident` (parts[2]) es quien RECIBE el item -- nuestro propio activo --
# y `[of]` nombra a quien lo pierde. Showdown nunca manda una linea `-item`
# ni `-enditem` separada para el que pierde: esta linea es la UNICA
# evidencia. El filtro generico de ident descartaba la linea completa antes
# de llegar a ningun handler, porque el ident nombra a nuestro lado -- la
# memoria del rival quedaba con el valor VIEJO para siempre.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [
    "[from] move: Thief",
    "[from] move: Covet",
    "[from] ability: Pickpocket",
    "[from] ability: Magician",
])
def test_transferencia_de_item_hacia_nuestro_lado_limpia_al_rival(suffix):
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"][0]["item"] = "unknown_item"
    _proyectar(
        ["|-item|p2a: Ludicolo|Life Orb|[from] move: Trick"],
        snapshot1, persistent_state=memoria,
    )
    assert memoria["ludicolo"]["item"] == "lifeorb"

    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"][0]["item"] = "lifeorb"
    tras_robo = _proyectar(
        [f"|-item|p1a: Tentacruel|Leftovers|{suffix}|[of] p2a: Ludicolo"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(tras_robo)["ludicolo"]["item"] is None
    assert memoria["ludicolo"]["item"] is None
    assert "ability" not in memoria.get("ludicolo", {}), (
        "MON-27: la ability de la VICTIMA (Ludicolo, a quien le robaron) "
        "nunca se toca -- Pickpocket/Magician revelan la ability de quien "
        "RECIBE el item (nuestro lado en este caso, ya conocido por "
        "el request privado), no de quien lo pierde"
    )

    # Snapshot fresco: poke-env todavia cree que Ludicolo tiene lifeorb (no
    # se entera de que lo perdio) -- la memoria tiene que sostener el None.
    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"][0]["item"] = "lifeorb"
    tras_snapshot_stale = _proyectar([], snapshot3, persistent_state=memoria)
    assert _por_especie(tras_snapshot_stale)["ludicolo"]["item"] is None


@pytest.mark.parametrize(
    ("suffix", "ability_esperada"),
    [
        ("[from] move: Thief", None),
        ("[from] move: Covet", None),
        ("[from] ability: Pickpocket", "pickpocket"),
        ("[from] ability: Magician", "magician"),
    ],
)
def test_transferencia_de_item_desde_nuestro_lado_actualiza_al_rival(suffix, ability_esperada):
    """Contrapeso: cuando el RIVAL es quien adquiere nuestro item, el
    `ident` de la linea YA es el rival -- el handler normal de `-item`
    (tras el filtro generico) sigue cubriendo esta direccion sin cambios.

    MON-27: cuando la causa es una ABILITY (Pickpocket/Magician), esta misma
    linea es la UNICA evidencia publica de que el receptor la tiene --
    Showdown nunca manda una linea `-ability` separada para estas dos
    (D40 T-01). Se revela en el receptor NOMBRADO por la linea (mecanismo
    `named_target` de MON-26), nunca por `active()`. Thief/Covet son
    MOVIMIENTOS: no revelan ninguna ability."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"][0]["item"] = "unknown_item"
    snapshot["opponent"]["pokemon"][0]["ability"] = None
    out = _proyectar(
        [f"|-item|p2a: Ludicolo|Leftovers|{suffix}|[of] p1a: Tentacruel"],
        snapshot, persistent_state=memoria,
    )
    ludicolo = _por_especie(out)["ludicolo"]
    assert ludicolo["item"] == "leftovers"
    assert memoria["ludicolo"]["item"] == "leftovers"
    assert ludicolo["ability"] == ability_esperada
    if ability_esperada is None:
        assert "ability" not in memoria.get("ludicolo", {}), (
            "Thief/Covet no revelan ninguna ability: no hay nada que backupear"
        )


def test_transferencia_de_item_no_contamina_otra_identidad_rival():
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"][0]["item"] = "lifeorb"
    snapshot["opponent"]["pokemon"].append({
        "species": "weezing", "hp_fraction": 1.0, "active": False,
        "fainted": False, "status": None, "level": 83,
        "item": "leftovers", "ability": "levitate", "types": ["POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    })
    out = _proyectar(
        ["|-item|p1a: Tentacruel|Life Orb|[from] move: Thief|[of] p2a: Ludicolo"],
        snapshot, persistent_state=memoria,
    )
    por = _por_especie(out)
    assert por["ludicolo"]["item"] is None
    assert por["weezing"]["item"] == "leftovers", "Weezing no debe verse afectado"
    assert "weezing" not in memoria


# ---------------------------------------------------------------------------
# D40 T-02 (MON-18 R4): provenance del item bajo Illusion.
#
# La memoria de D40 no distingue "evidencia sobre esta identidad" de
# "evidencia observada mientras otro pokemon la usaba de disfraz". Sin esta
# correccion, un item revelado mientras Zoroark imita a Mandibuzz queda
# pegado a la entrada de Mandibuzz para siempre, incluso despues de que el
# `|replace|` confirme que era Zoroark todo el tiempo.
# ---------------------------------------------------------------------------


def _mandibuzz_rival(item="unknown_item", active=True):
    return {
        "species": "mandibuzz", "hp_fraction": 1.0, "active": active,
        "fainted": False, "status": None, "level": 84,
        "item": item, "ability": None, "types": ["DARK", "FLYING"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }


def _zoroark_rival(item="unknown_item", active=True):
    return {
        "species": "zoroark", "hp_fraction": 1.0, "active": active,
        "fainted": False, "status": None, "level": 84,
        "item": item, "ability": "illusion", "types": ["DARK"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }


def test_illusion_restaura_el_item_previo_del_imitado_al_romperse():
    """Mandibuzz ya tenia `leftovers` conocido; mientras Zoroark lo imita,
    se revela `lifeorb` (en realidad el item de Zoroark); al romperse la
    Illusion, Mandibuzz tiene que recuperar `leftovers` -- no quedarse con
    `lifeorb` ni pasarselo a Zoroark. Otras claves de `persistent_state`
    (`ability`) no pueden perderse en el proceso."""
    memoria: dict[str, dict] = {"mandibuzz": {"item": "leftovers", "ability": "overcoat"}}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_mandibuzz_rival(item="leftovers")]
    tras_reveal = _proyectar(
        ["|-item|p2a: Mandibuzz|Life Orb|[from] move: Trick"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_reveal)["mandibuzz"]["item"] == "lifeorb"
    assert memoria["mandibuzz"]["item"] == "lifeorb"

    # Llamada fresca intermedia, sin nueva evidencia de item: el backup
    # tiene que sobrevivir el snapshot fresco, igual que la memoria misma.
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [_mandibuzz_rival(item="lifeorb")]
    tras_intermedia = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_intermedia)["mandibuzz"]["item"] == "lifeorb"

    # La Illusion se rompe: Zoroark era el disfrazado.
    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"] = [_mandibuzz_rival(item="lifeorb")]
    tras_replace = _proyectar(
        ["|replace|p2a: Zoroark|Zoroark, L84, M"],
        snapshot3, persistent_state=memoria,
    )
    por = _por_especie(tras_replace)
    assert por["mandibuzz"]["item"] == "leftovers", (
        "recupera el item previo, no el revelado durante el disfraz"
    )
    assert por["zoroark"]["item"] == "unknown_item", (
        "Zoroark no hereda el item observado durante Illusion"
    )
    assert memoria["mandibuzz"]["item"] == "leftovers"
    assert "item_backup" not in memoria["mandibuzz"]
    assert memoria["mandibuzz"]["ability"] == "overcoat", (
        "otras claves de persistent_state (ability, types, moves, PP) no "
        "pueden perderse al restaurar el item"
    )

    # Mandibuzz (el real) switchea despues: tiene que seguir mostrando
    # `leftovers`, nunca el item de Zoroark, aunque el snapshot fresco
    # todavia diga `lifeorb`.
    snapshot4 = _snapshot(gen=6)
    snapshot4["opponent"]["pokemon"] = [
        _mandibuzz_rival(item="lifeorb", active=False),
        _zoroark_rival(active=True),
    ]
    tras_switch = _proyectar(
        ["|switch|p2a: Mandibuzz|Mandibuzz, L84, F|100/100"],
        snapshot4, persistent_state=memoria,
    )
    assert _por_especie(tras_switch)["mandibuzz"]["item"] == "leftovers"


def test_illusion_sin_memoria_previa_vuelve_a_clave_ausente_al_romperse():
    """Si antes del disfraz no habia NINGUNA evidencia sobre Mandibuzz, tras
    el `replace` la clave `item` tiene que quedar AUSENTE -- no `None`, que
    significaria 'confirmado sin item'."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_mandibuzz_rival(item="unknown_item")]
    _proyectar(
        ["|-item|p2a: Mandibuzz|Life Orb|[from] move: Trick"],
        snapshot1, persistent_state=memoria,
    )
    assert memoria["mandibuzz"]["item"] == "lifeorb"

    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [_mandibuzz_rival(item="lifeorb")]
    tras_replace = _proyectar(
        ["|replace|p2a: Zoroark|Zoroark, L84, M"],
        snapshot2, persistent_state=memoria,
    )
    por = _por_especie(tras_replace)
    assert por["mandibuzz"]["item"] == "unknown_item"
    assert "item" not in memoria.get("mandibuzz", {}), (
        "sin memoria previa, tras el replace la clave 'item' debe quedar "
        "AUSENTE, no None"
    )


def test_illusion_item_none_previo_se_restaura_como_none_no_ausente():
    """T-03 (LINEAR_VERDICT R4): falta un canario para el TERCER estado de
    memoria previa -- clave `item` PRESENTE con valor `None` (p.ej. un
    `-enditem` anterior ya habia confirmado que Mandibuzz no tenia item).
    Los tests existentes sólo cubrian item concreto y clave ausente; una
    mutación que colapsara `None` en ausencia (`entry.get("item") or
    _NO_PRIOR_ITEM`, donde `None` es falsy) pasaria esos dos sin problema y
    seguiria corrompiendo este tercer caso -- exactamente lo que este test
    existe para impedir."""
    memoria: dict[str, dict] = {"mandibuzz": {"item": None, "ability": "overcoat"}}
    assert "item" in memoria["mandibuzz"], "arranca con la clave PRESENTE, no ausente"

    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_mandibuzz_rival(item="unknown_item")]
    tras_reveal = _proyectar(
        ["|-item|p2a: Mandibuzz|Life Orb|[from] move: Trick"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_reveal)["mandibuzz"]["item"] == "lifeorb"
    assert memoria["mandibuzz"]["item"] == "lifeorb"

    # Llamada fresca e independiente, sin nueva evidencia de item.
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [_mandibuzz_rival(item="lifeorb")]
    tras_intermedia = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_intermedia)["mandibuzz"]["item"] == "lifeorb"

    # La Illusion se rompe.
    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"] = [_mandibuzz_rival(item="lifeorb")]
    tras_replace = _proyectar(
        ["|replace|p2a: Zoroark|Zoroark, L84, M"],
        snapshot3, persistent_state=memoria,
    )
    por = _por_especie(tras_replace)
    assert por["mandibuzz"]["item"] is None, (
        "recupera el None previo, no el lifeorb revelado durante el disfraz"
    )
    assert "item" in memoria["mandibuzz"], (
        "la clave 'item' tiene que seguir PRESENTE con None -- no "
        "desaparecer como si nunca hubiera habido evidencia"
    )
    assert memoria["mandibuzz"]["item"] is None
    assert "item_backup" not in memoria["mandibuzz"]
    assert memoria["mandibuzz"]["ability"] == "overcoat", (
        "otras claves de persistent_state no pueden perderse al restaurar"
    )


def test_illusion_backup_un_switch_ordinario_confirma_el_item_nuevo():
    """Si el disfraz nunca se rompe (sale del campo con un switch normal,
    no un `replace`), la identidad aparente queda confirmada: el item
    nuevo es permanente y el backup se descarta."""
    memoria: dict[str, dict] = {"mandibuzz": {"item": "leftovers"}}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_mandibuzz_rival(item="leftovers")]
    _proyectar(
        ["|-item|p2a: Mandibuzz|Life Orb|[from] move: Trick"],
        snapshot1, persistent_state=memoria,
    )
    assert memoria["mandibuzz"]["item"] == "lifeorb"

    # Mandibuzz sigue activo en el snapshot (todavia no se proceso ningun
    # switch): es la linea `|switch|` la que dispara `switch_out` sobre
    # quien este activo AHORA, no un flag pre-armado a mano.
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [
        _mandibuzz_rival(item="lifeorb", active=True),
        {"species": "weezing", "hp_fraction": 1.0, "active": False,
         "fainted": False, "status": None, "level": 83,
         "item": "unknown_item", "ability": "levitate", "types": ["POISON"],
         "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                    "evasion": 0, "accuracy": 0}, "moves": []},
    ]
    out = _proyectar(
        ["|switch|p2a: Weezing|Weezing, L83, F|100/100"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(out)["mandibuzz"]["item"] == "lifeorb"
    assert "item_backup" not in memoria["mandibuzz"]


# ---------------------------------------------------------------------------
# MON-19 (D41): `canonical_types` -- tipos permanentes del rival, corregidos
# por el mismo hueco arquitectonico que D40 ya resolvio para `item`.
#
# ROOT-CAUSE CHECKPOINT: `Pokemon._update_from_details` de poke-env corta en
# seco si `details` no cambio desde la ultima vez ("if details ==
# self._last_details: return"), asi que tras un `-formechange` a una forma
# TEMPORAL (Relic Song), poke-env nunca vuelve a derivar `_type_1`/`_type_2`
# del dex en switches posteriores con el mismo `details` base. `switch_in()`
# corrige la llamada donde el switch ocurre (siempre recalcula del dex), pero
# esa correccion no sobrevive a la SIGUIENTE llamada si nada vuelve a
# nombrar a esa identidad -- el mismo patron que D40 corrigio para `item`.
#
# `canonical_types` es tipos permanentes/canonicos PUBLICAMENTE establecidos
# (switch_in, detailschange/Mega). La clave YA EXISTENTE `"types"` sigue
# significando exclusivamente "backup a restaurar de un override temporal
# activo" (typechange, Transform, y ahora tambien `-formechange`/Relic
# Song). Mientras `"types"` este presente, `canonical_types` NUNCA se
# reaplica -- son mutuamente excluyentes, mismo patron que D37 exige entre
# `unknown_pp_moves`/`transform_unknown_pp_moves`.
# ---------------------------------------------------------------------------


def _meloetta_rival(types, item="unknown_item", active=True):
    return {
        "species": "meloetta", "hp_fraction": 0.53, "active": active,
        "fainted": False, "status": None, "level": 82,
        "item": item, "ability": None, "types": types,
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }


def _weezing_rival(active=True):
    return {
        "species": "weezing", "hp_fraction": 1.0, "active": active,
        "fainted": False, "status": None, "level": 83,
        "item": "unknown_item", "ability": "levitate", "types": ["POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }


def _charizard_rival(types, active=True, species="charizard"):
    return {
        "species": species, "hp_fraction": 1.0, "active": active,
        "fainted": False, "status": None, "level": 79,
        "item": "unknown_item", "ability": None, "types": types,
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }


def _mawile_rival(ability, active=True, species="mawile"):
    """`battle-gen6randombattle-120` real (MON-27): Mawile no cambia de
    tipos al mega evolucionar, a diferencia de Charizard-X."""
    return {
        "species": species, "hp_fraction": 1.0, "active": active,
        "fainted": False, "status": None, "level": 76,
        "item": "unknown_item", "ability": ability, "types": ["STEEL", "FAIRY"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [],
    }


def test_meloetta_relic_song_revierte_tras_switch_out_con_snapshot_fresco():
    """Reproduccion exacta de `battles.id=2787` (`battle-gen6randombattle-
    2719`): Aria -> Relic Song (`-formechange` a Pirouette) -> Pirouette
    persiste mientras sigue activa, incluso con una llamada fresca
    intermedia sin evidencia nueva -> switch-out (revierte) -> switch-in con
    los MISMOS `details` base -> una llamada fresca POSTERIOR, sin ninguna
    linea nueva para Meloetta, sigue en NORMAL/PSYCHIC. El bug real la dejaba
    en NORMAL/FIGHTING en ese ultimo paso."""
    memoria: dict[str, dict] = {}

    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "PSYCHIC"])]
    tras_entrada = _proyectar(
        ["|switch|p2a: Meloetta|Meloetta, L82|100/100"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_entrada)["meloetta"]["types"] == ["NORMAL", "PSYCHIC"]
    assert memoria["meloetta"]["canonical_types"] == ["NORMAL", "PSYCHIC"]
    assert "types" not in memoria["meloetta"], "switch_in no crea backup temporal"

    # Relic Song: -formechange a Pirouette (forma TEMPORAL, no permanente).
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "PSYCHIC"])]
    tras_relic_song = _proyectar(
        ["|move|p2a: Meloetta|Relic Song|p1a: Xatu",
         "|-formechange|p2a: Meloetta|Meloetta-Pirouette|[msg]"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(tras_relic_song)["meloetta"]["types"] == ["NORMAL", "FIGHTING"]
    assert memoria["meloetta"]["types"] == ["NORMAL", "PSYCHIC"], "backup temporal"
    assert memoria["meloetta"]["canonical_types"] == ["NORMAL", "PSYCHIC"], (
        "-formechange NO actualiza canonical_types"
    )

    # Llamada fresca intermedia, SIN ninguna linea para Meloetta: Pirouette
    # tiene que sostenerse sola mientras el override sigue activo (el
    # snapshot fresco de abajo deliberadamente NO confirma Pirouette).
    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "FIGHTING"], active=True)]
    tras_intermedia = _proyectar([], snapshot3, persistent_state=memoria)
    assert _por_especie(tras_intermedia)["meloetta"]["types"] == ["NORMAL", "FIGHTING"], (
        "Pirouette tiene que sostenerse mientras el override sigue activo"
    )

    # Switch-out (Weezing entra): el backup temporal se restaura.
    snapshot4 = _snapshot(gen=6)
    snapshot4["opponent"]["pokemon"] = [
        _meloetta_rival(["NORMAL", "FIGHTING"], active=True),
        _weezing_rival(active=False),
    ]
    tras_switch_out = _proyectar(
        ["|switch|p2a: Weezing|Weezing, L83, F|100/100"],
        snapshot4, persistent_state=memoria,
    )
    meloetta_fuera = _por_especie(tras_switch_out)["meloetta"]
    assert meloetta_fuera["active"] is False
    assert meloetta_fuera["types"] == ["NORMAL", "PSYCHIC"], "revierte al salir"
    assert "types" not in memoria["meloetta"], "el backup temporal se consume"
    assert memoria["meloetta"]["canonical_types"] == ["NORMAL", "PSYCHIC"]

    # Switch-in con los MISMOS details base ("Meloetta, L82") -- el snapshot
    # fresco de abajo simula el valor stale medido en vivo de poke-env.
    snapshot5 = _snapshot(gen=6)
    snapshot5["opponent"]["pokemon"] = [
        _weezing_rival(active=True),
        _meloetta_rival(["NORMAL", "FIGHTING"], active=False),
    ]
    tras_switch_in = _proyectar(
        ["|switch|p2a: Meloetta|Meloetta, L82|53/100"],
        snapshot5, persistent_state=memoria,
    )
    meloetta_dentro = _por_especie(tras_switch_in)["meloetta"]
    assert meloetta_dentro["active"] is True
    assert meloetta_dentro["types"] == ["NORMAL", "PSYCHIC"]

    # Llamada fresca POSTERIOR, sin ninguna linea nueva para Meloetta: tiene
    # que seguir en NORMAL/PSYCHIC. Esto es lo que medimos roto en
    # battles.id=2787 (quedaba en NORMAL/FIGHTING).
    snapshot6 = _snapshot(gen=6)
    snapshot6["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "FIGHTING"], active=True)]
    tras_llamada_fresca = _proyectar([], snapshot6, persistent_state=memoria)
    assert _por_especie(tras_llamada_fresca)["meloetta"]["types"] == ["NORMAL", "PSYCHIC"], (
        "la llamada fresca posterior tiene que seguir en NORMAL/PSYCHIC -- "
        "el bug real medido en battles.id=2787 la dejaba en NORMAL/FIGHTING"
    )


def test_meloetta_sin_formechange_no_cambia():
    """Contrapeso: sin ningun Relic Song de por medio, Meloetta nunca tiene
    override temporal ni necesita canonical_types para sostenerse -- el
    snapshot fresco de poke-env ya es correcto en todo momento."""
    memoria: dict[str, dict] = {}
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "PSYCHIC"])]
    out = _proyectar(
        ["|switch|p2a: Meloetta|Meloetta, L82|100/100"],
        snapshot, persistent_state=memoria,
    )
    assert _por_especie(out)["meloetta"]["types"] == ["NORMAL", "PSYCHIC"]
    assert memoria["meloetta"]["canonical_types"] == ["NORMAL", "PSYCHIC"]
    assert "types" not in memoria["meloetta"]


def test_mega_conserva_permanentemente_sus_tipos_con_snapshot_fresco_independiente():
    """Contrapeso Mega: `detailschange` es PERMANENTE y actualiza
    `canonical_types`, nunca crea un backup temporal. Verificado con
    snapshots frescos genuinamente independientes en cada paso -- a
    diferencia de `test_una_mega_conserva_sus_tipos_al_salir_en_otra_
    llamada` (ya aceptado), que encadena la salida proyectada anterior."""
    memoria: dict[str, dict] = {}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_charizard_rival(["FIRE", "FLYING"])]
    _proyectar(
        ["|switch|p2a: Charizard|Charizard, L79, M|100/100"],
        snapshot1, persistent_state=memoria,
    )
    assert memoria["charizard"]["canonical_types"] == ["FIRE", "FLYING"]

    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [_charizard_rival(["FIRE", "FLYING"])]
    _proyectar(
        ["|detailschange|p2a: Charizard|Charizard-Mega-X, L79, M"],
        snapshot2, persistent_state=memoria,
    )
    assert memoria["charizard"]["canonical_types"] == ["FIRE", "DRAGON"]
    assert "types" not in memoria["charizard"], "detailschange no crea backup temporal"

    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"] = [
        _charizard_rival(["FIRE", "DRAGON"], active=True),
        _weezing_rival(active=False),
    ]
    tras_switch_out = _proyectar(
        ["|switch|p2a: Weezing|Weezing, L83, F|100/100"],
        snapshot3, persistent_state=memoria,
    )
    charizard_fuera = _por_especie(tras_switch_out)["charizard"]
    assert charizard_fuera["types"] == ["FIRE", "DRAGON"], "Mega persiste tras salir"

    snapshot4 = _snapshot(gen=6)
    snapshot4["opponent"]["pokemon"] = [
        _weezing_rival(active=True),
        _charizard_rival(["FIRE", "DRAGON"], active=False, species="charizardmegax"),
    ]
    tras_switch_in = _proyectar(
        ["|switch|p2a: Charizard|Charizard-Mega-X, L79, M|100/100"],
        snapshot4, persistent_state=memoria,
    )
    charizard_dentro = _por_especie(tras_switch_in)["charizardmegax"]
    assert charizard_dentro["active"] is True
    assert charizard_dentro["types"] == ["FIRE", "DRAGON"]


def test_mega_mawile_conserva_hugepower_pese_a_dos_revelaciones_identicas_de_intimidate():
    """MON-27 causa raiz, reproduccion de `battle-gen6randombattle-120`
    (turnos 12/14/15/19 reales, ver
    `/tmp/ludex-coordination/neoblex-mon27-mega-diagnosis.md`).

    Intimidate se revela dos veces con el MISMO valor: switch-in ordinario
    (T12) y un cambio forzado que la trae de vuelta, todavia en forma base
    (T14). Con `reveal_ability` sin el guard de igualdad, la segunda
    revelacion sembraba un backup espurio `persistent_state["mawile"]
    ["ability"] = "intimidate"`. Luego Mega Mawile (T15, `detailschange`
    PERMANENTE) pasa a `hugepower` -- pero `forme_change` nunca toca ese
    backup, y `switch_out` (T19, snapshot fresco INDEPENDIENTE) lo restauraba
    incondicionalmente SOBRE la ability de la Mega. Verificado con snapshots
    frescos genuinamente independientes en cada paso, como
    `test_mega_conserva_permanentemente_sus_tipos_con_snapshot_fresco_
    independiente`."""
    memoria: dict[str, dict] = {}

    # T12: switch-in, primera revelacion de Intimidate (actual=None -> pasa
    # a ser la base persistente; no siembra backup).
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"] = [_mawile_rival(None)]
    tras_primera = _proyectar(
        ["|switch|p2a: Mawile|Mawile, L76, M|100/100",
         "|-ability|p2a: Mawile|Intimidate|boost"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_primera)["mawile"]["ability"] == "intimidate"
    # `switch_in` SI siembra `canonical_types` (D41, no relacionado): lo que
    # la primera revelacion de ability no puede sembrar es un backup.
    assert "ability" not in memoria.get("mawile", {})

    # T14: Mawile vuelve a entrar, todavia en forma base -- Intimidate se
    # re-anuncia con el MISMO valor. Snapshot fresco INDEPENDIENTE: la
    # ability ya conocida llega en el snapshot, como la reportaria poke-env.
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"] = [_mawile_rival("intimidate")]
    tras_segunda = _proyectar(
        ["|switch|p2a: Mawile|Mawile, L76, M|100/100",
         "|-ability|p2a: Mawile|Intimidate|boost"],
        snapshot2, persistent_state=memoria,
    )
    assert _por_especie(tras_segunda)["mawile"]["ability"] == "intimidate"
    assert "ability" not in memoria.get("mawile", {}), (
        "la re-revelacion IDENTICA no puede sembrar backup"
    )

    # T15: detailschange a Mega -- PERMANENTE, ability pasa a hugepower.
    snapshot3 = _snapshot(gen=6)
    snapshot3["opponent"]["pokemon"] = [_mawile_rival("intimidate")]
    tras_mega = _proyectar(
        ["|detailschange|p2a: Mawile|Mawile-Mega, L76, M"],
        snapshot3, persistent_state=memoria,
    )
    assert _por_especie(tras_mega)["mawile"]["ability"] == "hugepower"
    assert "ability" not in memoria.get("mawile", {})

    # T19: switch-out (Weezing entra). Snapshot fresco INDEPENDIENTE con la
    # Mega ya en hugepower -- asi lo reporta la property `ability` real de
    # poke-env (confirmado con una simulacion local de poke_env==0.15.0
    # contra esta misma secuencia, ver el diagnostico). Sin el fix,
    # switch_out restauraba el backup espurio "intimidate" sobre esto.
    snapshot4 = _snapshot(gen=6)
    snapshot4["opponent"]["pokemon"] = [
        _mawile_rival("hugepower", active=True, species="mawilemega"),
        _weezing_rival(active=False),
    ]
    tras_switch_out = _proyectar(
        ["|switch|p2a: Weezing|Weezing, L83, F|100/100"],
        snapshot4, persistent_state=memoria,
    )
    mawile_fuera = _por_especie(tras_switch_out)["mawilemega"]
    assert mawile_fuera["active"] is False
    assert mawile_fuera["ability"] == "hugepower", (
        "la ability de la Mega es permanente: switch_out no puede restaurar "
        "un backup sembrado por una re-revelacion identica anterior"
    )
    assert "ability" not in memoria.get("mawile", {})


def test_typechange_temporal_sigue_activo_con_canonical_types_presente():
    """Contrapeso typechange: mientras el override temporal esta activo, la
    reaplicacion de canonical_types NO debe pisarlo. A diferencia de
    `-formechange`, `-start|typechange` no pasa por `_update_from_details`
    de poke-env (`_temporary_types` es un atributo separado, sin el mismo
    corte por cache), asi que un snapshot fresco GENUINO durante un
    typechange activo ya trae el valor temporal correcto (`FIRE`) -- lo que
    este test verifica es que `canonical_types`, sembrado con el valor BASE,
    no lo pise."""
    memoria: dict[str, dict] = {"ludicolo": {"canonical_types": ["WATER", "GRASS"]}}
    snapshot1 = _snapshot(gen=6)
    snapshot1["opponent"]["pokemon"][0]["types"] = ["WATER", "GRASS"]
    tras_typechange = _proyectar(
        ["|-start|p2a: Ludicolo|typechange|Fire"],
        snapshot1, persistent_state=memoria,
    )
    assert _por_especie(tras_typechange)["ludicolo"]["types"] == ["FIRE"]
    assert memoria["ludicolo"]["types"] == ["WATER", "GRASS"]

    # Snapshot fresco INDEPENDIENTE que YA refleja el typechange activo
    # (asi lo entregaria poke-env de verdad, sin lineas nuevas esta vez).
    snapshot2 = _snapshot(gen=6)
    snapshot2["opponent"]["pokemon"][0]["types"] = ["FIRE"]
    tras_fresco = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_fresco)["ludicolo"]["types"] == ["FIRE"], (
        "canonical_types (WATER/GRASS) no puede pisar un override temporal activo"
    )


def test_transform_sigue_activo_con_canonical_types_presente():
    """Contrapeso Transform: mismo mecanismo de backup/restauracion que
    typechange (`apply_transform` puebla la MISMA clave `"types"`) -- la
    reaplicacion de canonical_types no debe pisarlo. `-transform` tampoco
    pasa por `_update_from_details`, asi que un snapshot fresco GENUINO
    durante un Transform activo ya trae el valor copiado correcto; lo que
    este test verifica es que `canonical_types` (sembrado con el valor
    BASE, `NORMAL`) no lo pise."""
    memoria: dict[str, dict] = {"ditto": {"canonical_types": ["NORMAL"]}}
    tras_transform = _proyectar([
        "|switch|p2a: Ditto|Ditto, L84|100/100",
        "|-transform|p2a: Ditto|p1a: Tentacruel|[from] ability: Imposter",
    ], _snapshot_transform(), persistent_state=memoria)
    ditto = _por_especie(tras_transform)["ditto"]
    assert ditto["types"] == ["WATER", "POISON"]
    assert memoria["ditto"]["types"] == ["NORMAL"]

    # Snapshot fresco INDEPENDIENTE que YA refleja el Transform activo (los
    # tipos copiados de Tentacruel), sin lineas nuevas esta vez.
    snapshot2 = _snapshot(gen=6, me={"pokemon": [{
        "species": "tentacruel", "active": True,
        "types": ["WATER", "POISON"], "ability": "liquidooze",
        "boosts": {"spa": 2, "atk": 0, "def": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 20, "max_pp": 24}],
    }]})
    snapshot2["opponent"]["pokemon"] = [{
        "species": "ditto", "hp_fraction": 1.0, "active": True,
        "fainted": False, "status": None, "level": 84,
        "item": "unknown_item", "ability": "imposter", "types": ["WATER", "POISON"],
        "boosts": {"atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0,
                   "evasion": 0, "accuracy": 0},
        "moves": [{"id": "scald", "pp": 20, "max_pp": 24}],
    }]
    tras_fresco = _proyectar([], snapshot2, persistent_state=memoria)
    assert _por_especie(tras_fresco)["ditto"]["types"] == ["WATER", "POISON"], (
        "Transform sigue activo, canonical_types no debe pisarlo"
    )


def test_reaplicacion_de_canonical_types_ocurre_antes_del_loop_de_lineas():
    """Canario de orden (mismo patron que D37/D40): si la reaplicacion de
    canonical_types corriera DESPUES del loop de lineas, el backup temporal
    que un `-formechange` NUEVO en la MISMA llamada captura seria el valor
    crudo del snapshot (potencialmente stale), no el canonico ya
    reaplicado."""
    memoria: dict[str, dict] = {"meloetta": {"canonical_types": ["NORMAL", "PSYCHIC"]}}
    snapshot = _snapshot(gen=6)
    # snapshot fresco con un valor STALE deliberado, para que la reaplicacion
    # (si corre a tiempo) lo corrija ANTES de que -formechange capture backup.
    snapshot["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "FIGHTING"], active=True)]
    _proyectar(
        ["|move|p2a: Meloetta|Relic Song|p1a: Xatu",
         "|-formechange|p2a: Meloetta|Meloetta-Pirouette|[msg]"],
        snapshot, persistent_state=memoria,
    )
    assert memoria["meloetta"]["types"] == ["NORMAL", "PSYCHIC"], (
        "el backup temporal tiene que capturar el canonico YA reaplicado "
        "(PSYCHIC), no el valor crudo del snapshot fresco (FIGHTING)"
    )


def test_switch_in_descarta_un_override_temporal_colgado_de_la_misma_identidad():
    """`switch_in` tiene que terminar cualquier override temporal anterior
    de esta identidad (contrato MON-19, punto 4) -- defensivo: en curso
    normal `switch_out` ya lo limpia, pero switch_in no puede depender de
    eso para sembrar tipos correctos."""
    memoria: dict[str, dict] = {
        "meloetta": {"canonical_types": ["NORMAL", "PSYCHIC"], "types": ["NORMAL", "PSYCHIC"]},
    }
    snapshot = _snapshot(gen=6)
    snapshot["opponent"]["pokemon"] = [_meloetta_rival(["NORMAL", "FIGHTING"], active=False)]
    out = _proyectar(
        ["|switch|p2a: Meloetta|Meloetta, L82|53/100"],
        snapshot, persistent_state=memoria,
    )
    assert _por_especie(out)["meloetta"]["types"] == ["NORMAL", "PSYCHIC"]
    assert "types" not in memoria["meloetta"], (
        "switch_in descarta cualquier override temporal colgado, no lo deja "
        "para que un switch_out posterior lo restaure con datos viejos"
    )
    assert memoria["meloetta"]["canonical_types"] == ["NORMAL", "PSYCHIC"]


# --- MON-40/Fase 3 S9 (D-pendiente): replay_url, hook estrecho y pasivo ----
#
# poke-env no expone ningun hook activo (no hay `/savereplay`/`uploadreplay`
# en el paquete vendorizado): la UNICA fuente offline legitima es una linea
# `|raw|` que el propio Showdown manda cuando la sala ya tiene un replay
# subido, con un link a `replay.pokemonshowdown.com`. Nunca se construye la
# URL desde `battle_tag`: eso afirmaria un replay que puede no existir
# (D17 -- omitir, no inventar).

def test_extract_replay_url_encuentra_el_link_en_una_linea_raw():
    lines = [
        f">{BATTLE_TAG}", "|win|LudexBot3682",
        '|raw|<a href="https://replay.pokemonshowdown.com/gen6randombattle-386">'
        "View replay</a>",
    ]
    assert (
        extract_replay_url(lines)
        == "https://replay.pokemonshowdown.com/gen6randombattle-386"
    )


def test_extract_replay_url_devuelve_none_sin_linea_raw_de_replay():
    lines = [f">{BATTLE_TAG}", "|win|LudexBot3682", "|raw|GG"]
    assert extract_replay_url(lines) is None


def test_extract_replay_url_no_se_deja_enganar_por_un_host_distinto():
    """Saneado: un host que se PARECE a replay.pokemonshowdown.com pero no lo
    es (typosquat/subdominio ajeno) no cuenta como evidencia real."""
    lines = [
        '|raw|<a href="https://replay.pokemonshowdown.com.evil.example/x">link</a>',
    ]
    assert extract_replay_url(lines) is None


def test_extract_replay_url_ignora_esquema_no_https():
    lines = ['|raw|<a href="http://replay.pokemonshowdown.com/gen6-1">x</a>']
    assert extract_replay_url(lines) is None


def test_extract_replay_url_toma_el_primero_cuando_hay_varias_lineas_raw():
    lines = [
        '|raw|<a href="https://replay.pokemonshowdown.com/gen6randombattle-1">a</a>',
        '|raw|<a href="https://replay.pokemonshowdown.com/gen6randombattle-2">b</a>',
    ]
    assert (
        extract_replay_url(lines)
        == "https://replay.pokemonshowdown.com/gen6randombattle-1"
    )


def test_extract_replay_url_ignora_una_url_identica_fuera_de_una_linea_raw():
    """Hallazgo del tech lead (MON-40 R3, D70): el UNICO tipo de linea que
    Showdown usa para anunciar un replay ya subido es `|raw|`. Un jugador
    puede escribir el mismo texto (URL valida, host exacto, https) en un
    mensaje de chat (`|c|`) o en cualquier otro tipo de linea sin que eso
    signifique que el replay existe -- coincidir el regex no alcanza, el
    TIPO de linea tambien tiene que ser el correcto."""
    lines = [
        '|c|☆LudexBot3682|mira este link '
        '<a href="https://replay.pokemonshowdown.com/gen6randombattle-386">'
        "View replay</a>",
    ]
    assert extract_replay_url(lines) is None


# --- MON-40/Fase 3 S9: elo_bucket, sin bucketing por rangos ---------------

def test_elo_bucket_from_rating_none_da_none():
    assert elo_bucket_from_rating(None) is None


def test_elo_bucket_from_rating_rating_publico_da_el_string_exacto():
    assert elo_bucket_from_rating(1503) == "1503"


def test_elo_bucket_from_rating_no_agrupa_por_rangos():
    """Canario nombrado: dos ratings vecinos NO deben colapsar al mismo
    bucket. Un bucketing por rangos (p.ej. redondeo a centena) es
    exactamente el "bucket inventado" que el hook prohibe."""
    assert elo_bucket_from_rating(1499) != elo_bucket_from_rating(1501)
    assert elo_bucket_from_rating(1499) == "1499"
    assert elo_bucket_from_rating(1501) == "1501"
