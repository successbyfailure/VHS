"""Lote de traducción de la Batch API: agrupación, prosa y reintentos.

Antes se mandaba un segmento por petición (144 peticiones y 18.700 tokens de
entrada en un vídeo de 13 minutos). Ahora se agrupan igual que en la ruta
síncrona. Se puede ejecutar con `pytest tests` o directamente con python.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402


def _payload(cuantos, texto="hello"):
    return {"segments": [{"text": f"{texto} {i}", "start": i, "end": i + 1} for i in range(cuantos)]}


def _linea(custom_id, contenido):
    return {
        "custom_id": custom_id,
        "response": {"body": {"choices": [{"message": {"content": contenido}}]}},
    }


class _FakeChat:
    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.peticiones = []

    def create(self, **kwargs):
        self.peticiones.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.respuesta),
                    finish_reason="stop",
                )
            ]
        )


class _FakeClient:
    def __init__(self, respuesta="RESCATE"):
        self.chat_obj = _FakeChat(respuesta)
        self.chat = SimpleNamespace(completions=self.chat_obj)


def _con_config(**kwargs):
    """Fija atributos de main y devuelve los valores originales."""
    originales = {nombre: getattr(main, nombre) for nombre in kwargs}
    for nombre, valor in kwargs.items():
        setattr(main, nombre, valor)
    return originales


def _restaurar(originales):
    for nombre, valor in originales.items():
        setattr(main, nombre, valor)


def test_los_segmentos_se_agrupan_en_lotes():
    previos = _con_config(TRANSLATION_MODEL="chat-model", TRANSLATION_BATCH_SIZE=8)
    try:
        requests, has_segments = main._translation_requests(_payload(20))
    finally:
        _restaurar(previos)
    assert has_segments is True
    # 20 segmentos en lotes de 8 son 3 peticiones, no 20.
    assert len(requests) == 3, [r["custom_id"] for r in requests]
    assert [r["custom_id"] for r in requests] == [
        "group-000000-008",
        "group-000008-008",
        "group-000016-004",
    ]
    assert requests[0]["url"] == "/v1/chat/completions"


def test_el_tope_de_tokens_escala_con_el_lote():
    previos = _con_config(
        TRANSLATION_MODEL="chat-model",
        TRANSLATION_BATCH_SIZE=8,
        TRANSLATION_BATCH_MAX_TOKENS=512,
    )
    try:
        requests, _ = main._translation_requests(_payload(10))
    finally:
        _restaurar(previos)
    assert requests[0]["body"]["max_tokens"] == 512 * 8
    assert requests[1]["body"]["max_tokens"] == 512 * 2


def test_las_extensiones_de_ocabra_respetan_el_flag():
    previos = _con_config(
        TRANSLATION_MODEL="chat-model",
        OCABRA_EXTENSIONS=False,
        TRANSLATION_REASONING_EFFORT="off",
    )
    try:
        requests, _ = main._translation_requests(_payload(2))
    finally:
        _restaurar(previos)
    # "off" no es un valor válido en api.openai.com: tumbaría el lote entero.
    assert "reasoning_effort" not in requests[0]["body"]

    previos = _con_config(
        TRANSLATION_MODEL="chat-model",
        OCABRA_EXTENSIONS=True,
        TRANSLATION_REASONING_EFFORT="off",
    )
    try:
        requests, _ = main._translation_requests(_payload(2))
    finally:
        _restaurar(previos)
    assert requests[0]["body"]["reasoning_effort"] == "off"


def test_los_segmentos_vacios_no_ocupan_peticion():
    previos = _con_config(TRANSLATION_MODEL="chat-model", TRANSLATION_BATCH_SIZE=8)
    payload = {"segments": [{"text": "hola"}, {"text": "   "}, {"text": "mundo"}]}
    try:
        requests, _ = main._translation_requests(payload)
    finally:
        _restaurar(previos)
    assert len(requests) == 1
    assert requests[0]["custom_id"] == "group-000000-002"
    assert "   " not in requests[0]["body"]["messages"][1]["content"]


def test_el_resultado_agrupado_se_coloca_en_su_sitio():
    payload = _payload(3)
    lineas = [_linea("group-000000-003", "1. uno\n2. dos\n3. tres")]
    salida = main._apply_translation_batch(payload, lineas, True)
    assert [s["text"] for s in salida["segments"]] == ["uno", "dos", "tres"]
    assert salida["text"] == "uno dos tres"


def test_los_lotes_se_reordenan_por_custom_id():
    payload = _payload(4)
    lineas = [
        _linea("group-000002-002", "1. tres\n2. cuatro"),
        _linea("group-000000-002", "1. uno\n2. dos"),
    ]
    salida = main._apply_translation_batch(payload, lineas, True)
    assert [s["text"] for s in salida["segments"]] == ["uno", "dos", "tres", "cuatro"]


def test_un_lote_mal_alineado_se_reintenta_uno_a_uno():
    """Mismo criterio que la ruta síncrona: antes colaba basura en silencio."""
    payload = _payload(2)
    cliente = _FakeClient("RESCATE")
    previos = _con_config(TRANSLATION_MODEL="chat-model")
    try:
        salida = main._apply_translation_batch(
            payload, [_linea("group-000000-002", "1. uno\n2. dos\n2. BASURA")], True, cliente
        )
    finally:
        _restaurar(previos)
    assert [s["text"] for s in salida["segments"]] == ["RESCATE", "RESCATE"]
    assert len(cliente.chat_obj.peticiones) == 2


def test_los_segmentos_vacios_se_conservan_al_aplicar():
    payload = {"segments": [{"text": "hola"}, {"text": ""}]}
    lineas = [_linea("group-000000-001", "1. hello")]
    salida = main._apply_translation_batch(payload, lineas, True)
    assert [s["text"] for s in salida["segments"]] == ["hello", ""]
    assert salida["text"] == "hello"


def test_sigue_entendiendo_el_formato_de_un_segmento_por_peticion():
    """Un trabajo reanudado puede tener en vuelo un lote del formato anterior."""
    payload = _payload(2)
    lineas = [
        _linea("segment-000001", "dos"),
        _linea("segment-000000", "uno"),
    ]
    salida = main._apply_translation_batch(payload, lineas, True)
    assert [s["text"] for s in salida["segments"]] == ["uno", "dos"]


def test_falta_un_segmento_y_se_avisa():
    payload = _payload(4)
    lineas = [_linea("group-000000-002", "1. uno\n2. dos")]
    try:
        main._apply_translation_batch(payload, lineas, True)
    except RuntimeError as exc:
        assert "2 de 4" in str(exc), exc
    else:
        raise AssertionError("debería avisar de los segmentos que faltan")


def test_el_txt_plano_se_traduce_por_bloques_de_prosa():
    previos = _con_config(
        TRANSLATION_MODEL="chat-model",
        TRANSLATION_PROSE_TEXT=True,
        TRANSLATION_PROSE_WORDS=50,
    )
    try:
        assert main.is_prose_translation_format("transcript_translate_text") is True
        # El SRT y el JSON tienen que seguir yendo segmento a segmento.
        assert main.is_prose_translation_format("transcript_translate_srt") is False
        assert main.is_prose_translation_format("transcript_translate_json") is False
        payload = _payload(60, texto="palabra")
        requests, _ = main._translation_requests(
            payload, "transcript_translate_text"
        )
    finally:
        _restaurar(previos)
    assert all(r["custom_id"].startswith("prose-") for r in requests), requests
    assert len(requests) < 60


def test_la_prosa_se_reconstruye_en_orden():
    payload = _payload(4)
    lineas = [
        _linea("prose-000001", "Segundo bloque."),
        _linea("prose-000000", "Primer bloque."),
    ]
    salida = main._apply_translation_batch(payload, lineas, True)
    assert salida["text"] == "Primer bloque.\n\nSegundo bloque."
    # Los segmentos quedarían en el idioma original: no se dejan a medias.
    assert salida["segments"] == []


def test_se_detecta_el_presupuesto_agotado_sin_texto():
    """oCabra avisa de que finish_reason=length sin contenido es un fallo."""

    class _Truncado:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=""), finish_reason="length"
                    )
                ]
            )

    try:
        main._chat_translate(_Truncado(), "m", "sys", "user")
    except main.DownloadError as exc:
        assert "max_tokens" in str(exc), exc
    else:
        raise AssertionError("un presupuesto agotado no puede pasar por texto vacío")


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
