import asyncio
import http.server
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("fast_bot")

# --- CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO")
# Recomendado: define ADMIN_PASS como variable de entorno en Render (este valor es solo de respaldo)
ADMIN_PASS = os.getenv("ADMIN_PASS", "Carlos13mar")

if not TOKEN or not MONGO_URI:
    raise ValueError("Faltan variables de entorno requeridas (TOKEN, MONGO)")

# --- BASE DE DATOS (una por token) ---
db_name = f"fast_response_bot_db_{TOKEN.split(':')[0]}"
client = MongoClient(MONGO_URI, maxPoolSize=100, serverSelectionTimeoutMS=15000)
db = client[db_name]

users_collection = db["users"]
settings_collection = db["settings"]
groups_collection = db["groups"]

if settings_collection.count_documents({}) == 0:
    settings_collection.insert_one({
        "welcome_msg": "👋 ¡Hola! Para obtener el enlace de acceso al grupo, debes enviar un total de {req} aportes (imágenes, videos o archivos).",
        "acceptance_msg": "🎉 ¡Felicidades! Has completado el mínimo de aportes. Aquí tienes tu enlace de acceso:",
        "required_contributions": 150,
        "selected_group_id": None,
    })

try:  # evita documentos duplicados por usuario y permite upserts atómicos
    users_collection.create_index("user_id", unique=True)
except Exception as e:
    log.warning("No se pudo crear el índice único de users (¿hay duplicados?): %s", e)

# --- CACHÉ EN MEMORIA (evita consultar la BD en cada mensaje) ---
SETTINGS = settings_collection.find_one({}) or {}
completed_ids = {u["user_id"] for u in users_collection.find({"completed": True}, {"user_id": 1})}
admin_input_states = {}


async def get_settings():
    return SETTINGS


async def set_setting(**kv):
    SETTINGS.update(kv)  # la caché se actualiza al instante
    await asyncio.to_thread(settings_collection.update_one, {}, {"$set": kv})


async def get_group(group_id):
    return await asyncio.to_thread(groups_collection.find_one, {"group_id": group_id})


async def get_all_groups():
    return await asyncio.to_thread(lambda: list(groups_collection.find({})))


async def upsert_group(group_id, data):
    await asyncio.to_thread(groups_collection.update_one, {"group_id": group_id}, data, upsert=True)


# --- SERVIDOR WEB PARA RENDER Y CRONJOB ---
class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Fast Response Bot is active!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    # ThreadingHTTPServer: un ping lento ya no bloquea a los demás
    http.server.ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


# --- PANEL DE ADMINISTRACIÓN ---
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = SETTINGS
    selected_gid = settings.get("selected_group_id")

    group_name = "Ninguno seleccionado"
    if selected_gid:
        grp = await get_group(selected_gid)
        if grp:
            group_name = grp.get("title", f"ID: {selected_gid}")

    text = (
        "⚙️ **Panel de Administración (Respuesta Rápida - Ultra Rápido)**\n\n"
        f"👋 **Mensaje de Bienvenida:**\n`{settings.get('welcome_msg')}`\n\n"
        f"📦 **Aportes Requeridos:** `{settings.get('required_contributions')}`\n"
        f"📢 **Grupo Seleccionado:** `{group_name}`\n"
        f"📩 **Mensaje de Aceptación:**\n`{settings.get('acceptance_msg')}`\n"
    )

    keyboard = [
        [InlineKeyboardButton("👋 Editar Bienvenida", callback_data="set_welcome")],
        [InlineKeyboardButton("📦 Editar Aportes", callback_data="set_req_aportes")],
        [InlineKeyboardButton("📢 Seleccionar Grupo", callback_data="select_group")],
        [InlineKeyboardButton("📩 Editar Aceptación", callback_data="set_acceptance")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "set_welcome":
        admin_input_states[user_id] = "waiting_welcome"
        await query.message.reply_text("Envía el nuevo Mensaje de Bienvenida (puedes usar {req} para mostrar el número de aportes):")

    elif data == "set_req_aportes":
        admin_input_states[user_id] = "waiting_req_aportes"
        await query.message.reply_text("Envía la cantidad mínima de aportes requeridos (ej. 150):")

    elif data == "set_acceptance":
        admin_input_states[user_id] = "waiting_acceptance"
        await query.message.reply_text("Envía el nuevo Mensaje de Aceptación:")

    elif data == "select_group":
        groups = await get_all_groups()
        if not groups:
            await query.message.reply_text("⚠️ No hay grupos registrados. Usa el comando /reload dentro del grupo donde el bot sea admin.")
            return

        keyboard = [[InlineKeyboardButton(f"📢 {g.get('title', 'Sin título')}",
                                          callback_data=f"set_grp_{g['group_id']}")] for g in groups]
        keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="back_to_panel")])
        await query.message.reply_text("Selecciona el grupo donde se enviará el enlace de invitación:",
                                       reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("set_grp_"):
        target_gid = int(data[len("set_grp_"):])  # los IDs de grupo son negativos: no usar split("_")
        await set_setting(selected_group_id=target_gid)
        await query.message.reply_text("✅ Grupo actualizado correctamente.")
        await show_admin_panel(update, context)

    elif data == "back_to_panel":
        await show_admin_panel(update, context)


# --- COMANDO /reload EN GRUPOS ---
async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("⚠️ Este comando debe ejecutarse dentro de un grupo.")
        return

    await upsert_group(chat.id, {"$set": {"title": chat.title, "updated_at": datetime.utcnow()}})
    await update.message.reply_text("✅ Grupo registrado y habilitado para el bot de respuesta rápida.")


# --- COMANDO /start ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    welcome_text = SETTINGS.get("welcome_msg", "").replace(
        "{req}", str(SETTINGS.get("required_contributions", 150)))

    sent_msg = await update.message.reply_text(welcome_text)
    try:
        await sent_msg.pin()
    except Exception:
        pass


