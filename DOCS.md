# Documentación de VHS - Índice

**VHS (Video Harvester Service) v0.2.9**

Servicio FastAPI para descarga, conversión, transcripción y traducción de videos/audios.

---

## 📚 Documentación Principal

### [README.md](README.md)
**Inicio rápido y configuración**
- Requisitos del sistema
- Variables de entorno
- Instalación (local, Docker, Docker Compose)
- Imagen GPU con NVENC
- Integración continua

### [API.md](API.md)
**Referencia completa de la API REST**
- Todos los formatos disponibles (video, audio, transcripción)
- Endpoints principales (`/api/download`, `/api/transcribe/upload`, etc.)
- Caché y estadísticas
- Metadatos de archivos
- Correcciones en v0.2.9

### [AGENTS.md](AGENTS.md)
**Guía para colaboradores**
- Versionado automático
- Estilo y documentación
- Instrucciones para contribuir

---


## 🔧 Configuración

### [.env](example.env)
**Variables de entorno** (copiar de `example.env` a `.env`)

#### Básicas
```bash
CACHE_TTL_SECONDS=86400
CACHE_DIR=data/cache
USAGE_LOG_PATH=data/usage_log.jsonl
```

#### YouTube/yt-dlp
```bash
YTDLP_USER_AGENT=Mozilla/5.0...
YTDLP_BOT_PROTECTION_RETRIES=3
YTDLP_BOT_PROTECTION_DELAY=6
YTDLP_EXTRACTOR_ARGS={"youtube": ["player_client=default"]}
```

#### Transcripción
```bash
# OpenAI-compatible endpoint
TRANSCRIPTION_ENDPOINT=https://api.openai.com/v1
TRANSCRIPTION_API_KEY=sk-...
TRANSCRIPTION_MODEL=whisper-large-v3-turbo
TRANSCRIPTION_MODELS=whisper-large-v3-turbo - best, faster-whisper-large-v3-turbo-latam-int8-ct2 - Español (rápido), nvidia-parakeet-tdt-0.6b-v3 - solo texto (sin marcas de tiempo), faster-whisper-base - ligero (menor precisión)
DIARIZATION_MODEL=whisper-large-v3-turbo-diarized
DIARIZATION_MODELS=whisper-large-v3-turbo-diarized - best (diarized), faster-whisper-large-v3-turbo-latam-int8-ct2-diarized - Español (diarized), faster-whisper-base-diarized - ligero (diarized)

# Traducción con LLM (opcional para utilidades como el bot de Telegram)
TRANSLATION_MODEL=gemma4:12b-vllm-ctx64k
SUMMARY_MODEL=gemma4:12b-vllm-ctx64k
# Segmentos por petición y lotes en paralelo (vLLM agrupa; ollama serializa)
TRANSLATION_BATCH_SIZE=8
TRANSLATION_CONCURRENCY=8
# TRANSLATION_SYSTEM_PROMPT=... (opcional)
# TRANSLATION_USER_PROMPT_TEMPLATE=... (opcional)
```

---

## 🎯 Características Principales

### ✅ Descarga de Videos/Audios
- Múltiples plataformas (YouTube, Vimeo, TikTok, Instagram, etc.)
- Perfiles de calidad: high/med/low
- Formatos: MP4, MP3, WAV
- Caché con TTL configurable

### ✅ Transcripción
- Provider: OpenAI-compatible (modelo configurable)
- Formatos: JSON (completo), SRT (subtítulos), TXT (texto plano)
- Word-level timestamps y scores de confianza

### ✅ Conversión con FFmpeg
- Perfiles: 480p, 720p, 1080p, 1440p, 4K
- Audio: MP3 (varios bitrates), WAV
- Soporte GPU (NVENC) opcional
- Bitrates y resoluciones configurables

---

## 📊 Formatos Soportados

### Video
- `video_high` - Mejor calidad disponible
- `video_med` - MP4 hasta 720p
- `video_low` - MP4 hasta 480p

### Audio
- `audio_high` - Mejor audio sin recomprimir
- `audio_med` - MP3 96 kbps
- `audio_low` - MP3 48 kbps

### Transcripción Básica
- `transcript_json` - JSON completo con timestamps
- `transcript_text` - Texto plano
- `transcript_srt` - Subtítulos SRT

### Transcripción con Diarización
- `transcript_diarized_json` - JSON con speakers
- `transcript_diarized_text` - Texto con speakers

### Traducción al Español
- `transcript_translate_json` - JSON traducido
- `transcript_translate_text` - Texto traducido
- `transcript_translate_srt` - Subtítulos SRT en español

