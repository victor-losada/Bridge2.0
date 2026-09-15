"""Login MT5 cuando el slot cambia de bróker.

Los slots se clonan de una plantilla que trae dentro la sesión de OTRO
bróker. Al conectar una cuenta de un bróker distinto, el terminal tiene que
desconectarse, localizar el servidor nuevo y sincronizar sus símbolos: pasa
del minuto y el timeout por defecto de login() lo corta a medias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.worker import mt5_client as mt5_module
from app.worker.mt5_client import MT5Client

_EXE = Path("terminal64.exe")

IPC_TIMEOUT = (-10005, "IPC timeout")
AUTORIZACION = (-6, "Terminal: Authorization failed")


class _FakeMT5:
    """Terminal simulado: tarda N intentos en completar el cambio de bróker."""

    def __init__(self, exitos_tras: int, error=IPC_TIMEOUT) -> None:
        self.exitos_tras = exitos_tras
        self.error = error
        self.intentos: list[dict] = []

    def login(self, login: int, password: str, server: str, timeout: int = 60_000):
        self.intentos.append({"login": login, "server": server, "timeout": timeout})
        return len(self.intentos) >= self.exitos_tras

    def last_error(self):
        return self.error

    def shutdown(self):
        pass

    def initialize(self, path=None, timeout=None, portable=None):
        return True

    def account_info(self):
        return None  # nadie dentro: obliga a reintentar el login


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(mt5_module.time, "sleep", lambda _s: None)


def _cliente(tmp_path: Path, **kwargs) -> MT5Client:
    kwargs.setdefault("reattach_wait_sec", 0)
    return MT5Client(tmp_path, **kwargs)


def test_reintenta_mientras_el_terminal_cambia_de_broker(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5(exitos_tras=3)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(_EXE, 203395, "x", "NYSMarketsLtd-trade") is True
    assert len(fake.intentos) == 3


def test_el_timeout_de_login_llega_al_terminal(monkeypatch, tmp_path) -> None:
    """Sin timeout explícito, login() se rinde a los 60 s por defecto."""
    fake = _FakeMT5(exitos_tras=1)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_timeout_ms=180_000)

    client._login_with_patience(_EXE, 203395, "x", "NYSMarketsLtd-trade")

    assert fake.intentos[0]["timeout"] == 180_000


def test_credenciales_malas_no_se_reintentan(monkeypatch, tmp_path) -> None:
    """Repetir un login rechazado solo arriesga bloquear la cuenta."""
    fake = _FakeMT5(exitos_tras=99, error=AUTORIZACION)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(_EXE, 203395, "mala", "NYSMarketsLtd-trade") is False
    assert len(fake.intentos) == 1


def test_se_rinde_tras_agotar_los_intentos(monkeypatch, tmp_path) -> None:
    """Se rinde tras N intentos, y dice POR QUÉ.

    Este fixture es la firma exacta de un servidor inalcanzable: el IPC
    reengancha y el terminal no llega a estar dentro de ninguna cuenta. Antes
    devolvía un False mudo y el Worker repetía el ciclo entero cuatro veces
    (~50 min con el slot ocupado). Ahora sale con una causa accionable.
    """
    from app.worker.mt5_client import MT5ServerUnreachableError

    fake = _FakeMT5(exitos_tras=99)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    with pytest.raises(MT5ServerUnreachableError):
        client._login_with_patience(_EXE, 203395, "x", "NYSMarketsLtd-trade")

    # Le da TODAS sus oportunidades antes de rendirse: un cambio de bróker
    # lento es indistinguible de esto mientras ocurre.
    assert len(fake.intentos) == 3


# --- El IPC se cae al cambiar de bróker ------------------------------------
# login() devuelve -10005 AL INSTANTE (no agota el timeout) porque MT5 corta
# el pipe al reconectar contra el servidor nuevo. El terminal sí acaba
# entrando, así que hay que reenganchar y preguntar quién está dentro.


class _CuentaMT5:
    def __init__(self, login: int) -> None:
        self.login = login


class _FakeMT5PipeCaido:
    """El login prospera dentro del terminal, pero la llamada devuelve -10005."""

    def __init__(self, login_real: int, entra_tras: int = 1) -> None:
        self.login_real = login_real
        self.entra_tras = entra_tras
        self.logins = 0
        self.reenganches = 0

    def login(self, login, password, server, timeout=60_000):
        self.logins += 1
        return False  # siempre falla de cara a la API

    def last_error(self):
        return IPC_TIMEOUT

    def shutdown(self):
        pass

    def initialize(self, path=None, timeout=None, portable=None):
        self.reenganches += 1
        return True

    def account_info(self):
        if self.reenganches >= self.entra_tras:
            return _CuentaMT5(self.login_real)
        return None


def test_reengancha_y_detecta_que_el_terminal_ya_entro(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5PipeCaido(login_real=203395)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = MT5Client(tmp_path, login_attempts=3, reattach_wait_sec=0)

    ok = client._login_with_patience(
        tmp_path / "terminal64.exe", 203395, "x", "NYSMarketsLtd-trade"
    )

    assert ok is True
    assert fake.reenganches == 1  # bastó un reenganche


def test_si_entro_otra_cuenta_no_lo_da_por_bueno(monkeypatch, tmp_path) -> None:
    """Reenganchar y ver una cuenta distinta no es un login correcto."""
    fake = _FakeMT5PipeCaido(login_real=999999)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = MT5Client(tmp_path, login_attempts=2, reattach_wait_sec=0)

    ok = client._login_with_patience(
        tmp_path / "terminal64.exe", 203395, "x", "NYSMarketsLtd-trade"
    )

    assert ok is False


# --- Arranque con credenciales en un solo paso ------------------------------
# Medido en un slot recién clonado: initialize con credenciales entra en 4,6 s
# y sin diálogos, donde el camino en dos pasos moría con -10005 tras minutos.


class _FakeMT5UnPaso:
    """Terminal virgen: solo arranca si le dan las credenciales al initialize."""

    def __init__(self, login_real: int = 203395, acepta_un_paso: bool = True) -> None:
        self.login_real = login_real
        self.acepta_un_paso = acepta_un_paso
        self.con_credenciales = 0
        self.sin_credenciales = 0
        self.logins = 0

    def initialize(self, path=None, timeout=None, portable=None, **kw):
        if "login" in kw:
            self.con_credenciales += 1
            return self.acepta_un_paso
        self.sin_credenciales += 1
        return False  # terminal virgen: se queda en el asistente

    def last_error(self):
        return IPC_TIMEOUT

    def login(self, *a, **kw):
        self.logins += 1
        return False

    def shutdown(self):
        pass

    def account_info(self):
        return _CuentaMT5(self.login_real)

    def terminal_info(self):
        return None


def test_arranca_con_credenciales_sin_dar_el_rodeo(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5UnPaso()
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = MT5Client(tmp_path)

    assert client._initialize_with_credentials(
        tmp_path / "terminal64.exe", 203395, "x", "NYSMarketsLtd-trade"
    ) is True

    assert fake.con_credenciales == 1
    assert fake.sin_credenciales == 0  # no hizo falta el camino largo
    assert fake.logins == 0            # ni un login aparte


def test_si_falla_reintenta_una_vez_tras_limpiar(monkeypatch, tmp_path) -> None:
    """Un terminal colgado de un arranque anterior impide el attach."""
    fake = _FakeMT5UnPaso(acepta_un_paso=False)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    monkeypatch.setattr(mt5_module, "_kill_slot_terminals", lambda root: None)
    client = MT5Client(tmp_path)

    assert client._initialize_with_credentials(
        tmp_path / "terminal64.exe", 203395, "x", "S"
    ) is False
    assert fake.con_credenciales == 2  # lo intenta dos veces, no más


# --- Bróker que el terminal no conoce ---------------------------------------
#
# Caso real: una cuenta de FTMO-Server4 en un pool cuya plantilla nunca vio
# FTMO. El terminal arranca y atiende el IPC en 8 segundos, pero el login
# contra ese servidor expira. Antes esto costaba ~50 minutos de reintentos
# ciegos (4 ciclos de 12,4 min) con el slot ocupado y el Core sin saber nada.


class _TerminalSanoQueNoEntra:
    """El terminal responde, reengancha, pero nunca está en la cuenta."""

    def __init__(self) -> None:
        self.logins = 0
        self.reenganches = 0

    def login(self, **_kw) -> bool:
        self.logins += 1
        return False

    def last_error(self):
        return (-10005, "IPC timeout")

    def account_info(self):
        return None  # nadie dentro: nunca llegó al servidor

    def shutdown(self):
        pass


def test_un_servidor_inalcanzable_no_se_reintenta_a_ciegas(monkeypatch) -> None:
    """Si el IPC reengancha y el terminal no está dentro, insistir no informa."""
    from pathlib import Path

    from app.worker.mt5_client import MT5Client, MT5ServerUnreachableError

    falso = _TerminalSanoQueNoEntra()
    monkeypatch.setattr(mt5_module, "mt5", falso, raising=False)

    cliente = MT5Client(terminal_path=Path("."))
    cliente.login_attempts = 3
    cliente.reattach_wait_sec = 0.0
    monkeypatch.setattr(cliente, "_reattach", lambda _exe: True)   # IPC sano
    monkeypatch.setattr(cliente, "_logged_as", lambda _login: False)  # pero fuera

    with pytest.raises(MT5ServerUnreachableError) as err:
        cliente._login_with_patience(Path("terminal64.exe"), 541446323, "x", "FTMO-Server4")

    # Agota los 3 intentos: dentro del bucle NO se puede distinguir esto de un
    # cambio de bróker en curso, así que se le da su margen completo. Lo que se
    # corta es que el Worker repita el ciclo otras 4 veces.
    assert falso.logins == 3
    assert "FTMO-Server4" in str(err.value)


def test_si_el_ipc_esta_roto_si_se_reintenta(monkeypatch) -> None:
    """Un canal caído es otra cosa: ahí reintentar sí puede arreglarlo."""
    from pathlib import Path

    from app.worker.mt5_client import MT5Client

    falso = _TerminalSanoQueNoEntra()
    monkeypatch.setattr(mt5_module, "mt5", falso, raising=False)

    cliente = MT5Client(terminal_path=Path("."))
    cliente.login_attempts = 3
    cliente.reattach_wait_sec = 0.0
    monkeypatch.setattr(cliente, "_reattach", lambda _exe: False)  # canal roto

    assert cliente._login_with_patience(Path("t.exe"), 1, "x", "S") is False
    assert falso.logins == 3


def test_el_terminal_que_ya_entro_se_da_por_bueno(monkeypatch) -> None:
    """El corte de IPC al cambiar de bróker no puede confundirse con un fallo."""
    from pathlib import Path

    from app.worker.mt5_client import MT5Client

    falso = _TerminalSanoQueNoEntra()
    monkeypatch.setattr(mt5_module, "mt5", falso, raising=False)

    cliente = MT5Client(terminal_path=Path("."))
    cliente.login_attempts = 3
    cliente.reattach_wait_sec = 0.0
    monkeypatch.setattr(cliente, "_reattach", lambda _exe: True)
    monkeypatch.setattr(cliente, "_logged_as", lambda _login: True)  # sí entró

    assert cliente._login_with_patience(Path("t.exe"), 1, "x", "S") is True
