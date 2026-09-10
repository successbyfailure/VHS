import hashlib
import json
import os
import random
import re
import unicodedata
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import certifi
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Ensure the runtime always has a CA bundle to prevent SSL failures, even in
# slim containers where the OS certificates may be missing or a proxy injects
# a custom CA path. We forcefully override the environment variables so that
# Python's SSL module and any underlying libraries consistently rely on the
# certifi bundle.
CERT_BUNDLE = certifi.where()
os.environ["SSL_CERT_FILE"] = CERT_BUNDLE
os.environ["REQUESTS_CA_BUNDLE"] = CERT_BUNDLE

# Cargar variables definidas en un archivo .env si está presente. Esto permite
# configurar claves (como la de transcripción) sin depender del entorno del
# sistema o del orquestador.
load_dotenv()

import yt_dlp
from openai import OpenAI

from versioning import get_version
from vhs import jobs as jobs_mod
from vhs import upscale as upscale_mod

APP_TITLE = "VHS · Video Harvester Service"
VHS_VERSION = get_version("vhs")
CACHE_DIR = Path(os.getenv("CACHE_DIR", "data/cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
META_DIR = CACHE_DIR / "_meta"
META_DIR.mkdir(parents=True, exist_ok=True)
YTDLP_CACHE_DIR = Path(os.getenv("YTDLP_CACHE_DIR", CACHE_DIR / "yt_dlp_cache"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", 60 * 60 * 24))
USAGE_LOG_PATH = Path(os.getenv("USAGE_LOG_PATH", "data/usage_log.jsonl"))
USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
SUPPORTED_SERVICES = [
    "YouTube",
    "Vimeo",
    "TikTok",
    "Instagram",
    "Facebook",
    "Twitch",
    "Dailymotion",
    "SoundCloud",
    "Twitter / X",
    "Reddit",
]
YTDLP_PROXY = os.getenv("YTDLP_PROXY")
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE")

YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
YTDLP_BOT_PROTECTION_RETRIES = int(os.getenv("YTDLP_BOT_PROTECTION_RETRIES", "3"))
YTDLP_BOT_PROTECTION_DELAY = float(os.getenv("YTDLP_BOT_PROTECTION_DELAY", "6"))
_raw_extractor_args = os.getenv("YTDLP_EXTRACTOR_ARGS")
if _raw_extractor_args:
    try:
        YTDLP_EXTRACTOR_ARGS = json.loads(_raw_extractor_args)
    except json.JSONDecodeError:
        YTDLP_EXTRACTOR_ARGS = {"youtube": [_raw_extractor_args]}
else:
    YTDLP_EXTRACTOR_ARGS = {"youtube": ["player_client=default"]}
TRANSCRIPTION_ENDPOINT = os.getenv("TRANSCRIPTION_ENDPOINT", "https://api.openai.com/v1")
TRANSCRIPTION_API_KEY = os.getenv("TRANSCRIPTION_API_KEY")
_TRANSCRIPTION_MODEL_RAW = os.getenv("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip()
_TRANSCRIPTION_MODELS_RAW = os.getenv("TRANSCRIPTION_MODELS", "").strip()
_DIARIZATION_MODEL_RAW = os.getenv("DIARIZATION_MODEL", "").strip()
_DIARIZATION_MODELS_RAW = os.getenv("DIARIZATION_MODELS", "").strip()


def _legacy_transcription_aliases(model_id: str) -> List[str]:
    aliases = [model_id]
    if "/" in model_id:
        aliases.append(model_id.split("/", 1)[1].strip())
        return aliases
    if model_id.startswith("whisper-"):
        aliases.append(f"openai/{model_id}")
    elif model_id.startswith(("parakeet-", "canary-")):
        aliases.append(f"nvidia/{model_id}")
    elif model_id.startswith("nvidia-"):
        # El endpoint publica "nvidia-parakeet-...", pero clientes antiguos
        # siguen pidiendo "parakeet-..." o "nvidia/parakeet-...".
        bare_model = model_id.split("-", 1)[1]
        aliases.extend((bare_model, f"nvidia/{bare_model}"))
    elif model_id.startswith("faster-whisper-"):
        aliases.extend((f"nekusu/{model_id}", f"Systran/{model_id}"))
    return aliases


def _diarization_aliases(model_id: str) -> List[str]:
    aliases: List[str] = []
    if model_id.endswith("::diarize"):
        base_model = model_id[: -len("::diarize")].strip()
    elif model_id.endswith("-diarized"):
        base_model = model_id[: -len("-diarized")].strip()
    elif model_id.endswith("-diarize"):
        base_model = model_id[: -len("-diarize")].strip()
    else:
        base_model = model_id.strip()
    if not base_model:
        return aliases
    for transcription_alias in _legacy_transcription_aliases(base_model):
        aliases.extend(
            (
                f"{transcription_alias}-diarized",
                f"{transcription_alias}-diarize",
                f"{transcription_alias}::diarize",
            )
        )
    deduped: List[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if alias in seen:
            continue
        seen.add(alias)
        deduped.append(alias)
    return deduped


def _resolve_model_alias(requested_model: str, allowed_model_ids: List[str]) -> str:
    if requested_model in allowed_model_ids:
        return requested_model
    for allowed_model_id in allowed_model_ids:
        aliases = (
            _diarization_aliases(allowed_model_id)
            if any(
                suffix in allowed_model_id for suffix in ("-diarized", "-diarize", "::diarize")
            )
            else _legacy_transcription_aliases(allowed_model_id)
        )
        if requested_model in aliases:
            return allowed_model_id
    return requested_model


def _parse_transcription_models(raw_models: str, fallback_model: str) -> List[Dict[str, str]]:
    # Formato soportado: model-id - etiqueta o solo model-id.
    # Importante: no usar "::" como separador para no romper ids heredados.
    parsed: List[Dict[str, str]] = []
    if raw_models:
        for item in raw_models.split(","):
            value = item.strip()
            if not value:
                continue
            model_id = value
            label = ""
            if " - " in value:
                model_id, label = value.split(" - ", 1)
            model_id = model_id.strip()
            label = label.strip()
            if model_id:
                parsed.append({"id": model_id, "label": label or model_id})
    if not parsed and fallback_model:
        parsed = [{"id": fallback_model, "label": fallback_model}]

    deduped: List[Dict[str, str]] = []
    seen: set[str] = set()
    for option in parsed:
        model_id = option["id"]
        if model_id in seen:
            continue
        seen.add(model_id)
        deduped.append(option)
    return deduped


def _derive_diarization_models_from_transcription_options(
    transcription_options: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    derived: List[Dict[str, str]] = []
    for option in transcription_options:
        model_id = option["id"].strip()
        if not model_id:
            continue
        if model_id.endswith(("-diarized", "-diarize", "::diarize")):
            continue
        derived.append(
            {
                "id": f"{model_id}-diarized",
                "label": f"{option['label']} (diarized)",
            }
        )
    return derived


TRANSCRIPTION_MODEL_OPTIONS = _parse_transcription_models(
    _TRANSCRIPTION_MODELS_RAW, _TRANSCRIPTION_MODEL_RAW
)
TRANSCRIPTION_MODEL_IDS = [option["id"] for option in TRANSCRIPTION_MODEL_OPTIONS]
_resolved_transcription_default = _resolve_model_alias(
    _TRANSCRIPTION_MODEL_RAW, TRANSCRIPTION_MODEL_IDS
)
if _TRANSCRIPTION_MODEL_RAW and _resolved_transcription_default in TRANSCRIPTION_MODEL_IDS:
    TRANSCRIPTION_MODEL = _resolved_transcription_default
elif TRANSCRIPTION_MODEL_IDS:
    TRANSCRIPTION_MODEL = TRANSCRIPTION_MODEL_IDS[0]
else:
    TRANSCRIPTION_MODEL = ""

_derived_diarization_options = _derive_diarization_models_from_transcription_options(
    TRANSCRIPTION_MODEL_OPTIONS
)
_diarization_fallback_model = _DIARIZATION_MODEL_RAW or (
    _derived_diarization_options[0]["id"] if _derived_diarization_options else ""
)
DIARIZATION_MODEL_OPTIONS = _parse_transcription_models(
    _DIARIZATION_MODELS_RAW,
    _diarization_fallback_model,
)
if not _DIARIZATION_MODELS_RAW and _derived_diarization_options:
    DIARIZATION_MODEL_OPTIONS = _derived_diarization_options
DIARIZATION_MODEL_IDS = [option["id"] for option in DIARIZATION_MODEL_OPTIONS]
_resolved_diarization_default = _resolve_model_alias(
    _DIARIZATION_MODEL_RAW, DIARIZATION_MODEL_IDS
)
if _DIARIZATION_MODEL_RAW and _resolved_diarization_default in DIARIZATION_MODEL_IDS:
    DIARIZATION_MODEL = _resolved_diarization_default
elif DIARIZATION_MODEL_IDS:
    DIARIZATION_MODEL = DIARIZATION_MODEL_IDS[0]
else:
    DIARIZATION_MODEL = ""


def resolve_transcription_model(requested_model: Optional[str]) -> str:
    selected = (requested_model or "").strip()
    if selected:
        selected = _resolve_model_alias(selected, TRANSCRIPTION_MODEL_IDS)
        if TRANSCRIPTION_MODEL_IDS and selected not in TRANSCRIPTION_MODEL_IDS:
            raise DownloadError(
                "Modelo de transcripción no permitido. Revisa TRANSCRIPTION_MODELS."
            )
        return selected
    if TRANSCRIPTION_MODEL:
        return TRANSCRIPTION_MODEL
    raise DownloadError(
        "La transcripción no está disponible. Configura TRANSCRIPTION_MODEL o TRANSCRIPTION_MODELS."
    )


def resolve_diarization_model(requested_model: Optional[str]) -> str:
    selected = (requested_model or "").strip()
    if selected:
        selected = _resolve_model_alias(selected, DIARIZATION_MODEL_IDS)
        if DIARIZATION_MODEL_IDS and selected not in DIARIZATION_MODEL_IDS:
            raise DownloadError(
                "Modelo de diarización no permitido. Revisa DIARIZATION_MODELS."
            )
        return selected
    if DIARIZATION_MODEL:
        return DIARIZATION_MODEL
    raise DownloadError(
        "La diarización no está disponible. Configura DIARIZATION_MODEL o DIARIZATION_MODELS."
    )


FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFMPEG_ENABLE_NVENC = os.getenv("FFMPEG_ENABLE_NVENC", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FFMPEG_VIDEO_ENCODER = "h264_nvenc" if FFMPEG_ENABLE_NVENC else "libx264"
FFMPEG_HWACCEL_ARGS: List[str] = (
    ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if FFMPEG_ENABLE_NVENC else []
)

# NVENC no acepta los presets de x264 ("veryfast", "faster"...) ni la opción
# -crf: usa presets p1..p7 y -cq. Se traducen aquí para que los perfiles se
# sigan describiendo en términos de x264 y funcionen con ambos encoders.
#
# Las escalas no son equivalentes y traducirlas 1:1 sale caro. Medido sobre un
# tramo de 1080p (SSIM/PSNR contra el original, ficheros del mismo contenido):
#
#   libx264 veryfast crf22   26.6 MB   SSIM 0.99196   PSNR 45.47 dB
#   nvenc p2 cq22            52.2 MB   SSIM 0.99155   PSNR 45.84 dB  <- el doble
#   nvenc p6 cq32            27.1 MB   SSIM 0.99234   PSNR 46.81 dB  <- mejor
#
# Es decir, con "cq = crf" NVENC gastaba el doble de bytes sin ganar calidad.
# Con el desplazamiento y un preset más lento (que en GPU sigue siendo rápido)
# iguala en tamaño y mejora la calidad medida.
_NVENC_PRESET_MAP = {
    "ultrafast": "p1",
    "superfast": "p2",
    "veryfast": "p6",
    "faster": "p6",
    "fast": "p6",
    "medium": "p6",
    "slow": "p7",
    "slower": "p7",
    "veryslow": "p7",
}
# Dos puntos de operación razonables, ajustables sin tocar código:
#   p6 + offset 10 (por defecto): ~1.2x más rápido que libx264 veryfast, mismo
#     tamaño y algo mejor de calidad medida.
#   p2 + offset 12: ~2.6x más rápido, mismo tamaño, calidad ligeramente inferior.
_NVENC_CQ_OFFSET = int(os.getenv("FFMPEG_NVENC_CQ_OFFSET", "10"))
_NVENC_PRESET_OVERRIDE = os.getenv("FFMPEG_NVENC_PRESET", "").strip()


def ffmpeg_video_quality_args(preset: str, quality: str) -> List[str]:
    """Argumentos de preset y calidad para el encoder de vídeo activo."""
    if FFMPEG_ENABLE_NVENC:
        nvenc_preset = _NVENC_PRESET_OVERRIDE or _NVENC_PRESET_MAP.get(preset, "p6")
        cq = min(51, max(1, int(quality) + _NVENC_CQ_OFFSET))
        return ["-preset", nvenc_preset, "-cq", str(cq)]
    return ["-preset", preset, "-crf", quality]


AUDIO_FORMAT_PROFILES = {
    "audio_max": {
        "format": "bestaudio/best",
        "passthrough": True,
        "description": "Mejor audio disponible desde la fuente (sin recomprimir)",
    },
    "audio_med": {
        "codec": "mp3",
        "preferred_quality": "96",
        "description": "MP3 a 96 kbps equilibrado",
    },
    "audio_low": {
        "codec": "mp3",
        "preferred_quality": "48",
        "description": "MP3 a 48 kbps optimizado para tamaños pequeños",
    },
}
VIDEO_FORMAT_PROFILES = {
    "video_max": {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "description": "Video en la mejor calidad disponible desde la fuente",
    },
    "video_1080": {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/worst",
        "merge_output_format": "mp4",
        "description": "Video hasta 1080p equilibrado",
    },
    "video_med": {
        "format": "bv*[height<=720]+ba/b[height<=720]/worst",
        "merge_output_format": "mp4",
        "description": "Video reescalado hasta 720p",
    },
    "video_low": {
        "format": "bv*[height<=480]+ba/b[height<=480]/worst",
        "merge_output_format": "mp4",
        "description": "Video comprimido hasta 480p",
    },
}
DEFAULT_VIDEO_FORMAT = "video_max"
VIDEO_FORMAT_ALIASES = {
    "video": DEFAULT_VIDEO_FORMAT,
    "video_high": "video_max",
}
for old_audio in ("audio_high",):
    AUDIO_FORMAT_PROFILES[old_audio] = AUDIO_FORMAT_PROFILES["audio_max"]
FFMPEG_PRESETS: Dict[str, Dict[str, Any]] = {
    "ffmpeg_480p": {
        "description": "Transcodifica a 480p (h.264 CRF 24 máx. ~1.8 Mbps / AAC 128 kbps)",
        "extension": ".mp4",
        "media_type": "video/mp4",
        "input_args": FFMPEG_HWACCEL_ARGS,
        "args": [
            "-vf",
            "scale_cuda=-2:480" if FFMPEG_ENABLE_NVENC else "scale=-2:480",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            *ffmpeg_video_quality_args("veryfast", "24"),
            "-maxrate",
            "1800k",
            "-bufsize",
            "3600k",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ],
        "video_height": 480,
        "video_bitrate_kbps": 1800,
        "audio_bitrate_kbps": 128,
    },
    "ffmpeg_720p": {
        "description": "Transcodifica a 720p (h.264 CRF 23 máx. ~3.2 Mbps / AAC 160 kbps)",
        "extension": ".mp4",
        "media_type": "video/mp4",
        "input_args": FFMPEG_HWACCEL_ARGS,
        "args": [
            "-vf",
            "scale_cuda=-2:720" if FFMPEG_ENABLE_NVENC else "scale=-2:720",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            *ffmpeg_video_quality_args("veryfast", "23"),
            "-maxrate",
            "3200k",
            "-bufsize",
            "6400k",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ],
        "video_height": 720,
        "video_bitrate_kbps": 3200,
        "audio_bitrate_kbps": 160,
    },
    "ffmpeg_1080p": {
        "description": "Transcodifica a 1080p (h.264 CRF 22 máx. ~4.8 Mbps / AAC 176 kbps)",
        "extension": ".mp4",
        "media_type": "video/mp4",
        "input_args": FFMPEG_HWACCEL_ARGS,
        "args": [
            "-vf",
            "scale_cuda=-2:1080" if FFMPEG_ENABLE_NVENC else "scale=-2:1080",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            *ffmpeg_video_quality_args("veryfast", "22"),
            "-maxrate",
            "4800k",
            "-bufsize",
            "9600k",
            "-c:a",
            "aac",
            "-b:a",
            "176k",
        ],
        "video_height": 1080,
        "video_bitrate_kbps": 4800,
        "audio_bitrate_kbps": 176,
    },
    "ffmpeg_1440p": {
        "description": "Transcodifica a 1440p (h.264 CRF 21 máx. ~8 Mbps / AAC 192 kbps)",
        "extension": ".mp4",
        "media_type": "video/mp4",
        "input_args": FFMPEG_HWACCEL_ARGS,
        "args": [
            "-vf",
            "scale_cuda=-2:1440" if FFMPEG_ENABLE_NVENC else "scale=-2:1440",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            *ffmpeg_video_quality_args("faster", "21"),
            "-maxrate",
            "8000k",
            "-bufsize",
            "16000k",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ],
        "video_height": 1440,
        "video_bitrate_kbps": 8000,
        "audio_bitrate_kbps": 192,
    },
    "ffmpeg_3840p": {
        "description": "Transcodifica a 4K (h.264 CRF 20 máx. ~12 Mbps / AAC 256 kbps)",
        "extension": ".mp4",
        "media_type": "video/mp4",
        "input_args": FFMPEG_HWACCEL_ARGS,
        "args": [
            "-vf",
            "scale_cuda=-2:2160" if FFMPEG_ENABLE_NVENC else "scale=-2:2160",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            *ffmpeg_video_quality_args("fast", "20"),
            "-maxrate",
            "12000k",
            "-bufsize",
            "24000k",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
        ],
        "video_height": 2160,
        "video_bitrate_kbps": 12000,
        "audio_bitrate_kbps": 256,
    },
    "ffmpeg_wav": {
        "description": "Convierte a WAV sin pérdidas (44.1 kHz, estéreo)",
        "extension": ".wav",
        "media_type": "audio/wav",
        "args": ["-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2"],
        "audio_bitrate_kbps": 1411,
    },
    "ffmpeg_mp3-192": {
        "description": "MP3 192 kbps con libmp3lame",
        "extension": ".mp3",
        "media_type": "audio/mpeg",
        "args": ["-vn", "-acodec", "libmp3lame", "-b:a", "192k"],
        "audio_bitrate_kbps": 192,
    },
    "ffmpeg_mp3-128": {
        "description": "MP3 128 kbps",
        "extension": ".mp3",
        "media_type": "audio/mpeg",
        "args": ["-vn", "-acodec", "libmp3lame", "-b:a", "128k"],
        "audio_bitrate_kbps": 128,
    },
    "ffmpeg_mp3-96": {
        "description": "MP3 96 kbps",
        "extension": ".mp3",
        "media_type": "audio/mpeg",
        "args": ["-vn", "-acodec", "libmp3lame", "-b:a", "96k"],
        "audio_bitrate_kbps": 96,
    },
    "ffmpeg_mp3-64": {
        "description": "MP3 64 kbps",
        "extension": ".mp3",
        "media_type": "audio/mpeg",
        "args": ["-vn", "-acodec", "libmp3lame", "-b:a", "64k"],
        "audio_bitrate_kbps": 64,
    },
}
TRANSCRIPTION_FORMATS = {
    "transcript_json",
    "transcript_text",
    "transcript_srt",
    "transcript_diarized_json",
    "transcript_diarized_text",
    "transcript_diarized_srt",
    "transcript_translate_json",
    "transcript_translate_text",
    "transcript_translate_srt",
    "transcript_translate_diarized_json",
    "transcript_translate_diarized_text",
}
SUPPORTED_MEDIA_FORMATS = {
    *VIDEO_FORMAT_PROFILES,
    *VIDEO_FORMAT_ALIASES,
    *AUDIO_FORMAT_PROFILES,
    *FFMPEG_PRESETS,
    *TRANSCRIPTION_FORMATS,
}

FORMAT_DESCRIPTIONS: List[Dict[str, str]] = [
    {
        "name": "video_max",
        "description": "MP4 en la mejor calidad disponible (mezcla best video + best audio)",
    },
    {
        "name": "video_1080",
        "description": "MP4 hasta 1080p con buen equilibrio de peso/calidad",
    },
    {
        "name": "video_med",
        "description": "MP4 hasta 720p pensado para la web",
    },
    {
        "name": "video_low",
        "description": "MP4 comprimido hasta 480p para descargas ligeras",
    },
    {
        "name": "video",
        "description": "Alias histórico de video_high para compatibilidad",
    },
    {
        "name": "audio_max",
        "description": "Mejor pista de audio disponible sin recomprimir",
    },
    {
        "name": "audio_med",
        "description": "MP3 a 96 kbps equilibrado",
    },
    {
        "name": "audio_low",
        "description": "MP3 a 48 kbps optimizado para tamaños pequeños",
    },
    {
        "name": "transcript_json",
        "description": "Transcripción detallada con timestamps por segmento (JSON)",
    },
    {
        "name": "transcript_text",
        "description": "Transcripción en texto plano (TXT)",
    },
    {
        "name": "transcript_srt",
        "description": "Subtítulos sincronizados (SRT)",
    },
    {
        "name": "transcript_diarized_json",
        "description": "Transcripción detallada con identificación de hablantes (JSON)",
    },
    {
        "name": "transcript_diarized_text",
        "description": "Transcripción en texto plano con identificación de hablantes (TXT)",
    },
    {
        "name": "transcript_diarized_srt",
        "description": "Subtítulos sincronizados con identificación de hablantes (SRT)",
    },
    {
        "name": "transcript_translate_json",
        "description": "Transcripción traducida al español con timestamps (JSON)",
    },
    {
        "name": "transcript_translate_text",
        "description": "Transcripción traducida al español en texto plano (TXT)",
    },
    {
        "name": "transcript_translate_srt",
        "description": "Subtítulos sincronizados traducidos al español (SRT)",
    },
    {
        "name": "transcript_translate_diarized_json",
        "description": "Transcripción traducida al español con identificación de hablantes (JSON)",
    },
    {
        "name": "transcript_translate_diarized_text",
        "description": "Transcripción traducida al español con identificación de hablantes (TXT)",
    },
]

for preset_name, preset in FFMPEG_PRESETS.items():
    FORMAT_DESCRIPTIONS.append(
        {"name": preset_name, "description": preset["description"]}
    )

app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory="templates")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")


def template_context(request: Request, **kwargs: Any) -> Dict[str, Any]:
    context = {
        "request": request,
        "app_name": APP_TITLE,
        "vhs_version": VHS_VERSION,
    }
    context.update(kwargs)
    return context


class DownloadError(RuntimeError):
    """Error amigable para fallos de descarga."""


def cache_key(url: str, media_format: str) -> str:
    normalized = f"{url.strip()}::{media_format.strip().lower()}"
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def normalize_media_format(media_format: str) -> str:
    value = (media_format or "").strip().lower()
    return VIDEO_FORMAT_ALIASES.get(value, value)


def is_diarization_format(media_format: str) -> bool:
    normalized = normalize_media_format(media_format)
    return normalized in {
        "transcript_diarized_json",
        "transcript_diarized_text",
        "transcript_diarized_srt",
        "transcript_translate_diarized_json",
        "transcript_translate_diarized_text",
    }


def is_translation_format(media_format: str) -> bool:
    normalized = normalize_media_format(media_format)
    return normalized.startswith("transcript_translate")


def meta_path(key: str) -> Path:
    return META_DIR / f"{key}.json"


def legacy_meta_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def is_expired(meta: Dict) -> bool:
    downloaded_at = meta.get("downloaded_at") or 0
    return (time.time() - float(downloaded_at)) > CACHE_TTL_SECONDS


FORMAT_EXTENSIONS = {
    "video": ".mp4",
    "video_max": ".mp4",
    "video_med": ".mp4",
    "video_low": ".mp4",
    "video_1080": ".mp4",
    "audio_max": ".ogg",
    "audio_med": ".mp3",
    "audio_low": ".mp3",
    "transcript_json": ".json",
    "transcript_text": ".txt",
    "transcript_srt": ".srt",
    "transcript_diarized_json": ".json",
    "transcript_diarized_text": ".txt",
    "transcript_diarized_srt": ".srt",
    "transcript_translate_json": ".json",
    "transcript_translate_text": ".txt",
    "transcript_translate_srt": ".srt",
    "transcript_translate_diarized_json": ".json",
    "transcript_translate_diarized_text": ".txt",
}

for preset_name, preset in FFMPEG_PRESETS.items():
    FORMAT_EXTENSIONS[preset_name] = preset["extension"]
FORMAT_EXTENSIONS["video_high"] = FORMAT_EXTENSIONS["video_max"]
FORMAT_EXTENSIONS["audio_high"] = FORMAT_EXTENSIONS["audio_max"]

# Formatos de escalado. Se declaran por **resolución objetivo** y no por
# factor: el 4x del modelo es un detalle de implementación que no debe salir a
# la interfaz, y algunos modelos admiten factores arbitrarios.
UPSCALE_FORMATS = {
    "upscale_1080": 1080,
    "upscale_1440": 1440,
    "upscale_2160": 2160,
}
for _name in UPSCALE_FORMATS:
    FORMAT_EXTENSIONS[_name] = ".mp4"

# Extensiones de medios que se consideran "ya presentes" al construir el
# nombre de descarga (ver build_download_name).
KNOWN_MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".ts",
    ".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".aac", ".m4a", ".wma",
    ".srt", ".vtt", ".json", ".txt",
}

UPSCALE_JOBS_DIR = Path(os.getenv("UPSCALE_JOBS_DIR", str(CACHE_DIR.parent / "upscale_jobs")))
_job_store = jobs_mod.JobStore(UPSCALE_JOBS_DIR)
_job_queue = jobs_mod.JobQueue(_job_store)


def _run_upscale_job(params: Dict, on_progress, should_cancel) -> Dict:
    """Ejecutor del trabajo de escalado. Corre en un hilo, no en el bucle."""
    source = Path(params["source"])
    try:
        output, metadata = upscale_mod.upscale_file(
            source,
            model=params["model"],
            target_height=params["target_height"],
            ffmpeg=FFMPEG_BINARY,
            nvenc=FFMPEG_ENABLE_NVENC,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
    finally:
        # El original subido ya no hace falta ni si falla.
        cleanup_path(source)
    return {
        "path": output,
        "name": build_download_name(
            params.get("filename") or "upscaled", output, params["media_format"]
        ),
        "metadata": metadata,
    }


_job_queue.register("upscale", _run_upscale_job)

TRANSCRIPTION_FILE_SUFFIX = ".transcript.json"


def media_type_for_format(media_format: str) -> str:
    normalized = normalize_media_format(media_format)
    if normalized in {"transcript_srt", "transcript_diarized_srt", "transcript_translate_srt"}:
        return "text/srt"
    if normalized in {
        "transcript_json",
        "transcript_diarized_json",
        "transcript_translate_json",
        "transcript_translate_diarized_json",
    }:
        return "application/json"
    if normalized in TRANSCRIPTION_FORMATS - {"transcript_json"}:
        return "text/plain"
    if normalized in UPSCALE_FORMATS:
        return "video/mp4"
    if normalized in FFMPEG_PRESETS:
        return FFMPEG_PRESETS[normalized]["media_type"]
    if normalized in AUDIO_FORMAT_PROFILES:
        profile = AUDIO_FORMAT_PROFILES[normalized]
        if profile.get("passthrough"):
            # audio_max usa OGG/Opus después del remux
            return "audio/ogg"
        return "audio/mpeg"
    return "video/mp4"


def categorize_media_format(media_format: str) -> str:
    normalized = normalize_media_format(media_format)
    if normalized in FFMPEG_PRESETS:
        return "recoding"
    if normalized in TRANSCRIPTION_FORMATS:
        return "transcription"
    if normalized in AUDIO_FORMAT_PROFILES:
        return "audio"
    return "video"


def detect_request_source(request: Request, fallback: Optional[str] = None) -> str:
    raw_source = (
        getattr(request, "state", None) and getattr(request.state, "source", None)
    ) or request.query_params.get("source") or request.headers.get("X-VHS-Source") or fallback or ""
    source = str(raw_source).strip().lower()
    if source in {"api", "web"}:
        return source

    referer = (request.headers.get("referer") or "").lower()
    if referer and "/api/" not in referer:
        return "web"

    user_agent = (request.headers.get("user-agent") or "").lower()
    if "mozilla" in user_agent:
        return "web"

    return "api"


def record_download_event(
    media_format: str,
    cache_hit: bool,
    transcription_stats: Optional[Dict[str, Any]] = None,
    source: str = "api",
    *,
    size_bytes: Optional[int] = None,
    processing_ms: Optional[float] = None,
    provider: Optional[str] = None,
    translation: bool = False,
    diarization: bool = False,
) -> None:
    event: Dict[str, Any] = {
        "timestamp": time.time(),
        "media_format": media_format,
        "cache_hit": bool(cache_hit),
        "category": categorize_media_format(media_format),
    }
    normalized_source = source if source in {"api", "web"} else "other"
    event["source"] = normalized_source
    if transcription_stats:
        word_count = transcription_stats.get("word_count")
        token_count = transcription_stats.get("token_count")
        if isinstance(word_count, (int, float)):
            event["word_count"] = int(word_count)
        if isinstance(token_count, (int, float)):
            event["token_count"] = int(token_count)
    if size_bytes is not None:
        event["size_bytes"] = int(size_bytes)
    if processing_ms is not None:
        event["processing_ms"] = float(processing_ms)
    if provider:
        event["provider"] = provider
    if translation:
        event["translation"] = True
    if diarization:
        event["diarization"] = True
    try:
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        # El registro es best-effort: no debe impedir completar la descarga.
        print(f"[vhs] No se pudo registrar el uso: {exc}", file=sys.stderr)


def record_error_event(error_type: str, source: str = "api") -> None:
    """Registrar un evento de error para estadísticas de uso."""

    normalized_source = source if source in {"api", "web"} else "other"
    event = {
        "timestamp": time.time(),
        "category": "error",
        "error_type": error_type,
        "source": normalized_source,
    }
    try:
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[vhs] No se pudo registrar el error: {exc}", file=sys.stderr)


def summarize_usage(days: int = 7) -> Dict[str, Any]:
    if not USAGE_LOG_PATH.exists():
        points = []
    else:
        points = []
        with USAGE_LOG_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    points.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=days - 1)
    aggregates: Dict[str, Dict[str, int]] = {}
    for idx in range(days):
        day = (now - timedelta(days=days - idx - 1)).date()
        aggregates[day.isoformat()] = {
            "downloads": 0,
            "api_downloads": 0,
            "web_downloads": 0,
            "other_downloads": 0,
            "cache_hits": 0,
            "word_count": 0,
            "token_count": 0,
            "recodings": 0,
            "transcriptions": 0,
            "errors": 0,
            "translations": 0,
            "diarized": 0,
            "bytes": 0,
            "cache_bytes_saved": 0,
            "processing_ms": [],
        }

    # Totales para el período especificado (últimos N días)
    total_downloads = 0
    total_api_downloads = 0
    total_web_downloads = 0
    total_other_downloads = 0
    total_cache_hits = 0
    total_word_count = 0
    total_token_count = 0
    total_recodings = 0
    total_transcriptions = 0
    total_errors = 0
    total_translations = 0
    total_diarized = 0
    total_bytes = 0
    total_cache_bytes_saved = 0
    format_totals: Dict[str, int] = {}
    provider_counts: Dict[str, int] = {}
    error_counts: Dict[str, int] = {}
    processing_all: List[float] = []

    # Estadísticas totales desde el inicio de los tiempos (all-time)
    alltime_downloads = 0
    alltime_api_downloads = 0
    alltime_web_downloads = 0
    alltime_other_downloads = 0
    alltime_cache_hits = 0
    alltime_word_count = 0
    alltime_token_count = 0
    alltime_recodings = 0
    alltime_transcriptions = 0
    alltime_errors = 0
    alltime_translations = 0
    alltime_diarized = 0
    alltime_bytes = 0
    alltime_cache_bytes_saved = 0
    alltime_format_totals: Dict[str, int] = {}
    alltime_provider_counts: Dict[str, int] = {}
    alltime_error_counts: Dict[str, int] = {}
    alltime_processing_all: List[float] = []

    for event in points:
        timestamp = event.get("timestamp")
        if timestamp is None:
            continue
        event_dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)

        # Procesar estadísticas all-time (siempre)
        source = event.get("source") or "api"
        if event.get("category") == "error":
            alltime_errors += 1
            err_type = event.get("error_type") or "desconocido"
            alltime_error_counts[err_type] = alltime_error_counts.get(err_type, 0) + 1
        else:
            alltime_downloads += 1
            if source == "web":
                alltime_web_downloads += 1
            elif source == "api":
                alltime_api_downloads += 1
            else:
                alltime_other_downloads += 1

            if event.get("cache_hit"):
                alltime_cache_hits += 1

            word_count = int(event.get("word_count") or 0)
            token_count = int(event.get("token_count") or 0)
            alltime_word_count += word_count
            alltime_token_count += token_count

            media_format = event.get("media_format", "")
            label = media_format or "desconocido"
            alltime_format_totals[label] = alltime_format_totals.get(label, 0) + 1

            category = event.get("category") or categorize_media_format(media_format)
            if category == "recoding":
                alltime_recodings += 1
            if category == "transcription":
                alltime_transcriptions += 1
                if event.get("translation"):
                    alltime_translations += 1
                if event.get("diarization"):
                    alltime_diarized += 1

            size_bytes = event.get("size_bytes")
            if isinstance(size_bytes, (int, float)):
                alltime_bytes += int(size_bytes)
                if event.get("cache_hit"):
                    alltime_cache_bytes_saved += int(size_bytes)

            proc = event.get("processing_ms")
            if isinstance(proc, (int, float)):
                alltime_processing_all.append(float(proc))

        provider = event.get("provider")
        if provider:
            alltime_provider_counts[provider] = alltime_provider_counts.get(provider, 0) + 1

        # Procesar estadísticas del período especificado (últimos N días)
        if event_dt < cutoff:
            continue

        day_key = event_dt.date().isoformat()
        if day_key not in aggregates:
            continue

        if event.get("category") == "error":
            aggregates[day_key]["errors"] += 1
            total_errors += 1
            err_type = event.get("error_type") or "desconocido"
            error_counts[err_type] = error_counts.get(err_type, 0) + 1
            continue

        aggregates[day_key]["downloads"] += 1
        if source == "web":
            aggregates[day_key]["web_downloads"] += 1
            total_web_downloads += 1
        elif source == "api":
            aggregates[day_key]["api_downloads"] += 1
            total_api_downloads += 1
        else:
            aggregates[day_key]["other_downloads"] += 1
            total_other_downloads += 1
        if event.get("cache_hit"):
            aggregates[day_key]["cache_hits"] += 1
        total_downloads += 1
        if event.get("cache_hit"):
            total_cache_hits += 1
        word_count = int(event.get("word_count") or 0)
        token_count = int(event.get("token_count") or 0)
        aggregates[day_key]["word_count"] += word_count
        aggregates[day_key]["token_count"] += token_count
        total_word_count += word_count
        total_token_count += token_count
        media_format = event.get("media_format", "")
        label = media_format or "desconocido"
        format_totals[label] = format_totals.get(label, 0) + 1
        category = event.get("category") or categorize_media_format(media_format)
        if category == "recoding":
            aggregates[day_key]["recodings"] += 1
            total_recodings += 1
        if category == "transcription":
            aggregates[day_key]["transcriptions"] += 1
            total_transcriptions += 1
            if event.get("translation"):
                aggregates[day_key]["translations"] += 1
                total_translations += 1
            if event.get("diarization"):
                aggregates[day_key]["diarized"] += 1
                total_diarized += 1
        size_bytes = event.get("size_bytes")
        if isinstance(size_bytes, (int, float)):
            aggregates[day_key]["bytes"] += int(size_bytes)
            total_bytes += int(size_bytes)
            if event.get("cache_hit"):
                aggregates[day_key]["cache_bytes_saved"] += int(size_bytes)
                total_cache_bytes_saved += int(size_bytes)
        proc = event.get("processing_ms")
        if isinstance(proc, (int, float)):
            aggregates[day_key]["processing_ms"].append(float(proc))
            processing_all.append(float(proc))
        if provider:
            provider_counts[provider] = provider_counts.get(provider, 0) + 1

    series = [
        {"date": day, **aggregates[day]} for day in sorted(aggregates.keys())
    ]
    for day_entry in series:
        proc_list = day_entry.pop("processing_ms", [])
        if proc_list:
            proc_list_sorted = sorted(proc_list)
            processing_all.extend(proc_list_sorted)
            day_entry["processing_avg_ms"] = sum(proc_list_sorted) / len(proc_list_sorted)
            idx = max(0, int(len(proc_list_sorted) * 0.95) - 1)
            day_entry["processing_p95_ms"] = proc_list_sorted[idx]
        else:
            day_entry["processing_avg_ms"] = 0.0
            day_entry["processing_p95_ms"] = 0.0

    # Top formats para el período especificado
    top_formats = sorted(
        format_totals.items(), key=lambda item: item[1], reverse=True
    )[:3]

    # Processing summary para el período especificado
    processing_summary = {"average_ms": 0.0, "p95_ms": 0.0}
    if processing_all:
        processing_all.sort()
        processing_summary["average_ms"] = sum(processing_all) / len(processing_all)
        processing_summary["p95_ms"] = processing_all[max(0, int(len(processing_all) * 0.95) - 1)]

    # Top formats all-time
    alltime_top_formats = sorted(
        alltime_format_totals.items(), key=lambda item: item[1], reverse=True
    )[:3]

    # Processing summary all-time
    alltime_processing_summary = {"average_ms": 0.0, "p95_ms": 0.0}
    if alltime_processing_all:
        alltime_processing_all.sort()
        alltime_processing_summary["average_ms"] = sum(alltime_processing_all) / len(alltime_processing_all)
        alltime_processing_summary["p95_ms"] = alltime_processing_all[max(0, int(len(alltime_processing_all) * 0.95) - 1)]

    return {
        "points": series,
        "total": total_downloads,
        "api_downloads": total_api_downloads,
        "web_downloads": total_web_downloads,
        "other_downloads": total_other_downloads,
        "cache_hits": total_cache_hits,
        "total_words": total_word_count,
        "total_tokens": total_token_count,
        "ffmpeg_runs": total_recodings,
        "transcriptions": total_transcriptions,
        "translations": total_translations,
        "diarized": total_diarized,
        "bytes_served": total_bytes,
        "cache_bytes_saved": total_cache_bytes_saved,
        "processing": processing_summary,
        "providers": provider_counts,
        "errors": total_errors,
        "unique_formats": len(format_totals),
        "top_formats": [
            {"media_format": name, "count": count} for name, count in top_formats
        ],
        "top_errors": [
            {"error_type": name, "count": count}
            for name, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        ],
        "days": days,
        # Estadísticas all-time (desde el inicio)
        "alltime": {
            "total": alltime_downloads,
            "api_downloads": alltime_api_downloads,
            "web_downloads": alltime_web_downloads,
            "other_downloads": alltime_other_downloads,
            "cache_hits": alltime_cache_hits,
            "total_words": alltime_word_count,
            "total_tokens": alltime_token_count,
            "ffmpeg_runs": alltime_recodings,
            "transcriptions": alltime_transcriptions,
            "translations": alltime_translations,
            "diarized": alltime_diarized,
            "bytes_served": alltime_bytes,
            "cache_bytes_saved": alltime_cache_bytes_saved,
            "processing": alltime_processing_summary,
            "providers": alltime_provider_counts,
            "errors": alltime_errors,
            "unique_formats": len(alltime_format_totals),
            "top_formats": [
                {"media_format": name, "count": count} for name, count in alltime_top_formats
            ],
            "top_errors": [
                {"error_type": name, "count": count}
                for name, count in sorted(alltime_error_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            ],
        },
    }


def build_download_name(title: str, file_path: Path, media_format: str) -> str:
    base = title.strip() or "vhs"
    # Eliminar solo caracteres problemáticos para sistemas de archivos
    # Mantiene letras Unicode (acentos, ñ, etc.), números, espacios, guiones, etc.
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    # Reemplazar múltiples espacios/guiones bajos consecutivos por uno solo
    safe = re.sub(r"[\s_]+", "_", safe).strip("._") or "vhs"
    extension = file_path.suffix or FORMAT_EXTENSIONS.get(media_format, ".bin")
    # En las subidas el "título" es el nombre del fichero original, que ya trae
    # extensión: sin esto la descarga salía como "video.mp4.mp3". Solo se quita
    # si la extensión previa es una de medios conocidos, para no destrozar
    # títulos legítimos como "Episodio 1.5".
    previous = Path(safe).suffix.lower()
    if previous and previous in KNOWN_MEDIA_EXTENSIONS:
        safe = Path(safe).stem or safe
    return f"{safe}{extension}"


def _ascii_filename_fallback(filename: str) -> str:
    """Genera un nombre ASCII seguro cuando el original tiene Unicode."""

    normalized = unicodedata.normalize("NFKD", filename)
    stripped = normalized.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stripped)
    safe = re.sub(r"[\s_]+", "_", safe).strip("._") or "vhs"
    return safe


def build_content_disposition_header(filename: str) -> str:
    """
    Construye un header Content-Disposition compatible con RFC 5987
    que soporta caracteres Unicode.
    """
    try:
        # Intentar codificar como ASCII
        filename.encode('ascii')
        # Si es ASCII puro, usar formato simple
        return f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        # Si tiene caracteres Unicode, usar RFC 5987
        encoded_filename = quote(filename, safe='')
        ascii_fallback = _ascii_filename_fallback(filename)
        # Incluir fallback ASCII para clientes (curl -J/-O) que ignoran filename*
        return (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=utf-8''{encoded_filename}"
        )


def load_meta(key: str) -> Optional[Dict]:
    primary_path = meta_path(key)
    if primary_path.exists():
        with primary_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("cache_key", key)
        return data

    legacy_path = legacy_meta_path(key)
    if not legacy_path.exists():
        return None

    with legacy_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("cache_key", key)
    # Migrar a la nueva ubicación para evitar conflictos con archivos de datos.
    save_meta(key, data)
    legacy_path.unlink(missing_ok=True)
    return data


def delete_cache_entry(key: str, metadata: Optional[Dict] = None) -> None:
    meta = metadata or load_meta(key) or {}
    data_file = meta.get("filename")
    if data_file:
        stored_file = CACHE_DIR / data_file
        if stored_file.exists():
            stored_file.unlink(missing_ok=True)
    meta_path(key).unlink(missing_ok=True)
    legacy_meta_path(key).unlink(missing_ok=True)


# Un lock por clave de caché. Sin él, dos peticiones idénticas simultáneas
# escriben el mismo fichero a la vez: una muere con "Unable to rename file
# ... .part" y la otra puede llegar a servir una respuesta truncada.
# El diccionario se limpia por conteo de usuarios para que no crezca sin fin.
_CACHE_LOCKS: Dict[str, threading.Lock] = {}
_CACHE_LOCK_USERS: Dict[str, int] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


@contextmanager
def cache_key_lock(key: str):
    """Serializa la generación de una misma entrada de caché."""
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.setdefault(key, threading.Lock())
        _CACHE_LOCK_USERS[key] = _CACHE_LOCK_USERS.get(key, 0) + 1
    try:
        with lock:
            yield
    finally:
        with _CACHE_LOCKS_GUARD:
            remaining = _CACHE_LOCK_USERS.get(key, 1) - 1
            if remaining > 0:
                _CACHE_LOCK_USERS[key] = remaining
            else:
                _CACHE_LOCK_USERS.pop(key, None)
                _CACHE_LOCKS.pop(key, None)


def write_cache_file_atomic(path: Path, content: str) -> None:
    """Publica el contenido de golpe con os.replace().

    Escribir en el sitio definitivo deja al fichero a medias mientras dura la
    escritura, y una petición que lo esté sirviendo devuelve una respuesta
    truncada. Con el temporal + replace, o se ve la versión vieja o la nueva.
    """
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def fetch_cached_file(key: str) -> Tuple[Optional[Path], Optional[Dict]]:
    metadata = load_meta(key)
    if not metadata:
        return None, None
    if is_expired(metadata):
        delete_cache_entry(key, metadata)
        return None, None

    filename = metadata.get("filename")
    if not filename:
        delete_cache_entry(key, metadata)
        return None, None

    file_path = CACHE_DIR / filename
    if not file_path.exists():
        delete_cache_entry(key, metadata)
        return None, None

    cached_meta = {**metadata, "_cache_hit": True}
    return file_path, cached_meta


def purge_expired_entries() -> None:
    for meta_file in META_DIR.glob("*.json"):
        with meta_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if is_expired(data):
            delete_cache_entry(meta_file.stem, data)


def save_meta(key: str, metadata: Dict) -> None:
    sanitized = {k: v for k, v in metadata.items() if not k.startswith("_")}
    sanitized["cache_key"] = key
    # Atómico también aquí: purge_expired_entries() lee estos ficheros sin
    # protección y un JSON a medias abortaría la petición.
    write_cache_file_atomic(
        meta_path(key), json.dumps(sanitized, ensure_ascii=False, indent=2)
    )


def build_ydl_options(
    media_format: str, *, cache_key_value: str, force_no_proxy: bool = False
) -> Dict:
    # yt-dlp necesita un runtime de JavaScript (EJS) para resolver los desafíos
    # de YouTube. Deno es el único habilitado por defecto, así que se busca
    # primero; si se fijara solo "node" se estaría desactivando ese defecto.
    js_runtimes: Dict[str, Dict[str, str]] = {}
    for candidate in ("deno", "node", "nodejs"):
        path = shutil.which(candidate)
        if path:
            js_runtimes[candidate] = {"executable": path}
            break

    normalized_format = normalize_media_format(media_format)
    base_opts: Dict = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        # Force yt-dlp to rely on the bundled CA certificates instead of the
        # (possibly missing) system store. This avoids SSL failures when the
        # container lacks CA data or a proxy injects a custom CA path.
        "nocheckcertificate": False,
        "ca_certs": CERT_BUNDLE,
        "outtmpl": str(CACHE_DIR / f"{cache_key_value}.%(ext)s"),
        "overwrites": True,
        "retries": 3,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
        "js_runtimes": js_runtimes or None,
        "remote_components": ["ejs:github"],
        "cachedir": str(YTDLP_CACHE_DIR),
    }

    if YTDLP_EXTRACTOR_ARGS:
        base_opts["extractor_args"] = YTDLP_EXTRACTOR_ARGS

    if not force_no_proxy and YTDLP_PROXY:
        base_opts["proxy"] = YTDLP_PROXY
    if YTDLP_COOKIES_FILE:
        base_opts["cookiefile"] = YTDLP_COOKIES_FILE

    if normalized_format in AUDIO_FORMAT_PROFILES:
        profile = AUDIO_FORMAT_PROFILES[normalized_format]
        if profile.get("passthrough"):
            return {**base_opts, "format": profile.get("format", "bestaudio/best")}

        return {
            **base_opts,
            "format": profile.get("format", "bestaudio/best"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": profile.get("codec", "mp3"),
                    "preferredquality": str(profile.get("preferred_quality", "192")),
                }
            ],
        }

    profile_key = (
        normalized_format
        if normalized_format in VIDEO_FORMAT_PROFILES
        else DEFAULT_VIDEO_FORMAT
    )
    profile = VIDEO_FORMAT_PROFILES[profile_key]
    return {
        **base_opts,
        "format": profile.get("format", "bv*+ba/b"),
        "merge_output_format": profile.get("merge_output_format", "mp4"),
    }


def should_retry_without_proxy(error: Exception) -> bool:
    message = str(error).lower()
    return "proxy" in message or "403" in message or "forbidden" in message


def _should_retry_with_new_user_agent(error: Exception) -> bool:
    message = str(error).lower()
    if "sign in" in message and "not a bot" in message:
        return True
    if "bot" in message and "confirm" in message:
        return True
    return False


def _generate_user_agent() -> str:
    major = random.randint(121, 126)
    build = random.randint(0, 5999)
    patch = random.randint(0, 199)
    mac_minor = random.randint(0, 7)
    return (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_{mac_minor}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.{build}.{patch} Safari/537.36"
    )


def extract_info_with_user_agent_retries(
    url: str, *, ydl_opts: Dict, download: bool
) -> Dict:
    attempts = max(1, YTDLP_BOT_PROTECTION_RETRIES)
    delay = max(0.0, YTDLP_BOT_PROTECTION_DELAY)
    current_agent = ydl_opts.get("http_headers", {}).get("User-Agent", YTDLP_USER_AGENT)
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        opts = {**ydl_opts}
        headers = {**opts.get("http_headers", {})}
        headers["User-Agent"] = current_agent
        opts["http_headers"] = headers
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as exc:  # pragma: no cover - passthrough errors
            last_error = exc
            if attempt >= attempts - 1 or not _should_retry_with_new_user_agent(exc):
                raise
            current_agent = _generate_user_agent()
            time.sleep(delay)

    if last_error:
        raise last_error
    raise DownloadError("Fallo inesperado al extraer información")


def _extract_media_stats(info: Dict[str, Any]) -> Dict[str, Any]:
    def _as_int(value: Any) -> Optional[int]:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    candidate: Dict[str, Any] = {}
    requested = info.get("requested_downloads") or []
    if isinstance(requested, list) and requested:
        maybe = requested[0]
        if isinstance(maybe, dict):
            candidate = maybe
    if not candidate:
        candidate = info

    width = _as_int(candidate.get("width") or info.get("width"))
    height = _as_int(candidate.get("height") or info.get("height"))
    abr = _as_int(candidate.get("abr") or info.get("abr"))
    vbr = _as_int(candidate.get("vbr") or candidate.get("tbr") or info.get("tbr"))
    fps = _as_int(candidate.get("fps") or info.get("fps"))
    filesize = _as_int(
        candidate.get("filesize")
        or candidate.get("filesize_approx")
        or info.get("filesize")
        or info.get("filesize_approx")
    )

    metadata: Dict[str, Any] = {}
    if width:
        metadata["width"] = width
    if height:
        metadata["height"] = height
    if abr:
        metadata["audio_bitrate_kbps"] = abr
    if vbr:
        metadata["video_bitrate_kbps"] = vbr
    if fps:
        metadata["fps"] = fps
    if filesize:
        metadata["filesize_bytes"] = filesize
    format_id = candidate.get("format_id") or info.get("format_id")
    if isinstance(format_id, str):
        metadata["format_id"] = format_id
    return metadata


def download_media(url: str, media_format: str) -> Tuple[Path, Dict]:
    normalized_format = normalize_media_format(media_format)
    key = cache_key(url, normalized_format)
    purge_expired_entries()
    # La comprobación de caché va dentro del lock: si otra petición está
    # generando esta misma entrada, aquí se espera y se reutiliza su resultado.
    with cache_key_lock(key):
        cached_path, cached_meta = fetch_cached_file(key)
        if cached_path:
            return cached_path, cached_meta or {}
        return _download_media_uncached(url, normalized_format, key)


def _download_media_uncached(
    url: str, normalized_format: str, key: str
) -> Tuple[Path, Dict]:
    def extract(force_no_proxy: bool = False) -> Dict:
        ydl_opts = build_ydl_options(
            normalized_format, cache_key_value=key, force_no_proxy=force_no_proxy
        )
        try:
            return extract_info_with_user_agent_retries(
                url, ydl_opts=ydl_opts, download=True
            )
        except Exception as exc:  # pragma: no cover - yt-dlp errors are direct
            if not force_no_proxy and should_retry_without_proxy(exc):
                return extract(force_no_proxy=True)
            raise DownloadError(str(exc)) from exc

    info = extract()

    requested = info.get("requested_downloads") or []
    if requested:
        filepath = Path(requested[0]["filepath"])  # type: ignore[index]
    elif info.get("_filename"):
        filepath = Path(info["_filename"])  # type: ignore[index]
    else:
        raise DownloadError("No se pudo localizar el archivo descargado")

    if not filepath.exists():
        raise DownloadError("No se pudo localizar el archivo descargado")

    # Para audio_max, hacer remux de WebM a OGG (sin recodificar)
    if normalized_format == "audio_max":
        filepath = remux_to_ogg(filepath)

    title = info.get("title") or "video"
    metadata = {
        "title": title,
        "filename": filepath.name,
        "source_url": url,
        "media_format": normalized_format,
        "downloaded_at": time.time(),
        "cache_key": key,
        **_extract_media_stats(info),
    }
    try:
        metadata["filesize_bytes"] = filepath.stat().st_size
    except OSError:
        pass
    metadata["_cache_hit"] = False
    save_meta(key, metadata)
    return filepath, metadata


def download_media_no_cache(url: str, media_format: str) -> Tuple[Path, Dict]:
    """Descarga sin usar la caché global ni almacenar metadatos persistentes."""
    normalized_format = normalize_media_format(media_format)
    temp_dir = Path(tempfile.mkdtemp(prefix="vhs_incognito_"))

    def _run() -> Tuple[Path, Dict]:
        ydl_opts = build_ydl_options(
            normalized_format,
            cache_key_value=cache_key(url, f"{normalized_format}::{random.random()}"),
            force_no_proxy=False,
        )
        # Forzar salida y caché de yt-dlp en el dir temporal para no tocar /.cache ni data/cache
        ydl_opts["outtmpl"] = str(temp_dir / "%(id)s.%(ext)s")
        ydl_opts["cachedir"] = str(temp_dir)
        try:
            info = extract_info_with_user_agent_retries(
                url, ydl_opts=ydl_opts, download=True
            )
        except Exception as exc:  # pragma: no cover - passthrough
            cleanup_dir(temp_dir)
            raise DownloadError(str(exc)) from exc

        requested = info.get("requested_downloads") or []
        if requested:
            filepath = Path(requested[0]["filepath"])  # type: ignore[index]
        elif info.get("_filename"):
            filepath = Path(info["_filename"])  # type: ignore[index]
        else:
            cleanup_dir(temp_dir)
            raise DownloadError("No se pudo localizar el archivo descargado")

        if not filepath.exists():
            cleanup_dir(temp_dir)
            raise DownloadError("No se pudo localizar el archivo descargado")

        # Para audio_max, hacer remux de WebM a OGG (sin recodificar)
        if normalized_format == "audio_max":
            filepath = remux_to_ogg(filepath)

        meta: Dict[str, Any] = {
            "title": "no-cache",
            "filename": filepath.name,
            "source_url": None,
            "media_format": normalized_format,
            "downloaded_at": time.time(),
            "_no_cache": True,
        }
        try:
            meta["filesize_bytes"] = filepath.stat().st_size
        except OSError:
            pass
        meta.update(_extract_media_stats(info))
        return filepath, meta

    return _run()


def run_ffmpeg(
    source: Path,
    destination: Path,
    args: List[str],
    input_args: Optional[List[str]] = None,
) -> None:
    # Los flags de aceleración por hardware (-hwaccel...) son opciones de entrada
    # y ffmpeg las rechaza si aparecen después de -i.
    command = [
        FFMPEG_BINARY,
        "-y",
        *(input_args or []),
        "-i",
        str(source),
        *args,
        str(destination),
    ]
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
    except FileNotFoundError as exc:
        raise DownloadError(
            "ffmpeg no está instalado o no es accesible en el sistema"
        ) from exc

    if process.returncode != 0:
        message = (process.stderr or process.stdout or "").strip()
        tail = message.splitlines()[-1] if message else "error desconocido de ffmpeg"
        raise DownloadError(f"ffmpeg no pudo procesar el archivo: {tail}")


def remux_to_ogg(source_path: Path) -> Path:
    """
    Remux un archivo de audio (típicamente WebM/Opus) a contenedor OGG sin recodificar.
    Retorna la ruta al nuevo archivo .ogg y elimina el archivo original.
    """
    ogg_path = source_path.with_suffix(".ogg")
    # -c:a copy = copiar audio sin recodificar, -vn = sin video
    run_ffmpeg(source_path, ogg_path, ["-c:a", "copy", "-vn"])
    # Eliminar el archivo WebM original
    cleanup_path(source_path)
    return ogg_path


def process_with_ffmpeg(url: str, media_format: str) -> Tuple[Path, Dict]:
    key = cache_key(url, media_format)
    purge_expired_entries()
    with cache_key_lock(key):
        cached_path, cached_meta = fetch_cached_file(key)
        if cached_path:
            return cached_path, cached_meta or {}
        return _process_with_ffmpeg_uncached(url, media_format, key)


def _process_with_ffmpeg_uncached(
    url: str, media_format: str, key: str
) -> Tuple[Path, Dict]:
    preset = FFMPEG_PRESETS[media_format]
    source_path, source_metadata = download_media(url, DEFAULT_VIDEO_FORMAT)
    output_path = CACHE_DIR / f"{key}{preset['extension']}"
    output_path.unlink(missing_ok=True)
    run_ffmpeg(source_path, output_path, preset["args"], preset.get("input_args"))

    metadata = {
        "title": source_metadata.get("title") or "video",
        "filename": output_path.name,
        "source_url": url,
        "media_format": media_format,
        "downloaded_at": time.time(),
        "cache_key": key,
        "_cache_hit": False,
        "preset": media_format,
        "source_media": {
            key: value
            for key, value in source_metadata.items()
            if key
            in {
                "width",
                "height",
                "video_bitrate_kbps",
                "audio_bitrate_kbps",
                "fps",
                "format_id",
                "filesize_bytes",
            }
        },
    }
    if preset.get("video_height"):
        metadata["target_height"] = preset["video_height"]
    if preset.get("video_bitrate_kbps"):
        metadata["target_video_bitrate_kbps"] = preset["video_bitrate_kbps"]
    if preset.get("audio_bitrate_kbps"):
        metadata["target_audio_bitrate_kbps"] = preset["audio_bitrate_kbps"]
    try:
        metadata["filesize_bytes"] = output_path.stat().st_size
    except OSError:
        pass
    save_meta(key, metadata)
    return output_path, metadata


def process_with_ffmpeg_no_cache(url: str, media_format: str) -> Tuple[Path, Dict]:
    """Procesa con ffmpeg en un directorio temporal sin persistir caché ni metadatos."""
    preset = FFMPEG_PRESETS[media_format]
    source_path, source_metadata = download_media_no_cache(url, DEFAULT_VIDEO_FORMAT)
    output_path = source_path.parent / f"output{preset['extension']}"
    output_path.unlink(missing_ok=True)
    try:
        run_ffmpeg(source_path, output_path, preset["args"], preset.get("input_args"))
    finally:
        cleanup_path(source_path)

    metadata = {
        "title": source_metadata.get("title") or "video",
        "filename": output_path.name,
        "source_url": url,
        "media_format": media_format,
        "downloaded_at": time.time(),
        "_no_cache": True,
        "preset": media_format,
        "source_media": {
            key: value
            for key, value in source_metadata.items()
            if key
            in {
                "width",
                "height",
                "video_bitrate_kbps",
                "audio_bitrate_kbps",
                "fps",
                "format_id",
                "filesize_bytes",
            }
        },
    }
    if preset.get("video_height"):
        metadata["target_height"] = preset["video_height"]
    if preset.get("video_bitrate_kbps"):
        metadata["target_video_bitrate_kbps"] = preset["video_bitrate_kbps"]
    if preset.get("audio_bitrate_kbps"):
        metadata["target_audio_bitrate_kbps"] = preset["audio_bitrate_kbps"]
    try:
        metadata["filesize_bytes"] = output_path.stat().st_size
    except OSError:
        pass
    return output_path, metadata


def probe_media(url: str) -> Dict[str, Any]:
    key = cache_key(url, "probe")
    ydl_opts = build_ydl_options(DEFAULT_VIDEO_FORMAT, cache_key_value=key)
    ydl_opts["skip_download"] = True
    try:
        info = extract_info_with_user_agent_retries(
            url, ydl_opts=ydl_opts, download=False
        )
    except Exception as exc:  # pragma: no cover - passthrough errors
        raise DownloadError(str(exc)) from exc

    thumbnails = info.get("thumbnails") or []
    if isinstance(thumbnails, list) and thumbnails:
        thumb_url = thumbnails[-1].get("url")
    else:
        thumb_url = info.get("thumbnail")

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor"),
        "extractor_key": info.get("extractor_key"),
        "categories": info.get("categories") or [],
        "tags": info.get("tags") or [],
        "thumbnail": thumb_url,
    }


def search_media(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    cleaned_query = (query or "").strip()
    if len(cleaned_query) < 3:
        raise DownloadError("La búsqueda debe tener al menos 3 caracteres")

    safe_limit = max(1, min(limit, 25))
    search_expression = f"ytsearch{safe_limit}:{cleaned_query}"
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "extract_flat": True,
        "skip_download": True,
        "default_search": "auto",
        "nocheckcertificate": False,
        "ca_certs": CERT_BUNDLE,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
    }

    if YTDLP_PROXY:
        ydl_opts["proxy"] = YTDLP_PROXY
    if YTDLP_COOKIES_FILE:
        ydl_opts["cookiefile"] = YTDLP_COOKIES_FILE
    if YTDLP_EXTRACTOR_ARGS:
        ydl_opts["extractor_args"] = YTDLP_EXTRACTOR_ARGS

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(search_expression, download=False)
    except Exception as exc:  # pragma: no cover - passthrough errors
        raise DownloadError(str(exc)) from exc

    items: List[Dict[str, Any]] = []
    for entry in results.get("entries") or []:
        resolved_url = entry.get("webpage_url") or entry.get("url")
        if not resolved_url or not isinstance(resolved_url, str):
            continue
        items.append(
            {
                "id": entry.get("id"),
                "title": entry.get("title") or resolved_url,
                "url": resolved_url,
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or entry.get("channel"),
                "extractor": entry.get("extractor") or entry.get("ie_key"),
                "thumbnail": entry.get("thumbnail"),
            }
        )

    return items


def ensure_transcription_ready() -> None:
    if TRANSCRIPTION_API_KEY and (TRANSCRIPTION_MODEL or TRANSCRIPTION_MODEL_IDS):
        return
    raise DownloadError(
        "La transcripción no está disponible. Configura TRANSCRIPTION_API_KEY y TRANSCRIPTION_MODEL/TRANSCRIPTION_MODELS."
    )


def parse_bool_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _ensure_dir_writable(path: Path, purpose: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(f"No se pudo crear el directorio de {purpose} ({path}): {exc}") from exc
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".vhs_rw_test", delete=True):
            pass
    except OSError as exc:
        raise DownloadError(
            f"No se puede escribir en el directorio de {purpose} ({path}). "
            "Revisa permisos o ajusta las variables CACHE_DIR/USAGE_LOG_PATH."
        ) from exc


def ensure_storage_ready() -> None:
    _ensure_dir_writable(CACHE_DIR, "cache")
    _ensure_dir_writable(META_DIR, "metadatos de caché")
    _ensure_dir_writable(USAGE_LOG_PATH.parent, "registros (USAGE_LOG_PATH)")
    _ensure_dir_writable(YTDLP_CACHE_DIR, "caché de yt-dlp (YTDLP_CACHE_DIR)")


def _normalize_transcription_payload(payload: Any) -> Dict[str, Any]:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = payload
    elif isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            data = parsed if isinstance(parsed, dict) else {"text": payload.strip()}
        except json.JSONDecodeError:
            data = {"text": payload.strip()}
    else:
        text_value = getattr(payload, "text", None)
        if text_value is not None:
            data = {"text": str(text_value)}
        else:
            data = {"text": str(payload)}

    text_field = data.get("text")
    if isinstance(text_field, str):
        data["text"] = text_field.strip()
        if "segments" not in data:
            try:
                parsed_text = json.loads(text_field)
                if isinstance(parsed_text, dict) and parsed_text.get("segments"):
                    data.update(parsed_text)
            except Exception:
                pass
    diarization_blob = data.get("diarization")
    if "segments" not in data:
        if isinstance(diarization_blob, dict) and diarization_blob.get("segments"):
            data["segments"] = diarization_blob["segments"]
        elif isinstance(diarization_blob, list):
            data["segments"] = diarization_blob
    return data


# Los LLM pequeños tienden a colar énfasis Markdown o una coletilla explicativa
# aunque el prompt lo prohíba. Los subtítulos son texto plano, así que se limpia
# la salida de forma determinista en vez de confiar solo en el prompt.
# Solo se limpia el énfasis con asteriscos, que es el que emiten estos modelos.
# El guion bajo se deja intacto a propósito: "config_final_v2" o "__init__" son
# texto legítimo y no merece la pena arriesgarse a mutilarlos.
_MD_EMPHASIS_RE = re.compile(
    r"(?<![\w*])(\*{1,3})(?=\S)(.+?)(?<=\S)\1(?![\w*])", re.DOTALL
)
_TRAILING_NOTE_RE = re.compile(
    r"\n\s*\n\s*\(?\s*(?:Nota|Note|N\.B\.)\s*:.*\Z",
    re.DOTALL | re.IGNORECASE,
)


def _sanitize_translation(text: str) -> str:
    cleaned = _TRAILING_NOTE_RE.sub("", text)
    cleaned = _MD_EMPHASIS_RE.sub(r"\2", cleaned)
    return cleaned.strip()


# Formato de las líneas que se piden y se esperan en una traducción por lotes.
_BATCH_LINE_RE = re.compile(r"^\s*(\d+)\s*[.)\-:]\s*(.*)$")
_WHITESPACE_RE = re.compile(r"\s+")


def _chat_translate(client: OpenAI, model: str, system: str, user: str) -> str:
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    return completion.choices[0].message.content or ""


def _translate_one(client: OpenAI, model: str, text: str) -> str:
    user_content = TRANSLATION_USER_PROMPT_TEMPLATE.format(text=str(text))
    translated = _sanitize_translation(
        _chat_translate(client, model, TRANSLATION_SYSTEM_PROMPT, user_content)
    )
    if not translated:
        raise DownloadError("La traducción devolvió un texto vacío")
    return translated


def _translate_batch(
    client: OpenAI, model: str, batch: List[str]
) -> Optional[List[str]]:
    """Traduce varios segmentos en una sola petición.

    Devuelve None si la respuesta no respeta la numeración pedida, para que el
    llamante reintente ese lote segmento a segmento. Un lote mal alineado
    desplazaría los subtítulos, así que ante la duda no se acepta.
    """
    numbered = "\n".join(
        f"{index}. {_WHITESPACE_RE.sub(' ', str(text)).strip()}"
        for index, text in enumerate(batch, 1)
    )
    system = (
        f"{TRANSLATION_SYSTEM_PROMPT}\n"
        f"Recibirás {len(batch)} líneas numeradas. Devuelve exactamente "
        f"{len(batch)} líneas, con la misma numeración y en el mismo orden, "
        "traduciendo cada una por separado. No fusiones ni dividas líneas, "
        "y no añadas ninguna línea extra."
    )
    raw = _chat_translate(client, model, system, numbered)

    parsed: Dict[int, str] = {}
    for line in raw.splitlines():
        match = _BATCH_LINE_RE.match(line)
        if match:
            parsed[int(match.group(1))] = _sanitize_translation(match.group(2))
    expected = range(1, len(batch) + 1)
    if len(parsed) != len(batch) or not all(parsed.get(i) for i in expected):
        return None
    return [parsed[i] for i in expected]


def _translate_texts_to_spanish(texts: List[str]) -> List[str]:
    if not TRANSCRIPTION_API_KEY:
        raise DownloadError("La traducción requiere configurar TRANSCRIPTION_API_KEY")
    model = TRANSLATION_MODEL or TRANSCRIPTION_MODEL
    if not model or model.startswith("whisper"):
        raise DownloadError(
            "Configura TRANSLATION_MODEL con un modelo de chat válido para traducir al español"
        )
    if not texts:
        return []

    client = OpenAI(api_key=TRANSCRIPTION_API_KEY, base_url=TRANSCRIPTION_ENDPOINT)
    size = max(1, TRANSLATION_BATCH_SIZE)
    batches = [texts[i : i + size] for i in range(0, len(texts), size)]

    def translate_group(batch: List[str]) -> List[str]:
        if len(batch) > 1:
            grouped = _translate_batch(client, model, batch)
            if grouped is not None:
                return grouped
        return [_translate_one(client, model, text) for text in batch]

    workers = min(max(1, TRANSLATION_CONCURRENCY), len(batches))
    if workers > 1:
        # Los backends vLLM del endpoint agrupan peticiones concurrentes, así
        # que varios lotes a la vez salen casi gratis. Con ollama se serializan
        # igualmente y basta con dejar TRANSLATION_CONCURRENCY=1.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            grouped_results = list(pool.map(translate_group, batches))
    else:
        grouped_results = [translate_group(batch) for batch in batches]

    results = [text for group in grouped_results for text in group]
    if len(results) != len(texts):
        raise DownloadError(
            "La traducción devolvió un número de segmentos distinto al original"
        )
    return results


def translate_transcription_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    segments = payload.get("segments")
    if isinstance(segments, dict):
        seg_list = list(segments.values())
    else:
        seg_list = segments if isinstance(segments, list) else []

    if seg_list:
        texts = []
        for segment in seg_list:
            text_value = (
                segment.get("text")
                or segment.get("transcript")
                or segment.get("caption")
                or ""
            )
            texts.append(text_value if isinstance(text_value, str) else str(text_value))
        translations = _translate_texts_to_spanish(texts)
        for segment, translated in zip(seg_list, translations):
            segment["text"] = translated
        payload["segments"] = seg_list
        payload["text"] = " ".join(translations).strip()
        return payload

    text_only = payload.get("text") or ""
    if not isinstance(text_only, str):
        text_only = str(text_only)
    translated = _translate_texts_to_spanish([text_only])[0]
    payload["text"] = translated.strip()
    return payload


def _coerce_segments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    segments = payload.get("segments") or []
    if isinstance(segments, dict):
        segments = list(segments.values())
    return segments if isinstance(segments, list) else []


def _segment_text(segment: Dict[str, Any]) -> str:
    text_value = (
        segment.get("text")
        or segment.get("transcript")
        or segment.get("caption")
        or ""
    )
    return text_value if isinstance(text_value, str) else str(text_value)


def _segment_speaker(segment: Dict[str, Any]) -> str:
    raw_speaker = segment.get("speaker")
    if raw_speaker is None:
        return ""
    label = str(raw_speaker).strip()
    return f"{label}: " if label else ""


def _segments_have_speakers(segments: List[Dict[str, Any]]) -> bool:
    return any(bool(_segment_speaker(segment)) for segment in segments)


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(float(seconds) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def transcription_payload_to_srt(payload: Dict[str, Any]) -> str:
    segments = _coerce_segments(payload)
    if not isinstance(segments, list) or not segments:
        text_value = payload.get("text") or ""
        text_str = text_value.strip() if isinstance(text_value, str) else str(text_value)
        return "1\n00:00:00,000 --> 00:00:00,000\n" + text_str + "\n"

    entries: List[str] = []
    for index, segment in enumerate(segments, start=1):
        start = segment.get("start")
        end = segment.get("end")
        text_value = _segment_text(segment)
        start_ts = _format_srt_timestamp(float(start or 0))
        end_ts = _format_srt_timestamp(float(end or start or 0))
        speaker_prefix = _segment_speaker(segment)
        cleaned = f"{speaker_prefix}{text_value.strip()}"
        entries.append(f"{index}\n{start_ts} --> {end_ts}\n{cleaned}\n")
    return "\n".join(entries).strip() + "\n"


def _transcription_text_only(payload: Dict[str, Any]) -> str:
    segments = _coerce_segments(payload)
    if segments and _segments_have_speakers(segments):
        lines: List[str] = []
        for segment in segments:
            prefix = _segment_speaker(segment) or "Locutor: "
            text_value = _segment_text(segment).strip()
            lines.append(f"{prefix}{text_value}".strip())
        return "\n".join(lines).strip()

    text_only = payload.get("text") or ""
    if not isinstance(text_only, str):
        text_only = str(text_only)
    return text_only.strip()


WORD_TOKEN_PATTERN = re.compile(r"[\wÀ-ÿ]+(?:'[\wÀ-ÿ]+)?", flags=re.UNICODE)


def estimate_transcription_stats(payload: Dict[str, Any]) -> Dict[str, int]:
    text = _transcription_text_only(payload)
    if not text:
        return {"word_count": 0, "token_count": 0}
    normalized = text.strip()
    words = WORD_TOKEN_PATTERN.findall(normalized)
    word_count = len(words)
    token_count = len(normalized.split())
    return {
        "word_count": word_count,
        "token_count": token_count or word_count,
    }


def render_transcription_payload(payload: Dict[str, Any], media_format: str) -> bytes:
    normalized = normalize_media_format(media_format)
    if normalized in {
        "transcript_json",
        "transcript_diarized_json",
        "transcript_translate_json",
        "transcript_translate_diarized_json",
    }:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    elif normalized in {"transcript_srt", "transcript_diarized_srt", "transcript_translate_srt"}:
        text = transcription_payload_to_srt(payload)
    else:
        text = _transcription_text_only(payload)
    return text.encode("utf-8")


def build_transcription_download_name(source_name: str, media_format: str) -> str:
    extension = FORMAT_EXTENSIONS.get(media_format, ".txt")
    dummy_path = Path(f"transcript{extension}")
    return build_download_name(source_name or "transcript", dummy_path, media_format)


async def save_upload_file(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "upload.bin").suffix or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            tmp.write(chunk)
    await upload.close()
    return Path(tmp.name)


def cleanup_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def convert_uploaded_file_with_ffmpeg(source_path: Path, media_format: str) -> Path:
    preset = FFMPEG_PRESETS.get(media_format)
    if not preset:
        raise DownloadError("Perfil ffmpeg no soportado")
    if not source_path.exists():
        raise DownloadError("El archivo subido no está disponible para su procesamiento")
    try:
        if source_path.stat().st_size == 0:
            raise DownloadError("El archivo subido está vacío o corrupto")
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(delete=False, suffix=preset["extension"]) as tmp:
        output_path = Path(tmp.name)
    try:
        run_ffmpeg(source_path, output_path, preset["args"], preset.get("input_args"))
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    try:
        if output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise DownloadError("ffmpeg no generó salida. Revisa el archivo de entrada.")
    except OSError:
        output_path.unlink(missing_ok=True)
        raise DownloadError("ffmpeg no pudo preparar el archivo de salida")
    return output_path


def extract_audio_profile_from_file(source_path: Path, profile_key: str = "audio_med") -> Path:
    if not source_path.exists():
        raise DownloadError("El archivo subido no está disponible para su procesamiento")
    try:
        if source_path.stat().st_size == 0:
            raise DownloadError("El archivo subido está vacío o corrupto")
    except OSError:
        pass

    profile = AUDIO_FORMAT_PROFILES.get(profile_key) or AUDIO_FORMAT_PROFILES["audio_med"]
    suffix = f".{profile.get('codec', 'mp3')}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        output_path = Path(tmp.name)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        profile.get("codec", "mp3"),
        "-b:a",
        f"{profile.get('preferred_quality', '96')}k",
        str(output_path),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        error_message = result.stderr.decode("utf-8", errors="ignore").strip()
        raise DownloadError(
            "No se pudo extraer el audio del archivo subido para su transcripción"
            + (f": {error_message.splitlines()[-1]}" if error_message else "")
        )
    return output_path


def _call_openai_transcription(
    file_path: Path, transcription_model: Optional[str] = None
) -> Dict[str, Any]:
    model = (transcription_model or "").strip()
    if not model:
        model = resolve_transcription_model(None)
    client = OpenAI(api_key=TRANSCRIPTION_API_KEY, base_url=TRANSCRIPTION_ENDPOINT)
    with file_path.open("rb") as audio_stream:
        response = client.audio.transcriptions.create(
            model=model,
            file=audio_stream,
            response_format="verbose_json",
        )
    return _normalize_transcription_payload(response)


def transcribe_audio_file(
    file_path: Path,
    media_format: str,
    transcription_model: Optional[str] = None,
    diarize: bool = False,
) -> Dict[str, Any]:
    ensure_transcription_ready()
    normalized_format = normalize_media_format(media_format)
    if normalized_format not in TRANSCRIPTION_FORMATS:
        raise DownloadError("Formato de transcripción no soportado")
    effective_diarize = diarize or is_diarization_format(normalized_format)
    selected_model = (
        resolve_diarization_model(transcription_model)
        if effective_diarize
        else resolve_transcription_model(transcription_model)
    )
    try:
        return _call_openai_transcription(file_path, selected_model)
    except Exception as exc:  # pragma: no cover - servicios externos
        raise DownloadError(
            f"No se pudo transcribir el audio con el modelo '{selected_model}': {exc}"
        ) from exc


def generate_transcription_file(
    url: str,
    media_format: str,
    transcription_model: Optional[str] = None,
    diarize: bool = False,
) -> Tuple[Path, Dict]:
    if media_format not in TRANSCRIPTION_FORMATS:
        raise DownloadError("Formato de transcripción no soportado")
    translation = is_translation_format(media_format)
    effective_diarize = diarize or is_diarization_format(media_format)
    selected_model = (
        resolve_diarization_model(transcription_model)
        if effective_diarize
        else resolve_transcription_model(transcription_model)
    )
    model_suffix = f"model={selected_model}"
    diarize_suffix = f"diarize={int(effective_diarize)}"
    translation_suffix = f"translation={int(translation)}"
    key = cache_key(
        f"{url}::{model_suffix}::{diarize_suffix}::{translation_suffix}",
        media_format,
    )
    purge_expired_entries()
    with cache_key_lock(key):
        cached_path, cached_meta = fetch_cached_file(key)
        if cached_path:
            return cached_path, cached_meta or {}
        return _generate_transcription_file_uncached(
            url, media_format, key, selected_model, effective_diarize, translation
        )


def _generate_transcription_file_uncached(
    url: str,
    media_format: str,
    key: str,
    selected_model: str,
    effective_diarize: bool,
    translation: bool,
) -> Tuple[Path, Dict]:
    audio_path, audio_meta = download_media(url, "audio_med")
    transcript_payload = transcribe_audio_file(
        audio_path,
        media_format,
        selected_model,
        diarize=effective_diarize,
    )
    if translation:
        transcript_payload = translate_transcription_payload(transcript_payload)
    transcription_stats = estimate_transcription_stats(transcript_payload)

    if media_format.endswith("_json") or media_format == "transcript_json":
        if media_format == "transcript_json":
            transcript_path = CACHE_DIR / f"{key}{TRANSCRIPTION_FILE_SUFFIX}"
        else:
            transcript_path = CACHE_DIR / f"{key}.json"
        write_cache_file_atomic(
            transcript_path,
            json.dumps(transcript_payload, ensure_ascii=False, indent=2),
        )
    elif media_format.endswith("_srt") or media_format == "transcript_srt":
        transcript_path = CACHE_DIR / f"{key}.srt"
        srt_content = transcription_payload_to_srt(transcript_payload)
        write_cache_file_atomic(transcript_path, srt_content)
    else:
        text_only = transcript_payload.get("text") or ""
        if not isinstance(text_only, str):
            text_only = str(text_only)
        transcript_path = CACHE_DIR / f"{key}.txt"
        write_cache_file_atomic(transcript_path, text_only.strip())

    metadata = {
        "title": audio_meta.get("title") or "transcript",
        "filename": transcript_path.name,
        "source_url": url,
        "media_format": media_format,
        "downloaded_at": time.time(),
        "cache_key": key,
        "transcription_stats": transcription_stats,
        "transcription_model": selected_model,
        "diarization": bool(effective_diarize),
        "translation": bool(translation),
    }
    metadata.update(
        {
            key: value
            for key, value in audio_meta.items()
            if key
            in {
                "audio_bitrate_kbps",
                "filesize_bytes",
                "format_id",
            }
        }
    )
    metadata["_cache_hit"] = False
    save_meta(key, metadata)
    return transcript_path, metadata


def generate_transcription_file_no_cache(
    url: str,
    media_format: str,
    transcription_model: Optional[str] = None,
    diarize: bool = False,
) -> Tuple[Path, Dict]:
    """Genera transcripciones en un directorio temporal sin persistir caché."""
    if media_format not in TRANSCRIPTION_FORMATS:
        raise DownloadError("Formato de transcripción no soportado")
    translation = is_translation_format(media_format)
    effective_diarize = diarize or is_diarization_format(media_format)
    selected_model = (
        resolve_diarization_model(transcription_model)
        if effective_diarize
        else resolve_transcription_model(transcription_model)
    )

    audio_path, audio_meta = download_media_no_cache(url, "audio_med")
    try:
        transcript_payload = transcribe_audio_file(
            audio_path,
            media_format,
            selected_model,
            diarize=effective_diarize,
        )
        if translation:
            transcript_payload = translate_transcription_payload(transcript_payload)
    finally:
        cleanup_path(audio_path)

    transcription_stats = estimate_transcription_stats(transcript_payload)
    output_extension = FORMAT_EXTENSIONS.get(media_format, ".txt")
    transcript_path = audio_path.parent / f"transcript{output_extension}"
    # render_transcription_payload() ya devuelve UTF-8 codificado.
    transcript_path.write_bytes(
        render_transcription_payload(transcript_payload, media_format)
    )

    metadata = {
        "title": audio_meta.get("title") or "transcript",
        "filename": transcript_path.name,
        "source_url": url,
        "media_format": media_format,
        "downloaded_at": time.time(),
        "_no_cache": True,
        "transcription_stats": transcription_stats,
        "transcription_model": selected_model,
        "diarization": bool(effective_diarize),
        "translation": bool(translation),
    }
    metadata.update(
        {
            key: value
            for key, value in audio_meta.items()
            if key
            in {
                "audio_bitrate_kbps",
                "filesize_bytes",
                "format_id",
            }
        }
    )
    try:
        metadata["filesize_bytes"] = transcript_path.stat().st_size
    except OSError:
        pass
    return transcript_path, metadata


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=template_context(
            request,
            supported_services=SUPPORTED_SERVICES,
            transcription_models=TRANSCRIPTION_MODEL_OPTIONS,
            default_transcription_model=TRANSCRIPTION_MODEL,
            diarization_models=DIARIZATION_MODEL_OPTIONS,
            default_diarization_model=DIARIZATION_MODEL,
        ),
    )


@app.get("/docs/api", response_class=HTMLResponse)
async def api_docs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="api_docs.html",
        context=template_context(
            request,
            formats=FORMAT_DESCRIPTIONS,
        ),
    )


@app.get("/api/health")
async def health() -> Dict[str, str]:
    payload: Dict[str, str] = {"status": "ok"}
    if VHS_VERSION:
        payload["version"] = VHS_VERSION
    return payload


@app.get("/api/transcription/models", response_class=JSONResponse)
async def transcription_models_endpoint() -> Dict[str, Any]:
    return {
        "default_model": TRANSCRIPTION_MODEL,
        "models": TRANSCRIPTION_MODEL_OPTIONS,
        "default_diarization_model": DIARIZATION_MODEL,
        "diarization_models": DIARIZATION_MODEL_OPTIONS,
    }


@app.post("/api/probe", response_class=JSONResponse)
async def probe_endpoint(
    request: Request,
    payload: Dict[str, Any] = Body(..., description="JSON con url"),
):
    request.state.source = payload.get("source")
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Incluye una URL válida en el cuerpo")
    try:
        info = await run_in_threadpool(probe_media, url)
    except DownloadError as exc:
        await run_in_threadpool(
            record_error_event, "probe", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return info


@app.post("/api/search", response_class=JSONResponse)
async def search_endpoint(
    request: Request,
    payload: Dict[str, Any] = Body(..., description="JSON con query y limit"),
):
    request.state.source = payload.get("source")
    query = (payload.get("query") or "").strip()
    limit_raw = payload.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else 8
    except (TypeError, ValueError):
        limit = 8
    if limit < 1:
        limit = 1
    if limit > 25:
        limit = 25
    if len(query) < 3:
        raise HTTPException(
            status_code=400, detail="La búsqueda debe tener al menos 3 caracteres"
        )
    try:
        items = await run_in_threadpool(search_media, query, limit)
    except DownloadError as exc:
        await run_in_threadpool(
            record_error_event, "search", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"query": query.strip(), "items": items, "services": SUPPORTED_SERVICES}


@app.post("/api/download")
async def download_endpoint(
    request: Request,
    payload: Dict[str, Any] = Body(..., description="JSON con url y format"),
):
    request.state.source = payload.get("source")
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Incluye una URL válida en el cuerpo")
    media_format_raw = payload.get("format") or payload.get("media_format") or DEFAULT_VIDEO_FORMAT
    format_value = str(media_format_raw).lower()
    if format_value not in SUPPORTED_MEDIA_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Formato inválido. Usa uno de: "
                + ", ".join(sorted(SUPPORTED_MEDIA_FORMATS))
                + "."
            ),
        )
    normalized_format = normalize_media_format(format_value)
    requested_diarize = parse_bool_flag(payload.get("diarize"))
    effective_diarize = requested_diarize or is_diarization_format(normalized_format)
    transcription_model_raw = payload.get("transcription_model")
    transcription_model = (
        str(transcription_model_raw).strip() if transcription_model_raw else None
    )
    if normalized_format in TRANSCRIPTION_FORMATS and transcription_model:
        try:
            if effective_diarize:
                resolve_diarization_model(transcription_model)
            else:
                resolve_transcription_model(transcription_model)
        except DownloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        ensure_storage_ready()
        if normalized_format in TRANSCRIPTION_FORMATS:
            file_path, metadata = await run_in_threadpool(
                generate_transcription_file,
                url,
                normalized_format,
                transcription_model,
                effective_diarize,
            )
        elif normalized_format in FFMPEG_PRESETS:
            file_path, metadata = await run_in_threadpool(
                process_with_ffmpeg, url, normalized_format
            )
        else:
            file_path, metadata = await run_in_threadpool(
                download_media, url, normalized_format
            )
    except DownloadError as exc:
        await run_in_threadpool(
            record_error_event, "download", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    download_name = build_download_name(
        metadata.get("title", "vhs"), file_path, normalized_format
    )
    media_type = media_type_for_format(normalized_format)
    response = FileResponse(
        path=file_path,
        filename=download_name,
        media_type=media_type,
    )
    await run_in_threadpool(
        record_download_event,
        normalized_format,
        bool(metadata.get("_cache_hit")),
        metadata.get("transcription_stats"),
        detect_request_source(request),
        provider=metadata.get("extractor_key") or metadata.get("extractor"),
    )
    return response


@app.post("/api/no-cache")
async def no_cache_download_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any] = Body(..., description="JSON con url y format"),
):
    request.state.source = payload.get("source")
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Incluye una URL válida en el cuerpo")
    media_format_raw = payload.get("format") or payload.get("media_format") or DEFAULT_VIDEO_FORMAT
    format_value = str(media_format_raw).lower()
    if format_value not in SUPPORTED_MEDIA_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Formato inválido. Usa uno de: "
                + ", ".join(sorted(SUPPORTED_MEDIA_FORMATS))
                + "."
            ),
        )
    normalized_format = normalize_media_format(format_value)
    requested_diarize = parse_bool_flag(payload.get("diarize"))
    effective_diarize = requested_diarize or is_diarization_format(normalized_format)
    transcription_model_raw = payload.get("transcription_model")
    transcription_model = (
        str(transcription_model_raw).strip() if transcription_model_raw else None
    )
    if normalized_format in TRANSCRIPTION_FORMATS and transcription_model:
        try:
            if effective_diarize:
                resolve_diarization_model(transcription_model)
            else:
                resolve_transcription_model(transcription_model)
        except DownloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if normalized_format in TRANSCRIPTION_FORMATS:
            file_path, metadata = await run_in_threadpool(
                generate_transcription_file_no_cache,
                url,
                normalized_format,
                transcription_model,
                effective_diarize,
            )
        elif normalized_format in FFMPEG_PRESETS:
            file_path, metadata = await run_in_threadpool(
                process_with_ffmpeg_no_cache, url, normalized_format
            )
        else:
            file_path, metadata = await run_in_threadpool(
                download_media_no_cache, url, normalized_format
            )
    except DownloadError as exc:
        await run_in_threadpool(
            record_error_event, "download_no_cache", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    download_name = build_download_name(
        metadata.get("title", "vhs"), file_path, normalized_format
    )
    media_type = media_type_for_format(normalized_format)
    background_tasks.add_task(cleanup_path, file_path)
    background_tasks.add_task(cleanup_dir, file_path.parent)
    response = FileResponse(
        path=file_path,
        filename=download_name,
        media_type=media_type,
        background=background_tasks,
    )
    await run_in_threadpool(
        record_download_event,
        normalized_format,
        False,
        metadata.get("transcription_stats"),
        detect_request_source(request),
        provider=metadata.get("extractor_key") or metadata.get("extractor"),
    )
    return response


@app.get("/api/cache", response_class=JSONResponse)
async def cache_status() -> Dict:
    purge_expired_entries()
    entries: List[Dict[str, Any]] = []
    total_bytes = 0
    for meta_file in META_DIR.glob("*.json"):
        with meta_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if is_expired(data):
            delete_cache_entry(meta_file.stem, data)
            continue
        key = data.get("cache_key") or meta_file.stem
        filename = data.get("filename")
        if not filename:
            delete_cache_entry(key, data)
            continue
        file_path = CACHE_DIR / filename
        if not file_path.exists():
            delete_cache_entry(key, data)
            continue
        downloaded_at = float(data.get("downloaded_at") or 0)
        age_seconds = max(0, int(time.time() - downloaded_at))
        size = file_path.stat().st_size
        total_bytes += size
        iso_timestamp = (
            datetime.fromtimestamp(downloaded_at, tz=timezone.utc).isoformat()
            if downloaded_at
            else None
        )
        entries.append(
            {
                "cache_key": key,
                "title": data.get("title") or "descarga",
                "media_format": data.get("media_format"),
                "source_url": data.get("source_url"),
                "filename": filename,
                "filesize_bytes": size,
                "width": data.get("width"),
                "height": data.get("height") or data.get("target_height"),
                "video_bitrate_kbps": data.get("video_bitrate_kbps")
                or data.get("target_video_bitrate_kbps"),
                "audio_bitrate_kbps": data.get("audio_bitrate_kbps")
                or data.get("target_audio_bitrate_kbps"),
                "format_id": data.get("format_id"),
                "age_seconds": age_seconds,
                "downloaded_at": downloaded_at,
                "downloaded_at_iso": iso_timestamp,
                "download_url": f"/api/cache/{key}/download",
                "delete_url": f"/api/cache/{key}",
            }
        )

    entries.sort(key=lambda item: item.get("downloaded_at", 0), reverse=True)
    return {
        "items": entries,
        "ttl_seconds": CACHE_TTL_SECONDS,
        "total_bytes": total_bytes,
    }


@app.get("/api/cache/{cache_key}/download")
async def download_cached_entry(request: Request, cache_key: str):
    purge_expired_entries()
    file_path, metadata = fetch_cached_file(cache_key)
    if not file_path or not metadata:
        raise HTTPException(status_code=404, detail="Entrada de caché no disponible")

    title = metadata.get("title", "vhs")
    media_format = metadata.get("media_format", "video")
    download_name = build_download_name(title, file_path, media_format)
    media_type = media_type_for_format(media_format)
    response = FileResponse(
        path=file_path,
        filename=download_name,
        media_type=media_type,
    )
    await run_in_threadpool(
        record_download_event,
        media_format,
        True,
        metadata.get("transcription_stats") if metadata else None,
        detect_request_source(request),
        provider=metadata.get("extractor_key") or metadata.get("extractor"),
    )
    return response


@app.delete("/api/cache/{cache_key}", response_class=JSONResponse)
async def remove_cached_entry(cache_key: str) -> Dict[str, Any]:
    purge_expired_entries()
    metadata = load_meta(cache_key)
    if not metadata:
        raise HTTPException(status_code=404, detail="Entrada de caché no disponible")
    await run_in_threadpool(delete_cache_entry, cache_key, metadata)
    return {"status": "deleted", "cache_key": cache_key}


@app.get("/api/stats/usage", response_class=JSONResponse)
async def usage_stats() -> Dict[str, Any]:
    return summarize_usage()


@app.post("/api/ffmpeg/upload")
async def ffmpeg_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    media_format: str = Form("ffmpeg_mp3-192"),
    file: UploadFile = File(...),
):
    format_value = (media_format or "").strip().lower()
    if format_value not in FFMPEG_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Perfil inválido. Usa uno de: "
                + ", ".join(sorted(FFMPEG_PRESETS))
                + "."
            ),
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="Incluye un archivo de audio o video")

    temp_path: Optional[Path] = None
    try:
        ensure_storage_ready()
        temp_path = await save_upload_file(file)
        output_path = await run_in_threadpool(
            convert_uploaded_file_with_ffmpeg, temp_path, format_value
        )
    except DownloadError as exc:
        await run_in_threadpool(
            record_error_event, "ffmpeg_upload", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if temp_path:
            cleanup_path(temp_path)

    download_name = build_download_name(file.filename or "ffmpeg", output_path, format_value)
    background_tasks.add_task(cleanup_path, output_path)
    response = FileResponse(
        path=output_path,
        media_type=media_type_for_format(format_value),
        filename=download_name,
        background=background_tasks,
    )
    await run_in_threadpool(
        record_download_event,
        format_value,
        False,
        None,
        detect_request_source(request),
    )
    return response


@app.get("/api/upscale/models", response_class=JSONResponse)
async def upscale_models():
    """Modelos de escalado disponibles, con el aviso de tiempo para la UI.

    El aviso se calcula a partir del rendimiento medido que se declara en
    UPSCALE_MODELS, no está escrito a mano: así no miente si cambia el
    hardware o el modelo.
    """
    models = upscale_mod.models_for_ui()
    return {
        "models": models,
        "default_model": models[0]["id"] if models else "",
        "targets": sorted(UPSCALE_FORMATS.keys()),
        "segment_seconds": upscale_mod.segment_seconds(),
    }


def _validate_upscale_request(
    media_format: str, upscale_model: str, file: UploadFile
) -> Tuple[str, str]:
    """Valida formato y modelo. Devuelve (formato normalizado, modelo elegido)."""
    format_value = normalize_media_format(media_format)
    if format_value not in UPSCALE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="Formato inválido. Usa uno de: " + ", ".join(sorted(UPSCALE_FORMATS)) + ".",
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="Incluye un archivo de vídeo")

    available = upscale_mod.parse_models()
    if not available:
        raise HTTPException(
            status_code=503,
            detail="El escalado no está configurado. Define UPSCALE_MODELS.",
        )
    allowed = {m["id"] for m in available}
    selected = (upscale_model or "").strip() or available[0]["id"]
    if selected not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Modelo de escalado no permitido. Revisa UPSCALE_MODELS.",
        )
    # Techo de resolución por modelo. Se comprueba aquí y no cuando falla la
    # GPU: enterarse de un límite de VRAM después de minutos de cómputo es
    # inaceptable, y el mensaje puede además sugerir la alternativa.
    target = UPSCALE_FORMATS[format_value]
    chosen = next((m for m in available if m["id"] == selected), None)
    limit = int((chosen or {}).get("max_short_side") or 0)
    if limit and target > limit:
        alternativas = [
            m["label"] for m in available
            if m["id"] != selected and (not m.get("max_short_side") or m["max_short_side"] >= target)
        ]
        sugerencia = f" Prueba con: {', '.join(alternativas)}." if alternativas else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"«{chosen['label']}» no llega a {target}p en este hardware "
                f"(su tope es {limit}p por memoria de GPU).{sugerencia}"
            ),
        )
    return format_value, selected


