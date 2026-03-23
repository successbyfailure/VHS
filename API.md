# API de VHS

Esta guía resume los formatos disponibles y los endpoints REST para descargar, transcodificar y transcribir contenido.

## Perfiles del importador

Video:
- `video_high`: mejor calidad disponible desde la fuente.
- `video_med`: copia MP4 hasta 720p.
- `video_low`: copia MP4 hasta 480p.

Audio:
- `audio_high`: mejor audio disponible sin recomprimir.
- `audio_med`: MP3 a 96 kbps.
- `audio_low`: MP3 a 48 kbps.

Transcripciones (todos los formatos funcionan correctamente desde v0.2.9):
- `transcript_json`: salida JSON completa con segmentos y marcas de tiempo.
- `transcript_text`: solo el texto plano consolidado.
- `transcript_srt`: archivo SRT listo para reproductores.

## Perfiles ffmpeg y transcripción local

Los endpoints de conversión y transcripción aceptan los siguientes formatos:

- `ffmpeg_480p`, `ffmpeg_720p`, `ffmpeg_1080p`, `ffmpeg_1440p`, `ffmpeg_3840p`: MP4 con escalado y bitrates objetivo (2.5 Mbps, 4 Mbps, 6.5 Mbps, 12 Mbps, 20 Mbps respectivamente; audio AAC entre 128–256 kbps).
- `ffmpeg_wav`: WAV sin pérdidas (44.1 kHz, estéreo).
- `ffmpeg_mp3-192`, `ffmpeg_mp3-128`, `ffmpeg_mp3-96`, `ffmpeg_mp3-64`: MP3 con los bitrates indicados.
- `transcript_json`, `transcript_text`, `transcript_srt`: salidas de transcripción.

## Endpoints principales

### Descargar o transcribir desde una URL
`POST /api/download`

Body JSON:
```json
{
  "url": "https://...",
  "media_format": "video_high"
}
```

- Acepta cualquiera de los formatos listados arriba (video/audio/ffmpeg/transcripción).
- Para formatos `transcript_*` puedes enviar opcionalmente `transcription_model` para forzar el modelo de STT configurado en `TRANSCRIPTION_MODELS`.
- Para diarización puedes enviar `diarize=true`; en ese caso el modelo se valida contra `DIARIZATION_MODELS`.
- Guarda metadatos en caché con resolución (`width`, `height`), bitrates (`video_bitrate_kbps`, `audio_bitrate_kbps`), identificador de formato (`format_id`) y tamaño (`filesize_bytes`).
- Si la descarga ya existe en caché y no ha expirado, se reutiliza.

### Recodificar un archivo local con ffmpeg
`POST /api/ffmpeg/upload`

- `multipart/form-data` con campos `file` y `media_format` (por defecto `ffmpeg_mp3-192`).
- Devuelve la conversión solicitada sin conservar el archivo original.

### Transcribir un archivo local
`POST /api/transcribe/upload`

- `multipart/form-data` con campos `file` y `media_format` (`transcript_json`, `transcript_text`, `transcript_srt`).
- Campo opcional `transcription_model` para elegir el modelo STT (solo modelos permitidos por `TRANSCRIPTION_MODELS`).
- Campo opcional `diarize` (`true`/`false`) para activar diarización; al activarlo se usan modelos de `DIARIZATION_MODELS`.
- Usa el mismo pipeline de transcripción que el importador remoto y devuelve texto, JSON o SRT según se solicite.

### Modelos de transcripción disponibles
`GET /api/transcription/models`

- Devuelve el modelo por defecto y la lista de opciones visibles en UI:
  - `default_model`
  - `models[]` con `id` y `label`
  - `default_diarization_model`
  - `diarization_models[]` con `id` y `label`

### Inspeccionar sin descargar
`POST /api/probe`

Body JSON:
```json
{
  "url": "https://..."
}
```

- Ejecuta `yt-dlp` en modo inspección para recuperar título, duración, miniaturas y extractor sin descargar el archivo.

### Buscar
`POST /api/search`

Body JSON:
```json
{
  "query": "...",
  "limit": 8
}
```

- Devuelve resultados planos (id, título, URL) usando `yt-dlp` con búsqueda automática.

### Caché
- `GET /api/cache`: lista las entradas disponibles con tamaños, resolución, bitrates y URLs para descargar o eliminar.
- `GET /api/cache/{cache_key}/download`: devuelve el archivo en caché, registrando el acceso.
- `DELETE /api/cache/{cache_key}`: elimina el archivo y su metadato.

### Estadísticas y salud
- `GET /api/stats/usage`: totales por día (descargas, ffmpeg, transcripciones, palabras/tokens, errores) y top de formatos.
- `GET /api/health`: responde `{ "status": "ok" }` (incluye versión si está configurada).

## Notas sobre metadatos

- Todos los archivos escritos en caché incluyen los campos de resolución, bitrates y `format_id` cuando están disponibles.
- Las conversiones ffmpeg añaden los objetivos (`target_height`, `target_video_bitrate_kbps`, `target_audio_bitrate_kbps`) y una copia compacta de los metadatos del archivo fuente.
- Las transcripciones guardan estadísticas (`word_count`, `token_count`) junto al formato solicitado.

## Nota de compatibilidad

La rama actual elimina el soporte de `whisper-asr` y los formatos asociados de diarización/traducción (`transcript_diarized_*`, `transcript_translate_*`).

Para más detalles sobre testing y ejemplos de uso, ver `test_data/TRANSCRIPTION_TESTING.md`.
