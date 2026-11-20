"""Escalado de imágenes contra oCabra.

El servidor hace todo el trabajo: VHS valida la entrada, manda la foto y
guarda lo que vuelve. No hace falta trocear ni reensamblar nada, al contrario
que con el vídeo, porque el worker escala por teselas de 512 px y devuelve la
imagen completa.

Medido con un fotograma real de 1920x1080 (salida 7680x4320): 6,9 s con
`real-esrgan-compact-x4` y 18,5 s con `realesrgan-x4plus`. El caso límite de
3840x2160 a 4x tarda 45 s y devuelve 132 megapíxeles.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx


class ImageUpscaleError(RuntimeError):
    """Error con mensaje listo para mostrar al usuario."""


def _env(nombre: str, defecto: str = "") -> str:
    return (os.getenv(nombre) or defecto).strip()


def endpoint() -> str:
    """Base de la API. Comparte servidor con la transcripción."""
    explicit = _env("IMAGE_UPSCALE_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    return _env("TRANSCRIPTION_ENDPOINT", "http://localhost:8484/v1").rstrip("/")


def api_key() -> str:
    return _env("IMAGE_UPSCALE_API_KEY") or _env("TRANSCRIPTION_API_KEY")


# Límites del servidor (UPSCALER_IMAGE_MAX_PIXELS). Se validan aquí para dar un
# error claro sin subir la imagen y recibir un 413.
MAX_PIXELS = max(1, int(_env("IMAGE_UPSCALE_MAX_PIXELS", "8294400") or 8294400))
MAX_BYTES = max(1, int(_env("IMAGE_UPSCALE_MAX_BYTES", str(25 * 1024 * 1024))))
TIMEOUT = max(10.0, float(_env("IMAGE_UPSCALE_TIMEOUT", "900") or 900))

OUTPUT_FORMATS = ("png", "jpeg", "webp")
# WebP devuelve la misma imagen con 14 veces menos peso que el PNG (0,73 MB
# frente a 10,25 MB en la prueba), así que es un buen valor por defecto para
# descargar; png queda para quien quiera sin pérdida.
DEFAULT_OUTPUT_FORMAT = _env("IMAGE_UPSCALE_OUTPUT_FORMAT", "png").lower()

_EXTENSIONES = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
_TIPOS = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def parse_models(raw: str = "") -> List[Dict[str, str]]:
    """Interpreta IMAGE_UPSCALE_MODELS: ``id - etiqueta`` separados por comas."""
    raw = raw or _env("IMAGE_UPSCALE_MODELS")
    opciones: List[Dict[str, str]] = []
    for trozo in raw.split(","):
        valor = trozo.strip()
        if not valor:
            continue
        modelo, etiqueta = valor, ""
        if " - " in valor:
            modelo, etiqueta = valor.split(" - ", 1)
        modelo, etiqueta = modelo.strip(), etiqueta.strip()
        if modelo:
            opciones.append({"id": modelo, "label": etiqueta or modelo})
    vistos: set[str] = set()
    unicos: List[Dict[str, str]] = []
    for opcion in opciones:
        if opcion["id"] in vistos:
            continue
        vistos.add(opcion["id"])
        unicos.append(opcion)
    return unicos


def model_ids() -> List[str]:
    return [opcion["id"] for opcion in parse_models()]


def resolve_model(solicitado: Optional[str]) -> str:
    permitidos = model_ids()
    elegido = (solicitado or "").strip()
    if not permitidos:
        raise ImageUpscaleError(
            "El escalado de fotos no está configurado. Revisa IMAGE_UPSCALE_MODELS."
        )
    if not elegido:
        return permitidos[0]
    if elegido not in permitidos:
        raise ImageUpscaleError(
            "Modelo de escalado no permitido. Revisa IMAGE_UPSCALE_MODELS."
        )
    return elegido


def resolve_output_format(solicitado: Optional[str]) -> str:
    elegido = (solicitado or DEFAULT_OUTPUT_FORMAT or "png").strip().lower()
    if elegido == "jpg":
        elegido = "jpeg"
    if elegido not in OUTPUT_FORMATS:
        raise ImageUpscaleError(
            "Formato de salida no soportado. Usa png, jpeg o webp."
        )
    return elegido


def extension_for(formato: str) -> str:
    return _EXTENSIONES.get(formato, ".png")


def media_type_for(formato: str) -> str:
    return _TIPOS.get(formato, "image/png")


def image_dimensions(datos: bytes) -> Optional[Tuple[int, int]]:
    """Ancho y alto leyendo solo la cabecera, sin dependencias de imagen."""
    if datos[:8] == b"\x89PNG\r\n\x1a\n" and len(datos) >= 24:
        ancho, alto = struct.unpack(">II", datos[16:24])
        return int(ancho), int(alto)
    if datos[:2] == b"\xff\xd8":  # JPEG
        posicion = 2
        total = len(datos)
        while posicion + 9 < total:
            if datos[posicion] != 0xFF:
                posicion += 1
                continue
            marcador = datos[posicion + 1]
            if marcador in (0xD8, 0x01) or 0xD0 <= marcador <= 0xD7:
                posicion += 2
                continue
            longitud = struct.unpack(">H", datos[posicion + 2 : posicion + 4])[0]
            if marcador in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                alto, ancho = struct.unpack(">HH", datos[posicion + 5 : posicion + 9])
                return int(ancho), int(alto)
            posicion += 2 + longitud
        return None
    if datos[:12].startswith(b"RIFF") and datos[8:12] == b"WEBP":
        if datos[12:16] == b"VP8X" and len(datos) >= 30:
            ancho = int.from_bytes(datos[24:27], "little") + 1
            alto = int.from_bytes(datos[27:30], "little") + 1
            return ancho, alto
        if datos[12:16] == b"VP8 " and len(datos) >= 30:
            ancho = struct.unpack("<H", datos[26:28])[0] & 0x3FFF
            alto = struct.unpack("<H", datos[28:30])[0] & 0x3FFF
            return ancho, alto
    return None


def validate_image(datos: bytes) -> Optional[Tuple[int, int]]:
    """Comprueba los límites del servidor antes de gastar la subida."""
    if not datos:
        raise ImageUpscaleError("La imagen está vacía")
    if len(datos) > MAX_BYTES:
        raise ImageUpscaleError(
            f"La imagen pesa {len(datos) / 1024 / 1024:.1f} MB y el máximo son "
            f"{MAX_BYTES / 1024 / 1024:.0f} MB"
        )
    dimensiones = image_dimensions(datos)
    if dimensiones is None:
        # Formato que no sabemos medir: lo decide el servidor.
        return None
    ancho, alto = dimensiones
    if ancho * alto > MAX_PIXELS:
        raise ImageUpscaleError(
            f"La imagen tiene {ancho}x{alto} px y el máximo son {MAX_PIXELS} px "
            "de entrada. Redúcela antes de escalarla."
        )
    return dimensiones


def upscale_image(
    datos: bytes,
    *,
    nombre: str,
    model: str,
    output_format: str = "png",
    output_quality: Optional[int] = None,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    scale: Optional[float] = None,
    timeout: Optional[float] = None,
) -> Tuple[bytes, str]:
    """Escala la imagen y devuelve los bytes y la resolución entregada."""
    url = f"{endpoint()}/images/upscale"
    cabeceras = {}
    clave = api_key()
    if clave:
        cabeceras["Authorization"] = f"Bearer {clave}"
    campos: Dict[str, str] = {"model": model, "output_format": output_format}
    if output_quality is not None:
        campos["output_quality"] = str(output_quality)
    # `scale` y `target_*` solo reducen: nunca estiran por encima del 4x.
    if scale is not None:
        campos["scale"] = str(scale)
    if target_width is not None:
        campos["target_width"] = str(target_width)
    if target_height is not None:
        campos["target_height"] = str(target_height)
    try:
        respuesta = httpx.post(
            url,
            headers=cabeceras,
            files={"image": (nombre, datos, "application/octet-stream")},
            data=campos,
            timeout=timeout or TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ImageUpscaleError(
            f"No se pudo contactar con el servicio de escalado: {exc}"
        ) from exc
    if respuesta.status_code >= 400:
        raise ImageUpscaleError(_mensaje_de_error(respuesta))
    if not respuesta.content:
        raise ImageUpscaleError("El servicio de escalado devolvió una imagen vacía")
    entregada = respuesta.headers.get("X-Ocabra-Delivered-Resolution", "")
    return respuesta.content, entregada


def _mensaje_de_error(respuesta: httpx.Response) -> str:
    """Saca el mensaje útil del cuerpo, que llega anidado de dos formas.

    Las reglas por código se aplican aunque el cuerpo no sea JSON: si no, un
    413 con un cuerpo cualquiera se explicaba como un error genérico.
    """
    texto = respuesta.text[:300]
    mensaje: Optional[str] = None
    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = None
    if isinstance(cuerpo, dict):
        error = cuerpo.get("error")
        if not isinstance(error, dict):
            detalle = cuerpo.get("detail")
            if isinstance(detalle, dict):
                error = detalle.get("error")
        if isinstance(error, dict):
            mensaje = error.get("message")
    if respuesta.status_code == 413:
        # El servidor no manda `code` en este caso, así que se reconoce aquí.
        return mensaje or "La imagen es demasiado grande para escalarla"
    if respuesta.status_code == 503:
        espera = respuesta.headers.get("Retry-After")
        sufijo = f" Vuelve a intentarlo en {espera} s." if espera else ""
        return (mensaje or "El servicio de escalado no está disponible") + sufijo
    return mensaje or f"El servicio de escalado devolvió {respuesta.status_code}: {texto}"
