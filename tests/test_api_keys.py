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


def test_la_clave_de_ejemplo_del_repositorio_no_arranca() -> None:
    """Regresión: el marcador publicado satisfacía el único control (16 chars).

    Cualquiera que haya visto el repositorio conoce esas cadenas. Si llegan al
    .env desplegado, el Bridge tiene que negarse a arrancar en vez de arrancar
    y parecer seguro.
    """
    import pytest

    from app.config import CLAVES_DE_EJEMPLO, Settings

    fernet = "Ky1DFHTvPX2CjJKcgTLdmB2fF2ZQK5Xz3mFvMHVGLJU="
    for ejemplo in ("cambiame-por-una-cadena-larga-y-aleatoria", "cambia-esta-clave-bridge"):
        assert ejemplo in CLAVES_DE_EJEMPLO
        with pytest.raises(Exception):
            Settings(bridge_api_key=ejemplo, core_api_key="y" * 8, fernet_key=fernet)

    # Una clave propia sigue valiendo.
    ok = Settings(bridge_api_key="z" * 24, core_api_key="y" * 8, fernet_key=fernet)
    assert ok.bridge_api_key == "z" * 24


def test_un_cuerpo_enorme_se_rechaza_antes_de_leerlo() -> None:
    """Regresión: la autenticación es dependencia de ruta y corre TRAS el cuerpo.

    Sin tope, un anónimo hace que el único proceso que atiende a todas las
    cuentas lea y decodifique lo que le mande antes de responderle 401.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.limits import LimitarCuerpo

    app = FastAPI()

    @app.post("/eco")
    async def _eco(payload: dict) -> dict:  # noqa: ANN001
        return payload

    app.add_middleware(LimitarCuerpo, max_bytes=4096)
    cliente = TestClient(app, raise_server_exceptions=False)

    grande = cliente.post(
        "/eco", content=b"A" * 100_000, headers={"Content-Type": "application/json"}
    )
    assert grande.status_code == 413

    pequeno = cliente.post("/eco", json={"hola": "mundo"})
    assert pequeno.status_code == 200
    assert pequeno.json() == {"hola": "mundo"}
