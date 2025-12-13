import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from vhs.main import (
    CACHE_DIR,
    FFMPEG_PRESETS,
    YTDLP_CACHE_DIR,
    download_media,
    ensure_storage_ready,
    extract_audio_profile_from_file,
    generate_transcription_file,
    transcribe_audio_file,
    translate_transcription_payload,
    render_transcription_payload,
)
from openai import OpenAI


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_AUTH_FILE = Path(os.getenv("TELEGRAM_AUTH_FILE", "data/telegram_auth.json"))
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o-mini")

MENU_OPTIONS = ["Descargar", "Transcribir", "Traducir", "Resumir"]


def load_auth() -> Dict[str, Any]:
    if TELEGRAM_AUTH_FILE.exists():
        try:
            return json.loads(TELEGRAM_AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"admin_id": None, "allowed": []}


def save_auth(data: Dict[str, Any]) -> None:
    TELEGRAM_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    TELEGRAM_AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def ensure_admin(update: Update) -> None:
    auth = load_auth()
    user = update.effective_user
    if auth.get("admin_id") is None and user:
        auth["admin_id"] = user.id
        auth.setdefault("allowed", []).append(user.id)
        save_auth(auth)


def is_authorized(user_id: int) -> bool:
    auth = load_auth()
    if auth.get("admin_id") is None:
        return True
    return user_id == auth.get("admin_id") or user_id in auth.get("allowed", [])


async def notify_admin_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    auth = load_auth()
    admin_id = auth.get("admin_id")
    user = update.effective_user
    if not admin_id or not user:
        return
    button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Autorizar", callback_data=f"approve:{user.id}")]]
    )
    await context.bot.send_message(
        chat_id=admin_id,
        text=f"Solicitud de acceso: {user.full_name} (id={user.id})",
        reply_markup=button,
    )


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    if not query.data.startswith("approve:"):
        return
    auth = load_auth()
    if query.from_user.id != auth.get("admin_id"):
        await query.edit_message_text("Solo el admin puede autorizar.")
        return
    try:
        user_id = int(query.data.split(":")[1])
    except ValueError:
        return
    auth.setdefault("allowed", [])
    if user_id not in auth["allowed"]:
        auth["allowed"].append(user_id)
        save_auth(auth)
    await query.edit_message_text(f"Autorizado el usuario {user_id}.")
    try:
        await context.bot.send_message(chat_id=user_id, text="Acceso concedido. Envía una URL o un archivo.")
    except Exception:
        pass


async def summarize_text(text: str) -> str:
    api_key = os.getenv("TRANSCRIPTION_API_KEY")
    if not api_key:
        return "No hay API key configurada para resumir."
    client = OpenAI(api_key=api_key)
    prompt = (
        "Resume en español el siguiente contenido en 3-4 frases claras:\n\n"
        f"{text[:6000]}"
    )
    completion = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return completion.choices[0].message.content.strip()


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: Dict[str, Any]) -> None:
    context.user_data["payload"] = payload
    keyboard = ReplyKeyboardMarkup([[opt] for opt in MENU_OPTIONS], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("¿Qué quieres hacer?", reply_markup=keyboard)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_admin(update)
    await update.message.reply_text("Envía una URL de video/audio o sube un archivo para empezar.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user = update.effective_user
    if not user:
        return
    await ensure_admin(update)
    if not is_authorized(user.id):
        await update.message.reply_text("Estás en lista de espera. El admin revisará tu solicitud.")
        await notify_admin_for_approval(update, context)
        return

    text = (update.message.text or "").strip()
    if text in MENU_OPTIONS:
        await process_action(update, context, text)
        return

    if text.startswith("http://") or text.startswith("https://"):
        await send_menu(update, context, {"type": "url", "value": text})
        return

    file = update.message.effective_attachment
    if file and hasattr(file, "get_file"):
        tg_file = await file.get_file()
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(tg_file.file_path).suffix or ".bin") as tmp:
            await tg_file.download_to_drive(custom_path=tmp.name)
            path = Path(tmp.name)
        await send_menu(update, context, {"type": "file", "value": str(path)})
        return

    await update.message.reply_text("Envía una URL o un archivo para continuar.")


async def process_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    payload = context.user_data.get("payload")
    if not payload:
        await update.message.reply_text("Primero envía una URL o archivo.", reply_markup=ReplyKeyboardRemove())
        return
    await update.message.reply_text(f"Procesando {action.lower()}…", reply_markup=ReplyKeyboardRemove())
    try:
        if action == "Descargar":
            await handle_download(update, context, payload)
        elif action == "Transcribir":
            await handle_transcribe(update, context, payload, translate=False, summarize=False)
        elif action == "Traducir":
            await handle_transcribe(update, context, payload, translate=True, summarize=False)
        elif action == "Resumir":
            await handle_transcribe(update, context, payload, translate=False, summarize=True)
    finally:
        context.user_data["payload"] = None


async def handle_download(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: Dict[str, Any]) -> None:
    def _download() -> Path:
        ensure_storage_ready()
        if payload["type"] == "url":
            file_path, _ = download_media(payload["value"], "video_1080")
            return file_path
        file_path = Path(payload["value"])
        if not file_path.exists():
            raise RuntimeError("El archivo ya no está disponible.")
        # reutilizar presets ffmpeg_1080p si el archivo es video
        preset_key = "ffmpeg_1080p" if "ffmpeg_1080p" in FFMPEG_PRESETS else None
        if preset_key:
            return file_path
        return file_path

    path = await asyncio.to_thread(_download)
    try:
        await update.message.reply_document(document=path.open("rb"), filename=path.name)
    finally:
        if payload["type"] == "file":
            try:
                Path(payload["value"]).unlink(missing_ok=True)
            except OSError:
                pass


async def handle_transcribe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    payload: Dict[str, Any],
    translate: bool,
    summarize: bool,
) -> None:
    def _transcribe() -> Path:
        ensure_storage_ready()
        if payload["type"] == "url":
            fmt = "transcript_translate_text" if translate else "transcript_text"
            file_path, _ = generate_transcription_file(payload["value"], fmt)
            return file_path
        source_path = Path(payload["value"])
        audio_path = extract_audio_profile_from_file(source_path, "audio_med")
        transcript = transcribe_audio_file(audio_path, "transcript_translate_text" if translate else "transcript_text")
        text = render_transcription_payload(transcript, "transcript_text" if not translate else "transcript_translate_text")
        out = CACHE_DIR / f"telebot_{source_path.stem}.txt"
        out.write_text(text, encoding="utf-8")
        return out

    file_path = await asyncio.to_thread(_transcribe)
    summary_text = None
    if summarize:
        summary_text = await summarize_text(file_path.read_text(encoding="utf-8"))
    if summarize and summary_text:
        await update.message.reply_text(summary_text)
    await update.message.reply_document(document=file_path.open("rb"), filename=file_path.name)
    if payload["type"] == "file":
        try:
            Path(payload["value"]).unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Configura TELEGRAM_BOT_TOKEN para iniciar el bot de Telegram.")
    ensure_storage_ready()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CallbackQueryHandler(handle_approval_callback))
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
