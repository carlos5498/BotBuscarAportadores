import os
import logging
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

# Configuración de logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Variables de Entorno
TOKEN = os.getenv("TOKEN")
MY_ID = int(os.getenv("MY_ID", "0"))
MONGO_URL = os.getenv("MONGO_URL")

# Conexión a MongoDB
client = MongoClient(MONGO_URL)
db = client["bot_invitaciones"]
col_config = db["config"]

# Estados para la edición de mensajes
EDITANDO_INICIO, EDITANDO_CONFIRMACION = range(2)

def get_config():
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
    col_config.update_one({"_id": "main_config"}, {"$set": data}, upsert=True)

# ----------------- COMANDOS Y FLUJO GENERAL -----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde con el mensaje de inicio guardado."""
    cfg = get_config()
    await update.message.reply_text(cfg.get("mensaje_inicio"))

async def procesar_grupo_13mar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta 'Grupo13mar' únicamente si lo envió el propio bot."""
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    # Verifica si el texto es exacto y si el emisor es EL MISMO BOT
    if msg.text.strip() == "Grupo13mar":
        if msg.from_user and msg.from_user.id == context.bot.id:
            cfg = get_config()
            grupo_id = cfg.get("grupo_id")

            # 1. Eliminar el mensaje desencadenante
            try:
                await msg.delete()
            except Exception as e:
                logging.error(f"No se pudo eliminar el mensaje: {e}")

            if not grupo_id:
                await update.effective_chat.send_message("❌ Error: El bot aún no está vinculado a ningún grupo.")
                return

            # 2. Generar link de un solo uso
            try:
                link = await context.bot.create_chat_invite_link(
                    chat_id=grupo_id,
                    member_limit=1
                )
                
                # 3. Enviar mensaje de confirmación + Link
                texto_confirmacion = cfg.get("mensaje_confirmacion")
                mensaje_final = f"{texto_confirmacion}\n\n👉 {link.invite_link}"
                
                await update.effective_chat.send_message(mensaje_final)
            except Exception as e:
                await update.effective_chat.send_message(f"❌ Error al generar el enlace: {e}")

# ----------------- PANEL DE ADMINISTRACIÓN -----------------

async def login_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abre el panel de administración al recibir la contraseña del dueño."""
    if update.effective_user.id != MY_ID:
        return  # Ignorar si no es el administrador configurado

    keyboard = [
        [InlineKeyboardButton("📝 Editar mensaje de inicio", callback_data="btn_edit_inicio")],
        [InlineKeyboardButton("✅ Editar mensaje de confirmación", callback_data="btn_edit_conf")],
        [InlineKeyboardButton("🔗 Unir grupo", callback_data="btn_unir_grupo")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("⚙️ **Panel de Administración**", reply_markup=reply_markup, parse_mode="Markdown")

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las acciones del panel de admin."""
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
    if update.effective_user.id != MY_ID:
        return

    if update.effective_chat.type in ["group", "supergroup"]:
        set_config({"grupo_id": update.effective_chat.id})
        await update.message.reply_text("✅ ¡Este grupo ha sido vinculado correctamente!")
    else:
        await update.message.reply_text("❌ Este comando debe ejecutarse dentro del grupo objetivo.")

# ----------------- INICIALIZACIÓN -----------------

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Manejador de estado para la edición de mensajes
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_buttons)],
        states={
            EDITANDO_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_inicio)],
            EDITANDO_CONFIRMACION: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_confirmacion)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reload", cmd_reload))
    
    # Trigger para la contraseña del admin
    app.add_handler(MessageHandler(filters.Regex("^Carlos13mar$"), login_admin))
    
    # Conversación del panel de admin
    app.add_handler(conv_handler)
    
    # Detector global de mensajes (para capturar "Grupo13mar" enviado por el bot)
    app.add_handler(MessageHandler(filters.TEXT, procesar_grupo_13mar), group=1)

    print("Bot en marcha...")
    app.run_polling()

if __name__ == "__main__":
    main()
