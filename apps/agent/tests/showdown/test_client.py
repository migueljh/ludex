import logging

from ludex_agent.showdown.client import battle_tag_from, local_server_configuration


def _split(raw: str) -> list[str]:
    return raw.split("|")


def test_extrae_el_tag_de_la_primera_linea():
    lote = [[">battle-gen6randombattle-1"], _split("|init|battle")]
    assert battle_tag_from(lote) == "battle-gen6randombattle-1"


def test_encuentra_el_tag_aunque_no_sea_la_primera_linea():
    lote = [_split("|init|battle"), [">battle-gen9ou-42"]]
    assert battle_tag_from(lote) == "battle-gen9ou-42"


def test_sin_linea_de_tag_devuelve_none():
    assert battle_tag_from([_split("|init|battle"), _split("|turn|1")]) is None


def test_lote_vacio_y_lineas_vacias_no_revientan():
    assert battle_tag_from([]) is None
    assert battle_tag_from([[], [""]]) is None


def test_descarta_espacios_alrededor_del_tag():
    assert battle_tag_from([[">battle-x-1  "]]) == "battle-x-1"


def test_la_config_local_conserva_la_url_del_websocket():
    cfg = local_server_configuration("ws://localhost:8100/showdown/websocket")
    assert cfg.websocket_url == "ws://localhost:8100/showdown/websocket"
