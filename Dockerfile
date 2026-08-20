FROM python:3.13-slim

# DENO_DIR apunta a /tmp porque el contenedor corre como UID 1000 y Deno
# necesita un directorio de caché escribible.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DENO_INSTALL=/usr/local \
    DENO_DIR=/tmp/deno

WORKDIR /app

# Deno es el runtime de JavaScript que yt-dlp usa para resolver los desafíos
# de YouTube (EJS). Sustituye al paquete nodejs de Debian, demasiado antiguo
# para esta tarea.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        curl \
        unzip \
    && update-ca-certificates \
    && curl -fsSL https://deno.land/install.sh | sh -s -- -y --no-modify-path \
    && deno --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8601

CMD ["uvicorn", "vhs.main:app", "--host", "0.0.0.0", "--port", "8601"]
