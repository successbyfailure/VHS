import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

import requests
import yt_dlp
from openai import OpenAI
from versioning import get_version

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
    YTDLP_EXTRACTOR_ARGS = {"youtube": ["player_client=android"]}
TRANSCRIPTION_ENDPOINT = os.getenv("TRANSCRIPTION_ENDPOINT", "https://api.openai.com/v1")
TRANSCRIPTION_API_KEY = os.getenv("TRANSCRIPTION_API_KEY")
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
WHISPER_ASR_URL = os.getenv("WHISPER_ASR_URL")
WHISPER_ASR_TIMEOUT = int(os.getenv("WHISPER_ASR_TIMEOUT", "600"))
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

AUDIO_FORMAT_PROFILES = {
    "audio_high": {
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
    "video_high": {
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
DEFAULT_VIDEO_FORMAT = "video_high"
VIDEO_FORMAT_ALIASES = {
    "video": DEFAULT_VIDEO_FORMAT,
}
FFMPEG_PRESETS: Dict[str, Dict[str, Any]] = {
    "ffmpeg_480p": {
        "description": "Transcodifica a 480p (h.264 CRF 24 máx. ~1.8 Mbps / AAC 128 kbps)",
        "extension": ".mp4",
        "media_type": "video/mp4",
        "args": [
            *FFMPEG_HWACCEL_ARGS,
            "-vf",
            "scale_cuda=-2:480" if FFMPEG_ENABLE_NVENC else "scale=-2:480",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            "-preset",
            "veryfast",
            "-crf",
            "24",
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
        "args": [
            *FFMPEG_HWACCEL_ARGS,
            "-vf",
            "scale_cuda=-2:720" if FFMPEG_ENABLE_NVENC else "scale=-2:720",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            "-preset",
            "veryfast",
            "-crf",
            "23",
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
        "args": [
            *FFMPEG_HWACCEL_ARGS,
            "-vf",
            "scale_cuda=-2:1080" if FFMPEG_ENABLE_NVENC else "scale=-2:1080",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            "-preset",
            "veryfast",
            "-crf",
            "22",
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
        "args": [
            *FFMPEG_HWACCEL_ARGS,
            "-vf",
            "scale_cuda=-2:1440" if FFMPEG_ENABLE_NVENC else "scale=-2:1440",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            "-preset",
            "faster",
            "-crf",
            "21",
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
        "args": [
            *FFMPEG_HWACCEL_ARGS,
            "-vf",
            "scale_cuda=-2:2160" if FFMPEG_ENABLE_NVENC else "scale=-2:2160",
            "-c:v",
            FFMPEG_VIDEO_ENCODER,
            "-preset",
            "fast",
            "-crf",
            "20",
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
MEDIA_FORMAT_PATTERN = f"^({'|'.join(sorted(SUPPORTED_MEDIA_FORMATS))})$"

def is_diarization_format(media_format: str) -> bool:
    normalized = normalize_media_format(media_format)
    return normalized in {
        "transcript_diarized_json",
        "transcript_diarized_text",
        "transcript_translate_diarized_json",
        "transcript_translate_diarized_text",
    }


def is_translation_format(media_format: str) -> bool:
    normalized = normalize_media_format(media_format)
    return normalized.startswith("transcript_translate")

FORMAT_DESCRIPTIONS: List[Dict[str, str]] = [
    {
        "name": "video_high",
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
        "name": "audio_high",
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
        "description": "JSON completo con segmentos y timestamps",
    },
    {
        "name": "transcript_text",
        "description": "Solo el texto consolidado",
    },
    {
        "name": "transcript_srt",
        "description": "Subtítulos compatibles con reproductores",
    },
    {
        "name": "transcript_diarized_json",
        "description": "Transcripción JSON con etiquetas de hablante (whisper-asr)",
    },
    {
        "name": "transcript_diarized_text",
        "description": "Transcripción TXT con etiquetas de hablante (whisper-asr)",
    },
    {
        "name": "transcript_translate_json",
        "description": "Traducción al español en JSON (whisper-asr)",
    },
    {
        "name": "transcript_translate_text",
        "description": "Traducción al español en TXT (whisper-asr)",
    },
    {
        "name": "transcript_translate_srt",
        "description": "Traducción al español en SRT (whisper-asr)",
    },
    {
        "name": "transcript_translate_diarized_json",
        "description": "Traducción al español con etiquetas de hablante en JSON (whisper-asr)",
    },
    {
        "name": "transcript_translate_diarized_text",
        "description": "Traducción al español con etiquetas de hablante en TXT (whisper-asr)",
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


def meta_path(key: str) -> Path:
    return META_DIR / f"{key}.json"


def legacy_meta_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def is_expired(meta: Dict) -> bool:
    downloaded_at = meta.get("downloaded_at") or 0
    return (time.time() - float(downloaded_at)) > CACHE_TTL_SECONDS


FORMAT_EXTENSIONS = {
    "video": ".mp4",
    "video_high": ".mp4",
    "video_med": ".mp4",
    "video_low": ".mp4",
    "audio_high": ".mp3",
    "audio_med": ".mp3",
    "audio_low": ".mp3",
    "transcript_json": ".json",
    "transcript_text": ".txt",
    "transcript_srt": ".srt",
    "transcript_diarized_json": ".json",
    "transcript_diarized_text": ".txt",
    "transcript_translate_json": ".json",
    "transcript_translate_text": ".txt",
    "transcript_translate_srt": ".srt",
    "transcript_translate_diarized_json": ".json",
    "transcript_translate_diarized_text": ".txt",
}

for preset_name, preset in FFMPEG_PRESETS.items():
    FORMAT_EXTENSIONS[preset_name] = preset["extension"]

TRANSCRIPTION_FILE_SUFFIX = ".transcript.json"


def media_type_for_format(media_format: str) -> str:
    normalized = normalize_media_format(media_format)
    if normalized in {
        "transcript_diarized_json",
        "transcript_translate_json",
        "transcript_translate_diarized_json",
    }:
        return "application/json"
    if normalized == "transcript_json":
        return "application/json"
    if normalized in TRANSCRIPTION_FORMATS - {"transcript_json"}:
        return "text/plain"
    if normalized in FFMPEG_PRESETS:
        return FFMPEG_PRESETS[normalized]["media_type"]
    if normalized in AUDIO_FORMAT_PROFILES:
        profile = AUDIO_FORMAT_PROFILES[normalized]
        if profile.get("passthrough"):
            return "audio/*"
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
    for event in points:
        timestamp = event.get("timestamp")
        if timestamp is None:
            continue
        event_dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        if event_dt < cutoff:
            continue
        day_key = event_dt.date().isoformat()
        if day_key not in aggregates:
            continue
        source = event.get("source") or "api"
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
        provider = event.get("provider")
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
    top_formats = sorted(
        format_totals.items(), key=lambda item: item[1], reverse=True
    )[:3]
    processing_summary = {"average_ms": 0.0, "p95_ms": 0.0}
    if processing_all:
        processing_all.sort()
        processing_summary["average_ms"] = sum(processing_all) / len(processing_all)
        processing_summary["p95_ms"] = processing_all[max(0, int(len(processing_all) * 0.95) - 1)]
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
    }


def build_download_name(title: str, file_path: Path, media_format: str) -> str:
    base = title.strip().lower() or "vhs"
    safe = re.sub(r"[^a-z0-9\-_.]+", "_", base)
    safe = re.sub(r"_+", "_", safe).strip("._") or "vhs"
    extension = file_path.suffix or FORMAT_EXTENSIONS.get(media_format, ".bin")
    return f"{safe}{extension}"


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
    with meta_path(key).open("w", encoding="utf-8") as handle:
        json.dump(sanitized, handle, ensure_ascii=False, indent=2)


def build_ydl_options(
    media_format: str, *, cache_key_value: str, force_no_proxy: bool = False
) -> Dict:
    js_runtimes: Dict[str, Dict[str, str]] = {}
    for candidate in ("node", "nodejs"):
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
    cached_path, cached_meta = fetch_cached_file(key)
    if cached_path:
        return cached_path, cached_meta or {}

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


def run_ffmpeg(source: Path, destination: Path, args: List[str]) -> None:
    command = [FFMPEG_BINARY, "-y", "-i", str(source), *args, str(destination)]
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


def process_with_ffmpeg(url: str, media_format: str) -> Tuple[Path, Dict]:
    preset = FFMPEG_PRESETS[media_format]
    key = cache_key(url, media_format)
    purge_expired_entries()
    cached_path, cached_meta = fetch_cached_file(key)
    if cached_path:
        return cached_path, cached_meta or {}

    source_path, source_metadata = download_media(url, DEFAULT_VIDEO_FORMAT)
    output_path = CACHE_DIR / f"{key}{preset['extension']}"
    output_path.unlink(missing_ok=True)
    run_ffmpeg(source_path, output_path, preset["args"])

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
    if TRANSCRIPTION_API_KEY and TRANSCRIPTION_MODEL:
        return
    if WHISPER_ASR_URL:
        return
    raise DownloadError(
        "La transcripción no está disponible. Configura TRANSCRIPTION_API_KEY y TRANSCRIPTION_MODEL o un WHISPER_ASR_URL."
    )


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


def _translate_texts_to_spanish(texts: List[str]) -> List[str]:
    if not TRANSCRIPTION_API_KEY:
        raise DownloadError("La traducción requiere configurar TRANSCRIPTION_API_KEY")
    model = TRANSLATION_MODEL or TRANSCRIPTION_MODEL
    if not model or model.startswith("whisper"):
        raise DownloadError(
            "Configura TRANSLATION_MODEL con un modelo de chat válido para traducir al español"
        )
    client = OpenAI(api_key=TRANSCRIPTION_API_KEY, base_url=TRANSCRIPTION_ENDPOINT)
    results: List[str] = []
    for text in texts:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Traduce el siguiente texto al español. Devuelve solo el texto traducido."},
                {"role": "user", "content": str(text)},
            ],
            temperature=0,
        )
        translated = (completion.choices[0].message.content or "").strip()
        if not translated:
            raise DownloadError("La traducción devolvió un texto vacío")
        results.append(translated)
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
    elif normalized == "transcript_srt":
        text = transcription_payload_to_srt(payload)
    elif normalized == "transcript_translate_srt":
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
        run_ffmpeg(source_path, output_path, preset["args"])
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


def _call_openai_transcription(file_path: Path) -> Dict[str, Any]:
    client = OpenAI(api_key=TRANSCRIPTION_API_KEY, base_url=TRANSCRIPTION_ENDPOINT)
    with file_path.open("rb") as audio_stream:
        response = client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=audio_stream,
            response_format="verbose_json",
        )
    return _normalize_transcription_payload(response)


def _whisper_asr_request_params(media_format: str) -> Dict[str, Any]:
    normalized = normalize_media_format(media_format)
    diarization = is_diarization_format(normalized)
    translation = is_translation_format(normalized)
    task = "transcribe"
    output = "json"
    if normalized in {"transcript_srt", "transcript_translate_srt"}:
        output = "srt"
    elif normalized in {
        "transcript_text",
        "transcript_diarized_text",
        "transcript_translate_text",
        "transcript_translate_diarized_text",
    }:
        output = "txt"
    params = {"output": output, "task": task, "encode": "true"}
    if diarization:
        params["diarize"] = "true"
        params["min_speakers"] = "2"
    return params


def _call_whisper_asr(file_path: Path, media_format: str) -> Dict[str, Any]:
    if not WHISPER_ASR_URL:
        raise DownloadError("Servicio whisper-asr no configurado")
    base = WHISPER_ASR_URL.rstrip("/")
    endpoint = f"{base}/asr"
    params = _whisper_asr_request_params(media_format)
    mime_type = "audio/mpeg" if file_path.suffix.lower() in {".mp3", ".mpeg"} else "application/octet-stream"
    with file_path.open("rb") as audio_stream:
        response = requests.post(
            endpoint,
            params=params,
            files={"audio_file": (file_path.name, audio_stream, mime_type)},
            timeout=WHISPER_ASR_TIMEOUT,
        )
    if response.status_code >= 400:
        raise DownloadError(
            f"whisper-asr respondió con un error HTTP {response.status_code}: {response.text.strip()}"
        )
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - depends on remote service
            raise DownloadError("whisper-asr devolvió un JSON inválido") from exc
    else:
        text_content = response.text
        payload = {"text": text_content.strip()}
    return _normalize_transcription_payload(payload)


def transcribe_audio_file(file_path: Path, media_format: str) -> Dict[str, Any]:
    ensure_transcription_ready()
    Attempt = Tuple[str, Callable[[], Dict[str, Any]]]
    attempts: List[Attempt] = []

    normalized_format = normalize_media_format(media_format)
    translation = is_translation_format(normalized_format)
    diarization = is_diarization_format(normalized_format)

    if (translation or diarization) and not WHISPER_ASR_URL:
        raise DownloadError("La diarización y la traducción requieren configurar WHISPER_ASR_URL")

    if translation or diarization:
        attempts.append(("whisper-asr", lambda: _call_whisper_asr(file_path, media_format)))
    else:
        if TRANSCRIPTION_API_KEY and TRANSCRIPTION_MODEL:
            attempts.append(("openai", lambda: _call_openai_transcription(file_path)))
        if WHISPER_ASR_URL:
            attempts.append(("whisper-asr", lambda: _call_whisper_asr(file_path, media_format)))

    errors: List[str] = []
    for provider_name, provider_call in attempts:
        try:
            return provider_call()
        except Exception as exc:  # pragma: no cover - servicios externos
            errors.append(f"{provider_name}: {exc}")

    joined = "; ".join(errors)
    raise DownloadError(f"No se pudo transcribir el audio: {joined or 'error desconocido'}")


def generate_transcription_file(url: str, media_format: str) -> Tuple[Path, Dict]:
    if media_format not in TRANSCRIPTION_FORMATS:
        raise DownloadError("Formato de transcripción no soportado")
    diarization = is_diarization_format(media_format)
    translation = is_translation_format(media_format)
    if (diarization or translation) and not WHISPER_ASR_URL:
        raise DownloadError("La diarización y la traducción requieren configurar WHISPER_ASR_URL apuntando a whisper-asr")
    diarization_suffix = f"diarization={int(diarization)}"
    translation_suffix = f"translation={int(translation)}"
    key = cache_key(f"{url}::{diarization_suffix}::{translation_suffix}", media_format)
    purge_expired_entries()
    cached_path, cached_meta = fetch_cached_file(key)
    if cached_path:
        return cached_path, cached_meta or {}

    audio_path, audio_meta = download_media(url, "audio_med")
    transcript_payload = transcribe_audio_file(audio_path, media_format)
    if translation:
        transcript_payload = translate_transcription_payload(transcript_payload)
    transcription_stats = estimate_transcription_stats(transcript_payload)

    if media_format == "transcript_json":
        transcript_path = CACHE_DIR / f"{key}{TRANSCRIPTION_FILE_SUFFIX}"
        transcript_path.write_text(
            json.dumps(transcript_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif media_format == "transcript_srt":
        transcript_path = CACHE_DIR / f"{key}.srt"
        srt_content = transcription_payload_to_srt(transcript_payload)
        transcript_path.write_text(srt_content, encoding="utf-8")
    else:
        text_only = transcript_payload.get("text") or ""
        if not isinstance(text_only, str):
            text_only = str(text_only)
        transcript_path = CACHE_DIR / f"{key}.txt"
        transcript_path.write_text(text_only.strip(), encoding="utf-8")

    metadata = {
        "title": audio_meta.get("title") or "transcript",
        "filename": transcript_path.name,
        "source_url": url,
        "media_format": media_format,
        "downloaded_at": time.time(),
        "cache_key": key,
        "transcription_stats": transcription_stats,
        "diarization": bool(diarization),
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        template_context(
            request,
            supported_services=SUPPORTED_SERVICES,
        ),
    )


@app.get("/docs/api", response_class=HTMLResponse)
async def api_docs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "api_docs.html",
        template_context(
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

    try:
        ensure_storage_ready()
        if normalized_format in TRANSCRIPTION_FORMATS:
            file_path, metadata = await run_in_threadpool(
                generate_transcription_file, url, normalized_format
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

    try:
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


@app.post("/api/transcribe/upload")
async def transcribe_upload(
    request: Request,
    media_format: str = Form("transcript_text"),
    file: UploadFile = File(...),
):
    format_value = media_format.lower()
    if format_value not in TRANSCRIPTION_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="Formato inválido. Usa un formato transcript_* soportado.",
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="Incluye un archivo de audio o video")

    diarization = is_diarization_format(format_value)
    translation = is_translation_format(format_value)
    if (diarization or translation) and not WHISPER_ASR_URL:
        raise HTTPException(
            status_code=400,
            detail="La diarización y la traducción requieren configurar WHISPER_ASR_URL",
        )
    temp_path: Optional[Path] = None
    try:
        ensure_storage_ready()
        temp_path = await save_upload_file(file)
        audio_path = await run_in_threadpool(
            extract_audio_profile_from_file, temp_path, "audio_med"
        )
        try:
            payload = await run_in_threadpool(
                transcribe_audio_file, audio_path, format_value
            )
            if translation:
                payload = await run_in_threadpool(
                    translate_transcription_payload, payload
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
    headers = {"Content-Disposition": f'attachment; filename="{download_name}"'}
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
