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
# oCabra (contrato OpenAI-compatible)
TRANSCRIPTION_ENDPOINT=https://ocabra.mksmad.org/v1
TRANSCRIPTION_API_KEY=sk-...
OCABRA_EXTENSIONS=true
TRANSCRIPTION_MODEL=recomendado-stt
TRANSCRIPTION_MODELS=recomendado-stt - Recomendado (modelo caliente), whisper-large-v3-turbo - Máxima precisión y velocidad, faster-whisper-large-v3 - Alternativa
DIARIZATION_MODEL=recomendado-stt
DIARIZATION_MODELS=recomendado-stt - Recomendado con hablantes, whisper-large-v3-turbo - Máxima calidad con hablantes, faster-whisper-large-v3 - Alternativa con hablantes
TRANSCRIPTION_VAD_FILTER=true
TRANSCRIPTION_CONDITION_ON_PREVIOUS_TEXT=false
OPENAI_TIMEOUT_SECONDS=900
OPENAI_MAX_RETRIES=3
JOB_TTL_SECONDS=86400
SESSION_COOKIE_MAX_AGE=2592000
BATCH_JOB_CONCURRENCY=4
BATCH_POLL_SECONDS=2

# Traducción con LLM (opcional para utilidades como el bot de Telegram)
TRANSLATION_MODEL=recomendado-ha-chatbots
TRANSLATION_REASONING_EFFORT=off
TRANSLATION_MAX_TOKENS=4096
TRANSLATION_BATCH_MAX_TOKENS=512
SUMMARY_MODEL=recomendado-ha-chatbots
SUMMARY_REASONING_EFFORT=off
SUMMARY_MAX_TOKENS=512
# Segmentos por petición y lotes en paralelo (oCabra publica cuatro slots)
TRANSLATION_BATCH_SIZE=8
TRANSLATION_CONCURRENCY=4
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
- En oCabra, `diarize`, `vad_filter` y `condition_on_previous_text` se envían
  como parámetros explícitos; no es necesario duplicar pesos con perfiles
  distintos para activar la identificación de hablantes.

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

El router `recomendado-ha-chatbots` evita fijar un backend y prefiere un modelo
ya cargado. Se usan como máximo cuatro lotes simultáneos, que coincide con los
slots publicados por oCabra; superar esa cifra solo añade cola o respuestas
`429`. La traducción desactiva el razonamiento porque es una transformación
directa, y limita la salida para controlar latencia y consumo.

Un lote mal alineado no se detecta solo contando líneas: una línea de
continuación o un número repetido cuadraban el recuento y colaban texto en el
segmento equivocado. La respuesta tiene que ser exactamente la lista numerada
que se pidió. Los segmentos sin texto (silencios que recorta el VAD, huecos de
la diarización) no se mandan al modelo: pedirle traducir una cadena vacía
devolvía vacío y abortaba el trabajo completo.

Ojo al comparar tiempos entre pruebas: el arranque en frío del modelo son unos
60 s y se los lleva la primera petición de cada proceso, así que domina
cualquier medición que no empiece con el modelo ya caliente. Fijar un modelo
concreto (`gemma4:12b`) lo paga; los routers `recomendado-*` prefieren un
modelo ya cargado precisamente para evitarlo.

Medido con `gemma4:12b` sobre 13,3 minutos de audio (144 segmentos): la
transcripción por lote tarda 29 s y la traducción de los subtítulos 14 s con
lotes de 8. El tamaño del lote apenas mueve el tiempo (15 s con 4, 16 s con 16)
porque el cuello de botella son los cuatro slots y los tokens generados, no el
número de peticiones; a partir de 32 el modelo empieza a reformatear cifras.
`TRANSLATION_MAX_TOKENS` no afecta a la velocidad: entre 320 y 4.096 el tiempo
se mueve en una décima. Lo que sí se nota es el **arranque en frío**, unos 60 s
la primera petición si el modelo no está cargado.

### Texto plano frente a subtítulos

El TXT no lleva marcas de tiempo, así que no hay que preservar la alineación y
se traduce por bloques de texto corrido (`TRANSLATION_PROSE_WORDS`, en paralelo
hasta `TRANSLATION_CONCURRENCY`). El modelo restaura la puntuación y separa
párrafos, que es lo que se espera de un documento; uniendo 144 segmentos con
espacios el resultado arrastraba la falta de puntuación de Whisper. El SRT y el
JSON siguen traduciéndose segmento a segmento.

### Troceado de subtítulos