# --- MENSAJES Y APORTES ---
async def handle_private_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return

    user = message.from_user
    user_id = user.id

    # 1. Contraseña de admin
    if message.text and message.text.strip() == ADMIN_PASS:
        await show_admin_panel(update, context)
        return

    # 2. El admin está ingresando una configuración
    state = admin_input_states.get(user_id)
    if state:
        text = message.text
        if not text:
            await message.reply_text("⚠️ Envía el valor como texto.")
            return
        if state == "waiting_welcome":
            await set_setting(welcome_msg=text)
            await message.reply_text("✅ Mensaje de bienvenida actualizado.")
        elif state == "waiting_req_aportes":
            try:
                val = int(text.strip())
                if val < 1:
                    raise ValueError
            except ValueError:
                await message.reply_text("⚠️ Por favor ingresa un número entero válido.")
                return
            await set_setting(required_contributions=val)
            await message.reply_text(f"✅ Aportes requeridos actualizados a {val}.")
        elif state == "waiting_acceptance":
            await set_setting(acceptance_msg=text)
            await message.reply_text("✅ Mensaje de aceptación actualizado.")
        admin_input_states.pop(user_id, None)
        return

    # 3. Aportes multimedia
    if not (message.photo or message.video or message.document):
        return
    if user_id in completed_ids:  # sin consultar la BD
        return

    # Incremento atómico: varios aportes simultáneos del mismo usuario no se pisan
    try:
        doc = await asyncio.to_thread(
            lambda: users_collection.find_one_and_update(
                {"user_id": user_id, "completed": {"$ne": True}},
                {"$inc": {"contributions": 1},
                 "$set": {"name": user.first_name, "username": user.username}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        )
    except DuplicateKeyError:  # ya estaba completado
        completed_ids.add(user_id)
        return

    required = SETTINGS.get("required_contributions", 150)
    if doc["contributions"] < required:
        return

    selected_gid = SETTINGS.get("selected_group_id")
    if not selected_gid:
        if doc["contributions"] == required:
            await message.reply_text("🎉 ¡Has alcanzado el número de aportes! Sin embargo, el administrador aún no ha seleccionado el grupo de destino. Te daré tu enlace en tu próximo aporte una vez lo configure.")
        return

    # Solo UN manejador puede marcar como completado (evita enviar el enlace dos veces)
    res = await asyncio.to_thread(
        users_collection.update_one,
        {"user_id": user_id, "completed": {"$ne": True}},
        {"$set": {"completed": True, "completed_at": datetime.utcnow()}},
    )
    if res.modified_count != 1:
        return
    completed_ids.add(user_id)

    try:
        invite = await context.bot.create_chat_invite_link(chat_id=selected_gid, creates_join_request=True)
        sent_msg = await message.reply_text(f"{SETTINGS.get('acceptance_msg', '')}\n\n🔗 {invite.invite_link}")
        try:
            await sent_msg.pin()
        except Exception:
            pass
    except Exception:
        log.exception("Error generando el enlace para %s", user_id)
        # Revertir para que se reintente con su próximo aporte
        completed_ids.discard(user_id)
        await asyncio.to_thread(users_collection.update_one, {"user_id": user_id},
                                {"$set": {"completed": False}})
        await message.reply_text("🎉 ¡Has completado la meta! Ocurrió un problema al generar tu enlace. "
                                 "Envía otro aporte para reintentar o notifica al administrador.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error no controlado", exc_info=context.error)


async def post_init(app):
    # Más hilos para MongoDB: con muchos usuarios a la vez no se hace cola
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))


# --- FUNCIÓN PRINCIPAL ---
def main():
    threading.Thread(target=run_web_server, daemon=True).start()

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)          # CLAVE: procesa todos los mensajes a la vez
        .connection_pool_size(256)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("reload", reload_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_messages)
    )
    application.add_error_handler(on_error)

    print("🤖 Bot de Respuesta Rápida (Optimizado para Concurrencia) iniciado...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