@app.post("/api/upscale/jobs", status_code=202)
async def upscale_job_create(
    request: Request,
    media_format: str = Form("upscale_1080"),
    upscale_model: str = Form(""),
    file: UploadFile = File(...),
):
    """Encola un escalado y responde al momento con el identificador.

    Es la vía recomendada: el nivel de difusión va a ~20x el tiempo real, así
    que una petición síncrona se quedaría abierta horas.
    """
    format_value, selected = _validate_upscale_request(media_format, upscale_model, file)

    ensure_storage_ready()
    source = await save_upload_file(file)
    try:
        info = await run_in_threadpool(upscale_mod.probe, source)
    except upscale_mod.UpscaleError as exc:
        cleanup_path(source)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    fps = next((m["fps"] for m in upscale_mod.parse_models() if m["id"] == selected), 0.0)
    estimate = info["frames"] / fps if fps > 0 else 0.0

    job = jobs_mod.new_job(
        "upscale",
        {
            "source": str(source),
            "model": selected,
            "target_height": UPSCALE_FORMATS[format_value],
            "media_format": format_value,
            "filename": file.filename or "upscaled",
        },
        estimate_seconds=estimate,
    )
    _job_queue.submit(job)
    await run_in_threadpool(
        record_download_event, format_value, False, None, detect_request_source(request)
    )
    payload = job.public()
    payload["queued_ahead"] = max(0, _job_queue.pending() - 1)
    payload["estimate_human"] = upscale_mod.format_duration_es(estimate)
    return JSONResponse(payload, status_code=202)


