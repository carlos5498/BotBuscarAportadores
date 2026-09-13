import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)

# Configuración de Logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Variables de Entorno
TOKEN = os.getenv("TOKEN")
MY_ID = int(os.getenv("MY_ID", "0"))
MONGO_URL = os.getenv("MONGO_URL")

# --- SERVIDOR WEB DE SALUD PARA RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot Invitaciones Activo")

    def log_message(self, format, *args):
        return  # Desactiva logs HTTP innecesarios en la consola

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"Servidor HTTP escuchando en el puerto {port}")
    server.serve_forever()

# --- CONEXIÓN A MONGODB ---
mongo_client = MongoClient(MONGO_URL) if MONGO_URL else None
db = mongo_client["bot_invitaciones"] if mongo_client else None
col_config = db["config"] if db is not None else None
col_admins = db["admins"] if db is not None else None
col_banned = db["banned"] if db is not None else None
col_requests = db["pending_requests"] if db is not None else None

# Estados para la edición de mensajes
EDITANDO_INICIO, EDITANDO_CONFIRMACION = range(2)

# --- FUNCIONES DE BASE DE DATOS Y PERMISOS ---

def get_config():
    if col_config is None:
        return {
            "_id": "main_config",
            "mensaje_inicio": "Bienvenido. Espera las instrucciones para acceder.",
            "mensaje_confirmacion": "Acceso verificado correctamente. Únete mediante el siguiente enlace:",
            "grupo_id": None
        }
    cfg = col_config.find_one({"_id": "main_config"})
    if not cfg:
        cfg = {
            "_id": "main_config",
            "mensaje_inicio": "Bienvenido. Espera las instrucciones para acceder.",
            "mensaje_confirmacion": "Acceso verificado correctamente. Únete mediante el siguiente enlace:",
            "grupo_id": None
        }
        col_config.insert_one(cfg)
    return cfg

def set_config(data: dict):
    if col_config is not None:
        col_config.update_one({"_id": "main_config"}, {"$set": data}, upsert=True)

def get_admin_ids() -> set:
    """Obtiene el ID principal y todos los admins adicionales agregados."""
    admins = {MY_ID}
    if col_admins is not None:
        for doc in col_admins.find():
            admins.add(doc["_id"])
    return admins

def is_admin(user_id: int) -> bool:
    """Verifica si un usuario es admin (el principal o uno agregado)."""
    if user_id == MY_ID:
        return True
    if col_admins is not None:
        return col_admins.find_one({"_id": user_id}) is not None
    return False

def is_banned(user_id: int) -> bool:
    """Verifica si un usuario está en la lista de baneados."""
    if col_banned is not None:
        return col_banned.find_one({"_id": user_id}) is not None
    return False

def ban_user(user_id: int):
    """Guarda un ID en la lista negra."""
    if col_banned is not None:
        col_banned.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)

def add_admin_id(user_id: int):
    """Guarda un nuevo administrador en la base de datos."""
    if col_admins is not None:
        col_admins.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)

