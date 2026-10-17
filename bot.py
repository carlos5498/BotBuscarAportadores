import asyncio
from datetime import datetime
import http.server
import os
import threading
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from pymongo import MongoClient

# --- CONFIGURACIÓN DE VARIABLES DE ENTORNO ---
TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO")

if not TOKEN or not MONGO_URI:
    raise ValueError("Faltan variables de entorno requeridas (TOKEN, MONGO)")

# --- CONFIGURACIÓN DE BASE DE DATOS MONGODB ---
db_name = f"fast_response_bot_db_{TOKEN.split(':')[0]}"
client = MongoClient(MONGO_URI)
db = client[db_name]

users_collection = db["users"]
settings_collection = db["settings"]
groups_collection = db["groups"]

# Inicializar configuración por defecto si no existe
if settings_collection.count_documents({}) == 0:
    settings_collection.insert_one({
        "welcome_msg": "👋 ¡Hola! Para obtener el enlace de acceso al grupo, debes enviar un total de {req} aportes (imágenes, videos o archivos).",
        "acceptance_msg": "🎉 ¡Felicidades! Has completado el mínimo de aportes. Aquí tienes tu enlace de acceso:",
        "required_contributions": 150,
        "selected_group_id": None,
    })

# Diccionario en memoria para rastrear estados de entrada del admin
admin_input_states = {}


def get_settings():
    return settings_collection.find_one({})


# --- SERVIDOR WEB PARA RENDER Y CRONJOB ---
class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Fast Response Bot is active!")

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = http.server.HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- PANEL DE ADMINISTRACIÓN ---
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings()
    selected_gid = settings.get("selected_group_id")
    
    group_name = "Ninguno seleccionado"
    if selected_gid:
        grp = groups_collection.find_one({"group_id": selected_gid})
        if grp:
            group_name = grp.get("title", f"ID: {selected_gid}")

    text = (
        "⚙️ **Panel de Administración (Respuesta Rápida)**\n\n"
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
        await query.message.reply_text("Envía el nuevo **Mensaje de Bienvenida** (puedes usar `{req}` para mostrar el número de aportes):")

    elif data == "set_req_aportes":
        admin_input_states[user_id] = "waiting_req_aportes"
        await query.message.reply_text("Envía la cantidad mínima de aportes requeridos (ej. 150):")

    elif data == "set_acceptance":
        admin_input_states[user_id] = "waiting_acceptance"
        await query.message.reply_text("Envía el nuevo **Mensaje de Aceptación**:")

    elif data == "select_group":
        groups = list(groups_collection.find({}))
        if not groups:
            await query.message.reply_text("⚠️ No hay grupos registrados. Usa el comando `/reload` dentro del grupo donde el bot sea admin.")
            return

        keyboard = []
        for g in groups:
            btn_text = f"📢 {g.get('title', 'Sin título')}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"set_grp_{g['group_id']}")])

        keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="back_to_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Selecciona el grupo donde se enviará el enlace de invitación:", reply_markup=reply_markup)

    elif data.startswith("set_grp_"):
        target_gid = int(data.split("_")[2])
        settings_collection.update_one({}, {"$set": {"selected_group_id": target_gid}})
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

    groups_collection.update_one(
        {"group_id": chat.id},
        {
            "$set": {
                "title": chat.title,
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )
    await update.message.reply_text("✅ Grupo registrado y habilitado para el bot de respuesta rápida.")


# --- COMANDO /start ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != "private":
        return

    settings = get_settings()
    welcome_template = settings.get("welcome_msg", "")
    req = settings.get("required_contributions", 150)
    
    welcome_text = welcome_template.replace("{req}", str(req))
    await update.message.reply_text(welcome_text)


# --- MANEJADOR PRINCIPAL DE MENSAJES Y APORTES ---
async def handle_private_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return

    user = message.from_user
    user_id = user.id

    # 1. Verificar si el usuario intenta ingresar la contraseña de admin
    if message.text and message.text.strip() == "Carlos13mar":
        await show_admin_panel(update, context)
        return

    # 2. Verificar si el admin está en proceso de ingresar alguna configuración
    if user_id in admin_input_states:
        state = admin_input_states[user_id]
        if state == "waiting_welcome":
            settings_collection.update_one({}, {"$set": {"welcome_msg": message.text}})
            await message.reply_text("✅ Mensaje de bienvenida actualizado.")
        elif state == "waiting_req_aportes":
            try:
                val = int(message.text)
                settings_collection.update_one({}, {"$set": {"required_contributions": val}})
                await message.reply_text(f"✅ Aportes requeridos actualizados a `{val}`.", parse_mode="Markdown")
            except ValueError:
                await message.reply_text("⚠️ Por favor ingresa un número entero válido.")
                return
        elif state == "waiting_acceptance":
            settings_collection.update_one({}, {"$set": {"acceptance_msg": message.text}})
            await message.reply_text("✅ Mensaje de aceptación actualizado.")

        del admin_input_states[user_id]
        return

    # 3. Procesar aportes multimedia del usuario
    media_count = 0
    if message.photo:
        media_count = 1
    elif message.video:
        media_count = 1
    elif message.document:
        media_count = 1

    if media_count == 0:
        return

    db_user = users_collection.find_one({"user_id": user_id})
    current_contributions = db_user.get("contributions", 0) if db_user else 0
    completed = db_user.get("completed", False) if db_user else False

    if completed:
        return  # Si ya completó la meta anteriormente, no procesar más

    new_contributions = current_contributions + media_count
    settings = get_settings()
    required = settings.get("required_contributions", 150)

    if new_contributions >= required:
        # Registrar como completado
        users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "contributions": new_contributions,
                    "completed": True,
                    "name": user.first_name,
                    "username": user.username,
                    "completed_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

        selected_gid = settings.get("selected_group_id")
        if not selected_gid:
            await message.reply_text("🎉 ¡Has alcanzado el número de aportes! Sin embargo, el administrador aún no ha seleccionado el grupo de destino.")
            return

        # Generar enlace de invitación con solicitud de aprobación requerida
        try:
            invite_link_obj = await context.bot.create_chat_invite_link(
                chat_id=selected_gid,
                creates_join_request=True,
            )
            invite_link = invite_link_obj.invite_link

            acceptance_txt = settings.get("acceptance_msg", "")
            final_msg = f"{acceptance_txt}\n\n🔗 {invite_link}"

            await message.reply_text(final_msg)
        except Exception as e:
            await message.reply_text("🎉 ¡Has completado la meta! Ocurrió un problema al generar tu enlace. Por favor notifica al administrador.")
    else:
        # Guardar progreso silenciosamente (sin responder nada al usuario)
        users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "contributions": new_contributions,
                    "name": user.first_name,
                    "username": user.username,
                }
            },
            upsert=True,
        )


# --- FUNCIÓN PRINCIPAL ---
def main():
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("reload", reload_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.ALL & ~filters.COMMAND),
            handle_private_messages,
        )
    )

    print("🤖 Bot de Respuesta Rápida iniciado correctamente...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
