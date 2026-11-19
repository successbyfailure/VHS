"""Contrato de las llamadas OpenAI-compatible y extensiones de oCabra."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import main  # noqa: E402


class _FakeTranscriptions:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return {"text": "hola"}


class _FakeClient:
    def __init__(self):
        self.transcriptions = _FakeTranscriptions()
        self.audio = SimpleNamespace(transcriptions=self.transcriptions)


def test_transcripcion_envia_extensiones_ocabra():
    client = _FakeClient()
    original_client = main.create_openai_client
    original_extensions = main.OCABRA_EXTENSIONS
    main.create_openai_client = lambda: client
    main.OCABRA_EXTENSIONS = True
    try:
        with tempfile.NamedTemporaryFile() as audio:
            result = main._call_openai_transcription(
                Path(audio.name),
                "recomendado-stt",
                diarize=True,
            )
    finally:
        main.create_openai_client = original_client
        main.OCABRA_EXTENSIONS = original_extensions

    assert result["text"] == "hola"
    assert client.transcriptions.request["extra_body"] == {
        "vad_filter": main.TRANSCRIPTION_VAD_FILTER,
        "condition_on_previous_text": main.TRANSCRIPTION_CONDITION_ON_PREVIOUS_TEXT,
        "diarize": True,
    }


def test_transcripcion_generica_no_envia_extensiones():
    client = _FakeClient()
    original_client = main.create_openai_client
    original_extensions = main.OCABRA_EXTENSIONS
    main.create_openai_client = lambda: client
    main.OCABRA_EXTENSIONS = False
    try:
        with tempfile.NamedTemporaryFile() as audio:
            main._call_openai_transcription(Path(audio.name), "modelo")
    finally:
        main.create_openai_client = original_client
        main.OCABRA_EXTENSIONS = original_extensions

    assert client.transcriptions.request["extra_body"] is None


def test_batch_reordena_traducciones_por_custom_id():
    payload = {
        "segments": [
            {"text": "one", "start": 0, "end": 1},
            {"text": "two", "start": 1, "end": 2},
        ]
    }
    lines = [
        {
            "custom_id": "segment-000001",
            "response": {"body": {"choices": [{"message": {"content": "dos"}}]}},
        },
        {
            "custom_id": "segment-000000",
            "response": {"body": {"choices": [{"message": {"content": "uno"}}]}},
        },
    ]

    translated = main._apply_translation_batch(payload, lines, True)
    assert translated["text"] == "uno dos"
    assert [segment["text"] for segment in translated["segments"]] == ["uno", "dos"]