async def limpiar_solicitudes_usuario(context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    """Elimina las notificaciones de este usuario del privado de todos los administradores."""
    if col_requests is None:
        return

    solicitudes = list(col_requests.find({"user_id": target_user_id}))
    for req in solicitudes:
        try:
            await context.bot.delete_message(
                chat_id=req["admin_id"],
                message_id=req["message_id"]
            )
        except Exception as e:
            logging.warning(f"No se pudo eliminar el mensaje {req['message_id']} para el admin {req['admin_id']}: {e}")

    col_requests.delete_many({"user_id": target_user_id})

# ----------------- COMANDOS Y FLUJO GENERAL -----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde con el mensaje de inicio guardado."""
    cfg = get_config()
    await update.message.reply_text(cfg.get("mensaje_inicio"))

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite agregar nuevos administradores usando /admin <ID>."""
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("❌ Uso correcto: `/admin ID`", parse_mode="Markdown")
        return

    try:
        nuevo_admin_id = int(context.args[0])
        add_admin_id(nuevo_admin_id)
        await update.message.reply_text(f"✅ Administrador `{nuevo_admin_id}` añadido correctamente.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ El ID debe ser un número entero válido.")

async def cmd_solicitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa la solicitud de ingreso enviada por el usuario."""
    user = update.effective_user
    user_id = user.id

    if is_banned(user_id):
        return  # Los usuarios baneados no reciben respuesta ni pueden enviar más solicitudes

    # Responde al usuario que realizó la solicitud
    await update.message.reply_text(
        "Has enviado una solicitud, un admin la revisara asegúrate de haber cumplido todos tus requisitos"
    )

    # Datos para notificar a los administradores
    nombre = user.full_name
    username = f"@{user.username}" if user.username else "Sin username"
    texto_admin = (
        f"👤 **Nueva Solicitud de Acceso**\n\n"
        f"• **Nombre:** {nombre}\n"
        f"• **Username:** {username}\n"
        f"• **ID:** `{user_id}`"
    )

    keyboard = [
        [
            InlineKeyboardButton("Aceptar", callback_data=f"req_aceptar_{user_id}"),
            InlineKeyboardButton("Denegar", callback_data=f"req_denegar_{user_id}"),
            InlineKeyboardButton("Ban", callback_data=f"req_ban_{user_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Notifica a la lista completa de administradores
    admins = get_admin_ids()
    for admin_id in admins:
        try:
            msg = await context.bot.send_message(
                chat_id=admin_id,
                text=texto_admin,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            # Guarda la referencia del mensaje para poder borrarlo después
            if col_requests is not None:
                col_requests.insert_one({
                    "user_id": user_id,
                    "admin_id": admin_id,
                    "message_id": msg.message_id
                })
        except Exception as e:
            logging.error(f"Error al enviar mensaje de solicitud al admin {admin_id}: {e}")

async def cb_procesar_solicitud(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Acciones al accionar los botones Aceptar, Denegar o Ban en una solicitud."""
    query = update.callback_query
    admin_user = update.effective_user

    if not is_admin(admin_user.id):
        await query.answer("❌ No tienes permisos de administrador.", show_alert=True)
        return

    await query.answer()

    data_parts = query.data.split("_")
    accion = data_parts[1]
    target_user_id = int(data_parts[2])

    cfg = get_config()
    grupo_id = cfg.get("grupo_id")

    if accion == "aceptar":
        if not grupo_id:
            await query.message.reply_text("❌ Error: Vincula el grupo primero usando el comando /reload dentro del grupo.")
            return

        try:
            # 1. Generar enlace de invitación de un solo uso
            link = await context.bot.create_chat_invite_link(
                chat_id=grupo_id,
                member_limit=1
            )
            texto_conf = cfg.get("mensaje_confirmacion")
            mensaje_final = f"{texto_conf}\n\n👉 {link.invite_link}"

            # 2. Enviar confirmación al usuario
            await context.bot.send_message(chat_id=target_user_id, text=mensaje_final)
        except Exception as e:
            logging.error(f"Error enviando la invitación al usuario {target_user_id}: {e}")

    elif accion == "denegar":
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="Tu solicitud ha sido denegada, no cumpliste con los requisitos"
            )
        except Exception as e:
            logging.error(f"Error notificando denegación a {target_user_id}: {e}")

    elif accion == "ban":
        ban_user(target_user_id)

    # Elimina los mensajes de solicitud guardados en los chats privados de todos los admins
    await limpiar_solicitudes_usuario(context, target_user_id)

# ----------------- PANEL DE ADMINISTRACIÓN -----------------

async def login_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre el panel de administración."""
    if not is_admin(update.effective_user.id):
        return

    keyboard = [
        [InlineKeyboardButton("📝 Editar mensaje de inicio", callback_data="btn_edit_inicio")],
        [InlineKeyboardButton("✅ Editar mensaje de confirmación", callback_data="btn_edit_conf")],
        [InlineKeyboardButton("🔗 Unir grupo", callback_data="btn_unir_grupo")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("⚙️ **Panel de Administración**", reply_markup=reply_markup, parse_mode="Markdown")

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las opciones internas del panel de administración."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_edit_inicio":
        await query.edit_message_text("Envía por este chat el nuevo **mensaje de inicio**:")
        return EDITANDO_INICIO

    elif query.data == "btn_edit_conf":
        await query.edit_message_text("Envía por este chat el nuevo **mensaje de confirmación**:")
        return EDITANDO_CONFIRMACION

    elif query.data == "btn_unir_grupo":
        await query.edit_message_text(
            "Añade al bot a tu grupo como **Administrador** (con permiso para invitar usuarios) "
            "y luego envía el comando `/reload` dentro de ese grupo."
        )
        return ConversationHandler.END

async def guardar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_config({"mensaje_inicio": update.message.text})
    await update.message.reply_text("✅ Mensaje de inicio actualizado correctamente.")
    return ConversationHandler.END

async def guardar_confirmacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_config({"mensaje_confirmacion": update.message.text})
    await update.message.reply_text("✅ Mensaje de confirmación actualizado correctamente.")
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Acción cancelada.")
    return ConversationHandler.END

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para vincular el grupo objetivo desde el propio grupo."""
    if not is_admin(update.effective_user.id):
        return

    if update.effective_chat.type in ["group", "supergroup"]:
        set_config({"grupo_id": update.effective_chat.id})
        await update.message.reply_text("✅ ¡Este grupo ha sido vinculado correctamente!")
    else:
        await update.message.reply_text("❌ Este comando debe ejecutarse dentro del grupo objetivo.")

# ----------------- INICIALIZACIÓN -----------------

def main():
    # 1. Iniciar servidor de salud en un hilo secundario para Render
    threading.Thread(target=start_health_server, daemon=True).start()

    # 2. Construir la aplicación del bot
    app = ApplicationBuilder().token(TOKEN).build()

    # 3. Manejador de conversación para la edición de mensajes
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_buttons, pattern="^btn_")],
        states={
            EDITANDO_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_inicio)],
            EDITANDO_CONFIRMACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_confirmacion)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False
    )

    # 4. Registrar comandos y manejadores
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("solicitar", cmd_solicitar))
    app.add_handler(CommandHandler("reload", cmd_reload))
    
    # Manejador para los botones de la solicitud (Aceptar, Denegar, Ban)
    app.add_handler(CallbackQueryHandler(cb_procesar_solicitud, pattern="^req_(aceptar|denegar|ban)_"))
    
    # Contraseña para el panel de administración
    app.add_handler(MessageHandler(filters.Regex("^Carlos13mar$"), login_admin))
    
    # Conversación para la edición de textos
    app.add_handler(conv_handler)

    print("Bot de Invitaciones Online...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
