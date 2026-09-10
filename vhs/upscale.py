"""Escalado de vídeo delegando el modelo en oCabra.

VHS no carga el modelo: trocea, llama a oCabra por segmento y reensambla. El
reparto es a propósito — oCabra ya gestiona VRAM, políticas de carga y
expulsión, y aquí ya están afinados ffmpeg y NVENC.

Sobre el solape entre segmentos: el nivel rápido (Real-ESRGAN Compact) trabaja
fotograma a fotograma, sin estado temporal, así que **no puede haber costuras**
en las junturas y el solape sobra. Los modelos de difusión sí mantienen estado
y lo necesitarán; queda como pendiente explícito en UPSCALE_SEGMENT_OVERLAP,
que hoy solo alarga los segmentos sin descartar el calentamiento.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


# Escala nativa de los modelos de escalado (4x en Compact y FlashVSR).
NATIVE_SCALE = 4


def short_side_filter(short: int) -> str:
    """Filtro que lleva el **lado corto** a ``short`` conservando la relación.

    Se trabaja con el lado corto y no con la altura porque "1080p" significa
    1080 líneas en horizontal y 1080 columnas en vertical: usar la altura
    rechazaba vídeos verticales perfectamente ampliables.
    """
    return (
        f"scale=w='if(gt(iw,ih),-2,{short})':h='if(gt(iw,ih),{short},-2)'"
        ":flags=lanczos"
    )


class UpscaleError(RuntimeError):
    """Fallo recuperable durante el escalado, con mensaje para el usuario."""


def cleanup_workdir(path: Path) -> None:
    """Borra el directorio de trabajo completo.

    Hace falta un helper propio: ``cleanup_path`` de main.py solo hace
    ``unlink``, que sobre un directorio lanza IsADirectoryError y se traga el
    error, dejando los segmentos intermedios en disco.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def endpoint() -> str:
    """Base de la API de oCabra.

    Si no se configura aparte, se deriva del endpoint de transcripción
    quitándole el sufijo ``/v1``: en la práctica es el mismo servidor.
    """
    explicit = _env("UPSCALE_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    base = _env("TRANSCRIPTION_ENDPOINT", "http://localhost:8484/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/ocabra"


def api_key() -> str:
    return _env("UPSCALE_API_KEY") or _env("TRANSCRIPTION_API_KEY")


def segment_seconds() -> int:
    return max(5, int(_env("UPSCALE_SEGMENT_SECONDS", "60")))


def parse_models(raw: str = "") -> List[Dict[str, Any]]:
    """Interpreta ``UPSCALE_MODELS``: ``id - etiqueta - fps`` separados por comas.

    El ``fps`` es el rendimiento medido del modelo y sirve para estimar
    tiempos en la interfaz. Se declara en configuración en vez de codificarse
    para que el aviso no mienta cuando cambie el hardware.
    """
    raw = raw or _env("UPSCALE_MODELS")
    models: List[Dict[str, Any]] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        parts = [p.strip() for p in value.split(" - ")]
        model_id = parts[0]
        if not model_id:
            continue
        label = parts[1] if len(parts) > 1 and parts[1] else model_id
        try:
            fps = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        except ValueError:
            fps = 0.0
        models.append({"id": model_id, "label": label, "fps": fps})
    return models


def format_duration_es(seconds: float) -> str:
    """Duración en castellano, redondeada a algo que se pueda leer de un vistazo."""
    if seconds <= 0:
        return "desconocido"
    if seconds < 90:
        return f"{int(round(seconds))} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(round(minutes))} min"
    hours = int(minutes // 60)
    rest = int(round(minutes % 60))
    if rest == 0:
        return f"{hours} h"
    return f"{hours} h {rest} min"


def estimate_note(fps: float, *, reference_minutes: int = 10, video_fps: int = 30) -> str:
    """Aviso de tiempo para la UI, con la referencia que pidió el usuario."""
    if fps <= 0:
        return ""
    frames = reference_minutes * 60 * video_fps
    return (
        f"{reference_minutes} minutos de vídeo pueden tardar "
        f"{format_duration_es(frames / fps)}"
    )


def models_for_ui() -> List[Dict[str, Any]]:
    out = []
    for model in parse_models():
        note = estimate_note(model["fps"])
        out.append({**model, "estimate": note})
    return out


def plan_scaling(
    width: int, height: int, target_short_side: int
) -> Tuple[str, Optional[int]]:
    """Decide el modo y el pre-escalado de la entrada del modelo.

    Devuelve ``(modo, lado_corto_previo)``; ``None`` en el segundo si no hay que
    pre-escalar.

    Dos reglas, ambas aprendidas a base de fallos:

    * Se razona con el **lado corto**, no con la altura: "1080p" son 1080 líneas
      en horizontal y 1080 columnas en vertical, y usar la altura rechazaba
      vídeos verticales perfectamente ampliables.
    * La entrada del modelo se limita a ``objetivo/4`` porque su escala es 4x
      exacta. Así la salida aterriza en el objetivo y el coste depende del
      objetivo y no de la resolución de origen: sin este techo, un vertical
      1440x2560 pedido a 2160p generaba fotogramas de 5760x10240 y agotaba la
      VRAM.

    Si el vídeo ya tiene la resolución pedida o más, el modo es ``restore``: se
    reduce y se reconstruye en vez de rechazarlo. Es el caso más común de verdad
    — material con resolución nominal alta y sin detalle real — y es además
    donde estos modelos rinden, porque se entrenan con entradas degradadas de
    baja resolución.
    """
    short_side = min(width, height)
    mode = "restore" if short_side >= target_short_side else "upscale"
    ideal_input = max(64, round(target_short_side / NATIVE_SCALE))
    pre_short_side = min(short_side, ideal_input)
    # Solo se pre-escala si de verdad reduce: ampliar antes del modelo sería
    # regalarle desenfoque.
    return mode, (pre_short_side if pre_short_side < short_side else None)


def probe(path: Path) -> Dict[str, Any]:
    result = subprocess.run(
        [
            _env("FFPROBE_BINARY", "ffprobe"), "-v", "error",
            "-show_entries", "format=duration",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise UpscaleError("No se pudo analizar el vídeo de entrada")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or [{}]
    stream = streams[0] if streams else {}
    fps_raw = str(stream.get("r_frame_rate") or "30/1")
    try:
        num, _, den = fps_raw.partition("/")
        fps = float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 30.0
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": fps or 30.0,
        "duration": duration,
        "frames": int(stream.get("nb_frames") or 0) or int(duration * (fps or 30.0)),
    }


def has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            _env("FFPROBE_BINARY", "ffprobe"), "-v", "error",
            "-select_streams", "a:0", "-show_entries", "stream=index",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def estimate_seconds(path: Path, fps: float) -> float:
    """Estimación para este fichero concreto, no la genérica de la UI."""
    if fps <= 0:
        return 0.0
    return probe(path)["frames"] / fps


def _run(cmd: List[str], *, what: str) -> None:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="ignore").strip().splitlines()
        detail = tail[-1] if tail else ""
        raise UpscaleError(f"{what} falló{': ' + detail if detail else ''}")


def split_video_only(
    source: Path, work: Path, ffmpeg: str, *, pre_short_side: Optional[int] = None
) -> List[Path]:
    """Trocea solo el vídeo, alineando cortes a fotograma clave.

    Se recodifica a CRF 12 (visualmente sin pérdida) en vez de copiar el flujo:
    copiar deja los cortes donde haya fotogramas clave en el original, que no
    tiene por qué coincidir con los límites pedidos.
    """
    seconds = segment_seconds()
    pattern = work / "seg_%05d.mp4"
    filters = []
    if pre_short_side:
        filters.append(short_side_filter(pre_short_side))
    _run(
        [
            ffmpeg, "-y", "-v", "error", "-i", str(source),
            "-an", "-map", "0:v:0",
            *(["-vf", ",".join(filters)] if filters else []),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
            "-force_key_frames", f"expr:gte(t,n_forced*{seconds})",
            "-f", "segment", "-segment_time", str(seconds),
            "-reset_timestamps", "1", str(pattern),
        ],
        what="El troceado del vídeo",
    )
    segments = sorted(work.glob("seg_*.mp4"))
    if not segments:
        raise UpscaleError("El troceado no produjo ningún segmento")
    return segments


def upscale_segment(segment: Path, *, model: str, timeout: float) -> bytes:
    url = f"{endpoint()}/video/upscale"
    headers = {}
    key = api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with segment.open("rb") as handle:
        files = {"file": (segment.name, handle.read(), "video/mp4")}
    # No se le pasa target_height: el worker reescala por altura y aquí se
    # trabaja con el lado corto, así que uno desharía al otro en vertical. VHS
    # ya pre-escala para que el 4x del modelo aterrice en el objetivo, y el
    # ajuste fino se hace en la codificación final.
    data = {"model": model, "crf": "14"}
    try:
        response = httpx.post(url, headers=headers, files=files, data=data, timeout=timeout)
    except httpx.HTTPError as exc:
        raise UpscaleError(f"No se pudo contactar con el servicio de escalado: {exc}") from exc
    if response.status_code >= 400:
        raise UpscaleError(
            f"El servicio de escalado devolvió {response.status_code}: "
            f"{response.text[:300]}"
        )
    if not response.content:
        raise UpscaleError("El servicio de escalado devolvió un segmento vacío")
    return response.content


def concat_and_remux(
    pieces: List[Path],
    source: Path,
    output: Path,
    ffmpeg: str,
    *,
    nvenc: bool,
    target_short_side: Optional[int] = None,
) -> None:
    """Une los segmentos y devuelve el audio original a su sitio."""
    list_file = output.parent / "concat.txt"
    list_file.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8"
    )

    joined = output.parent / "joined.mp4"
    _run(
        [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(joined)],
        what="La unión de segmentos",
    )

    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(joined)]
    # Ajuste fino al objetivo en la codificación que ya existía. Solo reduce:
    # si el modelo se quedó por debajo (fuente muy pequeña para el objetivo
    # pedido), estirarlo aquí sería fingir una resolución que no existe, así
    # que se entrega lo que hay.
    if target_short_side:
        joined_short = min(probe(joined)["width"], probe(joined)["height"])
        if joined_short > target_short_side:
            cmd += ["-vf", short_side_filter(target_short_side)]
    if has_audio(source):
        # El audio se copia del original: no ha pasado por el escalado y
        # recodificarlo solo añadiría una generación de pérdida.
        cmd += ["-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
    else:
        cmd += ["-map", "0:v:0", "-an"]
    if nvenc:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p6", "-cq", "22"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    cmd += ["-movflags", "+faststart", str(output)]
    _run(cmd, what="La codificación final")


def upscale_file(
    source: Path,
    *,
    model: str,
    target_height: int,
    ffmpeg: str,
    nvenc: bool,
    segment_timeout: float = 3600.0,
) -> Tuple[Path, Dict[str, Any]]:
    """Escala un fichero completo. Devuelve (ruta de salida, metadatos)."""
    info = probe(source)
    if info["width"] <= 0 or info["height"] <= 0:
        raise UpscaleError("El archivo no contiene una pista de vídeo utilizable")

    mode, pre_short_side = plan_scaling(info["width"], info["height"], target_height)

    work = Path(tempfile.mkdtemp(prefix="vhs_upscale_"))
    output = work / "upscaled.mp4"
    try:
        segments = split_video_only(
            source, work, ffmpeg, pre_short_side=pre_short_side
        )
        pieces: List[Path] = []
        for index, segment in enumerate(segments):
            body = upscale_segment(segment, model=model, timeout=segment_timeout)
            piece = work / f"up_{index:05d}.mp4"
            piece.write_bytes(body)
            pieces.append(piece)
        concat_and_remux(
            pieces, source, output, ffmpeg,
            nvenc=nvenc, target_short_side=target_height,
        )
    except Exception:
        cleanup_workdir(work)
        raise

    delivered = probe(output)
    metadata = {
        "source_resolution": f"{info['width']}x{info['height']}",
        "delivered_resolution": f"{delivered['width']}x{delivered['height']}",
        "target_height": target_height,
        "mode": mode,
        "segments": len(pieces),
        "frames": info["frames"],
        "upscale_model": model,
    }
    return output, metadata
