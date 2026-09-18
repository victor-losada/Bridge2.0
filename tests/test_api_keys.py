"""La clave de la API: comparación en tiempo constante y sumidero cerrado."""

from __future__ import annotations

from app.api.routes import _coincide, _token_de


def test_acepta_la_clave_buena():
    assert _coincide("secreta", "secreta") is True


def test_rechaza_la_mala():
    assert _coincide("otra", "secreta") is False


def test_una_clave_vacia_no_pasa():
    """Sin cabecera, el token es cadena vacía: no puede colarse."""
    assert _coincide("", "secreta") is False


def test_vale_cualquiera_de_las_dos():
    assert _coincide("core", "bridge", "core") is True
    assert _coincide("bridge", "bridge", "core") is True
    assert _coincide("ninguna", "bridge", "core") is False


def test_una_clave_vacia_no_se_puede_ni_configurar():
    """Con una clave vacía a ambos lados, `_coincide` diría que sí.

    No es un agujero porque el ajuste es obligatorio y con longitud mínima: un
    .env a medio rellenar impide arrancar en vez de dejar la API abierta. El
    test fija esa dependencia, que de otro modo sería invisible desde aquí.
    """
    from app.config import Settings

    campo = Settings.model_fields["bridge_api_key"]
    assert campo.is_required()
    assert any(getattr(m, "min_length", None) == 16 for m in campo.metadata)


def test_lee_la_cabecera_y_el_bearer():
    assert _token_de("directa", None) == "directa"
    assert _token_de(None, "Bearer deltoken") == "deltoken"
    assert _token_de(None, "bearer minusculas") == "minusculas"
    assert _token_de(None, "Basic loquesea") == ""
    assert _token_de(None, None) == ""


def test_la_cabecera_gana_al_bearer():
    assert _token_de("directa", "Bearer otra") == "directa"
