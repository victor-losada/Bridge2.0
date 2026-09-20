"""Las cuentas asignadas sobreviven a un reinicio del Manager.

Sin esto, reiniciar uvicorn dejaba al Core creyendo que las cuentas seguían
conectadas mientras el Bridge no tenía ninguna: dejaban de llegar datos, el
Core no permitía reconectar la misma cuenta y el desvincular no encontraba
nada que desvincular.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.manager.slot_manager import SlotManager, SlotManagerError
from app.manager.state import SlotStatus

FERNET = "Ky1DFHTvPX2CjJKcgTLdmB2fF2ZQK5Xz3mFvMHVGLJU="


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        bridge_api_key="x" * 16,
        core_api_key="y" * 8,
        fernet_key=FERNET,
        slot_count=3,
        terminals_root=tmp_path / "terminals",
        data_dir=tmp_path / "data",
    )


async def _manager(tmp_path: Path, spawned: list, settings=None) -> SlotManager:
    settings = settings or _settings(tmp_path)
    settings.worker_spawn_stagger_sec = 0  # sin esperas en los tests
    manager = SlotManager(settings)
    manager.proc.start_worker = lambda **kw: (  # type: ignore[method-assign]
        spawned.append(kw) or 4242
    )
    manager.terminal_ready = lambda slot_id: True  # type: ignore[method-assign]
    await manager.start()
    if manager._restore_task:
        await manager._restore_task  # la recuperación va en segundo plano
    if manager._watch_task:
        manager._watch_task.cancel()
    return manager


async def test_la_cuenta_vuelve_sola_tras_reiniciar(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="NYSMarketsLtd-trade",
        investor=False,
    )
    assert len(spawned) == 1

    # Reinicio del Manager: proceso nuevo, memoria en blanco.
    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert len(spawned) == 1
    assert spawned[0]["mt5_login"] == 203395
    assert spawned[0]["mt5_password"] == "secreta"  # descifrado correctamente
    estado = reiniciado.find_by_account("uuid-nys")
    assert estado is not None and estado.slot_id == "Slot-01"
    assert estado.mt5_server == "NYSMarketsLtd-trade"
    assert estado.investor is False


async def test_desvincular_no_deja_rastro(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="S",
    )
    await manager.disconnect(account_id="uuid-nys")

    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert spawned == []
    assert reiniciado.find_by_account("uuid-nys") is None


async def test_dos_cuentas_vuelven_a_sus_slots(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys", mt5_login=203395, mt5_password="a", mt5_server="NYS"
    )
    await manager.connect(
        account_id="uuid-exness", mt5_login=198812927, mt5_password="b", mt5_server="EX"
    )

    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert {s["mt5_login"] for s in spawned} == {203395, 198812927}
    assert reiniciado.find_by_account("uuid-nys").slot_id == "Slot-01"
    assert reiniciado.find_by_account("uuid-exness").slot_id == "Slot-02"


async def test_fernet_cambiada_deja_el_slot_en_error_visible(tmp_path: Path) -> None:
    """Un fallo al recuperar no puede pasar en silencio."""
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys", mt5_login=203395, mt5_password="a", mt5_server="NYS"
    )

    otra_clave = _settings(tmp_path)
    otra_clave.fernet_key = "3sHkPTsw1DBKLbEnYbNjPmZXNXR0hFzHFyZlNCUjuQE="
    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned, settings=otra_clave)

    assert spawned == []
    estado = reiniciado.slots["Slot-01"]
    assert estado.status == SlotStatus.ERROR
    assert "no se pudo recuperar" in (estado.last_error or "")


async def test_un_archivo_corrupto_no_impide_arrancar(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "slots.json").write_text("{ esto no es json", encoding="utf-8")

    spawned: list = []
    manager = await _manager(tmp_path, spawned, settings=settings)

    assert spawned == []
    assert len(manager.slots) == 3


async def test_el_password_no_se_guarda_en_claro(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="password-en-claro",
        mt5_server="NYS",
    )

    guardado = (manager.settings.data_dir / "slots.json").read_text(encoding="utf-8")
    assert "password-en-claro" not in guardado
    assert json.loads(guardado)["slots"][0]["mt5_login"] == 203395


async def test_los_workers_no_arrancan_todos_a_la_vez(tmp_path: Path) -> None:
    """Tres terminales MT5 levantando a la vez se ahogan entre ellos.

    Visto en producción: al recuperar tres cuentas de golpe, la primera
    entraba y las otras dos morían con -10005 IPC timeout en el initialize.
    """
    import asyncio

    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    for i, (uuid, login) in enumerate(
        [("a", 203395), ("b", 198812585), ("c", 198812927)]
    ):
        await manager.connect(
            account_id=uuid, mt5_login=login, mt5_password="x", mt5_server="S"
        )
    assert len(spawned) == 3

    # Reinicio con margen entre arranques.
    settings = _settings(tmp_path)
    settings.worker_spawn_stagger_sec = 0.05
    spawned.clear()
    reiniciado = SlotManager(settings)
    reiniciado.proc.start_worker = lambda **kw: (  # type: ignore[method-assign]
        spawned.append(kw) or 4242
    )
    reiniciado.terminal_ready = lambda slot_id: True  # type: ignore[method-assign]
    await reiniciado.start()

    # El primero sale enseguida; los demás esperan su turno.
    await asyncio.sleep(0.01)
    assert len(spawned) == 1

    await reiniciado._restore_task
    assert len(spawned) == 3
    if reiniciado._watch_task:
        reiniciado._watch_task.cancel()


async def test_el_watchdog_no_relanza_lo_que_espera_turno(tmp_path: Path) -> None:  # noqa: E501
    """Un slot en cola de recuperación no está caído: está esperando.

    Sin esto, el watchdog lo veía sin proceso, lo daba por muerto y lo
    arrancaba en paralelo con la recuperación: dos terminales del mismo slot
    lanzándose y matándose entre ellos.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    for uuid, login in [("a", 203395), ("b", 198812585), ("c", 198812927)]:
        await manager.connect(
            account_id=uuid, mt5_login=login, mt5_password="x", mt5_server="S"
        )

    settings = _settings(tmp_path)
    settings.worker_spawn_stagger_sec = 5  # los últimos siguen en cola
    reiniciado = SlotManager(settings)
    reiniciado.proc.start_worker = lambda **kw: 4242  # type: ignore[method-assign]
    reiniciado.proc.is_running = lambda slot_id: False  # type: ignore[method-assign]
    reiniciado.terminal_ready = lambda slot_id: True  # type: ignore[method-assign]
    await reiniciado.start()
    await asyncio.sleep(0.05)  # deja arrancar al primero

    assert reiniciado._pending_restore == {"Slot-02", "Slot-03"}
    # El watchdog no debe tocarlos aunque no tengan proceso vivo.
    for slot_id in ("Slot-02", "Slot-03"):
        assert reiniciado.slots[slot_id].restart_count == 0

    for task in (reiniciado._restore_task, reiniciado._watch_task):
        if task:
            task.cancel()


