"""Troceado de subtítulos a la norma de dos líneas de 42 caracteres.

Whisper devuelve segmentos de hasta 20 segundos: volcados tal cual daban
subtítulos de más de 250 caracteres en una sola línea, que ningún reproductor
muestra bien. Se puede ejecutar con `pytest tests` o directamente con python.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402


LARGO = (
    "Bien, bienvenidos de nuevo a Gary's Economics. Hoy vamos a presentar la "
    "primera parte de nuestro curso en línea, el vídeo número uno, que va a "
    "ser ¿Qué es la riqueza?"
)


def _cues(srt):
    """Devuelve [(inicio, fin, [líneas])] a partir de un SRT."""
    salida = []
    for bloque in srt.strip().split("\n\n"):
        lineas = bloque.splitlines()
        marcas = re.match(
            r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lineas[1]
        )
        valores = [int(x) for x in marcas.groups()]
        inicio = valores[0] * 3600 + valores[1] * 60 + valores[2] + valores[3] / 1000
        fin = valores[4] * 3600 + valores[5] * 60 + valores[6] + valores[7] / 1000
        salida.append((inicio, fin, lineas[2:]))
    return salida


def test_ninguna_linea_pasa_del_ancho_maximo():
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 0, "end": 12, "text": LARGO}]}
    )
    cues = _cues(srt)
    assert len(cues) > 1, "un segmento de 12 s y 180 caracteres debe trocearse"
    for _inicio, _fin, lineas in cues:
        assert len(lineas) <= main.SRT_MAX_LINES, lineas
        for linea in lineas:
            assert len(linea) <= main.SRT_MAX_LINE_CHARS, linea


def test_los_cues_van_en_orden_y_no_se_solapan():
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 5, "end": 25, "text": LARGO}]}
    )
    cues = _cues(srt)
    assert cues[0][0] == 5, cues[0]
    assert abs(cues[-1][1] - 25) < 0.01, cues[-1]
    for anterior, siguiente in zip(cues, cues[1:]):
        assert anterior[1] <= siguiente[0] + 0.001, (anterior, siguiente)
        assert siguiente[0] < siguiente[1], siguiente


def test_el_texto_completo_se_conserva():
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 0, "end": 12, "text": LARGO}]}
    )
    reconstruido = " ".join(
        " ".join(lineas) for _inicio, _fin, lineas in _cues(srt)
    )
    assert reconstruido.split() == LARGO.split()


def test_el_hablante_solo_aparece_en_el_primer_cue():
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 0, "end": 12, "speaker": "SPEAKER_01", "text": LARGO}]}
    )
    cues = _cues(srt)
    assert cues[0][2][0].startswith("SPEAKER_01: ")
    for _inicio, _fin, lineas in cues[1:]:
        assert "SPEAKER_01" not in " ".join(lineas)


def test_sin_trocear_se_mantiene_el_formato_anterior():
    original = main.SRT_SPLIT_CUES
    main.SRT_SPLIT_CUES = False
    try:
        srt = main.transcription_payload_to_srt(
            {"segments": [{"start": 0, "end": 12, "text": LARGO}]}
        )
    finally:
        main.SRT_SPLIT_CUES = original
    cues = _cues(srt)
    assert len(cues) == 1, cues
    assert cues[0][2] == [LARGO]


def test_un_segmento_corto_no_se_trocea():
    corto = "Vale, gracias."
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 0, "end": 2, "text": corto}]}
    )
    cues = _cues(srt)
    assert len(cues) == 1
    assert cues[0][2] == [corto]


def test_segmento_sin_texto_no_genera_cue():
    srt = main.transcription_payload_to_srt(
        {
            "segments": [
                {"start": 0, "end": 1, "text": "   "},
                {"start": 1, "end": 3, "text": "Hola."},
            ]
        }
    )
    cues = _cues(srt)
    assert len(cues) == 1, cues
    assert cues[0][2] == ["Hola."]


def test_no_deja_subtitulos_parpadeando():
    """El reparto por caracteres dejaba trozos de 0,11 s con una palabra."""
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 0, "end": 20, "text": LARGO}]}
    )
    for inicio, fin, lineas in _cues(srt):
        assert fin - inicio >= main.SRT_MIN_CUE_SECONDS - 0.01, (inicio, fin, lineas)


def test_las_lineas_quedan_equilibradas():
    # Repartir a lo ancho evita la última línea con una sola palabra.
    lineas = main._wrap_cue_lines(LARGO, 42)
    assert lineas
    assert min(len(linea) for linea in lineas) > 10, lineas


def test_el_hablante_no_desborda_la_linea():
    """El prefijo cuenta para el ancho: añadido después, la primera línea se pasaba."""
    srt = main.transcription_payload_to_srt(
        {"segments": [{"start": 0, "end": 12, "speaker": "SPEAKER_01", "text": LARGO}]}
    )
    for _inicio, _fin, lineas in _cues(srt):
        for linea in lineas:
            assert len(linea) <= main.SRT_MAX_LINE_CHARS, linea


def test_el_hablante_se_escribe_solo_cuando_cambia():
    """Repetirlo en cada subtítulo del mismo locutor es ruido."""
    srt = main.transcription_payload_to_srt(
        {
            "segments": [
                {"start": 0, "end": 3, "speaker": "A", "text": "Hola, buenas tardes."},
                {"start": 3, "end": 6, "speaker": "A", "text": "Seguimos hablando."},
                {"start": 6, "end": 9, "speaker": "B", "text": "Ahora hablo yo."},
                {"start": 9, "end": 12, "speaker": "A", "text": "Y vuelvo yo."},
            ]
        }
    )
    con_prefijo = [
        lineas for _i, _f, lineas in _cues(srt) if ":" in " ".join(lineas)
    ]
    # tres cambios de hablante: A, B y A otra vez
    assert len(con_prefijo) == 3, con_prefijo
    assert con_prefijo[0][0].startswith("A: ")
    assert con_prefijo[1][0].startswith("B: ")
    assert con_prefijo[2][0].startswith("A: ")


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
