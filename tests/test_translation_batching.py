"""Regresión del troceado por lotes de la traducción.

Un lote mal alineado desplazaría los subtítulos, así que ante cualquier
respuesta que no respete la numeración hay que caer al modo uno-a-uno.
Se puede ejecutar con `pytest tests` o directamente con python.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402


class _FakeClient:
    """Sustituye al cliente OpenAI: devuelve respuestas fijas y cuenta llamadas."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.peticiones = []

    def _responder(self, *_args, **kwargs):
        self.peticiones.append(kwargs)
        return self.respuestas.pop(0)


def _parchear(monkey_client):
    original = main._chat_translate
    main._chat_translate = lambda client, model, system, user: client._responder(
        system=system, user=user
    )
    return original


def test_lote_bien_formado():
    cliente = _FakeClient(["1. uno\n2. dos\n3. tres"])
    original = _parchear(cliente)
    try:
        salida = main._translate_batch(cliente, "m", ["one", "two", "three"])
    finally:
        main._chat_translate = original
    assert salida == ["uno", "dos", "tres"], salida
    assert len(cliente.peticiones) == 1, "debe ser una sola petición"


def test_lote_desalineado_se_rechaza():
    # Faltan líneas: aceptarlo desplazaría los subtítulos.
    for respuesta in (
        "1. uno\n2. dos",                 # falta la tercera
        "uno\ndos\ntres",                 # sin numeración
        "1. uno\n2. dos\n3. tres\n4. x",  # una línea de más
        "1. uno\n2. \n3. tres",           # una traducción vacía
    ):
        cliente = _FakeClient([respuesta])
        original = _parchear(cliente)
        try:
            assert main._translate_batch(cliente, "m", ["a", "b", "c"]) is None, respuesta
        finally:
            main._chat_translate = original


def test_fallback_uno_a_uno_conserva_el_orden():
    # El lote falla y se reintenta segmento a segmento, sin perder el orden.
    cliente = _FakeClient(["respuesta basura", "uno", "dos", "tres"])
    original = _parchear(cliente)
    original_openai = main.OpenAI
    original_batch = main.TRANSLATION_BATCH_SIZE
    original_conc = main.TRANSLATION_CONCURRENCY
    # La clave real no hace falta (el cliente es falso), pero sin ella la
    # función aborta antes de traducir y el test dependería del entorno.
    original_key = main.TRANSCRIPTION_API_KEY
    main.TRANSCRIPTION_API_KEY = original_key or "clave-de-prueba"
    main.OpenAI = lambda **_kwargs: cliente
    main.TRANSLATION_BATCH_SIZE = 3
    main.TRANSLATION_CONCURRENCY = 1
    try:
        salida = main._translate_texts_to_spanish(["one", "two", "three"])
    finally:
        main._chat_translate = original
        main.OpenAI = original_openai
        main.TRANSLATION_BATCH_SIZE = original_batch
        main.TRANSLATION_CONCURRENCY = original_conc
        main.TRANSCRIPTION_API_KEY = original_key
    assert salida == ["uno", "dos", "tres"], salida
    assert len(cliente.peticiones) == 4, cliente.peticiones


def test_numeros_con_separadores_distintos():
    cliente = _FakeClient(["1) uno\n2 - dos\n3: tres"])
    original = _parchear(cliente)
    try:
        salida = main._translate_batch(cliente, "m", ["a", "b", "c"])
    finally:
        main._chat_translate = original
    assert salida == ["uno", "dos", "tres"], salida


def test_linea_de_continuacion_se_rechaza():
    # El modelo parte un segmento en dos líneas: la segunda no lleva número y
    # antes se descartaba en silencio, perdiendo media traducción.
    cliente = _FakeClient(["1. primera parte\ncontinuacion perdida\n2. segunda"])
    original = _parchear(cliente)
    try:
        assert main._translate_batch(cliente, "m", ["a", "b"]) is None
    finally:
        main._chat_translate = original


def test_numero_repetido_se_rechaza():
    # Con un número repetido el recuento cuadraba y colaba basura en el
    # segmento equivocado.
    cliente = _FakeClient(["1. uno\n2. dos\n2. BASURA"])
    original = _parchear(cliente)
    try:
        assert main._translate_batch(cliente, "m", ["a", "b"]) is None
    finally:
        main._chat_translate = original


def test_numero_fuera_de_rango_se_rechaza():
    cliente = _FakeClient(["1. uno\n7. dos"])
    original = _parchear(cliente)
    try:
        assert main._translate_batch(cliente, "m", ["a", "b"]) is None
    finally:
        main._chat_translate = original


def test_preambulo_antes_de_la_numeracion_se_ignora():
    # Un encabezado antes de la lista es inofensivo: lo que no vale es texto
    # suelto después de haber empezado a numerar.
    cliente = _FakeClient(["Aquí tienes las traducciones:\n1. uno\n2. dos"])
    original = _parchear(cliente)
    try:
        assert main._translate_batch(cliente, "m", ["a", "b"]) == ["uno", "dos"]
    finally:
        main._chat_translate = original


def test_segmentos_vacios_no_se_mandan_al_modelo():
    """Un segmento sin texto abortaba el trabajo entero.

    Whisper deja segmentos vacíos cuando el VAD recorta, y pedirle al LLM que
    tradujera una cadena vacía devolvía vacío y lanzaba DownloadError.
    """
    cliente = _FakeClient(["1. uno\n2. dos"])
    original = _parchear(cliente)
    original_openai = main.OpenAI
    original_batch = main.TRANSLATION_BATCH_SIZE
    original_conc = main.TRANSLATION_CONCURRENCY
    main.OpenAI = lambda **_kwargs: cliente
    main.TRANSLATION_BATCH_SIZE = 8
    main.TRANSLATION_CONCURRENCY = 1
    try:
        salida = main._translate_texts_to_spanish(["one", "   ", "two", ""])
    finally:
        main._chat_translate = original
        main.OpenAI = original_openai
        main.TRANSLATION_BATCH_SIZE = original_batch
        main.TRANSLATION_CONCURRENCY = original_conc
    assert salida == ["uno", "   ", "dos", ""], salida
    # Una sola petición, con los dos únicos segmentos que tenían texto.
    assert len(cliente.peticiones) == 1, cliente.peticiones
    assert "one" in cliente.peticiones[0]["user"]
    assert "two" in cliente.peticiones[0]["user"]


def test_todos_los_segmentos_vacios_no_llaman_al_modelo():
    cliente = _FakeClient([])
    original = _parchear(cliente)
    original_openai = main.OpenAI
    main.OpenAI = lambda **_kwargs: cliente
    try:
        assert main._translate_texts_to_spanish(["", "  "]) == ["", "  "]
    finally:
        main._chat_translate = original
        main.OpenAI = original_openai
    assert cliente.peticiones == []


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
