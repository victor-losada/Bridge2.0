"""Configuración del Bridge a partir de variables de entorno."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Valores de ejemplo publicados en este repositorio. Cualquiera que haya visto
# el árbol los conoce, así que no pueden servir como credencial: si llegan al
# .env desplegado, el Bridge no arranca en vez de arrancar y parecer seguro.
CLAVES_DE_EJEMPLO = frozenset(
    {
        "cambiame-por-una-cadena-larga-y-aleatoria",
        "cambia-esta-clave-bridge",
        "la-que-espera-el-core",
        "pon-aqui-una-clave-larga",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Manager HTTP
    bridge_host: str = "0.0.0.0"
    bridge_port: int = 8088
    bridge_api_key: str = Field(..., min_length=16)
    # Tope del cuerpo de una petición. El mayor cuerpo legítimo es el alta de
    # cuenta, de unos cientos de bytes; 1 MiB deja margen de sobra.
    max_request_bytes: int = 1_048_576

    # Core
    core_events_url: str = "http://localhost:8000/internal/bridge/events"
    core_api_key: str = Field(..., min_length=8)
    # Reintentos del POST de eventos al Core (por evento).
    core_retry_max: int = 3
    # True = usar HTTP(S)_PROXY del sistema para llegar al Core.
    core_http_trust_env: bool = False

    # Cifrado en reposo (Fernet.urlsafe_b64)
    fernet_key: str = Field(..., min_length=32)

    @field_validator("bridge_api_key", "core_api_key")
    @classmethod
    def _no_es_de_ejemplo(cls, v: str, info) -> str:  # noqa: ANN001
        if v.strip().lower() in CLAVES_DE_EJEMPLO:
            raise ValueError(
                f"{info.field_name} tiene el valor de ejemplo del repositorio. "
                "Genera una cadena larga y aleatoria antes de arrancar."
            )
        return v

    # Pool
    slot_count: int = 15
    terminals_root: Path = Path("./terminals")
    data_dir: Path = Path("./data")

    # Polling (segundos)
    poll_account_sec: float = 9.0
    poll_positions_sec: float = 6.0
    poll_history_sec: float = 7.0
    history_lookback_days: int = 7
    # Holgura del tope de la ventana de historial: MT5 filtra por hora del
    # servidor del bróker, no por UTC.
    history_forward_buffer_hours: float = 24.0

    # Si True, al conectar se reenvían trade.closed del lookback.
    # Por defecto False: se marcan como ya vistos para no inundar al Core.
    replay_history_on_connect: bool = False

    # False = no emitir position.opened/updated/closed. El Core que solo
    # quiere operaciones cerradas se ahorra el libro de abiertas; el
    # seguimiento interno y los trade.closed no cambian.
    emit_position_events: bool = True

    # True = tras cada trade.closed aceptado, mandar tambien las velas de esa
    # operacion en un evento trade.candles, para que el Core pueda dibujarla.
    # Apagado por defecto: un Core que no conozca el evento lo rechazaria.
    emit_trade_candles: bool = False

    # Sumidero local de eventos en /api/v1/debug/events. Escribe cuanto le
    # manden en data/events.jsonl, sin limite: util en local, un disco que se
    # llena en produccion. Apagado salvo que se pida.
    enable_debug_sink: bool = False

    # Directorio publico que traduce el nombre de un broker a su direccion,
    # para dar de alta brokers que el terminal no conoce. Es un TERCERO que
    # decide a que host se conecta el terminal con las credenciales de un
    # cliente: vaciarlo desactiva el alta automatica y deja el resto igual.
    broker_directory_url: str = "https://search.mtapi.io/Search"
    # Velas alrededor del trade. 150 encuadra bien la operacion sin inflar el
    # payload (~8 KB). El timeframe se elige solo segun lo que duro el trade.
    trade_candles_count: int = 150

    # Lifecycle
    worker_restart_max: int = 5
    worker_restart_backoff_sec: float = 5.0
    # Margen entre el arranque de un Worker y el siguiente. Varios terminales
    # MT5 levantando a la vez se ahogan y mueren con -10005 IPC timeout.
    worker_spawn_stagger_sec: float = 25.0
    login_retry_max: int = 4
    login_retry_backoff_sec: float = 8.0
    shutdown_timeout_sec: float = 12.0
    mt5_init_timeout_ms: int = 60_000
    # Cambiar de bróker (plantilla con otro broker dentro) puede pasar del
    # minuto: descarga del servidor nuevo y sincronización de símbolos.
    mt5_login_timeout_ms: int = 180_000
    mt5_login_attempts: int = 3
    # Espera antes de reenganchar el IPC y preguntar quién entró.
    mt5_reattach_wait_sec: float = 20.0

    @field_validator("terminals_root", "data_dir", mode="before")
    @classmethod
    def _as_path(cls, value: str | Path) -> Path:
        return Path(value).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
