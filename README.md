# VHS · Video Harvester Service

**Versión**: 0.1.26

Servicio FastAPI que descarga, convierte y transcribe vídeos o audios mediante `yt-dlp` y perfiles rápidos de `ffmpeg`. Este directorio está listo para vivir como repositorio independiente y generar su propia imagen de Docker.

## Requisitos

- Python 3.11+
- `ffmpeg` disponible en el sistema

## Variables de entorno

Copia `example.env` a `.env` y ajusta las rutas o claves necesarias:

```bash
cp example.env .env
```

Las variables más relevantes son `CACHE_DIR`, `USAGE_LOG_PATH`, las opciones de `TRANSCRIPTION_*` y `WHISPER_ASR_*`.

Para evitar bloqueos de YouTube es posible ajustar:

- `YTDLP_USER_AGENT`: agente de usuario enviado a YouTube.
- `YTDLP_BOT_PROTECTION_RETRIES`: número de intentos con agentes nuevos ante un desafío de inicio de sesión (por defecto, 3).
- `YTDLP_BOT_PROTECTION_DELAY`: segundos de espera entre intentos (por defecto, 6).
- `YTDLP_EXTRACTOR_ARGS`: argumentos adicionales para yt-dlp en formato JSON. Por defecto se usa `{ "youtube": ["player_client=default"] }` para evitar depender de un runtime de JavaScript.

## Diarización y traducción con whisper-asr

Configura `WHISPER_ASR_URL` para habilitar las variantes de diarización (`transcript_diarized_json`, `transcript_diarized_text`)
y las traducciones al español (`transcript_translate_*` y `transcript_translate_diarized_*`) disponibles en `/api/download`
o `/api/transcribe/upload`. Los resultados incluirán etiquetas de hablante siempre que el endpoint whisper-asr devuelva esa información.

## Ejecución local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn vhs.main:app --reload --host 0.0.0.0 --port 8601
```

## Construcción de la imagen

Para generar una imagen dedicada a VHS sin depender de repositorios previos:

```bash
docker build -t ghcr.io/successbyfailure/vhs:latest -f Dockerfile .
docker run --env-file .env -p 8601:8601 ghcr.io/successbyfailure/vhs:latest
```

El contenedor expone `/api/health`, `/api/probe`, `/api/download`, `/api/cache`, `/api/transcribe/upload` y `/api/ffmpeg/upload`.

### Imagen con soporte NVIDIA (GPU)

Para acelerar ffmpeg con NVENC se incluye `Dockerfile.gpu` y una imagen específica:

```bash
docker build -t ghcr.io/successbyfailure/vhs-gpu:latest -f Dockerfile.gpu .
docker run --gpus all --env-file .env -p 8602:8601 ghcr.io/successbyfailure/vhs-gpu:latest
```

La variable `FFMPEG_ENABLE_NVENC=1` se activa por defecto en esta imagen. Asegúrate de usar `--gpus` (o el runtime NVIDIA) y que el host tenga drivers recientes.

## Ejecución con Docker Compose

El repositorio incluye un `docker-compose.yml` que prepara volúmenes separados para
la configuración y la caché del servicio dentro del propio directorio de trabajo. Antes de levantar el
stack, crea tu fichero `.env` (puedes partir de `example.env`). El servicio `env_sync`
monta el `.env` local y añade automáticamente nuevas variables que aparezcan en
`example.env`, manteniendo intactos los valores existentes.

```bash
docker compose up -d
```

El servicio quedará disponible en `http://localhost:8601` y mantendrá los datos en
los directorios locales `./config` (por ejemplo, para `YTDLP_COOKIES_FILE` en `/config`) y
`./data` (caché y logs en `/app/data`). Un contenedor `watchtower` se encarga de
actualizar solo el servicio `vhs` cuando aparezcan nuevas imágenes.
Para GPU, usa el servicio `vhs-gpu` del compose (`--gpus all`) que expone `http://localhost:8602` y habilita NVENC.

## Integración continua

Un flujo de GitHub Actions construye la imagen Docker en cada pull request y la publica en GHCR (`ghcr.io/<owner>/vhs`) al
hacer push a `main`. Esto garantiza que el servicio pueda desplegarse de forma independiente del repositorio original.

## Ficheros clave

- `vhs/main.py`: aplicación FastAPI principal.
- `templates/`: vistas HTML (`/` y `/docs/api`).
- `assets/`: recursos estáticos utilizados por las plantillas.
- `versions.json`: versión publicada del servicio.
