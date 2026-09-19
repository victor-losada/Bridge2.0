"""Tope de tamaño del cuerpo de las peticiones.

FastAPI resuelve la dependencia de autenticación DENTRO del manejador, es
decir DESPUÉS de haber leído el cuerpo entero: sin este tope, cualquiera que
alcance el puerto —sin clave ninguna— obliga al único proceso que atiende a
todas las cuentas a leer y decodificar lo que le mande antes de poder
responderle 401, y por cada megabyte enviado el Bridge reserva unos tres
(bytes crudos, cadena decodificada y objeto Python). Ese mismo proceso es el
que vigila y relanza los Workers.

Va como middleware ASGI puro, sin BaseHTTPMiddleware, para poder cortar antes
de que nadie lea nada.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DEMASIADO_GRANDE = {
    "type": "http.response.start",
    "status": 413,
    "headers": [
        (b"content-type", b"application/json"),
        (b"connection", b"close"),
    ],
}
_CUERPO = b'{"detail":"request entity too large"}'


class LimitarCuerpo:
    """Rechaza con 413 cualquier petición cuyo cuerpo supere `max_bytes`."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declarado = 0
        for nombre, valor in scope.get("headers") or []:
            if nombre == b"content-length":
                try:
                    declarado = int(valor)
                except ValueError:
                    declarado = 0
                break

        if declarado > self.max_bytes:
            logger.warning(
                "cuerpo rechazado: %s bytes declarados, máximo %s",
                declarado,
                self.max_bytes,
            )
            await self._rechazar(send)
            return

        # Sin content-length (chunked) hay que contar lo que llega: un cuerpo
        # troceado puede ser tan grande como quiera quien lo manda.
        leidos = 0
        excedido = False

        async def receive_acotado():
            nonlocal leidos, excedido
            mensaje = await receive()
            if mensaje["type"] == "http.request":
                leidos += len(mensaje.get("body") or b"")
                if leidos > self.max_bytes:
                    excedido = True
                    # Cortamos el flujo: el manejador ve un cuerpo truncado y
                    # falla con 422, pero ya no seguimos acumulando memoria.
                    return {"type": "http.disconnect"}
            return mensaje

        async def send_con_aviso(mensaje):
            if excedido and mensaje["type"] == "http.response.start":
                await self._rechazar(send)
                return
            if excedido and mensaje["type"] == "http.response.body":
                return
            await send(mensaje)

        await self.app(scope, receive_acotado, send_con_aviso)

    async def _rechazar(self, send) -> None:
        await send(dict(_DEMASIADO_GRANDE))
        await send({"type": "http.response.body", "body": _CUERPO})
