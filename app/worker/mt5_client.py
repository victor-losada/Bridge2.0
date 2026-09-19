"""Wrapper del paquete oficial MetaTrader5.

Un Worker = un proceso = un terminal portable = un login.
Nunca se llama a initialize() con dos paths distintos en el mismo proceso.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import psutil

from app.utils import mt5_time_to_utc, ruta_dentro_de, to_iso_z, utc_now
from app.worker import broker_directory

logger = logging.getLogger(__name__)

#: MT5: el servidor contestó y rechazó usuario/contraseña.
AUTH_FAILED = -6
#: MT5: el canal con el terminal no llegó a estar listo.
IPC_TIMEOUT = -10005

try:
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - solo se instala en Windows con MT5
    mt5 = None  # type: ignore[assignment]

#: Nombres de timeframe que aceptamos. La constante de MT5 se busca en tiempo
#: de uso porque el paquete no existe fuera de Windows.
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


def mt5_timeframe(name: str) -> Any:
    """Constante TIMEFRAME_* de MT5 para un nombre nuestro."""
    if name not in TIMEFRAMES:
        raise ValueError(f"timeframe desconocido: {name}")
    return getattr(mt5, f"TIMEFRAME_{name}")


class MT5NotInstalledError(RuntimeError):
    pass


class MT5ConnectionError(RuntimeError):
    pass


class MT5AuthorizationError(MT5ConnectionError):
    """El servidor del bróker rechazó las credenciales.

    Es el `-6` de MT5, y es una respuesta, no un timeout: el servidor contestó
    en menos de dos segundos. Repetirlo no puede cambiar el veredicto y encima
    arriesga que el bróker bloquee la cuenta por intentos fallidos.

    Merece su propio tipo porque a escala va a ser el fallo más común con
    diferencia: un cliente que teclea mal su contraseña. Sin esto, cada error
    de dedo costaba dos arranques, el camino largo y cuatro ciclos del Worker.
    """


class MT5ServerUnreachableError(MT5ConnectionError):
    """El terminal está sano pero no consigue entrar en ESE servidor.

    Se distingue del resto de fallos de conexión porque reintentar no cambia
    nada: el terminal atiende el canal IPC, arranca y responde, y aun así el
    login contra ese servidor expira. El caso típico es un bróker que no está
    en la lista de servidores del terminal (`config/servers.dat`): sin la
    dirección del servidor no hay a dónde conectar, y eso no mejora por
    insistir.

    Existe para cortar la hemorragia: sin esta distinción, un bróker
    desconocido consumía ~50 minutos de reintentos ciegos con el slot ocupado
    y el Core sin saber por qué.
    """


@dataclass(slots=True)
class PositionSnapshot:
    ticket: int
    symbol: str
    type: str  # buy | sell
    volume: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    swap: float
    commission: float
    open_time: datetime
    comment: str
    magic: int

    def fingerprint(self) -> tuple:
        """Campos que disparan position.updated (no cada tick de profit)."""
        return (
            round(self.volume, 8),
            round(self.open_price, 8),
            round(self.sl, 8),
            round(self.tp, 8),
            self.comment,
        )


@dataclass(slots=True)
class AccountSnapshot:
    login: int
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float | None
    profit: float
    currency: str
    leverage: int
    name: str
    server: str


class MT5Client:
    def __init__(
        self,
        terminal_path: Path,
        timeout_ms: int = 60_000,
        history_forward_buffer_hours: float = 24.0,
        login_timeout_ms: int = 180_000,
        login_attempts: int = 3,
        reattach_wait_sec: float = 20.0,
    ) -> None:
        self.terminal_path = Path(terminal_path)
        self.timeout_ms = timeout_ms
        self.history_forward_buffer_hours = history_forward_buffer_hours
        self.login_timeout_ms = login_timeout_ms
        self.login_attempts = max(1, login_attempts)
        # Margen para que el terminal termine de cambiar de bróker antes de
        # preguntarle quién está dentro.
        self.reattach_wait_sec = reattach_wait_sec
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def initialize_and_login(
        self,
        login: int,
        password: str,
        server: str,
    ) -> None:
        if mt5 is None:
            raise MT5NotInstalledError(
                "El paquete MetaTrader5 no está instalado. "
                "pip install MetaTrader5 (solo Windows)."
            )

        exe = self._resolve_terminal_exe()
        logger.info("initialize portable path=%s login=%s server=%s", exe, login, server)

        try:
            mt5.shutdown()
        except Exception:
            pass

        # Un bróker que no está en la lista del terminal se presenta de dos
        # formas según el estado del slot: en uno limpio, el nombre resuelve a
        # otro sitio y ESE contesta -6; en uno que viene de otra cuenta, el
        # terminal ni llega a atender el canal y da -10005. Son el mismo
        # problema, así que la dirección real se prueba en los dos casos, y
        # antes del camino largo porque cuesta segundos en vez de minutos.
        try:
            ok = self._initialize_with_credentials(exe, login, password, server)
        except MT5AuthorizationError:
            if not self._reintentar_por_direccion(exe, login, password, server):
                raise
        else:
            if not ok and not self._reintentar_por_direccion(
                exe, login, password, server
            ):
                self._initialize_then_login(exe, login, password, server)

        info = mt5.account_info()
        if info is None:
            err = mt5.last_error()
            mt5.shutdown()
            raise MT5ConnectionError(f"account_info() vacío tras login: {err}")

        if int(info.login) != int(login):
            mt5.shutdown()
            raise MT5ConnectionError(
                f"Login inesperado: terminal={info.login} esperado={login}"
            )

        # Al conectar por dirección hay que comprobar con QUIÉN se acabó
        # hablando: el nombre canónico lo devuelve el propio servidor, y si no
        # es el que pedimos, las credenciales del cliente han viajado a un sitio
        # que no era. Mejor cortar que seguir.
        esperado = server.strip()
        real = str(getattr(info, "server", "") or "").strip()
        if not broker_directory.parece_direccion(esperado) and real and real.lower() != esperado.lower():
            mt5.shutdown()
            raise MT5ConnectionError(
                f"Servidor inesperado: se pidió {esperado!r} y el terminal está "
                f"en {real!r}"
            )

        self._connected = True
        logger.info(
            "conectado trade_mode=%s name=%s server=%s",
            getattr(info, "trade_mode", None),
            info.name,
            info.server,
        )

    def _reintentar_por_direccion(
        self, exe: Path, login: int, password: str, server: str
    ) -> bool:
        """Reintenta contra la dirección real del bróker. True si entró.

        Solo se prueba UN punto de acceso. Si la contraseña es de verdad mala,
        insistir contra seis direcciones son seis logins fallidos seguidos, y
        eso es lo que hace que un bróker bloquee la cuenta del cliente.

        Un -6 contra la dirección BUENA sí es concluyente —el servidor correcto
        ha dicho que no— así que se deja propagar en vez de tragarlo: ahorra el
        rodeo de diez minutos del camino largo.
        """
        accesos = broker_directory.puntos_de_acceso(server)
        if not accesos:
            return False
        destino = accesos[0]
        logger.info(
            "%s no estaba en la lista del terminal; reintentando por %s",
            server,
            destino,
        )
        return self._initialize_with_credentials(exe, login, password, destino)

    def _initialize_with_credentials(
        self, exe: Path, login: int, password: str, server: str
    ) -> bool:
        """Arranca el terminal YA con las credenciales, en una sola llamada.

        Es la vía buena, y la única que funciona en un terminal recién clonado:
        el terminal nace sabiendo a qué cuenta conectarse, así que no enseña el
        asistente de cuenta ni el diálogo de contraseña. Un terminal parado en
        un diálogo no atiende al canal IPC, y eso es lo que producía los
        `-10005 IPC timeout` que parecían de red o de credenciales.

        Además evita depender de qué cuenta quedó guardada en el terminal, así
        que cualquier slot sirve para cualquier bróker, incluido uno que la
        máquina no haya visto nunca.
        """
        for intento in (1, 2):
            if mt5.initialize(
                path=str(exe),
                login=int(login),
                password=password,
                server=server,
                timeout=self.timeout_ms,
                portable=True,
            ):
                logger.info("mt5.initialize ok con credenciales (intento %s)", intento)
                return True

            err = mt5.last_error()
            code = err[0] if isinstance(err, (tuple, list)) and err else None
            logger.warning(
                "mt5.initialize con credenciales falló (intento %s): %s",
                intento,
                err,
            )
            try:
                mt5.shutdown()
            except Exception:
                pass
            if code == AUTH_FAILED:
                # El servidor ya ha contestado. Insistir no cambia nada y puede
                # bloquear la cuenta.
                raise MT5AuthorizationError(
                    f"el servidor rechazó las credenciales de {login} en "
                    f"{server!r}: {err}"
                )
            if intento == 1:
                # Un terminal de este slot colgado de un arranque anterior
                # impide el attach: se limpia y se reintenta una vez.
                _kill_slot_terminals(str(exe.parent.resolve()).lower())
                time.sleep(2.0)
        return False

    def _initialize_then_login(
        self, exe: Path, login: int, password: str, server: str
    ) -> None:
        """Camino largo: lanzar el terminal, engancharse y luego hacer login.

        Solo se usa si el arranque con credenciales no funcionó. Es más lento y
        depende del estado guardado del terminal, pero cubre los casos raros.
        """
        logger.info("probando el arranque en dos pasos")
        self._restart_portable_terminal(exe)

        last_err: Any = None
        attached = False
        for portable in (True, False):
            if mt5.initialize(path=str(exe), timeout=self.timeout_ms, portable=portable):
                logger.info("mt5.initialize ok portable=%s", portable)
                attached = True
                break
            last_err = mt5.last_error()
            logger.warning("mt5.initialize portable=%s falló: %s", portable, last_err)
            try:
                mt5.shutdown()
            except Exception:
                pass
            time.sleep(1.0)

        if not attached:
            raise MT5ConnectionError(
                f"mt5.initialize falló: {last_err}. Si el terminal ha abierto "
                "una ventana esperando un clic, ese es el motivo."
            )

        if not self._wait_terminal_ipc(max(40.0, self.timeout_ms / 1000.0)):
            err = mt5.last_error()
            mt5.shutdown()
            raise MT5ConnectionError(f"IPC no listo tras initialize: {err}")

        if not self._login_with_patience(exe, login, password, server):
            err = mt5.last_error()
            mt5.shutdown()
            raise MT5ConnectionError(f"mt5.login falló: {err}")

    def _login_with_patience(
        self, exe: Path, login: int, password: str, server: str
    ) -> bool:
        """Login tolerante al cambio de bróker.

        Si el terminal viene de una plantilla con otro bróker, `login()` lo
        obliga a desconectarse y reconectarse contra el servidor nuevo. En ese
        corte MT5 **tira el canal IPC**, así que la llamada devuelve -10005 de
        inmediato aunque el terminal acabe entrando en la cuenta (se ve en el
        título de su ventana). Y reintentar el login sobre un pipe muerto
        devuelve -10005 otra vez, para siempre.

        Por eso, ante -10005, no se reintenta a ciegas: se vuelve a enganchar
        el IPC y se pregunta al terminal quién está dentro. Si ya es la cuenta
        pedida, el login había funcionado. Un fallo de credenciales no se
        reintenta — repetirlo solo arriesga bloquear la cuenta.
        """
        # Tres cosas distintas producen -10005, y solo una justifica rendirse:
        #   IPC no reengancha        -> canal roto, otro ciclo puede arreglarlo
        #   reengancha, nadie dentro -> nunca llegó al servidor: inalcanzable
        #   reengancha, otra cuenta  -> el servidor SÍ respondió; fallo normal
        solo_ipc_timeout = True
        ipc_sano = False
        vio_alguna_cuenta = False

        for attempt in range(1, self.login_attempts + 1):
            if mt5.login(
                login=int(login),
                password=password,
                server=server,
                timeout=self.login_timeout_ms,
            ):
                return True

            err = mt5.last_error()
            code = err[0] if isinstance(err, (tuple, list)) and err else None
            solo_ipc_timeout = solo_ipc_timeout and code == -10005
            logger.warning(
                "mt5.login intento %s/%s: %s", attempt, self.login_attempts, err
            )
            if code != -10005:
                return False  # credenciales o servidor: reintentar no ayuda
            if attempt == self.login_attempts:
                break

            logger.info(
                "IPC caído por el cambio de bróker; reenganchando para ver "
                "si el terminal ya entró"
            )
            time.sleep(self.reattach_wait_sec)
            if not self._reattach(exe):
                continue
            ipc_sano = True
            if self._logged_as(login):
                logger.info("el terminal ya está dentro de la cuenta %s", login)
                return True
            if mt5.account_info() is not None:
                vio_alguna_cuenta = True

        # Agotados los intentos. Si el canal reenganchaba y aun así el terminal
        # nunca llegó a estar dentro de NINGUNA cuenta, no es lentitud del
        # cambio de bróker: el terminal no alcanza ese servidor. Repetir el
        # ciclo entero cuesta doce minutos y no puede descubrir nada nuevo.
        if solo_ipc_timeout and ipc_sano and not vio_alguna_cuenta:
            raise MT5ServerUnreachableError(
                f"el terminal responde pero no alcanza {server!r} "
                f"(login {login}) tras {self.login_attempts} intentos. Lo "
                f"habitual es que ese bróker no esté en la lista de servidores "
                f"de este terminal."
            )
        return False

    def _reattach(self, exe: Path) -> bool:
        """Rehace el canal IPC con el terminal, que sigue vivo."""
        try:
            mt5.shutdown()
        except Exception:
            pass
        time.sleep(1.0)
        for portable in (True, False):
            if mt5.initialize(path=str(exe), timeout=self.timeout_ms, portable=portable):
                return True
        logger.warning("no se pudo reenganchar el IPC: %s", mt5.last_error())
        return False

    def _logged_as(self, login: int) -> bool:
        info = mt5.account_info()
        return info is not None and int(info.login) == int(login)

    def shutdown(self) -> None:
        self._connected = False
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception:
                logger.exception("mt5.shutdown")

    def last_error(self) -> Any:
        if mt5 is None:
            return None
        return mt5.last_error()

    def terminal_info(self) -> Any:
        self._ensure()
        return mt5.terminal_info()

    def account_info(self) -> AccountSnapshot:
        self._ensure()
        info = mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info None: {mt5.last_error()}")
        level = float(info.margin_level) if getattr(info, "margin_level", 0) else None
        return AccountSnapshot(
            login=int(info.login),
            balance=float(info.balance),
            equity=float(info.equity),
            margin=float(info.margin),
            free_margin=float(info.margin_free),
            margin_level=level,
            profit=float(info.profit),
            currency=str(info.currency),
            leverage=int(info.leverage),
            name=str(info.name),
            server=str(info.server),
        )

    def positions(self) -> list[PositionSnapshot]:
        self._ensure()
        rows = mt5.positions_get()
        if rows is None:
            # None = error; tupla vacía = sin posiciones
            err = mt5.last_error()
            if err and err[0] != 1:  # 1 = RES_S_OK
                raise MT5ConnectionError(f"positions_get None: {err}")
            return []
        out: list[PositionSnapshot] = []
        for p in rows:
            ptype = "buy" if int(p.type) == 0 else "sell"
            time_raw = getattr(p, "time_msc", None) or getattr(p, "time", None)
            out.append(
                PositionSnapshot(
                    ticket=int(p.ticket),
                    symbol=str(p.symbol),
                    type=ptype,
                    volume=float(p.volume),
                    open_price=float(p.price_open),
                    current_price=float(p.price_current),
                    sl=float(p.sl),
                    tp=float(p.tp),
                    profit=float(p.profit),
                    swap=float(p.swap),
                    # positions_get no siempre trae commission; 0.0 si falta
                    commission=float(getattr(p, "commission", 0.0) or 0.0),
                    open_time=mt5_time_to_utc(time_raw),
                    comment=str(getattr(p, "comment", "") or ""),
                    magic=int(getattr(p, "magic", 0) or 0),
                )
            )
        return out

    def history_deals(self, lookback_days: int) -> Sequence[Any]:
        """Deals del lookback.

        El rango se compara contra la hora del SERVIDOR del bróker, no contra
        UTC. Con un bróker en EET (UTC+2/+3) un tope en `utc_now()` deja fuera
        los cierres de las últimas horas y esos trade.closed no llegan nunca al
        Core. Por eso el tope va con holgura hacia delante; los duplicados los
        filtra el DealMatcher por ticket.
        """
        self._ensure()
        now = utc_now()
        buffer = timedelta(hours=self.history_forward_buffer_hours)
        date_from = now - timedelta(days=lookback_days) - buffer
        date_to = now + buffer
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            err = mt5.last_error()
            if err and err[0] != 1:
                raise MT5ConnectionError(f"history_deals_get None: {err}")
            return []
        return deals

    def history_orders(self, lookback_days: int) -> Sequence[Any]:
        """Ordenes del lookback, para recuperar el SL/TP de apertura.

        Mismo rango y misma holgura que history_deals: MT5 filtra por hora del
        servidor del broker, no por UTC. Que no haya ordenes no es un error
        (una cuenta sin actividad en la ventana), asi que se devuelve vacio.
        """
        self._ensure()
        now = utc_now()
        buffer = timedelta(hours=self.history_forward_buffer_hours)
        date_from = now - timedelta(days=lookback_days) - buffer
        date_to = now + buffer
        orders = mt5.history_orders_get(date_from, date_to)
        if orders is None:
            err = mt5.last_error()
            if err and err[0] != 1:
                logger.warning("history_orders_get None: %s", err)
            return []
        return orders

    def copy_rates(
        self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime
    ) -> Sequence[Any]:
        """Velas de un simbolo en un rango. Vacio si el simbolo no existe.

        El simbolo tiene que estar en el Market Watch para que MT5 sirva su
        historial, y un simbolo que solo aparece en trades viejos puede no
        estarlo: por eso se selecciona antes.
        """
        self._ensure()
        tf = mt5_timeframe(timeframe)
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_range(symbol, tf, date_from, date_to)
        if rates is None:
            err = mt5.last_error()
            if err and err[0] != 1:
                logger.warning("copy_rates_range None para %s: %s", symbol, err)
            return []
        return rates

    def _ensure(self) -> None:
        if mt5 is None or not self._connected:
            raise MT5ConnectionError("MT5 no inicializado")

    def _restart_portable_terminal(self, exe: Path) -> None:
        """Mata un terminal64 colgado de este slot y lanza uno nuevo con /portable.

        Reusar un proceso 'ya en ejecución' tras un IPC timeout deja el pipe
        muerto y el siguiente initialize vuelve a fallar -10005.
        """
        root = str(exe.parent.resolve()).lower()
        _kill_slot_terminals(root)
        time.sleep(1.5)
        logger.info("lanzando %s /portable", exe)
        subprocess.Popen(
            [str(exe), "/portable"],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            if _slot_terminal_pid(root) is not None:
                time.sleep(8.0)
                return
            time.sleep(0.4)
        logger.warning("terminal64.exe no apareció en 45s; initialize igual lo intentará")

    def _wait_terminal_ipc(self, timeout_sec: float) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            info = mt5.terminal_info()
            if info is not None:
                logger.info("IPC listo community=%s", getattr(info, "community_account", None))
                return True
            time.sleep(0.5)
        return False

    def _resolve_terminal_exe(self) -> Path:
        path = self.terminal_path
        if path.is_file() and path.suffix.lower() == ".exe":
            return path
        candidate = path / "terminal64.exe"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(
            f"No se encontró terminal64.exe en {path}. "
            "Copia un terminal MT5 portable en esa carpeta."
        )


def _kill_slot_terminals(root_lower: str) -> None:
    for proc in list(psutil.process_iter(["pid", "name", "exe"])):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            if "terminal64.exe" not in name and "terminal.exe" not in name:
                continue
            if not ruta_dentro_de(exe, root_lower):
                continue
            logger.info("kill terminal colgado pid=%s", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.Error, OSError):
            continue


def _slot_terminal_pid(root_lower: str) -> int | None:
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            if "terminal64.exe" not in name and "terminal.exe" not in name:
                continue
            if ruta_dentro_de(exe, root_lower):
                return int(proc.info["pid"])
        except (psutil.Error, OSError):
            continue
    return None


def position_to_persist(pos: PositionSnapshot) -> dict[str, Any]:
    """Serializa una posición para worker_state.json.

    Distinta de `position_to_event_dict`: aquella es el contrato con el Core
    y omite campos; esta tiene que poder reconstruir el snapshot entero para
    que, tras un reinicio, `_poll_positions` compare contra el libro real y
    no contra un libro vacío.
    """
    return {
        "ticket": pos.ticket,
        "symbol": pos.symbol,
        "type": pos.type,
        "volume": pos.volume,
        "open_price": pos.open_price,
        "current_price": pos.current_price,
        "sl": pos.sl,
        "tp": pos.tp,
        "profit": pos.profit,
        "swap": pos.swap,
        "commission": pos.commission,
        "open_time": pos.open_time.isoformat(),
        "comment": pos.comment,
        "magic": pos.magic,
    }


def position_from_persist(payload: dict[str, Any]) -> PositionSnapshot:
    """Reconstruye una posición guardada. Lanza si el registro no sirve."""
    return PositionSnapshot(
        ticket=int(payload["ticket"]),
        symbol=str(payload["symbol"]),
        type=str(payload["type"]),
        volume=float(payload["volume"]),
        open_price=float(payload["open_price"]),
        current_price=float(payload["current_price"]),
        sl=float(payload["sl"]),
        tp=float(payload["tp"]),
        profit=float(payload["profit"]),
        swap=float(payload["swap"]),
        commission=float(payload["commission"]),
        open_time=datetime.fromisoformat(str(payload["open_time"])),
        comment=str(payload.get("comment", "")),
        magic=int(payload.get("magic", 0)),
    )


def position_to_event_dict(pos: PositionSnapshot) -> dict[str, Any]:
    return {
        "positionId": str(pos.ticket),
        "symbol": pos.symbol,
        "type": pos.type,
        "volume": pos.volume,
        "openPrice": pos.open_price,
        "currentPrice": pos.current_price,
        "profit": round(pos.profit, 2),
        "swap": round(pos.swap, 2),
        "commission": round(pos.commission, 2),
        "stopLoss": pos.sl or None,
        "takeProfit": pos.tp or None,
        "openTime": to_iso_z(pos.open_time),
        "comment": pos.comment or None,
    }