@app.get("/api/upscale/jobs", response_class=JSONResponse)
async def upscale_job_list():
    _job_store.purge_expired(upscale_mod.cleanup_workdir)
    return {"jobs": [job.public() for job in _job_store.list()]}


@app.get("/api/upscale/jobs/{job_id}", response_class=JSONResponse)
async def upscale_job_status(job_id: str):
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    payload = job.public()
    payload["estimate_human"] = upscale_mod.format_duration_es(job.estimate_seconds)
    return payload


@app.get("/api/upscale/jobs/{job_id}/download")
async def upscale_job_download(job_id: str):
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    if job.status != jobs_mod.STATUS_DONE or not job.result_path:
        raise HTTPException(
            status_code=409,
            detail=f"El trabajo está en estado '{job.status}', todavía no hay resultado",
        )
    path = Path(job.result_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="El resultado ya se ha limpiado")
    # No se borra al descargar: el TTL se encarga, y así se puede descargar
    # más de una vez.
    return FileResponse(
        path=path,
        media_type="video/mp4",
        filename=job.result_name or path.name,
        headers={
            "x-vhs-upscale-model": str(job.metadata.get("upscale_model", "")),
            "x-vhs-mode": str(job.metadata.get("mode", "")),
            "x-vhs-delivered-resolution": str(job.metadata.get("delivered_resolution", "")),
        },
    )


