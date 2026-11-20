"""Validación de los trabajos de medios y de subida.

Todo se gestiona desde el panel de trabajos, así que las peticiones se
comprueban antes de encolar: encolar algo que va a fallar solo sirve para que
el usuario descubra el error un minuto más tarde. Los tests se quedan en la
validación a propósito: el camino feliz descargaría de internet de verdad.
Se puede ejecutar con `pytest tests` o directamente con python.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import vhs.main as main  # noqa: E402


def test_una_url_vacia_se_rechaza():
    try:
        main._validate_media_request({"url": "  ", "format": "audio_med"})
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "URL" in exc.detail
    else:
        raise AssertionError("debería rechazar una url vacía")


def test_un_formato_inventado_se_rechaza():
    try:
        main._validate_media_request({"url": "https://x/y", "format": "no_existe"})
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "Formato inválido" in exc.detail
    else:
        raise AssertionError("debería rechazar el formato")


def test_el_formato_se_normaliza_y_los_alias_valen():
    url, formato, modelo, diarize = main._validate_media_request(
        {"url": "https://x/y", "format": "AUDIO_MED"}
    )
    assert (url, formato) == ("https://x/y", "audio_med")
    assert modelo is None and diarize is False


def test_la_diarizacion_se_deduce_del_formato():
    _url, formato, _modelo, diarize = main._validate_media_request(
        {"url": "https://x/y", "format": "transcript_diarized_srt"}
    )
    assert formato == "transcript_diarized_srt"
    assert diarize is True, "el formato ya pide hablantes"


def test_un_modelo_de_transcripcion_no_permitido_se_rechaza():
    try:
        main._validate_media_request(
            {
                "url": "https://x/y",
                "format": "transcript_text",
                "transcription_model": "modelo-que-no-existe",
            }
        )
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("debería rechazar el modelo")


def test_la_traduccion_comprueba_el_modelo_de_chat():
    """Antes el trabajo se encolaba y moría al llegar a traducir."""
    original = main.TRANSLATION_MODEL
    main.TRANSLATION_MODEL = "whisper-large-v3-turbo"
    try:
        main._validate_media_request(
            {"url": "https://x/y", "format": "transcript_translate_srt"}
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "TRANSLATION_MODEL" in exc.detail
    else:
        raise AssertionError("un whisper no puede traducir")
    finally:
        main.TRANSLATION_MODEL = original


def test_una_subida_sin_formato_valido_se_rechaza():
    with TestClient(main.app) as cliente:
        respuesta = cliente.post(
            "/api/jobs/upload",
            files={"file": ("a.mp3", b"datos", "audio/mpeg")},
            data={"media_format": "video_max"},
        )
        assert respuesta.status_code == 400, respuesta.text
        assert "subida" in respuesta.json()["detail"]


def test_una_subida_sin_fichero_se_rechaza():
    with TestClient(main.app) as cliente:
        respuesta = cliente.post(
            "/api/jobs/upload", data={"media_format": "ffmpeg_mp3-192"}
        )
        assert respuesta.status_code == 422, respuesta.text


def test_el_trabajo_de_medios_valida_antes_de_encolar():
    with TestClient(main.app) as cliente:
        respuesta = cliente.post("/api/jobs/media", json={"url": "", "format": "audio_med"})
        assert respuesta.status_code == 400, respuesta.text
        # Nada se ha encolado.
        listado = cliente.get("/api/jobs").json()
        assert listado["jobs"] == [], listado


if __name__ == "__main__":
    for nombre, funcion in sorted(globals().items()):
        if nombre.startswith("test_"):
            funcion()
            print(f"OK · {nombre}")
