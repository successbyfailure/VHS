"""Subtítulos cortados por las pausas reales del audio.

Con `timestamp_granularities: ["segment", "word"]` oCabra devuelve cada palabra
con su marca de tiempo, así que los subtítulos se pueden cortar donde el
hablante respira en vez de repartir el tiempo del segmento a prorrata.
Se puede ejecutar con `pytest tests` o directamente con python.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402


def _palabras(pares):
    """[(texto, inicio, fin)] -> lista de palabras como las devuelve el modelo."""
    return [{"word": f" {texto}", "start": inicio, "end": fin} for texto, inicio, fin in pares]


# Dos frases separadas por un silencio de 1,2 s en el segundo 6.
PRIMERA = "Bien bienvenidos de nuevo a Gary's Economics hoy vamos a ver algo".split()
SEGUNDA = "La riqueza no es lo mismo que los ingresos y conviene entenderlo".split()


def _payload_con_pausa():
    palabras = []
    reloj = 0.0
    for texto in PRIMERA:
        palabras.append((texto, reloj, reloj + 0.3))
        reloj += 0.4
    reloj += 1.2  # la pausa
    for texto in SEGUNDA:
        palabras.append((texto, reloj, reloj + 0.3))
        reloj += 0.4
    return {
        "text": " ".join(PRIMERA + SEGUNDA),
        "segments": [
            {"start": 0.0, "end": reloj, "text": " ".join(PRIMERA + SEGUNDA)}
        ],
        "words": _palabras(palabras),
    }


def test_sin_palabras_el_payload_no_se_toca():
    payload = {"segments": [{"start": 0, "end": 5, "text": "hola"}]}
    salida = main.resegment_payload_by_words(dict(payload))
    assert salida["segments"] == payload["segments"]


def test_se_corta_en_la_pausa_y_no_a_mitad_de_frase():
    salida = main.resegment_payload_by_words(_payload_con_pausa())
    segmentos = salida["segments"]
    assert len(segmentos) >= 2, segmentos
    # El corte debe caer justo antes de la segunda frase.
    primeras = [s for s in segmentos if s["text"].startswith("Bien")]
    assert primeras, segmentos
    assert primeras[0]["text"].endswith(PRIMERA[-1]), primeras[0]["text"]


def test_los_tiempos_son_los_de_las_palabras():
    payload = _payload_con_pausa()
    primera_palabra = payload["words"][0]
    ultima_palabra = payload["words"][-1]
    salida = main.resegment_payload_by_words(payload)
    segmentos = salida["segments"]
    assert segmentos[0]["start"] == primera_palabra["start"]
    assert segmentos[-1]["end"] == ultima_palabra["end"]
    # Un subtítulo no puede empezar antes de acabar el anterior.
    for anterior, siguiente in zip(segmentos, segmentos[1:]):
        assert anterior["end"] <= siguiente["start"] + 0.001, (anterior, siguiente)


def test_ningun_subtitulo_pasa_del_maximo():
    salida = main.resegment_payload_by_words(_payload_con_pausa())
    maximo = main.SRT_MAX_LINE_CHARS * main.SRT_MAX_LINES
    for segmento in salida["segments"]:
        assert len(segmento["text"]) <= maximo, segmento


def test_no_se_pierde_ni_se_duplica_texto():
    payload = _payload_con_pausa()
    esperado = (PRIMERA + SEGUNDA)
    salida = main.resegment_payload_by_words(payload)
    reconstruido = " ".join(s["text"] for s in salida["segments"]).split()
    assert reconstruido == esperado, reconstruido


def test_se_conserva_el_hablante():
    payload = _payload_con_pausa()
    payload["segments"][0]["speaker"] = "SPEAKER_02"
    salida = main.resegment_payload_by_words(payload)
    assert all(s.get("speaker") == "SPEAKER_02" for s in salida["segments"])


def test_las_palabras_no_se_quedan_duplicadas_en_el_payload():
    salida = main.resegment_payload_by_words(_payload_con_pausa())
    assert "words" not in salida


def test_las_palabras_de_cada_segmento_no_se_mezclan():
    """Con diarización, dos hablantes seguidos no pueden caer en el mismo cue."""
    payload = {
        "segments": [
            {"start": 0.0, "end": 1.0, "speaker": "A", "text": "hola que tal"},
            {"start": 1.0, "end": 2.0, "speaker": "B", "text": "muy bien gracias"},
        ],
        "words": _palabras(
            [
                ("hola", 0.0, 0.3),
                ("que", 0.3, 0.6),
                ("tal", 0.6, 0.9),
                ("muy", 1.0, 1.3),
                ("bien", 1.3, 1.6),
                ("gracias", 1.6, 1.9),
            ]
        ),
    }
    salida = main.resegment_payload_by_words(payload)
    for segmento in salida["segments"]:
        if segmento["speaker"] == "A":
            assert "gracias" not in segmento["text"], segmento
        else:
            assert "hola" not in segmento["text"], segmento


def test_solo_se_resegmenta_para_los_formatos_de_subtitulos():
    payload = _payload_con_pausa()
    originales = len(payload["segments"])
    # El JSON y el TXT conservan los segmentos del modelo.
    for formato in ("transcript_json", "transcript_text", "transcript_translate_text"):
        salida = main.prepare_transcription_payload(dict(payload), formato)
        assert len(salida["segments"]) == originales, formato
    salida = main.prepare_transcription_payload(dict(payload), "transcript_srt")
    assert len(salida["segments"]) > originales


def test_una_palabra_fuera_del_segmento_no_rompe_nada():
    payload = {
        "segments": [{"start": 5.0, "end": 6.0, "text": "hola"}],
        "words": _palabras([("ruido", 0.0, 0.5), ("hola", 5.0, 5.5)]),
    }
    salida = main.resegment_payload_by_words(payload)
    assert [s["text"] for s in salida["segments"]] == ["hola"]


def test_los_subtitulos_a_traducir_se_cortan_mas_cortos():
    """El español ocupa más: si se corta al tope, la traducción no cabe.

    Sin margen, el subtítulo traducido había que volver a partirlo a prorrata,
    perdiendo justo la sincronía que dan las marcas de palabra.
    """
    # Hace falta un texto seguido y largo: si hay una pausa clara, el corte lo
    # manda ella y el tope de caracteres no llega a notarse.
    palabras = ("la riqueza no es dinero el dinero es solo un derecho sobre la "
                "riqueza que otros poseen y conviene tenerlo muy claro desde el "
                "principio del curso").split()
    reloj = 0.0
    seguidas = []
    for texto in palabras:
        seguidas.append((texto, reloj, reloj + 0.25))
        reloj += 0.3
    payload = {
        "text": " ".join(palabras),
        "segments": [{"start": 0.0, "end": reloj, "text": " ".join(palabras)}],
        "words": _palabras(seguidas),
    }
    sin_traducir = main.prepare_transcription_payload(dict(payload), "transcript_srt")
    con_traduccion = main.prepare_transcription_payload(
        dict(payload), "transcript_translate_srt"
    )
    largo_sin = max(len(s["text"]) for s in sin_traducir["segments"])
    largo_con = max(len(s["text"]) for s in con_traduccion["segments"])
    assert largo_con < largo_sin, (largo_con, largo_sin)
    tope = main.SRT_MAX_LINE_CHARS * main.SRT_MAX_LINES
    assert largo_con <= int(tope / main.TRANSLATION_EXPANSION) + 1, largo_con


def test_el_srt_no_vuelve_a_partir_lo_ya_cortado():
    """Si los segmentos ya son subtítulos, el SRT los deja como están."""
    payload = main.prepare_transcription_payload(
        _payload_con_pausa(), "transcript_srt"
    )
    cues_esperados = len(payload["segments"])
    srt = main.transcription_payload_to_srt(payload)
    bloques = [b for b in srt.strip().split("\n\n") if b.strip()]
    assert len(bloques) == cues_esperados, (len(bloques), cues_esperados)


def test_el_reparto_en_dos_lineas_encuentra_lo_que_el_voraz_no():
    """Hay textos que caben en dos líneas solo si la primera no se llena."""
    texto = "una frase bastante larga que necesita exactamente dos lineas de texto"
    lineas = main._wrap_cue_lines(texto, 42)
    assert len(lineas) == 2, lineas
    assert all(len(linea) <= 42 for linea in lineas), lineas
    # y quedan parejas
    assert abs(len(lineas[0]) - len(lineas[1])) < 20, lineas


def test_si_no_cabe_en_dos_lineas_se_usan_tres():
    texto = "Vale, bienvenidos de nuevo a Gary's Economics. Hoy vamos a presentar la primera"
    lineas = main._wrap_cue_lines(texto, 42)
    assert len(lineas) == 3, lineas
    assert all(len(linea) <= 42 for linea in lineas), lineas


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
