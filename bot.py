import logging
import os
import django
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from asgiref.sync import sync_to_async
from telegram.ext import ContextTypes, ConversationHandler
from dotenv import load_dotenv
import traceback

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
import sys
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
logger = logging.getLogger(__name__)

# Загружаем переменные окружения из файла .env после настройки логирования
load_dotenv()
logger.info(f"Loaded .env file from {os.getcwd()}")

# Настройка Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'volunteer_project.settings')
try:
    django.setup()
    logger.info("Django setup completed successfully")
except Exception as e:
    logger.error(f"Failed to setup Django: {e}\n{traceback.format_exc()}")
    raise

from core.models import User
from volunteer_handlers import register_handlers as register_volunteer_handlers, volunteer_menu as volunteer_start, get_volunteer_keyboard
from organization_handlers import register_handlers as register_organization_handlers, org_menu

# Загрузка токена из переменной окружения
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set in environment variables")
    raise ValueError("TELEGRAM_BOT_TOKEN not set in environment variables")
logger.info(f"Loaded TOKEN: {TOKEN[:5]}... (partial for security)")

# Создаём приложение
try:
    application = Application.builder().token(TOKEN).build()
    logger.info("Application built successfully")
except Exception as e:
    logger.error(f"Failed to build Application: {e}\n{traceback.format_exc()}")
    raise

# Состояния для регистрации
USERNAME_REQUEST, PHONE_REQUEST, ROLE_REQUEST, ORGANIZATION_REQUEST = range(4)

# Вспомогательные функции
@sync_to_async
def get_user(telegram_id):
    try:
        user = User.objects.get(telegram_id=telegram_id)
        logger.info(f"User found: {user.username} (telegram_id: {telegram_id})")
        return user
    except User.DoesNotExist:
        logger.warning(f"User not found with telegram_id: {telegram_id}")
        return None

@sync_to_async
def create_user(telegram_id, phone_number, username, is_organizer=False, organization_name=None):
    try:
        user = User.objects.create(
            telegram_id=telegram_id,
            phone_number=phone_number,
            username=username,
            rating=0,
            is_organizer=is_organizer,
            organization_name=organization_name
        )
        logger.info(f"User created: {username} (telegram_id: {telegram_id}, phone: {phone_number}, org: {organization_name})")
        return user
    except Exception as e:
        logger.error(f"Error creating user: {e}\n{traceback.format_exc()}")
        return None

@sync_to_async
def get_admin():
    try:
        admin = User.objects.filter(is_staff=True).first()
        if admin:
            logger.info(f"Admin found: {admin.username}")
            return admin
        else:
            logger.warning("No admin found")
            return None
    except Exception as e:
        logger.error(f"Error fetching admin: {e}\n{traceback.format_exc()}")
        return None

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    telegram_id = str(user.id)
    logger.info(f"Received /start command from telegram_id: {telegram_id}")
    db_user = await get_user(telegram_id)

    if db_user:
        if db_user.is_staff:
            logger.info(f"User {db_user.username} is an admin, redirecting to admin menu")
            # await admin_menu(update, context)  # Разкомментируйте, если добавите admin_handlers
        elif db_user.is_organizer:
            logger.info(f"User {db_user.username} is an organizer, redirecting to org menu")
            await org_menu(update, context)
        else:
            logger.info(f"User {db_user.username} is a volunteer, redirecting to volunteer menu")
            await volunteer_start(update, context)
        return ConversationHandler.END

    # Начинаем регистрацию
    context.user_data['telegram_id'] = telegram_id
    await update.message.reply_text(
        "Добро пожаловать! Введите ваше имя:",
        reply_markup=ReplyKeyboardMarkup([[]], one_time_keyboard=True)
    )
    return USERNAME_REQUEST

# Обработчик имени пользователя
async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = context.user_data.get('telegram_id')
    if not telegram_id:
        await update.message.reply_text("Ошибка: сессия истекла. Попробуйте снова с /start.")
        return ConversationHandler.END

    username = update.message.text.strip()
    if not username:
        await update.message.reply_text("Имя не может быть пустым. Введите имя:")
        return USERNAME_REQUEST

    context.user_data['username'] = username
    keyboard = [[KeyboardButton("Отправить номер телефона", request_contact=True)]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Спасибо! Отправьте ваш номер телефона:",
        reply_markup=reply_markup
    )
    return PHONE_REQUEST