Whisper devuelve segmentos de hasta 20 segundos. Volcados tal cual, 123 de los
144 subtítulos del vídeo de prueba pasaban de 84 caracteres (el máximo de dos
líneas de 42), con un máximo de 266, y además se emitían en una sola línea: la
traducción al español solo expande 1,06x, el problema era el segmentado. Ahora
se trocean a `SRT_MAX_LINES` x `SRT_MAX_LINE_CHARS` y el tiempo se reparte en
proporción a los caracteres de cada trozo.

El número de líneas se redondea al múltiplo del tamaño del subtítulo para que
no sobre un trozo suelto: repartir 5 líneas en subtítulos de 2 dejaba una sola
línea con una palabra y una fracción mínima de tiempo ("es" durante 0,11 s).
Si pese a eso un subtítulo queda por debajo de `SRT_MIN_CUE_SECONDS` se le roba
tiempo al anterior, siempre que al anterior le quede su propio mínimo. Con
`SRT_SPLIT_CUES=false` se recupera el comportamiento anterior.

#### Cortes por pausa en vez de a prorrata

La transcripción pide `timestamp_granularities: ["segment", "word"]`
(`TRANSCRIPTION_WORD_TIMESTAMPS`), que oCabra también sirve en los lotes: en el
vídeo de prueba devuelve 2.475 palabras con `start` y `end`. Con ellas los
subtítulos se cortan donde el hablante hace una pausa o cierra una frase, y
cada subtítulo arranca y acaba en el tiempo real de su primera y su última
palabra.

Repartir el tiempo del segmento en proporción a los caracteres parecía
suficiente, pero desfasa: medido sobre 276 subtítulos, el inicio se desviaba
0,80 s de mediana, 3,08 s en el percentil 90 y hasta 5,08 s, con un 63% de los
subtítulos desplazados más de medio segundo. Cortando por palabra el desfase es
cero por construcción, y los cortes que caen en un signo de puntuación pasan
del 4% al 10%.

El precio es un CPS algo más alto (percentil 90 de 23,9 frente a 21,4): el
prorrateo repartía también los silencios internos del segmento, así que
inflaba la duración de cada subtítulo y bajaba el CPS a costa de que el texto
no coincidiera con lo que se oye. Para recuperar algo de tiempo de lectura, un
subtítulo se alarga hasta `SRT_CUE_MAX_EXTENSION` segundos dentro del silencio
que le sigue, dejando `SRT_CUE_GAP_SECONDS` de separación.

La re-segmentación se hace **antes** de traducir (`prepare_transcription_payload`),
así que cada subtítulo es también la unidad que se traduce y la alineación no
depende de que el texto en español mida lo mismo que el original. Si el modelo
no devuelve palabras, el payload se queda igual y el SRT cae al troceado por
caracteres.

Para los formatos que se traducen, el corte se hace a `TRANSLATION_EXPANSION`
veces menos caracteres (1,18 por defecto): cortando al tope, la traducción ya
no entraba en dos líneas y había que volver a partirla a prorrata, perdiendo la
sincronía recién ganada. Con margen, los 287 subtítulos del vídeo de prueba
llegan al SRT sin una sola re-partición.

El reparto en líneas se calcula con programación dinámica, probando todos los
puntos de corte y quedándose con el más parejo. Un reparto voraz dejaba una
línea de más —y por tanto un subtítulo huérfano— en los textos que solo caben
en dos líneas si la primera no se llena hasta el tope.