@app.delete("/api/upscale/jobs/{job_id}", response_class=JSONResponse)
async def upscale_job_cancel(job_id: str):
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    if job.status in jobs_mod.TERMINAL_STATUSES:
        if job.result_path:
            await run_in_threadpool(
                upscale_mod.cleanup_workdir, Path(job.result_path).parent
            )
        return {"status": job.status, "detail": "El trabajo ya había terminado"}
    # Se pide la cancelación y se aplica entre segmentos: matar un proceso de
    # GPU a mitad es peor que esperar a que acabe el segmento en curso.
    job.cancel_requested = True
    _job_store.persist(job)
    return {"status": "cancelling", "detail": "Se detendrá al terminar el segmento en curso"}


@app.post("/api/upscale/upload")
async def upscale_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    media_format: str = Form("upscale_1080"),
    upscale_model: str = Form(""),
    file: UploadFile = File(...),
):
    """Sube un vídeo y devuélvelo escalado.

    Es síncrono como el resto de VHS. Con el nivel rápido va a ~1x el tiempo
    real, así que encaja; con un modelo de difusión (~9x) esto se queda corto
    y hará falta una cola de trabajos.
    """
    format_value, selected = _validate_upscale_request(media_format, upscale_model, file)

    temp_path: Optional[Path] = None
    output_path: Optional[Path] = None
    try:
        ensure_storage_ready()
        temp_path = await save_upload_file(file)
        output_path, metadata = await run_in_threadpool(
            upscale_mod.upscale_file,
            temp_path,
            model=selected,
            target_height=UPSCALE_FORMATS[format_value],
            ffmpeg=FFMPEG_BINARY,
            nvenc=FFMPEG_ENABLE_NVENC,
        )
    except upscale_mod.UpscaleError as exc:
        await run_in_threadpool(
            record_error_event, "upscale_upload", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if temp_path:
            cleanup_path(temp_path)

    download_name = build_download_name(
        file.filename or "upscaled", output_path, format_value
    )
    # El directorio de trabajo lleva dentro los segmentos intermedios, así que
    # se borra el árbol entero, no solo el fichero de salida.
    background_tasks.add_task(upscale_mod.cleanup_workdir, output_path.parent)
    response = FileResponse(
        path=output_path,
        media_type="video/mp4",
        filename=download_name,
        background=background_tasks,
        headers={
            "x-vhs-upscale-model": metadata["upscale_model"],
            "x-vhs-source-resolution": metadata["source_resolution"],
            "x-vhs-segments": str(metadata["segments"]),
            # "restore" avisa de que el vídeo se redujo antes de reconstruirlo,
            # en vez de ampliarse: el usuario debe poder saberlo.
            "x-vhs-mode": metadata["mode"],
            # Puede ser menor que lo pedido si la fuente no daba para más:
            # se prefiere entregar menos que fingir resolución.
            "x-vhs-delivered-resolution": metadata["delivered_resolution"],
        },
    )
    await run_in_threadpool(
        record_download_event,
        format_value,
        False,
        None,
        detect_request_source(request),
    )
    return response


@app.post("/api/transcribe/upload")
async def transcribe_upload(
    request: Request,
    media_format: str = Form("transcript_text"),
    transcription_model: str = Form(""),
    diarize: str = Form("false"),
    file: UploadFile = File(...),
):
    format_value = media_format.lower()
    requested_diarize = parse_bool_flag(diarize)
    effective_diarize = requested_diarize or is_diarization_format(format_value)
    translation_enabled = is_translation_format(format_value)
    requested_model = (transcription_model or "").strip() or None
    if format_value not in TRANSCRIPTION_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="Formato inválido. Usa un formato transcript_* soportado.",
        )
    if requested_model:
        try:
            if effective_diarize:
                resolve_diarization_model(requested_model)
            else:
                resolve_transcription_model(requested_model)
        except DownloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not file.filename:
        raise HTTPException(status_code=400, detail="Incluye un archivo de audio o video")
    temp_path: Optional[Path] = None
    try:
        ensure_storage_ready()
        temp_path = await save_upload_file(file)
        audio_path = await run_in_threadpool(
            extract_audio_profile_from_file, temp_path, "audio_med"
        )
        try:
            payload = await run_in_threadpool(
                transcribe_audio_file,
                audio_path,
                format_value,
                requested_model,
                effective_diarize,
            )
            if translation_enabled:
                payload = await run_in_threadpool(
                    translate_transcription_payload,
                    payload,
                )
        finally:
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass
    except DownloadError as exc:
        await run_in_threadpool(
            record_error_event, "transcription_upload", detect_request_source(request)
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    
    transcription_stats = estimate_transcription_stats(payload)
    content = render_transcription_payload(payload, format_value)
    download_name = build_transcription_download_name(file.filename or "transcript", format_value)
    headers = {"Content-Disposition": build_content_disposition_header(download_name)}
    response = Response(
        content=content,
        media_type=media_type_for_format(format_value),
        headers=headers,
    )
    await run_in_threadpool(
        record_download_event,
        format_value,
        False,
        transcription_stats,
        detect_request_source(request),
    )
    return response
TRANSLATION_MODEL = os.getenv("TRANSLATION_MODEL")
# Segmentos por petición y lotes simultáneos. La traducción hacía una llamada
# por segmento (152 en un vídeo de 8 minutos), que era el cuello de botella.
TRANSLATION_BATCH_SIZE = max(1, int(os.getenv("TRANSLATION_BATCH_SIZE", "8")))
TRANSLATION_CONCURRENCY = max(1, int(os.getenv("TRANSLATION_CONCURRENCY", "4")))
TRANSLATION_SYSTEM_PROMPT = os.getenv(
    "TRANSLATION_SYSTEM_PROMPT",
    "Eres un traductor profesional. Tu única tarea es traducir el texto al español de forma directa y precisa. "
    "NO resumas, NO razones, NO expliques, NO añadas comentarios. "
    "Mantén el significado exacto, el tono y la estructura del texto original. "
    "No uses Markdown ni ningún otro formato: el resultado va a subtítulos en texto plano. "
    "Devuelve ÚNICAMENTE el texto traducido, nada más."
)
TRANSLATION_USER_PROMPT_TEMPLATE = os.getenv(
    "TRANSLATION_USER_PROMPT_TEMPLATE",
    "Traduce el siguiente texto al español:\n\n{text}"
)
