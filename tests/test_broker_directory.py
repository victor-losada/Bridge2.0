"""Resolver el nombre de un bróker a su dirección.

El caso que lo motiva: una cuenta de FTMO en un pool cuya plantilla nunca vio
FTMO. El terminal resuelve 'FTMO-Server4' a otro sitio y ESE contesta
'Invalid account', que suena a credenciales y no lo es. Con la dirección real
la misma cuenta entra en 2,9 s.
"""

from __future__ import annotations

import pytest

from app.worker.broker_directory import (
    extraer,
    parece_direccion,
    puntos_de_acceso,
)

RESPUESTA = {
    "result": [
        {
            "company": "FTMO Global Markets Ltd",
            "results": [
                {"name": "FTMO-Server5", "access": ["35.197.253.234:443"]},
                {
                    "name": "FTMO-Server4",
                    "access": ["35.234.132.67:443", "185.96.244.5:443"],
                },
            ],
        }
    ]
}


def test_saca_las_direcciones_del_servidor_pedido():
    assert extraer(RESPUESTA, "FTMO-Server4") == [
        "35.234.132.67:443",
        "185.96.244.5:443",
    ]


def test_el_nombre_coincide_exacto_no_por_compania():
    """Buscar FTMO trae Server3, 4 y 5. Conectar al que no es da el MISMO
    'Invalid account' del problema original, así que no vale aproximar."""
    assert extraer(RESPUESTA, "FTMO-Server5") == ["35.197.253.234:443"]
    assert extraer(RESPUESTA, "FTMO") == []
    assert extraer(RESPUESTA, "FTMO-Server9") == []


def test_no_distingue_mayusculas():
    assert extraer(RESPUESTA, "ftmo-server4") == ["35.234.132.67:443", "185.96.244.5:443"]


@pytest.mark.parametrize(
    "basura",
    [
        {"result": [{"results": [{"name": "S", "access": ["sin-puerto"]}]}]},
        {"result": [{"results": [{"name": "S", "access": ["h:99999"]}]}]},
        {"result": [{"results": [{"name": "S", "access": "no-es-lista"}]}]},
        {"result": [{"results": [{"name": "S"}]}]},
        {"result": "no-es-lista"},
        {},
        [],
        None,
    ],
)
def test_lo_que_no_sea_una_direccion_valida_se_descarta(basura):
    """Lo que sale de aquí es el destino de la contraseña de un cliente."""
    assert extraer(basura, "S") == []


def test_un_directorio_caido_no_tumba_la_conexion():
    """Es una ayuda para un caso raro, no puede romper lo que ya funcionaba."""

    def revienta(url, nombre, timeout):
        raise RuntimeError("sin red")

    assert puntos_de_acceso("FTMO-Server4", fetch=revienta) == []


def test_una_direccion_no_se_resuelve_otra_vez():
    def no_deberia_llamarse(url, nombre, timeout):
        raise AssertionError("no hay que preguntar por algo que ya es una dirección")

    assert puntos_de_acceso("35.234.132.67:443", fetch=no_deberia_llamarse) == []


def test_reconoce_una_direccion():
    assert parece_direccion("35.234.132.67:443") is True
    assert parece_direccion("broker.example.com:443") is True
    assert parece_direccion("FTMO-Server4") is False
    assert parece_direccion("NYSMarketsLtd-trade") is False


def test_se_puede_desactivar_el_directorio(monkeypatch):
    """Es un tercero decidiendo a qué host van unas credenciales.

    Quien no quiera esa dependencia la apaga, y el resto del sistema sigue
    igual: los brókers que el terminal ya conoce conectan por su nombre.
    """
    monkeypatch.setenv("BROKER_DIRECTORY_URL", "")

    def no_deberia_llamarse(url, nombre, timeout):
        raise AssertionError("con el directorio desactivado no se consulta nada")

    assert puntos_de_acceso("FTMO-Server4", fetch=no_deberia_llamarse) == []


def test_el_directorio_se_puede_apuntar_a_otro_sitio(monkeypatch):
    monkeypatch.setenv("BROKER_DIRECTORY_URL", "https://interno.example/buscar")
    vistas: list[str] = []

    def espia(url, nombre, timeout):
        vistas.append(url)
        return RESPUESTA

    assert puntos_de_acceso("FTMO-Server4", fetch=espia) == [
        "35.234.132.67:443",
        "185.96.244.5:443",
    ]
    assert vistas == ["https://interno.example/buscar"]
