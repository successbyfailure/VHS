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
    assert upscale.parse_models("") == []


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


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