### Traducción + Diarización
- `transcript_translate_diarized_json` - JSON traducido con speakers
- `transcript_translate_diarized_text` - Texto traducido con speakers

### FFmpeg
- `ffmpeg_480p`, `ffmpeg_720p`, `ffmpeg_1080p`, `ffmpeg_1440p`, `ffmpeg_3840p`
- `ffmpeg_mp3-192`, `ffmpeg_mp3-128`, `ffmpeg_mp3-96`, `ffmpeg_mp3-64`
- `ffmpeg_wav`

---

## 🚀 Inicio Rápido

### Con Docker Compose (Recomendado)

```bash
# 1. Clonar y configurar
git clone <repo>
cd VHS
cp example.env .env
# Editar .env con tus credenciales

# 2. Levantar servicio
docker compose up -d

# 3. Verificar
curl http://localhost:8601/api/health
```

### Uso Básico

```bash
# Descargar video
curl -X POST http://localhost:8601/api/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=...", "media_format": "video_720p"}'

# Transcribir
curl -X POST http://localhost:8601/api/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=...", "media_format": "transcript_json"}'

```

---

## 🆘 Soporte y Troubleshooting

### Problemas Comunes

**Error de transcripción**: Verificar que `TRANSCRIPTION_API_KEY` esté configurado
**Modelo no permitido**: Revisar `TRANSCRIPTION_MODELS` o `DIARIZATION_MODELS` según el caso
**Traducción del bot falla**: Verificar que `TRANSLATION_MODEL` sea compatible con chat
**La subida de archivos desde la web falla con `[object Object]` o 422**: corregido en 0.4.1. El cliente forzaba `Content-Type: application/json` en todos los POST, lo que rompía el multipart de los tres formularios de subida (ffmpeg, transcribir y escalado)
**YouTube bloquea descargas**: Ajustar `YTDLP_USER_AGENT` y `YTDLP_EXTRACTOR_ARGS`
**El servicio se queda obsoleto**: `watchtower` ya viene activo en ambos ficheros compose (cada 5 min, solo contenedores con la etiqueta `watchtower.scope=vhs`). Comprueba que está vivo con `docker logs vhs-watchtower`. Ojo: watchtower despliega lo que haya en GHCR, así que una imagen construida en local y no publicada será reemplazada por la del registro
**Dos peticiones idénticas a la vez**: desde 0.3.1 se serializan con un lock por clave de caché; la primera genera el fichero y el resto reutilizan su resultado. Si vuelves a ver `Unable to rename file ... .part`, el lock no se está aplicando a esa ruta
**`HTTP Error 403: Forbidden` al descargar** (el `probe` sí funciona): yt-dlp está desactualizado o falta el runtime JS. Actualiza la imagen y comprueba con `docker exec vhs deno --version`

### Logs

```bash
# Docker Compose
docker compose logs -f vhs

# Docker directo
docker logs -f vhs

# Local
# Los logs van a stdout/stderr
```

### Verificar Configuración

```bash
# Health check
curl http://localhost:8601/api/health

# Probe (test sin descargar)
curl -X POST http://localhost:8601/api/probe \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"}'

# Ver caché
curl http://localhost:8601/api/cache

# Estadísticas de uso
curl http://localhost:8601/api/stats/usage
```

---

## 📞 Contacto y Contribución

- **Issues**: Reportar bugs en GitHub Issues
- **Pull Requests**: Seguir guía en [AGENTS.md](AGENTS.md)
- **Versionado**: Automático (ver AGENTS.md)

---

## 📄 Licencia

Ver archivo LICENSE en el repositorio.

## 🎛️ Rendimiento

### Traducción

La traducción hacía **una llamada de chat por segmento** (152 en un vídeo de 8
minutos). Ahora los segmentos se agrupan (`TRANSLATION_BATCH_SIZE`) y los lotes
se lanzan en paralelo (`TRANSLATION_CONCURRENCY`). Si el modelo no respeta la
numeración pedida, ese lote se reintenta segmento a segmento: es preferible ir
lento a desplazar los subtítulos.

El backend del modelo manda. Medido sobre el endpoint, lotes de 8 segmentos:

| modelo | backend | 1 hilo | 8 hilos |
|---|---|---|---|
| `ministral-3:14b` | ollama | 1.7 seg/s | 1.9 seg/s |
| `gemma4:12b-vllm-ctx64k` | vLLM | 3.0 seg/s | **19.5 seg/s** |

ollama serializa las peticiones, así que subir la concurrencia no aporta nada;
vLLM las agrupa y escala casi lineal. Con un modelo `*-vllm-*` la traducción de
un vídeo de 8 minutos baja de 2m46s a ~20s (50s contando descarga y STT).

### Vídeo: NVENC frente a libx264

