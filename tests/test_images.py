"""Escalado de fotos: validación, modelos y lectura de errores.

Los límites se comprueban en VHS antes de subir la imagen para no gastar el
envío en algo que el servidor va a rechazar con un 413. Se puede ejecutar con
`pytest tests` o directamente con python.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import images as images_mod  # noqa: E402


def _png(ancho: int, alto: int, relleno: int = 0) -> bytes:
    """PNG con la cabecera correcta; el resto no se lee."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", ancho, alto)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * relleno
    )


def test_se_leen_las_dimensiones_de_un_png():
    assert images_mod.image_dimensions(_png(1920, 1080)) == (1920, 1080)


def test_un_formato_que_no_sabemos_medir_no_rompe():
    """Si no se puede medir, decide el servidor en vez de rechazar a ciegas."""
    assert images_mod.image_dimensions(b"no soy una imagen") is None
    assert images_mod.validate_image(b"no soy una imagen" * 10) is None


def test_una_imagen_dentro_de_los_limites_pasa():
    assert images_mod.validate_image(_png(3840, 2160)) == (3840, 2160)


def test_demasiados_pixeles_se_rechaza_antes_de_subir():
    try:
        images_mod.validate_image(_png(4200, 2362))
    except images_mod.ImageUpscaleError as exc:
        assert "4200x2362" in str(exc), exc
        assert str(images_mod.MAX_PIXELS) in str(exc)
    else:
        raise AssertionError("debería rechazarse por tamaño")


def test_demasiados_bytes_se_rechaza():
    gorda = _png(100, 100, relleno=images_mod.MAX_BYTES + 1)
    try:
        images_mod.validate_image(gorda)
    except images_mod.ImageUpscaleError as exc:
        assert "MB" in str(exc), exc
    else:
        raise AssertionError("debería rechazarse por peso")


def test_una_imagen_vacia_se_rechaza():
    try:
        images_mod.validate_image(b"")
    except images_mod.ImageUpscaleError as exc:
        assert "vacía" in str(exc)
    else:
        raise AssertionError("debería rechazarse")


def test_los_modelos_se_leen_con_etiqueta():
    opciones = images_mod.parse_models(
        "real-esrgan-compact-x4 - Rápido (7 s), remacri-x4, real-esrgan-compact-x4 - repetido"
    )
    assert [o["id"] for o in opciones] == ["real-esrgan-compact-x4", "remacri-x4"]
    assert opciones[0]["label"] == "Rápido (7 s)"
    # Sin etiqueta se usa el propio identificador.
    assert opciones[1]["label"] == "remacri-x4"


def test_un_modelo_fuera_de_la_lista_se_rechaza():
    original = images_mod.parse_models
    images_mod.parse_models = lambda raw="": [{"id": "remacri-x4", "label": "x"}]
    try:
        assert images_mod.resolve_model(None) == "remacri-x4"
        assert images_mod.resolve_model("remacri-x4") == "remacri-x4"
        try:
            images_mod.resolve_model("gemma4:26b")
        except images_mod.ImageUpscaleError as exc:
            assert "no permitido" in str(exc)
        else:
            raise AssertionError("debería rechazar un modelo no listado")
    finally:
        images_mod.parse_models = original


def test_los_formatos_de_salida_se_normalizan():
    assert images_mod.resolve_output_format("JPG") == "jpeg"
    assert images_mod.resolve_output_format("webp") == "webp"
    assert images_mod.extension_for("jpeg") == ".jpg"
    assert images_mod.media_type_for("webp") == "image/webp"
    try:
        images_mod.resolve_output_format("tiff")
    except images_mod.ImageUpscaleError as exc:
        assert "png" in str(exc)
    else:
        raise AssertionError("debería rechazar un formato no soportado")


class _Respuesta:
    """Lo mínimo de httpx.Response que usa el lector de errores."""

    def __init__(self, status_code, cuerpo, headers=None):
        self.status_code = status_code
        self._cuerpo = cuerpo
        self.headers = headers or {}
        self.text = str(cuerpo)

    def json(self):
        if isinstance(self._cuerpo, dict):
            return self._cuerpo
        raise ValueError("no es JSON")


def test_el_mensaje_de_error_se_saca_de_las_dos_formas():
    """oCabra manda el error dentro de `detail` y también en la raíz."""
    en_raiz = _Respuesta(400, {"error": {"message": "no capaz", "code": "model_not_capable"}})
    assert images_mod._mensaje_de_error(en_raiz) == "no capaz"
    anidado = _Respuesta(400, {"detail": {"error": {"message": "anidado"}}})
    assert images_mod._mensaje_de_error(anidado) == "anidado"


def test_la_imagen_demasiado_grande_se_explica_aunque_no_traiga_codigo():
    """El 413 del servidor viene con code a null."""
    respuesta = _Respuesta(413, {"error": {"message": "tiene 4200x2362 px", "code": None}})
    assert "4200x2362" in images_mod._mensaje_de_error(respuesta)
    sin_cuerpo = _Respuesta(413, "vaya")
    assert "demasiado grande" in images_mod._mensaje_de_error(sin_cuerpo)


def test_el_503_incluye_cuanto_esperar():
    respuesta = _Respuesta(
        503, {"error": {"message": "ocupado"}}, {"Retry-After": "30"}
    )
    mensaje = images_mod._mensaje_de_error(respuesta)
    assert "ocupado" in mensaje and "30" in mensaje


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
