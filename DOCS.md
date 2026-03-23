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
TRANSCRIPTION_MODEL=openai/whisper-large-v3-turbo
TRANSCRIPTION_MODELS=openai/whisper-large-v3-turbo - best, nvidia/parakeet-tdt-0.6b-v3 - fast, nekusu/faster-whisper-large-v3-turbo-latam-int8-ct2 - Español

# Whisper-ASR para diarización y traducción
WHISPER_ASR_URL=http://localhost:9900
WHISPER_ASR_TIMEOUT=600

# Traducción con LLM
TRANSLATION_MODEL=gpt-4o-mini
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
- Providers: OpenAI-compatible, whisper-asr
- Formatos: JSON (completo), SRT (subtítulos), TXT (texto plano)
- Word-level timestamps y scores de confianza
- Fallback automático entre providers

### ✅ Traducción al Español
- Motor: LLM configurable (gpt-4o-mini, mistral:7b, etc.)
- Traducción segmento por segmento
- Preserva timestamps para formato SRT
- Prompts personalizables

### ✅ Diarización (Identificación de Hablantes)
- Via whisper-asr
- Etiquetas: SPEAKER_00, SPEAKER_01, etc.
- Word-level speaker attribution
- Disponible en JSON y texto

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

# Traducir a español (SRT)
curl -X POST http://localhost:8601/api/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=...", "media_format": "transcript_translate_srt"}'
```

---

## 🔄 Changelog

### v0.2.9 (2026-01-10)
- ✅ Corregida detección de formatos de transcripción
- ✅ `transcript_translate_json` ahora genera archivos `.json` (antes `.txt`)
- ✅ `transcript_translate_srt` ahora genera formato SRT válido (antes texto plano)
- ✅ `transcript_diarized_json` ahora usa extensión `.json` (antes `.txt`)
- ✅ Todos los formatos generan Content-Type HTTP correcto
- 📝 Documentación actualizada y consolidada

### Versiones anteriores
Ver commits en el repositorio para historial completo.

---

## 🆘 Soporte y Troubleshooting

### Problemas Comunes

**Error de transcripción**: Verificar que `TRANSCRIPTION_API_KEY` esté configurado
**Diarización no funciona**: Asegurar que `WHISPER_ASR_URL` apunte a instancia whisper-asr
**Traducción falla**: Verificar que `TRANSLATION_MODEL` sea compatible con chat (no whisper)
**YouTube bloquea descargas**: Ajustar `YTDLP_USER_AGENT` y `YTDLP_EXTRACTOR_ARGS`

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
