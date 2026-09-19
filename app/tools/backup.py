"""Copia de seguridad de lo que no se puede regenerar.

Del Bridge solo hay tres cosas irremplazables, y ninguna pesa:

  .env             la FERNET_KEY, sin la cual slots.json es ilegible y hay que
                   reconectar cada cuenta pidiendo su contraseña otra vez
  data/slots.json  qué cuenta ocupa cada slot
  la plantilla     las listas de servidores de los brókers, que se añaden a
                   mano una por una (opcional, con --template)

Lo demás —bases, logs, los propios slots— lo regenera MT5 solo.

    python -m app.tools.backup --dest D:\\copias
    python -m app.tools.backup --dest D:\\copias --template C:\\MT5-plantilla
    python -m app.tools.backup --dest D:\\copias --keep 14

OJO: la copia contiene la FERNET_KEY en claro. Guárdala donde guardarías una
contraseña, no en una carpeta compartida.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

#: Lo pequeño e irremplazable. Sin esto no se recupera nada.
ESENCIALES = (".env", "data/slots.json")

#: Lo que MT5 regenera solo; copiarlo multiplica el tamaño sin aportar.
PLANTILLA_IGNORA = {"bases", "logs", "tester", "history"}


def _ignora_plantilla(directory: str, names: list[str]) -> set[str]:
    parent = Path(directory).name.lower()
    fuera = set()
    for name in names:
        lowered = name.lower()
        if (Path(directory) / name).is_dir():
            if lowered in PLANTILLA_IGNORA or (parent == "mql5" and lowered == "logs"):
                fuera.add(name)
        elif lowered.endswith(".log"):
            fuera.add(name)
    return fuera


#: Nombre de las carpetas que crea este script. Solo se poda lo que encaje:
#: --dest puede apuntar a una carpeta con más cosas dentro.
PATRON = "bridge-backup-*"


#: Registro de lo que este script ha creado de verdad. Sin él, la poda decide
#: qué destruir mirando solo los nombres que encuentra en el destino, y basta
#: con que alguien cree ahí `bridge-backup-99991231-2359` —una carpeta vacía,
#: sin escribir un byte dentro— para que las copias legítimas se vayan todas.
REGISTRO = "copias.json"


def _leer_registro(dest: Path) -> list[str]:
    fichero = dest / REGISTRO
    if not fichero.is_file():
        return []
    try:
        datos = json.loads(fichero.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(datos, list):
        return []
    return [str(x) for x in datos if isinstance(x, str)]


def _escribir_registro(dest: Path, nombres: list[str]) -> None:
    try:
        (dest / REGISTRO).write_text(
            json.dumps(nombres, indent=2), encoding="utf-8"
        )
    except OSError:
        print("  aviso: no se pudo actualizar el registro de copias")


def anotar(dest: Path, carpeta: Path) -> None:
    """Apunta una copia recién creada en el registro del destino."""
    nombres = _leer_registro(dest)
    if carpeta.name not in nombres:
        nombres.append(carpeta.name)
    _escribir_registro(dest, nombres)


def poda(dest: Path, keep: int) -> list[Path]:
    """Deja solo las `keep` copias más recientes. Devuelve las borradas.

    Solo se borra lo que ESTE script anotó al crearlo. Una carpeta que encaje
    con el patrón pero no esté en el registro se deja en paz y se avisa: puede
    ser una copia manual del operador, o un señuelo puesto por alguien que
    pueda escribir en el destino.

    La marca de tiempo es AAAAMMDD-HHMMSS, así que ordenar por nombre ordena
    por fecha sin tocar el disco.
    """
    if keep <= 0:
        return []
    presentes = sorted((d for d in dest.glob(PATRON) if d.is_dir()), key=lambda d: d.name)
    if not (dest / REGISTRO).is_file() and presentes:
        # Primera ejecución tras la actualización: las copias que ya están las
        # creó la versión anterior de esta misma herramienta, así que se
        # adoptan. Si no, no se podaría nunca y el disco acabaría lleno, que es
        # un daño seguro frente a uno hipotético.
        print(f"  registro nuevo: adopto {len(presentes)} copia(s) ya presentes")
        _escribir_registro(dest, [d.name for d in presentes])
    registradas = set(_leer_registro(dest))
    ajenas = [d for d in presentes if d.name not in registradas]
    for extraña in ajenas:
        print(f"  no la borro, no la cree yo: {extraña.name}")
    copias = [d for d in presentes if d.name in registradas]
    sobran = copias[:-keep] if keep < len(copias) else []
    for vieja in sobran:
        shutil.rmtree(vieja)
    vivas = [d.name for d in copias if d not in sobran]
    _escribir_registro(dest, vivas)
    return sobran


def backup(
    root: Path, dest: Path, *, template: Path | None = None, marca: str | None = None
) -> Path:
    """Copia lo esencial a dest/bridge-backup-<fecha>. Devuelve esa carpeta."""
    marca = marca or datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = dest / f"bridge-backup-{marca}"
    destino.mkdir(parents=True, exist_ok=True)

    copiados: list[str] = []
    faltan: list[str] = []
    for relativo in ESENCIALES:
        origen = root / relativo
        if not origen.is_file():
            faltan.append(relativo)
            continue
        final = destino / relativo
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origen, final)
        copiados.append(relativo)

    for relativo in copiados:
        print(f"  guardado  {relativo}")
    for relativo in faltan:
        # slots.json no existe hasta la primera conexión: no es un error.
        print(f"  NO EXISTE {relativo}")

    if template is not None:
        if not (template / "terminal64.exe").is_file():
            print(f"  OJO: {template} no parece una plantilla (sin terminal64.exe)")
        else:
            print(f"  copiando la plantilla desde {template} (tarda)...")
            shutil.copytree(
                template, destino / "plantilla", ignore=_ignora_plantilla
            )
            print("  guardado  plantilla")

    return destino


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copia de seguridad de la configuración del Bridge."
    )
    parser.add_argument(
        "--dest", type=Path, required=True, help="Dónde dejar la copia."
    )
    parser.add_argument(
        "--root", type=Path, default=Path("."), help="Raíz del proyecto (por defecto .)"
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=0,
        help="Conservar solo las N copias mas recientes (0 = no borrar ninguna).",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Incluir también la plantilla de terminal (p.ej. C:\\MT5-plantilla).",
    )
    args = parser.parse_args(argv)

    if not (args.root / "app").is_dir():
        parser.error(f"{args.root.resolve()} no parece la raíz del Bridge")

    destino = backup(args.root, args.dest, template=args.template)
    anotar(args.dest, destino)

    for vieja in poda(args.dest, args.keep):
        print(f"  borrada   {vieja.name}")

    total = sum(f.stat().st_size for f in destino.rglob("*") if f.is_file())
    print()
    print(f"Copia en: {destino}  ({total / 1_048_576:.1f} MB)")
    print()
    print("Contiene la FERNET_KEY en claro: guárdala como guardarías una")
    print("contraseña. Y ten la clave también en tu gestor de contraseñas, no")
    print("solo aquí: si pierdes la copia Y el servidor, no hay vuelta atrás.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
