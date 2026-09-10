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

def test_short_side_filter_sirve_para_las_dos_orientaciones():
    """El objetivo es el lado corto: "1080p" vale en horizontal y en vertical."""
    f = upscale.short_side_filter(1080)
    # Horizontal (iw>ih): se fija la altura. Vertical: se fija la anchura.
    assert "if(gt(iw,ih),-2,1080)" in f
    assert "if(gt(iw,ih),1080,-2)" in f


def test_escala_nativa_define_el_prescalado_de_restauracion():
    # Restaurar a 2560 implica reducir a 640 para que el 4x aterrice justo.
    assert upscale.NATIVE_SCALE == 4
    assert round(2560 / upscale.NATIVE_SCALE) == 640

def test_plan_scaling_vertical_no_se_rechaza():
    # 1440x2560 vertical: el lado corto es 1440, así que 2160p SÍ es ampliar.
    # Con la lógica basada en altura esto se rechazaba por "ya tiene 2560px".
    modo, pre = upscale.plan_scaling(1440, 2560, 2160)
    assert modo == "upscale"
    assert pre == 540, pre  # techo objetivo/4, para que el 4x aterrice justo


def test_plan_scaling_acota_la_entrada_del_modelo():
    """Sin este techo el 4x generaba fotogramas gigantes y agotaba la VRAM."""
    _, pre = upscale.plan_scaling(1440, 2560, 2160)
    assert pre * upscale.NATIVE_SCALE == 2160
    _, pre = upscale.plan_scaling(3840, 2560, 1080)
    assert pre * upscale.NATIVE_SCALE == 1080


def test_plan_scaling_restaura_en_vez_de_rechazar():
    modo, pre = upscale.plan_scaling(3840, 2560, 2160)
    assert modo == "restore"
    assert pre == 540


def test_plan_scaling_no_preescala_si_ampliaria():
    # Fuente ya pequeña: no se toca antes del modelo.
    modo, pre = upscale.plan_scaling(480, 270, 1080)
    assert modo == "upscale"
    assert pre is None
    # Y si el objetivo excede lo que el modelo puede dar, se entrega menos
    # antes que fingir resolución.
    modo, pre = upscale.plan_scaling(480, 270, 2160)
    assert (modo, pre) == ("upscale", None)

def test_parse_models_lee_el_tope_de_resolucion():
    # El cuarto campo es el lado corto máximo: FlashVSR no llega a 1440p en
    # una tarjeta de 24 GB y hay que saberlo antes de gastar GPU.
    modelos = upscale.parse_models("a - Rápido - 63 - 0, b - Calidad - 2.1 - 1080")
    assert modelos[0]["max_short_side"] == 0
    assert modelos[1]["max_short_side"] == 1080
    # Ausente o inválido = sin límite, no un fallo.
    assert upscale.parse_models("c - X - 10")[0]["max_short_side"] == 0
    assert upscale.parse_models("d - X - 10 - mucho")[0]["max_short_side"] == 0


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
