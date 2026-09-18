"""Traduce el nombre de un servidor MT5 a sus direcciones de acceso.

Un terminal solo sabe conectar a los servidores que tiene en su
`config/servers.dat`, y esa lista la hereda de la plantilla. Un bróker que la
plantilla nunca vio queda fuera: el terminal resuelve el nombre a otro sitio
—o a ninguno— y el servidor contesta `Invalid account`, que despista porque
suena a credenciales y no lo es.

MetaQuotes documenta que el parámetro `server` admite **`dirección:puerto`**
además del nombre. Así que basta con averiguar la dirección, y eso lo da su
directorio público de brókers: el mismo que consulta el botón "Buscar tu
bróker" del asistente. Con eso, dar de alta un bróker nuevo deja de ser un
paso manual.

Medido: `server='35.234.132.67:443'` conecta una cuenta de FTMO en 2,9 s en un
slot que nunca había visto ese bróker, y MT5 devuelve el nombre canónico
(`FTMO-Server4`) en `account_info()`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

#: Espejo público del directorio de brókers de MT5. Va por HTTPS a propósito:
#: quien pudiera alterar esta respuesta elegiría a qué máquina manda el
#: terminal la contraseña de un cliente.
#:
#: Es un TERCERO en el camino de unas credenciales, así que es configurable:
#: BROKER_DIRECTORY_URL vacío desactiva el alta automática de brókers y deja
#: el resto del sistema igual. Quien no lo quiera, no lo tiene.
DIRECTORIO_URL = "https://search.mtapi.io/Search"


def _url_configurada() -> str:
    return os.environ.get("BROKER_DIRECTORY_URL", DIRECTORIO_URL).strip()

#: host:puerto. Se valida antes de usarlo porque lo que sale de aquí acaba
#: siendo el destino de unas credenciales.
_ACCESO = re.compile(r"^[A-Za-z0-9._-]{1,253}:\d{1,5}$")


def _pedir(url: str, nombre: str, timeout: float) -> object:
    with httpx.Client(timeout=timeout, trust_env=False) as cliente:
        r = cliente.get(url, params={"company": nombre, "mt5": "true"})
        r.raise_for_status()
        return r.json()


def _accesos_validos(valores: object) -> list[str]:
    salida: list[str] = []
    if not isinstance(valores, list):
        return salida
    for v in valores:
        if isinstance(v, str) and _ACCESO.match(v):
            host, _, puerto = v.rpartition(":")
            if 0 < int(puerto) < 65536:
                salida.append(v)
    return salida


def extraer(payload: object, nombre: str) -> list[str]:
    """Saca las direcciones del servidor que se llama EXACTAMENTE `nombre`.

    El directorio devuelve todos los servidores de la compañía, así que buscar
    'FTMO-Server4' trae también Server3 y Server5. Conectar al que no es da un
    `Invalid account` idéntico al del problema original, así que la coincidencia
    tiene que ser exacta.
    """
    buscado = nombre.strip().lower()
    if not isinstance(payload, dict):
        return []
    for compania in payload.get("result") or []:
        if not isinstance(compania, dict):
            continue
        for servidor in compania.get("results") or []:
            if not isinstance(servidor, dict):
                continue
            if str(servidor.get("name", "")).strip().lower() == buscado:
                return _accesos_validos(servidor.get("access"))
    return []


def puntos_de_acceso(
    nombre: str,
    *,
    timeout: float = 8.0,
    url: str | None = None,
    fetch: Callable[[str, str, float], object] | None = None,
) -> list[str]:
    """Direcciones `host:puerto` de ese servidor, o lista vacía.

    Nunca lanza: esto es una ayuda para un caso raro, y un directorio caído no
    puede tumbar una conexión que quizá funcionaba igual por su nombre.
    """
    if parece_direccion(nombre):
        return []  # ya es una dirección; no hay nada que resolver
    url = _url_configurada() if url is None else url
    if not url:
        logger.info("alta automática de brókers desactivada (sin directorio)")
        return []
    try:
        payload = (fetch or _pedir)(url, nombre, timeout)
    except Exception:
        logger.warning("no se pudo consultar el directorio de brókers", exc_info=True)
        return []
    accesos = extraer(payload, nombre)
    if accesos:
        logger.info("%s resuelve a %s", nombre, ", ".join(accesos))
    else:
        logger.warning("el directorio no conoce el servidor %r", nombre)
    return accesos


def parece_direccion(server: str) -> bool:
    """True si ya es `host:puerto` y no un nombre de servidor."""
    return bool(_ACCESO.match(server.strip()))
