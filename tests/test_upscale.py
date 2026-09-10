"""Regresión del módulo de escalado de VHS.

Cubre lo que se puede probar sin GPU ni servicio: interpretación de la
configuración de modelos, avisos de tiempo y derivación del endpoint. La parte
de troceado/reensamblado se valida end-to-end contra el worker real.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import upscale  # noqa: E402


def test_parse_models_formato_completo():
    modelos = upscale.parse_models("a - Rápido - 90, b - Calidad - 3.2")
    assert [m["id"] for m in modelos] == ["a", "b"]
    assert modelos[0]["label"] == "Rápido"
    assert modelos[0]["fps"] == 90.0
    assert modelos[1]["fps"] == 3.2


def test_parse_models_tolera_campos_ausentes():
    # Sin fps no debe reventar: simplemente no se puede estimar el tiempo.
    modelos = upscale.parse_models("solo-id, otro - Con etiqueta")
    assert modelos[0]["id"] == "solo-id"
    assert modelos[0]["label"] == "solo-id"
    assert modelos[0]["fps"] == 0.0
    assert modelos[1]["label"] == "Con etiqueta"


def test_parse_models_sin_configuracion_devuelve_vacio():
    # Con la entrada vacía se recurre a UPSCALE_MODELS del entorno, así que hay
    # que limpiarlo: importar vhs.main ejecuta load_dotenv() y lo rellenaría.
    import os

    previo = os.environ.pop("UPSCALE_MODELS", None)
    try:
        assert upscale.parse_models("") == []
    finally:
        if previo is not None:
            os.environ["UPSCALE_MODELS"] = previo


def test_parse_models_ignora_fps_no_numerico():
    modelos = upscale.parse_models("a - Etiqueta - rapidísimo")
    assert modelos[0]["fps"] == 0.0


def test_format_duration_es():
    assert upscale.format_duration_es(45) == "45 s"
    assert upscale.format_duration_es(300) == "5 min"
    assert upscale.format_duration_es(3600) == "1 h"
    assert upscale.format_duration_es(5400) == "1 h 30 min"
    assert upscale.format_duration_es(0) == "desconocido"


def test_estimate_note_usa_los_fps_medidos():
    # 10 min a 30 fps son 18.000 fotogramas; a 60 fps son 5 minutos.
    assert upscale.estimate_note(60.0) == "10 minutos de vídeo pueden tardar 5 min"
    # A 3,2 fps (difusión) el aviso tiene que asustar, que para eso está.
    assert "1 h" in upscale.estimate_note(3.2)
    # Sin fps declarados no se inventa un tiempo.
    assert upscale.estimate_note(0) == ""


def test_endpoint_se_deriva_del_de_transcripcion(monkeypatch=None):
    import os
    previo = dict(os.environ)
    try:
        os.environ.pop("UPSCALE_ENDPOINT", None)
        os.environ["TRANSCRIPTION_ENDPOINT"] = "http://servidor:8484/v1"
        assert upscale.endpoint() == "http://servidor:8484/ocabra"
        os.environ["UPSCALE_ENDPOINT"] = "http://otro:9000/ocabra/"
        assert upscale.endpoint() == "http://otro:9000/ocabra"
    finally:
        os.environ.clear()
        os.environ.update(previo)


def test_build_download_name_no_duplica_extension():
    """En las subidas el título es el nombre original, que ya trae extensión."""
    from pathlib import Path as _P

    from vhs.main import build_download_name

    assert build_download_name("video.mp4", _P("out.mp3"), "ffmpeg_mp3-64") == "video.mp3"
    assert build_download_name("clip.mp4", _P("out.mp4"), "upscale_1080") == "clip.mp4"
    # Un título sin extensión no se toca.
    assert build_download_name("Dale Acabado LISO", _P("o.srt"), "transcript_srt") == "Dale_Acabado_LISO.srt"
    # Y un título que solo *parece* tener extensión tampoco.
    assert build_download_name("Episodio 1.5", _P("o.mp4"), "upscale_1080") == "Episodio_1.5.mp4"


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