Medido sobre una fuente 1080p AV1 de 8:29 (SSIM/PSNR sobre un tramo de 90 s):

| encoder | tiempo | tamaño | SSIM | PSNR |
|---|---|---|---|---|
| `libx264 veryfast crf22` (CPU) | 129 s | 184.0 MB | 0.99196 | 45.47 dB |
| `nvenc p2 cq22` (el antiguo `cq = crf`) | 47 s | 302.5 MB | 0.99155 | 45.84 dB |
| `nvenc p2 cq34` | 50 s | 186.5 MB | 0.98984 | 45.05 dB |
| `nvenc p6 cq32` (actual) | 106 s | ~186 MB | **0.99234** | **46.81 dB** |

Con `cq = crf` NVENC gastaba el doble de bytes sin ganar calidad: la escala de
`-cq` no equivale a la de `-crf`. Con el desplazamiento (`FFMPEG_NVENC_CQ_OFFSET`,
10 por defecto) el tamaño se iguala. A partir de ahí hay dos puntos de operación:

- **`p6` + offset 10** (por defecto): ~1.2× más rápido que libx264 y algo mejor
  de calidad medida.
- **`p2` + offset 12** (`FFMPEG_NVENC_PRESET=p2`): ~2.6× más rápido, mismo
  tamaño, calidad ligeramente inferior.

Además NVENC usa el bloque de codificación dedicado de la GPU, no los núcleos
CUDA, así que apenas compite con los modelos de IA que corren en la misma
máquina y deja la CPU libre.

## 🔍 Mejorar la resolución de un vídeo

`POST /api/upscale/upload` (multipart) sube un vídeo y lo devuelve escalado.
`GET /api/upscale/models` lista los modelos con su aviso de tiempo.

Campos: `file`, `media_format` (`upscale_1080` | `upscale_1440` | `upscale_2160`)
y `upscale_model` opcional.

El modelo no corre en VHS: lo sirve **oCabra**. VHS trocea el vídeo (sin audio),
manda un segmento por petición, reensambla y remezcla el audio original. El
reparto es deliberado — oCabra ya gestiona VRAM y expulsión, y aquí ya estaban
afinados ffmpeg y NVENC.

Los formatos se declaran por **resolución objetivo**, no por factor: el 4x de
los modelos es un detalle de implementación.

La resolución objetivo se refiere al **lado corto**, así que funciona igual con
vídeo horizontal y vertical ("1080p" son 1080 líneas en horizontal y 1080
columnas en vertical). Razonar con la altura rechazaba verticales perfectamente
ampliables.

Hay dos modos, elegidos automáticamente y expuestos en `x-vhs-mode`:

- **`upscale`** — el lado corto es menor que el objetivo.
- **`restore`** — el vídeo ya tiene esa resolución o más. En lugar de
  rechazarlo, se reduce y se reconstruye. Es el caso más común de verdad
  (resolución nominal alta sin detalle real) y es donde estos modelos rinden,
  porque se entrenan con entradas degradadas de baja resolución.

La entrada del modelo se limita siempre a `objetivo/4`, de modo que **el coste
depende del objetivo y no de la resolución de origen**. Sin ese techo, un
vertical 1440x2560 pedido a 2160p generaba fotogramas de 5760x10240 y agotaba
la VRAM.

Y nunca se amplía en el post-proceso: si el modelo se queda por debajo del
objetivo se entrega tal cual y se informa en `x-vhs-delivered-resolution`,
porque estirarlo sería fingir una resolución que no existe.

### Configuración

```bash
UPSCALE_ENDPOINT=            # vacío => se deriva de TRANSCRIPTION_ENDPOINT
UPSCALE_API_KEY=             # vacío => se reutiliza TRANSCRIPTION_API_KEY
# id - etiqueta - fps medidos. El fps alimenta el aviso de tiempo de la UI.
UPSCALE_MODELS=upscaler/realesr-compact-x4 - Rápido - 63, flashvsr/FlashVSR-v1.1 - Máxima calidad - 2.1
UPSCALE_SEGMENT_SECONDS=60
```

El `fps` **debe reflejar tu hardware**: el aviso de la interfaz se calcula a
partir de él, así que un número inventado produce una promesa falsa.

### Rendimiento medido (RTX 3090, salida 1080p)

| nivel | fps | VRAM | 10 min de vídeo |
|---|---|---|---|
| Real-ESRGAN Compact | 63 | 51 MiB | ~5 min |
| FlashVSR (difusión) | 2,1 | 19,1 GB | ~2 h 20 min |

Medido de punta a punta a través de VHS: 150 s de vídeo tardaron 85 s (0,57x
el tiempo real) con el nivel rápido.
