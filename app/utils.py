"""Utilidades compartidas (tiempo UTC, volumen, logs)."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VOLUME_EPS = 1e-6


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_z(dt: datetime | None = None) -> str:
    """ISO-8601 en UTC con sufijo Z, sin microsegundos."""
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def mt5_time_to_utc(value: Any) -> datetime:
    """Convierte time/time_msc de MT5 a datetime UTC.

    MetaTrader5 suele devolver epoch en segundos (int) o datetime naive
    interpretado como reloj del terminal (típicamente UTC en brokers serios).
    """
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        # time_msc viene en milisegundos; algunos brókers entregan microsegundos.
        ts = float(value)
        if ts != ts or ts in (float("inf"), float("-inf")):
            # NaN o infinito: fromtimestamp lanzaría y el fallo subiría hasta
            # el bucle del Worker, dejando sin correr los demás sondeos.
            return utc_now()
        while ts > 10_000_000_000:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            # Fuera del rango representable: una marca de tiempo absurda de un
            # tercero no puede tumbar el ciclo de sondeo entero.
            return utc_now()
    return utc_now()


def ruta_dentro_de(exe: str | None, raiz_lower: str) -> bool:
    """¿El ejecutable cuelga de esa carpeta de slot?

    Comparar prefijos de cadena a secas no sirve: `...\\terminals\\slot-1` es
    prefijo de `...\\terminals\\slot-10`, y también de cualquier carpeta dejada
    al lado con un nombre parecido (`Slot-01.bak`, típica en un sistema cuyos
    slots se clonan de una plantilla). Parar un slot mataba entonces el
    terminal de otra cuenta. Exigimos que tras la raíz venga un separador.
    """
    if not exe:
        return False
    try:
        candidato = str(Path(exe).resolve()).lower()
    except OSError:
        return False
    raiz = raiz_lower.rstrip("\\/")
    return candidato == raiz or candidato.startswith(raiz + os.sep) or candidato.startswith(raiz + "/")


def volumes_equal(a: float, b: float) -> bool:
    return abs(a - b) < VOLUME_EPS


def setup_logging(slot_id: str | None = None) -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    if slot_id:
        fmt = f"%(asctime)s | %(levelname)-7s | {slot_id} | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
        force=True,
    )
    logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