async def test_un_slots_json_con_bom_se_lee_igual(tmp_path: Path) -> None:
    """Windows mete un BOM con facilidad y el fichero se edita a mano.

    Visto en producción: tras editar slots.json con PowerShell
    (Set-Content -Encoding UTF8), el Manager arrancaba sin recuperar ninguna
    cuenta por un 'Unexpected UTF-8 BOM'.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys", mt5_login=203395, mt5_password="x", mt5_server="S"
    )

    ruta = manager.settings.data_dir / "slots.json"
    ruta.write_text("﻿" + ruta.read_text(encoding="utf-8"), encoding="utf-8")

    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert len(spawned) == 1
    assert reiniciado.find_by_account("uuid-nys") is not None


async def test_desvincular_libera_todas_las_asignaciones_de_la_cuenta(
    tmp_path: Path,
) -> None:
    """Regresión: una cuenta puede acabar en dos slots y desvincular liberaba uno.

    La guarda de `connect` solo rechazaba CONNECTED/CONNECTING, y `_pick_slot`
    solo reutiliza slots FREE: reconectar una cuenta cuyo slot quedó en ERROR
    le asignaba un segundo slot. `disconnect` resolvía con `find_by_account`,
    que retorna en la primera coincidencia, así que la otra asignación sobrevivía
    entera —con `password_encrypted` dentro y escrita en slots.json— lista para
    descifrarse y relogar en el siguiente arranque del Manager.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)

    primero = await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="NYSMarketsLtd-trade",
        investor=False,
    )
    # El login falla y el watchdog agota los reintentos: el slot queda en ERROR
    # con la cuenta todavía asignada.
    primero.status = SlotStatus.ERROR
    primero.last_error = "login rechazado"

    # El Core reconecta la misma cuenta. Antes esto creaba una segunda asignación.
    segundo = await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="NYSMarketsLtd-trade",
        investor=False,
    )

    # Una sola desvinculación tiene que dejar la cuenta fuera de TODO el pool.
    await manager.disconnect(account_id="uuid-nys")

    restantes = manager.find_all_by_account("uuid-nys")
    assert restantes == [], f"la cuenta sigue en {[s.slot_id for s in restantes]}"
    for state in manager.slots.values():
        assert state.password_encrypted is None, f"{state.slot_id} conserva la clave"

    guardado = json.loads((tmp_path / "data" / "slots.json").read_text())
    assert not [
        item
        for item in guardado["slots"]
        if item.get("account_id") == "uuid-nys"
    ], "slots.json aún tiene la cuenta desvinculada"
    assert segundo.slot_id  # el segundo connect devolvió un slot válido