# Обработчик номера телефона
async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = context.user_data.get('telegram_id')
    if not telegram_id:
        await update.message.reply_text("Ошибка: сессия истекла. Попробуйте снова с /start.")
        return ConversationHandler.END

    if update.message.contact:
        phone_number = update.message.contact.phone_number
        context.user_data['phone_number'] = phone_number
        buttons = [
            [InlineKeyboardButton("Волонтёр", callback_data="role_volunteer"),
             InlineKeyboardButton("Организатор", callback_data="role_organizer")]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Кем вы являетесь?",
            reply_markup=keyboard
        )
        return ROLE_REQUEST
    else:
        await update.message.reply_text("Пожалуйста, отправьте номер телефона, используя кнопку.")
        return PHONE_REQUEST

# Обработчик выбора роли
async def receive_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    telegram_id = context.user_data.get('telegram_id')
    username = context.user_data.get('username')
    phone_number = context.user_data.get('phone_number')
    if not telegram_id or not username or not phone_number:
        await query.message.reply_text("Ошибка: сессия истекла. Попробуйте снова с /start.")
        return ConversationHandler.END

    role = query.data
    if role == "role_volunteer":
        db_user = await create_user(telegram_id, phone_number, username, is_organizer=False)
        if db_user:
            await query.message.reply_text(
                f"Регистрация завершена, {username}! Добро пожаловать, волонтёр!",
                reply_markup=get_volunteer_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END
        else:
            await query.message.reply_text("Ошибка при регистрации. Попробуйте снова.")
            return ConversationHandler.END
    elif role == "role_organizer":
        await query.message.reply_text(
            "Введите название вашей организации:",
            reply_markup=ReplyKeyboardMarkup([[]], one_time_keyboard=True)
        )
        return ORGANIZATION_REQUEST
    else:
        await query.message.reply_text("Ошибка: неверный выбор роли.")
        return ROLE_REQUEST

# Обработчик названия организации
async def receive_organization(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = context.user_data.get('telegram_id')
    username = context.user_data.get('username')
    phone_number = context.user_data.get('phone_number')
    if not telegram_id or not username or not phone_number:
        await update.message.reply_text("Ошибка: сессия истекла. Попробуйте снова с /start.")
        return ConversationHandler.END

    organization_name = update.message.text.strip()
    if not organization_name:
        await update.message.reply_text("Название организации не может быть пустым. Введите название:")
        return ORGANIZATION_REQUEST

    db_user = await create_user(telegram_id, phone_number, username, is_organizer=False, organization_name=organization_name)
    if db_user:
        await update.message.reply_text(
            f"Регистрация завершена, {username}! Ваш запрос на статус организатора отправлен на рассмотрение.",
            reply_markup=get_volunteer_keyboard()  # Пока даём волонтёрское меню
        )
        # Уведомляем админа
        admin = await get_admin()
        if admin and admin.telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=admin.telegram_id,
                    text=f"Новый запрос на статус организатора:\nПользователь: {username}\nТелефон: {phone_number}\nОрганизация: {organization_name}\nПроверьте в админ-панели."
                )
                logger.info(f"Admin {admin.username} notified about organizer request from {username}")
            except Exception as e:
                logger.error(f"Failed to notify admin about organizer request: {e}\n{traceback.format_exc()}")
        else:
            logger.warning("Admin not found or telegram_id missing for admin")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await update.message.reply_text("Ошибка при регистрации. Попробуйте снова.")
        return ConversationHandler.END

# Глобальный обработчик ошибок
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}\n{traceback.format_exc()}")
    if update and update.effective_message:
        await update.effective_message.reply_text("Произошла ошибка. Пожалуйста, попробуйте снова.")

# Регистрируем обработчики
registration_conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        USERNAME_REQUEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)],
        PHONE_REQUEST: [MessageHandler(filters.CONTACT, receive_phone)],
        ROLE_REQUEST: [CallbackQueryHandler(receive_role, pattern=r"^role_")],
        ORGANIZATION_REQUEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_organization)],
    },
    fallbacks=[CommandHandler("start", start)],
    per_message=False
)
application.add_handler(registration_conv)

logger.info("Registering volunteer handlers...")
register_volunteer_handlers(application)
logger.info("Volunteer handlers registered successfully")

logger.info("Registering organization handlers...")
register_organization_handlers(application)
logger.info("Organization handlers registered successfully")

# Добавляем обработчик для всех обновлений для отладки
async def debug_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received update: {update}")
application.add_handler(MessageHandler(filters.ALL, debug_update))

# register_admin_handlers(application)  # Разкомментируйте, если добавите admin_handlers
application.add_error_handler(error_handler)

# Запуск бота
logger.info("Starting bot...")
try:
    application.run_polling(allowed_updates=Update.ALL_TYPES)
except Exception as e:
    logger.error(f"Bot polling failed: {e}\n{traceback.format_exc()}")
    raise
logger.info("Bot stopped.")