Para revisar modelos, métricas y cambios del servidor consulta
[`/docs/models`](https://ocabra.mksmad.org/docs/models) y
[`/docs/changelog`](https://ocabra.mksmad.org/docs/changelog). El router
`recomendado-stt` elige el mejor perfil disponible sin pagar un arranque en
frío innecesario; actualmente prioriza `whisper-large-v3-turbo`.

### Todo pasa por el panel de trabajos

El portal ya no espera en la petición para nada: descargas, conversiones,
transcripciones, escalado de vídeo y de fotos crean un trabajo y el resultado
se recoge desde «Mis trabajos». Así se puede cerrar la pestaña, se ve en qué
fase va cada cosa y todo se gestiona en el mismo sitio.

Las rutas síncronas (`/api/download`, `/api/ffmpeg/upload`,
`/api/transcribe/upload`) **se mantienen** para scripts y clientes que ya las
usan: devolver `202` en ellas habría roto cualquier integración existente. El
portal usa `/api/jobs/media` y `/api/jobs/upload`, que comparten exactamente la
misma validación que las síncronas.

Hay dos colas separadas: `MEDIA_JOB_CONCURRENCY` (2 por defecto) para descargas
y conversiones, que gastan red y CPU local, y `BATCH_JOB_CONCURRENCY` (4) para
lo que espera a oCabra. El escalado de vídeo tiene la suya en serie, porque ahí
el cuello es la GPU local.

**Cuidado con el resultado de una descarga**: vive en la caché compartida, no en
el directorio del trabajo. Borrar o purgar el trabajo haría `rmtree` del
directorio padre del resultado, es decir, de `CACHE_DIR` entera. Por eso el
trabajo marca `result_external` y entonces solo se limpia su propio directorio.
Hay tests que lo comprueban explícitamente.

### Trabajos persistentes y Batch

Las transcripciones solicitadas desde el portal se crean como trabajos y
responden inmediatamente con `202 Accepted`. VHS asigna al navegador una cookie
anónima `HttpOnly` y muestra únicamente los trabajos que pertenecen a esa
sesión. El usuario puede cerrar la pestaña y volver después para descargar el
resultado.

El pipeline usa la extensión Batch de oCabra en dos fases:

1. El audio se sube a `/v1/files` con `purpose=batch_input_audio` y se procesa
   mediante un lote de `/v1/audio/transcriptions`.
2. Si se pidió traducción, los segmentos se agrupan en lotes de
   `TRANSLATION_BATCH_SIZE` y se envían en un segundo lote a
   `/v1/chat/completions`, correlacionados mediante `custom_id`. Mandar un
   segmento por petición costaba 144 peticiones y 18.700 tokens de entrada en
   un vídeo de 13 minutos (el prompt de sistema repetido 144 veces); agrupados
   son 18 peticiones y 7.400 tokens de entrada. El `custom_id`
   (`group-<posición>-<cuántos>`) codifica el tramo que lleva cada petición,
   así que un trabajo reanudado reconstruye el mapeo sin guardar estado. Si un
   lote vuelve mal alineado se repite segmento a segmento, igual que en la ruta
   síncrona.

oCabra ejecuta **un lote a la vez**: los demás esperan en cola y el campo
`ocabra.queue_position` dice cuántos van delante. `BATCH_JOB_CONCURRENCY` solo
reparte el trabajo local, no multiplica lo que el servidor atiende en paralelo.

Desde oCabra 0.117.0 el objeto `Batch` está tipado en el esquema
(`status` con sus ocho estados, `request_counts`, `output_file_id`,
`error_file_id`, `total_tokens`) y trae la extensión `ocabra`
(`BatchLiveStatus`) mientras el lote no ha terminado, que explica **por qué**
está donde está: `phase` (`queued`, `starting`, `loading_model`, `running`,
`yielding_to_interactive`, `waiting_for_vram`, `finalizing`, `cancelling`),
`detail` como frase lista para una barra de progreso, `in_flight`, `waiting`,
`preemptions`, `retry_in_seconds` y `models[].expected_wait_seconds`. Es la
respuesta a los 65 s que un lote de traducción pasaba en 0 de 144 completadas
sin decir que estaba cargando el modelo.

El portal usa ese estado: `GET /api/jobs/{id}` devuelve `live_phase`, `detail`,
`queue_position`, `expected_wait_seconds` y el par `batch_completed` /
`batch_total`, y la interfaz traduce la fase (el `detail` de oCabra viene en
inglés y se deja como tooltip). Medido contra un modelo descargado, el trabajo
pasa de quedarse en 0% a mostrar «Cargando el modelo · ~36 s» con cuenta atrás.
El estado solo se persiste cuando algo cambia, así que sondear cada
`BATCH_POLL_SECONDS` no reescribe el trabajo en cada vuelta.

Límites del servidor: **50 000 peticiones** por lote (`batch_too_large` si se
pasa, `empty_batch` si no hay ninguna) y **512 MB** por fichero de entrada. Una
línea mal formada hace que `POST /v1/batches` rechace el lote entero con un 400
que lista hasta 20 errores en `error.errors`, cada uno con `line`, `code` y
`message`; esos errores no llegan a `error_file_id`.

Si un lote acaba cancelado o caducado, el trabajo queda en error: el lote ya no
se puede consultar y esperarlo sería esperar para siempre. `POST
/api/jobs/{id}/retry` (botón «Reintentar» en el portal) lo vuelve a encolar
comprobando antes en oCabra qué lotes siguen sirviendo: los que terminaron bien
se conservan y solo se reenvían los que hagan falta, retrocediendo la fase lo
mínimo. Antes la única salida era borrar el trabajo y crearlo de cero.

Los identificadores externos se guardan con el trabajo. Tras reiniciar VHS, el
servidor vuelve a consultar esos lotes en lugar de repetirlos. Cuando termina,
VHS copia el resultado a su almacenamiento y elimina los ficheros remotos de
oCabra. Los resultados locales se conservan `JOB_TTL_SECONDS` (24 horas por
defecto); la cookie puede durar más, pero no prolonga la vida de los archivos.

La cookie no es una cuenta y no permite recuperar trabajos desde otro navegador
o después de borrar sus datos. Nunca contiene rutas, claves API ni resultados.

### Fotos: escalado 4x

oCabra escala la imagen y VHS solo valida y reenvía: no hay que trocear ni
reensamblar nada, porque el worker trabaja por teselas de 512 px y devuelve la
imagen completa. Medido con un fotograma real de 1920x1080 (salida 7680x4320):

| Modelo | Tiempo | PNG |
| --- | --- | --- |
| `real-esrgan-compact-x4` | 6,9 s | 7,56 MB |
| `remacri-x4` | 11,4 s | 5,73 MB |
| `ultrasharp-x4` | 15,9 s | 8,06 MB |
| `ultrasharp-v2-lite` | 16,8 s | 10,92 MB |
| `realesrgan-x4plus` | 18,5 s | 10,25 MB |

El caso límite de 3840x2160 a 4x (132 megapíxeles) tarda 45 s y devuelve un PNG
de 35,6 MB. `output_format=webp` da el mismo resultado pesando catorce veces
menos (0,73 MB frente a 10,25 MB), así que es la opción recomendable para
descargar; PNG queda para quien no quiera pérdida.

Va como trabajo aunque tarde menos de un minuto: así se puede cerrar la pestaña
y recoger el resultado después, y el portal lo muestra en el mismo panel que el
resto. Los límites de entrada (3840x2160 y 25 MB) se comprueban en VHS antes de
subir la imagen, para no gastar el envío en algo que acabaría en un 413.

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

### Cola de trabajos (recomendado)

```
POST   /api/upscale/jobs            → 202 {id, status, estimate_human, queued_ahead}
GET    /api/upscale/jobs            → {jobs: [...]}
GET    /api/upscale/jobs/{id}       → {status, progress, segments_done/total, elapsed_seconds}
GET    /api/upscale/jobs/{id}/download → el vídeo, cuando status == "done"
DELETE /api/upscale/jobs/{id}       → pide cancelación
```

Los trabajos se ejecutan **de uno en uno**: el cuello es la GPU y lanzar dos en
paralelo solo hace que se pisen la VRAM. Los metadatos se persisten en disco,
así que un reinicio no pierde un resultado ya calculado; un trabajo que estaba
en curso al reiniciar se marca como error, porque no es reanudable. La
cancelación se aplica **entre segmentos**: matar un proceso de GPU a mitad es
peor que esperar a que acabe el segmento en curso. Los terminados se conservan
24 h (`JOB_TTL_SECONDS`).

### Vía síncrona

`POST /api/upscale/upload` (multipart) sube un vídeo y lo devuelve escalado en
la misma petición. Sirve para el nivel rápido y clips cortos; con el de
difusión (~20x el tiempo real) usa la cola.

`GET /api/upscale/models` lista los modelos con su aviso de tiempo y su
**tope de resolución**.

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

Esto pasa de forma habitual con FlashVSR: recorta a múltiplos de 128, así que
pidiendo 1080p entrega 1024 de lado corto. Es intencionado — mejor 1024 real
que 1080 estirado.

### Configuración

```bash
UPSCALE_ENDPOINT=            # vacío => se deriva de TRANSCRIPTION_ENDPOINT
UPSCALE_API_KEY=             # vacío => se reutiliza TRANSCRIPTION_API_KEY
# id - etiqueta - fps medidos. El fps alimenta el aviso de tiempo de la UI.
# id - etiqueta - fps medidos - lado corto máximo (0 = sin límite)
UPSCALE_MODELS=real-esrgan-compact-x4 - Rápido - 63 - 0, flashvsr-v1-1 - Máxima calidad - 2.1 - 1080
UPSCALE_SEGMENT_SECONDS=60
```

El `fps` **debe reflejar tu hardware**: el aviso de la interfaz se calcula a
partir de él, así que un número inventado produce una promesa falsa.

### Rendimiento medido (RTX 3090, salida 1080p)

| nivel | fps | VRAM | 10 min de vídeo | calidad |
|---|---|---|---|---|
| Real-ESRGAN Compact | 53 | 394 MiB | ~6 min | limpia bordes, no añade textura |
| 4x UltraSharpV2 Lite | 12,9 | 789 MiB | ~23 min | detalle claramente mayor |
| FlashVSR (difusión) | 2,1 | 19,1 GB | ~2 h 23 min | máxima textura |

Los tres medidos en una RTX 3090 con salida 1080p. Compact está limitado por
la tubería, no por la GPU, así que su cifra apenas cambia entre tarjetas; los
otros dos sí son sensibles a ella. **x4plus rinde 2,53 fps en la 3060**, donde
deja de tener sentido frente a FlashVSR, así que conviene fijarlo a la GPU
grande con `preferred_gpu` en oCabra.

Ambos verificados de punta a punta a través de oCabra. Compact: 150 s de vídeo
en 97 s (0,65x el tiempo real). FlashVSR: 6 s en 88 s (~15x el tiempo real),
que es la razón de que necesite una cola y no encaje en la UI síncrona para
material largo.

Medido de punta a punta a través de VHS: 150 s de vídeo tardaron 85 s (0,57x
el tiempo real) con el nivel rápido.

### Topes de resolución por modelo

El cuarto campo de `UPSCALE_MODELS` es el lado corto máximo que el modelo
aguanta en esta máquina. Existe porque los modelos de difusión tienen un techo
de VRAM real: **FlashVSR gasta 19,1 GB para 1080p**, y 1440p son 1,78x los
píxeles, así que no entra en una tarjeta de 24 GB. Sin declararlo, el usuario
elegía 1440p y se enteraba del fallo tras minutos de GPU; ahora se rechaza al
instante y el mensaje sugiere el otro modelo.

### Elegir pesos: el nivel intermedio es intercambiable

Todos los modelos RRDBNet cuestan **exactamente lo mismo** (5,9 fps y 4,6 GB
medidos), así que elegir entre ellos es gratis y es solo una cuestión de
gusto. Medido sobre el mismo clip:

| pesos | SSIM | detalle observado |
|---|---|---|
| `4x-UltraSharp` (actual) | 0,896 | el más nítido: líneas finas muy separadas, grano en superficies |
| `4x_NMKD-Siax_200k` | 0,895 | prácticamente idéntico a UltraSharp |
| `4x_foolhardy_Remacri` | 0,911 | algo más suave, el más natural |
| `RealESRGAN_x4plus` | 0,901 | el oficial, referencia |
| `4x_NMKD-Superscale` | 0,861 | **descartado**: inventa una textura de lienzo cruzado |

Cambiar de pesos es registrar el `.pth` en oCabra y editar `UPSCALE_MODELS`. El
worker detecta la arquitectura por las claves del `state_dict`, incluido el
nombrado ESRGAN antiguo que usa casi todo el catálogo de la comunidad, así que
no hay que tocar código.

Ojo con el SSIM: penaliza a los modelos que añaden detalle, porque el detalle
sintetizado no coincide píxel a píxel. Remacri gana en SSIM siendo el más
conservador. Para esta decisión hay que mirar las imágenes.

### Arquitecturas: PLKSR gana, DAT queda descartado

El worker carga los pesos con **spandrel**, que reconoce 42 arquitecturas y
detecta la correcta sola. Medido sobre el mismo clip (RTX 3060, para poder
comparar entre sí; la 3090 estaba ocupada):

| pesos | arquitectura | precisión | fps | VRAM | SSIM |
|---|---|---|---|---|---|
| `4x-UltraSharpV2_Lite` | **PLKSR** | fp16 | **6,06** | **789 MiB** | **0,917** |
| `4x-UltraSharp` | ESRGAN | fp16 | 2,48 | 4,1 GB | 0,896 |
| `4x-UltraSharpV2` | DAT | fp32 | 0,30 | 4,3 GB | 0,914 |
| `4xNomos2_hq_dat2` | DAT | fp32 | 0,30 | 4,3 GB | — |

**PLKSR gana en las tres dimensiones a la vez**: 2,4x más rápido que ESRGAN,
5x menos VRAM y mejor SSIM. Con 789 MiB entra en la 3060 y deja la grande
libre.

**DAT queda descartado**: 0,30 fps es 8x más lento que ESRGAN y más lento que
un modelo de difusión. La causa es que **solo funciona en fp32** (spandrel lo
declara con `supports_half=False`), y un transformer sin fp16 pierde los
tensor cores. Su calidad no compensa: está a la par con la variante Lite, que
es 20 veces más rápida.

Dos consecuencias de usar spandrel que conviene no perder: la precisión la
decide el modelo (forzar fp16 en DAT rompería la salida) y **la escala también**,
así que se pueden usar pesos 2x u 8x sin tocar código.