async def test_el_watchdog_sobrevive_a_un_runtime_json_corrupto(tmp_path: Path) -> None:
    """Regresión: un fichero con tipos incompatibles mataba la vigilancia entera.

    `_core_channel_error` compara marcas de tiempo; con un `int` y un `str` el
    `>` lanza TypeError. Antes eso terminaba la tarea `slot-watch` y el Manager
    dejaba de vigilar TODOS los slots, sin un solo rastro: `/slots` seguía
    respondiendo con datos congelados.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    estado = manager.slots["Slot-01"]
    estado.account_id = "uuid-envenenada"
    estado.status = SlotStatus.CONNECTING
    manager.proc.is_running = lambda slot_id: True  # type: ignore[method-assign]

    envenenado = manager.slot_dir("Slot-01") / "slot_runtime.json"
    envenenado.write_text(
        json.dumps(
            {
                "status": "connected",
                "emit": {
                    "last_error": "x",
                    "last_error_at": 1,
                    "last_ok_at": "2026-01-01T00:00:00Z",
                },
            }
        ),
        encoding="utf-8",
    )

    # El ciclo tiene que tragarse el fallo y seguir vivo.
    await manager._watch_once()
    await manager._watch_once()


async def test_desvincular_falla_si_no_se_puede_persistir(tmp_path: Path) -> None:
    """Regresión: la desvinculación respondía ok sin haberla escrito en disco.

    El olvido solo ocurría en memoria; el fichero conservaba la contraseña
    cifrada y la cuenta revivía sola en el siguiente arranque del Manager.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="NYSMarketsLtd-trade",
        investor=False,
    )
    manager._save_assignments = lambda: False  # type: ignore[method-assign]

    with pytest.raises(SlotManagerError):
        await manager.disconnect(account_id="uuid-nys")


async def test_conectar_borra_tambien_el_runtime_del_inquilino_anterior(
    tmp_path: Path,
) -> None:
    """Regresión: el cliente entrante heredaba estado y contadores del saliente."""
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    runtime = manager.slot_dir("Slot-01") / "slot_runtime.json"
    runtime.write_text(
        json.dumps({"status": "connected", "account_id": "cuenta-anterior"}),
        encoding="utf-8",
    )

    await manager.connect(
        account_id="cuenta-nueva",
        mt5_login=111,
        mt5_password="secreta",
        mt5_server="Broker",
        slot_id="Slot-01",
    )

    assert not runtime.exists(), "el runtime del inquilino anterior sigue ahí"


async def test_el_ciclo_del_watchdog_no_muere_por_una_excepcion(tmp_path: Path) -> None:
    """Cinturón y tirantes: aunque algo falle dentro, la tarea sigue viva.

    El endurecimiento de `_core_channel_error` cierra el caso conocido; este
    test fija que cualquier OTRO fallo futuro tampoco pueda dejar al pool sin
    vigilancia, que era lo grave.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    fallos = {"n": 0}

    async def _explota() -> None:
        fallos["n"] += 1
        raise RuntimeError("algo inesperado dentro del ciclo")

    manager._watch_once = _explota  # type: ignore[method-assign]
    tarea = asyncio.create_task(manager._watch_workers())
    await asyncio.sleep(7)  # dos vueltas del ciclo, que duerme 3 s
    assert not tarea.done(), "el watchdog murió con la primera excepción"
    assert fallos["n"] >= 2, "el ciclo dejó de intentarlo"
    tarea.cancel()


async def test_solo_un_veredicto_del_broker_corta_el_reintento(tmp_path: Path) -> None:
    """Regresión: un fallo de login transitorio se anunciaba como rechazo.

    El Worker salía con 2 en TRES casos distintos (credenciales rechazadas,
    servidor inalcanzable y cualquier otro fallo tras agotar reintentos), y el
    watchdog trataba los tres como veredicto del bróker: el slot quedaba en
    error sin un solo reintento y con un mensaje que culpaba al cliente.
    """
    from app.worker.worker import (
        EXIT_CREDENCIALES,
        EXIT_LOGIN_FALLIDO,
        EXIT_SERVIDOR,
    )

    async def _un_ciclo(salida: int) -> tuple[SlotStatus, str | None, int]:
        spawned: list = []
        manager = await _manager(tmp_path / str(salida), spawned)
        estado = manager.slots["Slot-01"]
        estado.account_id = "uuid-x"
        estado.mt5_login = 1
        estado.mt5_server = "Broker"
        estado.password_encrypted = manager.cipher.encrypt("secreta")
        estado.status = SlotStatus.CONNECTING
        manager.proc.is_running = lambda slot_id: False  # type: ignore[method-assign]
        manager.proc.exit_code = lambda slot_id: salida  # type: ignore[method-assign]
        await manager._watch_once()
        return estado.status, estado.last_error, estado.restart_count

    # El bróker dijo que no: se corta, y el mensaje dice la verdad.
    status, error, reintentos = await _un_ciclo(EXIT_CREDENCIALES)
    assert status == SlotStatus.ERROR
    assert "credenciales" in error
    assert reintentos == 0

    # Servidor inalcanzable: también se corta, con SU motivo.
    status, error, _ = await _un_ciclo(EXIT_SERVIDOR)
    assert status == SlotStatus.ERROR
    assert "alcanzable" in error

    # Fallo genérico de login: esto SÍ se reintenta, como antes.
    status, error, reintentos = await _un_ciclo(EXIT_LOGIN_FALLIDO)
    assert reintentos == 1, "un fallo transitorio tiene que reintentarse"
    assert status != SlotStatus.ERROR
