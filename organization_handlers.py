import logging
import os
from datetime import datetime, time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
from telegram.error import TimedOut
from asgiref.sync import sync_to_async
from django.db import transaction 
from django.utils import timezone
import asyncio
import traceback
import aiofiles 

from core.models import User, Project, VolunteerProject, Task, TaskAssignment, Photo

# Настройка логирования
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
TITLE, DESCRIPTION, CITY, TAGS = range(4)
SELECT_PROJECT, SELECT_RECIPIENTS, SELECT_VOLUNTEERS, TASK_TEXT, TASK_DEADLINE_DATE, TASK_DEADLINE_START_TIME, TASK_DEADLINE_END_TIME, TASK_PHOTO, TASK_PHOTO_UPLOAD, CONFIRM_TASK, FEEDBACK = range(11)
MODERATE_PHOTO, MODERATE_PHOTO_ACTION = range(2)

# Количество элементов на странице для пагинации
PHOTOS_PER_PAGE = 5

# Основная клавиатура для организаторов
def get_org_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Создать проект", callback_data="create_project"),
         InlineKeyboardButton("👥 Просмотреть волонтёров", callback_data="manage_volunteers")],
        [InlineKeyboardButton("📌 Отправить задание", callback_data="send_task"),
         InlineKeyboardButton("🖼️ Проверить фото", callback_data="check_photos")]
    ])

# Вспомогательные функции с sync_to_async
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

@sync_to_async
def create_project(title, description, city, tags, creator):
    logger.info(f"Creating project: {title} by {creator.username}")
    try:
        project = Project.objects.create(
            title=title,
            description=description,
            city=city,
            creator=creator,
            status='pending'
        )
        project.tags.add(*tags.split(','))
        logger.info(f"Project created: {project.title} (id: {project.id})")
        return project
    except Exception as e:
        logger.error(f"Error creating project: {e}\n{traceback.format_exc()}")
        raise

@sync_to_async
def get_volunteers_for_project(creator):
    logger.info(f"Fetching volunteers for creator: {creator.username}")
    try:
        projects = Project.objects.filter(creator=creator).prefetch_related('volunteer_projects__volunteer')
        result = []
        for project in projects:
            volunteers = [vp.volunteer.username for vp in project.volunteer_projects.all()]
            result.append((project.title, volunteers))
        logger.info(f"Found {len(result)} projects with volunteers for {creator.username}")
        return result
    except Exception as e:
        logger.error(f"Error fetching volunteers: {e}\n{traceback.format_exc()}")
        raise

@sync_to_async
def get_organizer_projects(organizer):
    logger.info(f"Fetching projects for organizer: {organizer.username}")
    try:
        projects = Project.objects.filter(creator=organizer, status='approved')
        result = [(project, project.title) for project in projects]
        logger.info(f"Found {len(result)} projects for organizer {organizer.username}: {[p[1] for p in result]}")
        return result
    except Exception as e:
        logger.error(f"Error fetching organizer projects: {e}\n{traceback.format_exc()}")
        raise

@sync_to_async
def get_project_volunteers(project):
    logger.info(f"Fetching volunteers for project: {project.title} (id: {project.id})")
    try:
        volunteer_projects = VolunteerProject.objects.filter(project=project, is_active=True).select_related('volunteer')
        logger.info(f"Found {volunteer_projects.count()} VolunteerProject records")
        for vp in volunteer_projects:
            logger.info(f"VolunteerProject id={vp.id}, volunteer={vp.volunteer.username if vp.volunteer else 'None'}, is_active={vp.is_active}")
        result = []
        for vp in volunteer_projects:
            if vp.volunteer:
                logger.info(f"Found volunteer: {vp.volunteer.username} (telegram_id: {vp.volunteer.telegram_id})")
                result.append((vp.volunteer, vp.volunteer.username, vp.volunteer.telegram_id))
            else:
                logger.warning(f"VolunteerProject {vp.id} has no volunteer")
        logger.info(f"Total volunteers found: {len(result)}")
        return result
    except Exception as e:
        logger.error(f"Error fetching project volunteers: {e}\n{traceback.format_exc()}")
        raise

@sync_to_async
def create_task(project, creator, text, deadline_date, start_time, end_time, photo_path=None):
    logger.info(f"Creating task for project: {project.title} by {creator.username}")
    try:
        task = Task.objects.create(project=project, creator=creator, text=text, deadline_date=deadline_date, start_time=start_time, end_time=end_time)
        if photo_path:
            task.task_image = photo_path
            task.save()
        logger.info(f"Task created: {task.id}")
        return task
    except Exception as e:
        logger.error(f"Error creating task: {e}\n{traceback.format_exc()}")
        raise

@sync_to_async
def get_pending_photos_for_organizer(organizer, page=0, per_page=PHOTOS_PER_PAGE):
    logger.info(f"Fetching pending photos for organizer: {organizer.username}, page: {page}")
    try:
        photos = Photo.objects.filter(project__creator=organizer, status='pending').select_related('volunteer', 'project', 'task')
        total = photos.count()
        photos = photos[page * per_page:(page + 1) * per_page]
        result = [(photo, photo.volunteer.username, photo.project.title, photo.task) for photo in photos]
        logger.info(f"Found {len(result)} pending photos for organizer {organizer.username} on page {page}")
        return result, total
    except Exception as e:
        logger.error(f"Error fetching pending photos: {e}\n{traceback.format_exc()}")
        raise

@sync_to_async
def approve_photo(photo):
    logger.info(f"Approving photo from {photo.volunteer.username} for project {photo.project.title}")
    try:
        photo.status = 'approved'
        photo.moderated_at = timezone.now()
        photo.save()
        logger.info(f"Photo approved: {photo.id}")
    except Exception as e:
        logger.error(f"Error approving photo: {e}\n{traceback.format_exc()}")
        raise

async def reject_photo(photo, context):
    logger.info(f"Rejecting photo from {photo.volunteer.username} for project {photo.project.title}")
    try:
        photo.status = 'rejected'
        photo.moderated_at = timezone.now()
        await sync_to_async(photo.save)()
        logger.info("Photo rejected")

        if photo.volunteer.telegram_id:
            await context.bot.send_message(
                chat_id=photo.volunteer.telegram_id,
                text=f"Ваше фото для проекта {photo.project.title} отклонено организатором."
            )
    except Exception as e:
        logger.error(f"Error rejecting photo: {e}\n{traceback.format_exc()}")
        raise


async def notify_organizer_status(user, context):
    try:
        if user.telegram_id:  # Проверяем, что telegram_id не пустой
            if user.is_organizer:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text="Ваш запрос на статус организатора одобрен!"
                )
                logger.info(f"Notification sent to {user.username}: Status approved")
            else:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text="Ваш запрос на статус организатора отклонён."
                )
                logger.info(f"Notification sent to {user.username}: Status rejected")
        else:
            logger.warning(f"No telegram_id for user {user.username}, notification not sent")
    except Exception as e:
        logger.error(f"Error notifying user {user.username}: {e}\n{traceback.format_exc()}")

# Функции для создания календаря
def create_year_keyboard():
    current_year = datetime.now().year
    years = list(range(current_year, current_year + 6))
    buttons = [
        [InlineKeyboardButton(str(year), callback_data=f"deadline_date_year_{year}")]
        for year in years
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")])
    return InlineKeyboardMarkup(buttons)

def create_month_keyboard(year):
    months = [
        ("Янв", 1), ("Фев", 2), ("Мар", 3), ("Апр", 4),
        ("Май", 5), ("Июн", 6), ("Июл", 7), ("Авг", 8),
        ("Сен", 9), ("Окт", 10), ("Ноя", 11), ("Дек", 12)
    ]
    buttons = []
    row = []
    for month_name, month in months:
        if year == datetime.now().year and month < datetime.now().month:
            continue
        row.append(InlineKeyboardButton(month_name, callback_data=f"deadline_date_month_{month}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")])
    return InlineKeyboardMarkup(buttons)

def create_day_keyboard(year, month):
    if month in [4, 6, 9, 11]:
        days_in_month = 30
    elif month == 2:
        days_in_month = 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    else:
        days_in_month = 31

    buttons = []
    row = []
    for day in range(1, days_in_month + 1):
        if year == datetime.now().year and month == datetime.now().month and day < datetime.now().day:
            continue
        row.append(InlineKeyboardButton(str(day), callback_data=f"deadline_date_day_{day}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")])
    return InlineKeyboardMarkup(buttons)

async def notify_project_status(user, project, status, context):
    try:
        if user and user.telegram_id:
            if status == 'approved':
                message = f"Ваш проект '{project.title}' был одобрен!"
            else:
                message = f"Ваш проект '{project.title}' был отклонён."
            
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=message
                )
                logger.info(f"Notification sent to {user.username} (telegram_id: {user.telegram_id}): Project {project.title} {status}")
            except Exception as e:
                logger.error(f"Failed to send notification to {user.username}: {e}")
        else:
            logger.warning(f"User {user.username if user else 'None'} has no telegram_id, notification not sent")
    except Exception as e:
        logger.error(f"Error in notify_project_status: {e}\n{traceback.format_exc()}")

def create_time_keyboard(context, is_start=True):
    buttons = []
    row = []
    # Убираем фильтрацию по текущему времени, чтобы позволить выбор любого часа
    for hour in range(24):
        # if (context.user_data.get('deadline_date') and
        #     context.user_data.get('deadline_date') == datetime.now().date() and
        #     hour < datetime.now().hour):
        #     continue  # Убираем эту проверку
        row.append(InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"deadline_{'start' if is_start else 'end'}_time_{hour}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")])
    return InlineKeyboardMarkup(buttons)

def get_pagination_keyboard(page, total_pages):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"photo_prev_{page}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Следующая ➡️", callback_data=f"photo_next_{page}"))
    buttons.append(InlineKeyboardButton("❌ Отмена", callback_data="cancel_moderate"))
    return InlineKeyboardMarkup([buttons])

async def org_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    telegram_id = str(user.id)
    logger.info(f"Org menu requested by telegram_id: {telegram_id}")
    
    db_user = await get_user(telegram_id)
    if not db_user:
        await update.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return
    
    if not db_user.is_organizer:
        logger.warning(f"Access denied for telegram_id: {telegram_id}, not an organizer")
        if db_user.organization_name:
            await update.message.reply_text("Ваш запрос на статус организатора находится на рассмотрении.")
        else:
            await update.message.reply_text("У вас нет прав организатора. Зарегистрируйтесь как организатор.")
        return
    
    await update.message.reply_text(
        "Добро пожаловать в панель организации!\nВыберите действие:",
        reply_markup=get_org_keyboard()
    )

async def create_project_start(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user or not db_user.is_organizer:
        await query.message.reply_text("У вас нет прав организатора.")
        return ConversationHandler.END

    context.user_data['telegram_id'] = telegram_id
    await query.message.reply_text(
        "Давайте создадим новый проект.\nВведите название проекта:",
        reply_markup=ReplyKeyboardRemove()
    )
    logger.info(f"Started project creation for telegram_id: {telegram_id}")
    return TITLE

async def create_project_title(update, context):
    telegram_id = context.user_data.get('telegram_id')
    context.user_data['title'] = update.message.text.strip()
    if not context.user_data['title']:
        await update.message.reply_text("Название проекта не может быть пустым. Введите название:")
        return TITLE
    logger.info(f"Project title set: {context.user_data['title']} for telegram_id: {telegram_id}")
    await update.message.reply_text("Введите описание проекта:")
    return DESCRIPTION

async def create_project_description(update, context):
    telegram_id = context.user_data.get('telegram_id')
    context.user_data['description'] = update.message.text.strip()
    if not context.user_data['description']:
        await update.message.reply_text("Описание проекта не может быть пустым. Введите описание:")
        return DESCRIPTION
    logger.info(f"Project description set: {context.user_data['description']} for telegram_id: {telegram_id}")
    await update.message.reply_text("Введите город проекта:")
    return CITY

async def create_project_city(update, context):
    telegram_id = context.user_data.get('telegram_id')
    context.user_data['city'] = update.message.text.strip()
    if not context.user_data['city']:
        await update.message.reply_text("Город проекта не может быть пустым. Введите город:")
        return CITY
    logger.info(f"Project city set: {context.user_data['city']} for telegram_id: {telegram_id}")
    await update.message.reply_text("Введите теги проекта (через запятую, например: уборка, экология):")
    return TAGS

async def create_project_tags(update, context):
    telegram_id = context.user_data.get('telegram_id')
    tags = update.message.text.strip()
    if not tags:
        await update.message.reply_text("Теги не могут быть пустыми. Введите теги:")
        return TAGS
    logger.info(f"Project tags set: {tags} for telegram_id: {telegram_id}")

    db_user = await get_user(telegram_id)
    title = context.user_data['title']
    description = context.user_data['description']
    city = context.user_data['city']

    try:
        project = await create_project(title, description, city, tags, db_user)
        await update.message.reply_text(
            f"Проект '{project.title}' создан и отправлен на модерацию!",
            reply_markup=get_org_keyboard()
        )

        admin = await get_admin()
        if admin and admin.telegram_id:
            try:
                await context.bot.send_message(
                    chat_id=admin.telegram_id,
                    text=f"Создан новый проект '{project.title}' от {db_user.username}. Проверьте его в админ-панели."
                )
                logger.info(f"Admin {admin.username} (telegram_id: {admin.telegram_id}) notified about new project: {project.title}")
            except Exception as e:
                logger.error(f"Failed to notify admin about new project: {e}\n{traceback.format_exc()}")
        else:
            logger.warning(f"Admin not found or telegram_id missing for admin: {admin.username if admin else 'None'}")
    except Exception as e:
        logger.error(f"Error in create_project_tags: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("Ошибка при создании проекта. Попробуйте снова.")
        return ConversationHandler.END

    context.user_data.clear()
    return ConversationHandler.END

async def create_project_cancel(update, context):
    await (update.message.reply_text if update.message else update.callback_query.message.reply_text)(
        "Создание проекта отменено.",
        reply_markup=get_org_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update, context):
    await update.message.reply_text("Действие отменено.", reply_markup=get_org_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

async def manage_volunteers(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user or not db_user.is_organizer:
        await query.message.reply_text("У вас нет прав организатора.")
        return

    try:
        projects = await get_volunteers_for_project(db_user)
        if not projects:
            await query.message.reply_text("У вас нет проектов или волонтёров.")
            return

        response = ""
        for project_title, volunteers in projects:
            volunteers_text = ", ".join(volunteers) if volunteers else "Нет волонтёров"
            response += f"Проект: {project_title}\nВолонтёры: {volunteers_text}\n\n"
        await query.message.reply_text(response)
    except Exception as e:
        logger.error(f"Error in manage_volunteers: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка при получении списка волонтёров.")

async def send_task_start(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user or not db_user.is_organizer:
        await query.message.reply_text("У вас нет прав организатора.")
        return ConversationHandler.END

    try:
        projects = await get_organizer_projects(db_user)
        if not projects:
            await query.message.reply_text("У вас нет одобренных проектов для отправки заданий.")
            return ConversationHandler.END

        buttons = [
            [InlineKeyboardButton(project[1], callback_data=f"task_project_{i}")]
            for i, project in enumerate(projects)
        ]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")])
        keyboard = InlineKeyboardMarkup(buttons)
        await query.message.reply_text("Выберите проект для отправки задания:", reply_markup=keyboard)

        context.user_data['projects'] = projects
        context.user_data['organizer'] = db_user
        return SELECT_PROJECT
    except Exception as e:
        logger.error(f"Error in send_task_start: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка при выборе проекта.")
        return ConversationHandler.END

async def select_project(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания отменена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        choice = int(query.data.split('_')[2])
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback_data format: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор проекта.")
        return ConversationHandler.END

    projects = context.user_data.get('projects', [])
    if 0 <= choice < len(projects):
        project = projects[choice][0]
        context.user_data['selected_project'] = project
        buttons = [
            [InlineKeyboardButton("Всем волонтёрам", callback_data="task_recipients_all"),
             InlineKeyboardButton("Одному волонтёру", callback_data="task_recipients_one")],
            [InlineKeyboardButton("Нескольким волонтёрам", callback_data="task_recipients_multiple"),
             InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        await query.message.reply_text(
            f"Проект: {project.title}\nКому отправить задание?",
            reply_markup=keyboard
        )
        return SELECT_RECIPIENTS
    else:
        await query.message.reply_text("Неверный выбор проекта.")
        return ConversationHandler.END

async def select_recipients(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания отменена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    project = context.user_data.get('selected_project')
    if query.data == "task_recipients_all":
        context.user_data['recipients'] = 'all'
        await query.message.reply_text("Введите текст задания:")
        return TASK_TEXT
    elif query.data in ["task_recipients_one", "task_recipients_multiple"]:
        context.user_data['recipients'] = query.data
        try:
            volunteers = await get_project_volunteers(project)
            if not volunteers:
                await query.message.reply_text("В этом проекте нет волонтёров.")
                return ConversationHandler.END

            buttons = [
                [InlineKeyboardButton(volunteer[1], callback_data=f"task_volunteer_{i}")]
                for i, volunteer in enumerate(volunteers)
            ]
            buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")])
            if query.data == "task_recipients_multiple":
                buttons.append([InlineKeyboardButton("✅ Готово", callback_data="task_volunteers_done")])
            keyboard = InlineKeyboardMarkup(buttons)
            await query.message.reply_text(
                "Выберите одного волонтёра:" if query.data == "task_recipients_one" else "Выберите волонтёров (нажмите 'Готово' после выбора):",
                reply_markup=keyboard
            )
            context.user_data['volunteers'] = volunteers
            context.user_data['selected_volunteers'] = []
            return SELECT_VOLUNTEERS
        except Exception as e:
            logger.error(f"Error in select_recipients: {e}\n{traceback.format_exc()}")
            await query.message.reply_text("Ошибка при выборе волонтёров.")
            return ConversationHandler.END

async def select_volunteers(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания отменена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "task_volunteers_done":
        selected_volunteers = context.user_data.get('selected_volunteers', [])
        if not selected_volunteers:
            await query.message.reply_text("Вы не выбрали ни одного волонтёра. Попробуйте снова.")
            return SELECT_VOLUNTEERS
        await query.message.reply_text("Введите текст задания:")
        return TASK_TEXT

    try:
        choice = int(query.data.split('_')[2])
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback_data format: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор волонтёра.")
        return SELECT_VOLUNTEERS

    volunteers = context.user_data.get('volunteers', [])
    if 0 <= choice < len(volunteers):
        volunteer = volunteers[choice][0]
        if context.user_data['recipients'] == "task_recipients_one":
            context.user_data['selected_volunteers'] = [volunteer]
            await query.message.reply_text("Введите текст задания:")
            return TASK_TEXT
        else:
            selected_volunteers = context.user_data.get('selected_volunteers', [])
            if volunteer not in selected_volunteers:
                selected_volunteers.append(volunteer)
                context.user_data['selected_volunteers'] = selected_volunteers
                await query.message.reply_text(f"Выбран волонтёр: {volunteer.username}. Выберите ещё или нажмите 'Готово'.")
            else:
                await query.message.reply_text(f"Волонтёр {volunteer.username} уже выбран. Выберите другого или нажмите 'Готово'.")
            return SELECT_VOLUNTEERS
    else:
        await query.message.reply_text("Неверный выбор волонтёра.")
        return SELECT_VOLUNTEERS

async def task_text(update, context):
    context.user_data['task_text'] = update.message.text.strip()
    if not context.user_data['task_text']:
        await update.message.reply_text("Текст задания не может быть пустым. Введите текст:")
        return TASK_TEXT
    logger.info(f"Task text set: {context.user_data['task_text']}")
    keyboard = await sync_to_async(create_year_keyboard)()
    await update.message.reply_text("Выберите дату и срок выполнение:", reply_markup=keyboard)
    return TASK_DEADLINE_DATE

async def task_deadline_date_year(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        year = int(query.data.split('_')[3])
        context.user_data['deadline_date_year'] = year
        keyboard = await sync_to_async(create_month_keyboard)(year)
        await query.message.reply_text(f"Вы выбрали год: {year}\nВыберите месяц:", reply_markup=keyboard)
        return TASK_DEADLINE_DATE
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid year selection: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор года.")
        return TASK_DEADLINE_DATE

async def task_deadline_date_month(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        month = int(query.data.split('_')[3])
        context.user_data['deadline_date_month'] = month
        year = context.user_data['deadline_date_year']
        keyboard = await sync_to_async(create_day_keyboard)(year, month)
        await query.message.reply_text(f"Вы выбрали месяц: {month}\nВыберите день:", reply_markup=keyboard)
        return TASK_DEADLINE_DATE
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid month selection: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор месяца.")
        return TASK_DEADLINE_DATE

async def task_deadline_date_day(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        day = int(query.data.split('_')[3])
        year = context.user_data['deadline_date_year']
        month = context.user_data['deadline_date_month']
        deadline_date = datetime(year, month, day).date()
        context.user_data['deadline_date'] = deadline_date
        keyboard = await sync_to_async(create_time_keyboard)(context, True)
        await query.message.reply_text(f"Вы выбрали дату: {deadline_date}\nВыберите начальное время:", reply_markup=keyboard)
        return TASK_DEADLINE_START_TIME
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid day selection: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор дня.")
        return TASK_DEADLINE_DATE

async def task_deadline_start_time(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        hour = int(query.data.split('_')[3])
        start_time = time(hour, 0)
        context.user_data['start_time'] = start_time
        keyboard = await sync_to_async(create_time_keyboard)(context, False)
        await query.message.reply_text(f"Вы выбрали начальное время: {start_time.strftime('%H:%M')}\nВыберите конечное время:", reply_markup=keyboard)
        return TASK_DEADLINE_END_TIME
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid start time selection: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор времени.")
        return TASK_DEADLINE_START_TIME

async def task_deadline_end_time(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        hour = int(query.data.split('_')[3])
        end_time = time(hour, 0)
        if end_time <= context.user_data.get('start_time', time(0, 0)):
            await query.message.reply_text("Конечное время должно быть позже начального. Выберите снова.")
            return TASK_DEADLINE_END_TIME
        context.user_data['end_time'] = end_time
        buttons = [
            [InlineKeyboardButton("Да", callback_data="task_photo_yes"),
             InlineKeyboardButton("Нет", callback_data="task_photo_no")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        await query.message.reply_text(f"Вы выбрали конечное время: {end_time.strftime('%H:%M')}\nХотите прикрепить фото к заданию?", reply_markup=keyboard)
        return TASK_PHOTO

    except (ValueError, IndexError) as e:
        logger.error(f"Invalid end time selection: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор времени.")
        return TASK_DEADLINE_END_TIME

async def task_photo(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "task_photo_no":
        context.user_data['task_photo'] = None
        buttons = [
            [InlineKeyboardButton("Отправить", callback_data="task_confirm_send"),
             InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")]
        ]
        keyboard = InlineKeyboardMarkup(buttons)
        deadline_date = context.user_data['deadline_date'].strftime('%d-%m-%Y')
        start_time = context.user_data['start_time'].strftime('%H:%M')
        end_time = context.user_data['end_time'].strftime('%H:%M')
        await query.message.reply_text(
            f"Текст задания: {context.user_data['task_text']}\nСрок: {deadline_date}\nВремя: {start_time} - {end_time}\nПодтвердите отправку:",
            reply_markup=keyboard
        )
        return CONFIRM_TASK

    if query.data == "task_photo_yes":
        await query.message.reply_text("Пожалуйста, отправьте фото для задания:")
        return TASK_PHOTO_UPLOAD

async def task_photo_upload(update, context):
    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            current_date = datetime.now()
            year, month, day = current_date.year, current_date.month, current_date.day
            save_dir = os.path.join("media", f"tasks/{year}/{month}/{day}")
            await aiofiles.os.makedirs(save_dir, exist_ok=True)  # Асинхронное создание директории
            telegram_id = str(update.message.from_user.id)
            file_name = f"{telegram_id}_{photo_file.file_id}.jpg"
            file_path = os.path.join(save_dir, file_name)

            # Увеличение тайм-аута и повторные попытки
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    photo_data = await photo_file.download_as_bytearray()  # Тайм-аут 30 секунд
                    if not photo_data:
                        raise ValueError("Downloaded photo data is empty")
                    async with aiofiles.open(file_path, 'wb') as f:
                        await f.write(photo_data)
                    logger.info(f"Photo saved to {file_path}")
                    break
                except TimedOut as e:  # Используем импортированный TimedOut
                    logger.warning(f"Attempt {attempt + 1}/{max_retries} failed with timeout: {e}")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка между попытками

                try:
                    photo_data = await photo_file.download_as_bytearray()
                except Exception as e:
                    logger.error(f"Error downloading photo: {e}")
                    await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка при загрузке фото. Попробуйте снова.")
                return

            # Сохраняем относительный путь для базы данных
            db_file_path = os.path.join(f"tasks/{year}/{month}/{day}", file_name)
            context.user_data['task_photo'] = db_file_path
            buttons = [
                [InlineKeyboardButton("Отправить", callback_data="task_confirm_send"),
                 InlineKeyboardButton("❌ Отмена", callback_data="cancel_task")]
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            deadline_date = context.user_data['deadline_date'].strftime('%d-%m-%Y')
            start_time = context.user_data['start_time'].strftime('%H:%M')
            end_time = context.user_data['end_time'].strftime('%H:%M')
            await update.message.reply_text(
                f"Фото загружено.\nТекст задания: {context.user_data['task_text']}\nСрок выполнение: {deadline_date}\nВремя: {start_time} - {end_time}\nПодтвердите отправку:",
                reply_markup=keyboard
            )
            return CONFIRM_TASK
        except Exception as e:
            logger.error(f"Error uploading photo for task: {e}\n{traceback.format_exc()}")
            await update.message.reply_text("Ошибка при загрузке фото. Попробуйте снова.")
            return TASK_PHOTO_UPLOAD
    else:
        await update.message.reply_text("Пожалуйста, отправьте фото.")
        return TASK_PHOTO_UPLOAD

async def confirm_task(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_task":
        await query.message.reply_text("Отправка задания завершена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "task_confirm_send":
        project = context.user_data.get('selected_project')
        organizer = context.user_data.get('organizer')
        text = context.user_data.get('task_text')
        deadline_date = context.user_data.get('deadline_date')
        start_time = context.user_data.get('start_time')
        end_time = context.user_data.get('end_time')
        photo_path = context.user_data.get('task_photo')
        recipients = context.user_data.get('recipients')
        selected_volunteers = context.user_data.get('selected_volunteers', [])

        try:
            task = await create_task(project, organizer, text, deadline_date, start_time, end_time, photo_path)
            
            if recipients == "task_recipients_all":
                # Получаем волонтёров с отладочными логами
                volunteers_data = await sync_to_async(list)(VolunteerProject.objects.filter(
                    project=project
                ).select_related('volunteer'))
                logger.info(f"Found {len(volunteers_data)} VolunteerProject records for project {project.title}")
                
                volunteers = []
                for vp in volunteers_data:
                    if vp.volunteer and vp.volunteer.telegram_id:
                        volunteers.append(vp.volunteer)
                        logger.info(f"Volunteer added: {vp.volunteer.username} (telegram_id: {vp.volunteer.telegram_id})")
                    else:
                        logger.warning(f"Skipping VolunteerProject id={vp.id}: no volunteer or telegram_id")
                
                if not volunteers:
                    logger.warning(f"No valid volunteers found for project {project.title}")
                    await query.message.reply_text(
                        "Задание создано, но в проекте нет активных волонтёров с действительными telegram_id. "
                        "Проверьте список волонтёров в проекте через 'Просмотреть волонтёров'.",
                        reply_markup=get_org_keyboard()
                    )
                    return ConversationHandler.END
            else:
                volunteers = [v for v in selected_volunteers if v.telegram_id]

            if not volunteers:
                await query.message.reply_text(
                    "Задание создано, но в проекте нет волонтёров для отправки уведомлений.",
                    reply_markup=get_org_keyboard()
                )
                return ConversationHandler.END

            success_count = 0
            for volunteer in volunteers:
                try:
                    await sync_to_async(TaskAssignment.objects.create)(
                        task=task, 
                        volunteer=volunteer
                    )
                    
                    buttons = [
                        [InlineKeyboardButton("Да, хочу работать", callback_data=f"task_accept_{task.id}")],
                        [InlineKeyboardButton("Нет, не хочу", callback_data=f"task_decline_{task.id}")]
                    ]
                    keyboard = InlineKeyboardMarkup(buttons)
                    
                    deadline_date_str = deadline_date.strftime('%d-%m-%Y')
                    time_range = f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
                    
                    message_text = (
                        f"Новое задание для проекта {project.title}:\n"
                        f"{text}\n"
                        f"Срок выполнения: {deadline_date_str}\n"
                        f"Время: {time_range}\n"
                        "Хотите работать над этим заданием?"
                    )

                    if photo_path:
                        try:
                            async with aiofiles.open(os.path.join("media", photo_path), 'rb') as photo_file:
                                await context.bot.send_photo(
                                    chat_id=volunteer.telegram_id,
                                    photo=await photo_file.read(),
                                    caption=message_text,
                                    reply_markup=keyboard
                                )
                                success_count += 1
                        except Exception as e:
                            logger.error(f"Failed to send photo task to {volunteer.username}: {e}")
                    else:
                        await context.bot.send_message(
                            chat_id=volunteer.telegram_id,
                            text=message_text,
                            reply_markup=keyboard
                        )
                        success_count += 1
                except Exception as e:
                    logger.error(f"Failed to send task to {volunteer.username}: {e}\n{traceback.format_exc()}")

            await query.message.reply_text(
                f"Задание отправлено {success_count} волонтёрам из {len(volunteers)}!",
                reply_markup=get_org_keyboard()
            )
            
            context.user_data.clear()
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error in confirm_task: {e}\n{traceback.format_exc()}")
            await query.message.reply_text("Ошибка при отправке задания.")
            return ConversationHandler.END
                
async def check_photos(update, context):
    query = update.callback_query
    await query.answer() if query else None  # Безопасная обработка, если query отсутствует
    logger.info(f"Entering check_photos with callback_data: {getattr(query, 'data', 'No callback data')}")
    logger.info(f"context.user_data at start: {await sync_to_async(lambda: str(context.user_data))()}")

    user = query.from_user if query else update.message.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user or not db_user.is_organizer:
        logger.warning(f"Access denied for telegram_id: {telegram_id}, not an organizer")
        await (update.message.reply_text if not query else query.message.reply_text)("У вас нет прав организатора.")
        return ConversationHandler.END

    page = context.user_data.get('photos_page', 0)
    try:
        photos, total = await get_pending_photos_for_organizer(db_user, page)
        logger.info(f"Fetched photos: {len(photos)} photos, total: {total}")
        if not photos:
            logger.info("No pending photos found")
            await (update.message.reply_text if not query else query.message.reply_text)("Нет фото, ожидающих проверки.")
            return ConversationHandler.END

        total_pages = (total + PHOTOS_PER_PAGE - 1) // PHOTOS_PER_PAGE
        context.user_data['pending_photos'] = photos
        context.user_data['photos_page'] = page
        context.user_data['selected_photo'] = photos[0][0]  # Сохраняем первое фото
        logger.info(f"Saved pending_photos: {len(photos)} photos, page: {page}, total_pages: {total_pages}, selected_photo: {photos[0][0].id}")

        photo, volunteer_username, project_title, task = photos[0]
        logger.info(f"Processing photo: id={photo.id}, path={photo.image.path}")
        if not await sync_to_async(os.path.exists)(photo.image.path):
            logger.error(f"File not found: {photo.image.path}")
            await query.message.reply_text("Ошибка: файл фото не найден.")
            return ConversationHandler.END

        async with aiofiles.open(photo.image.path, 'rb') as photo_file:
            buttons = [
                [InlineKeyboardButton("✅ Подтверждаю выполнение", callback_data=f"mod_photo_action_0_approve"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_photo_action_0_reject")]
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            logger.info(f"Sending photo with keyboard: {buttons}")
            deadline_date = task.deadline_date.strftime('%d-%m-%Y') if task else "Не указана"
            time_range = f"{task.start_time.strftime('%H:%M')} - {task.end_time.strftime('%H:%M')}" if task else "Не указано"
            await (update.message.reply_photo if not query else query.message.reply_photo)(
                photo=await photo_file.read(),
                caption=f"Фото от {volunteer_username} (проект: {project_title})\nЗадание: {task.text if task else 'Нет задания'}\nСрок выполнение: {deadline_date}\nВремя: {time_range}",
                reply_markup=keyboard
            )

        keyboard = get_pagination_keyboard(page, total_pages)
        await (update.message.reply_text if not query else query.message.reply_text)(
            f"Фото, ожидающие проверки (страница {page + 1} из {total_pages}):",
            reply_markup=keyboard
        )
        logger.info(f"Transitioning to MODERATE_PHOTO state")
        return MODERATE_PHOTO
    except Exception as e:
        logger.error(f"Error in check_photos: {e}\n{traceback.format_exc()}")
        await (update.message.reply_text if not query else query.message.reply_text)(f"Ошибка при отображении фото: {str(e)}")
        return ConversationHandler.END

async def handle_photo_moderation_selection(update, context):
    query = update.callback_query
    await query.answer()
    logger.info(f"Received callback_data in handle_photo_moderation_selection: {query.data}")
    logger.info(f"context.user_data in handle_photo_moderation_selection: {await sync_to_async(lambda: str(context.user_data))()}")

    if query.data == "cancel_moderate":
        logger.info("Canceling photo moderation")
        await query.message.reply_text("Проверка фото отменена.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    if not query.data.startswith("photo_"):
        logger.warning(f"Unexpected callback_data in handle_photo_moderation_selection: {query.data}")
        await query.message.reply_text("Ошибка: неверная команда.")
        return MODERATE_PHOTO

    try:
        action, page = query.data.split('_')[1:3]
        page = int(page)
        logger.info(f"Pagination action: {action}, page: {page}")
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid pagination callback_data: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный формат пагинации.")
        return MODERATE_PHOTO

    if action == "prev":
        page -= 1
    elif action == "next":
        page += 1
    else:
        logger.error(f"Unknown pagination action: {action}")
        await query.message.reply_text("Ошибка: неизвестное действие пагинации.")
        return MODERATE_PHOTO

    context.user_data['photos_page'] = page
    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    try:
        context.user_data['photos_page'] = page
        photos, total = await get_pending_photos_for_organizer(db_user, page)
        total_pages = (total + PHOTOS_PER_PAGE - 1) // PHOTOS_PER_PAGE
        context.user_data['pending_photos'] = photos
        context.user_data['selected_photo'] = photos[0][0] if photos else None
        logger.info(f"Updated pending_photos: {len(photos)} photos, page: {page}, total_pages: {total_pages}")

        if not photos:
            logger.info("No photos on this page")
            await query.message.reply_text("Нет фото на этой странице.", reply_markup=get_org_keyboard())
            context.user_data.clear()
            return ConversationHandler.END

        photo, volunteer_username, project_title, task = photos[0]
        if not await sync_to_async(os.path.exists)(photo.image.path):
            logger.error(f"File not found: {photo.image.path}")
            await query.message.reply_text(f"Ошибка: файл фото {photo.image.path} не найден.")
            return ConversationHandler.END

        async with aiofiles.open(photo.image.path, 'rb') as photo_file:
            buttons = [
                [InlineKeyboardButton("✅ Подтверждаю выполнение", callback_data=f"mod_photo_action_0_approve"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_photo_action_0_reject")]
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            logger.info(f"Sending photo with keyboard: {buttons}")
            deadline_date = task.deadline_date.strftime('%d-%m-%Y') if task else "Не указана"
            time_range = f"{task.start_time.strftime('%H:%M')} - {task.end_time.strftime('%H:%M')}" if task else "Не указано"
            await query.message.reply_photo(
                photo=await photo_file.read(),
                caption=f"Фото от {volunteer_username} (проект: {project_title})\nЗадание: {task.text if task else 'Нет задания'}\nСрок выполнение: {deadline_date}\nВремя: {time_range}",
                reply_markup=keyboard
            )

        keyboard = get_pagination_keyboard(page, total_pages)
        await query.message.reply_text(
            f"Фото, ожидающие проверки (страница {page + 1} из {total_pages}):",
            reply_markup=keyboard
        )
        return MODERATE_PHOTO
    except Exception as e:
        logger.error(f"Error in handle_photo_moderation_selection: {e}\n{traceback.format_exc()}")
        await query.message.reply_text(f"Ошибка при обработке пагинации: {str(e)}")
        return MODERATE_PHOTO

async def handle_photo_moderation_action(update, context):
    query = update.callback_query
    await query.answer()
    logger.info(f"Processing photo moderation action with data: {query.data}")

    if not context.user_data.get('pending_photos'):
        logger.error("No pending_photos in context.user_data")
        await query.message.reply_text("Ошибка: список фотографий недоступен.")
        return ConversationHandler.END

    try:
        parts = query.data.split('_')
        if len(parts) != 5 or parts[0:3] != ['mod', 'photo', 'action']:
            logger.error(f"Invalid callback_data structure: {query.data}")
            await query.message.reply_text("Ошибка: неверный формат команды.")
            return ConversationHandler.END
        choice = int(parts[3])
        action = parts[4]
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback_data format: {query.data}, error: {e}")
        await query.message.reply_text("Ошибка: неверный формат данных.")
        return ConversationHandler.END

    photos = context.user_data.get('pending_photos', [])
    if not (0 <= choice < len(photos)):
        logger.error(f"Invalid photo choice: {choice}, available photos: {len(photos)}")
        await query.message.reply_text("Ошибка: выбранное фото недоступно.")
        return ConversationHandler.END

    photo, volunteer_username, project_title, task = photos[choice]
    context.user_data['selected_photo'] = photo

    try:
        if action == "approve":
            await approve_photo(photo)
            rating_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(str(i), callback_data=f"rating_{i}") for i in range(1, 6)],
                [InlineKeyboardButton("Пропустить", callback_data="rating_skip")]
            ])
            await query.message.reply_text(
                f"Фото от {volunteer_username} одобрено. Оцените работу волонтёра (1–5 звёзд):",
                reply_markup=rating_keyboard
            )
            context.user_data['awaiting_rating_for'] = photo.id
            return MODERATE_PHOTO_ACTION
        elif action == "reject":
            await reject_photo(photo, context)
            deadline_date = task.deadline_date.strftime('%d-%m-%Y') if task else "Не указана"
            time_range = f"{task.start_time.strftime('%H:%M')} - {task.end_time.strftime('%H:%M')}" if task else "Не указано"
            await query.message.edit_caption(
                caption=f"Фото от {volunteer_username} (проект: {project_title})\nЗадание: {task.text if task else 'Нет задания'}\nСрок выполнения: {deadline_date}\nВремя: {time_range}\n[Отклонено]"
            )
            return await show_next_photo(update, context)
        else:
            logger.error(f"Unknown action: {action}")
            await query.message.reply_text("Ошибка: неизвестное действие.")
            return MODERATE_PHOTO
    except Exception as e:
        logger.error(f"Failed to process action '{action}' for photo {photo.id}: {e}")
        await query.message.reply_text(f"Ошибка при обработке фото: {str(e)}")
        return ConversationHandler.END
    
# Обработчик команды /moderate_photos
async def moderate_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /moderate_photos command from telegram_id: {update.message.from_user.id}")
    user = update.message.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user or not db_user.is_organizer:
        logger.warning(f"Access denied for telegram_id: {telegram_id}, not an organizer")
        await update.message.reply_text("У вас нет прав организатора.")
        return

    # Создание нового объекта Update с callback_query
    fake_query = type('FakeQuery', (), {
        'data': 'check_photos',
        'from_user': user,
        'message': update.message,
        'answer': lambda *args: None  # Пустая реализация метода answer
    })()
    new_update = Update(
        update_id=update.update_id,
        message=update.message,
        callback_query=fake_query
    )
    await check_photos(new_update, context)

async def provide_feedback(update, context):
    query = update.callback_query
    await query.answer()

    task = context.user_data.get('selected_task')
    if not task:
        await query.message.reply_text("Задание не найдено.")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(i), callback_data=f"feedback_{i}") for i in range(1, 6)],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_feedback")]
    ])
    await query.message.reply_text("Оцените работу волонтёра (1-5):", reply_markup=keyboard)
    return FEEDBACK

async def handle_rating_selection(update, context):
    query = update.callback_query
    await query.answer()

    if not context.user_data.get('awaiting_rating_for'):
        await query.message.reply_text("Ошибка: не найдено фото для оценки.")
        return MODERATE_PHOTO

    try:
        photo_id = context.user_data['awaiting_rating_for']
        photo = await sync_to_async(Photo.objects.select_related('volunteer').get)(id=photo_id)

        if query.data == "rating_skip":
            rating = None
            message = "Оценка пропущена."
            photo.status = 'approved'
            photo.moderated_at = timezone.now()
            await sync_to_async(photo.save)()
        else:
            rating = int(query.data.split('_')[1])
            photo.rating = rating
            photo.status = 'approved'
            photo.moderated_at = timezone.now()
            await sync_to_async(photo.save)()
            if rating:
                volunteer = photo.volunteer
                volunteer.rating = min(100, volunteer.rating + rating * 2)
                await sync_to_async(volunteer.save)()
            message = f"Оценка {rating}★ сохранена."

        if photo.volunteer.telegram_id:
            if rating:
                await context.bot.send_message(
                    chat_id=photo.volunteer.telegram_id,
                    text=f"Ваше фото для проекта {photo.project.title} было одобрено! Ваша оценка: {rating}★"
                )
            else:
                await context.bot.send_message(
                    chat_id=photo.volunteer.telegram_id,
                    text=f"Ваше фото для проекта {photo.project.title} было одобрено!"
                )

        await query.message.edit_text(f"{message} Спасибо!")
        return await show_next_photo(update, context)
    except Exception as e:
        logger.error(f"Error handling rating: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка при сохранении оценки.")
        return MODERATE_PHOTO
    

async def show_next_photo(update, context):
    query = update.callback_query
    page = context.user_data.get('photos_page', 0)
    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    
    try:
        photos, total = await get_pending_photos_for_organizer(db_user, page)
        total_pages = (total + PHOTOS_PER_PAGE - 1) // PHOTOS_PER_PAGE
        
        if photos:
            photo, volunteer_username, project_title, task = photos[0]
            context.user_data['pending_photos'] = photos
            context.user_data['selected_photo'] = photo
            
            if not await sync_to_async(os.path.exists)(photo.image.path):
                logger.error(f"File not found: {photo.image.path}")
                await query.message.reply_text(f"Ошибка: файл фото {photo.image.path} не найден.")
                return ConversationHandler.END

            async with aiofiles.open(photo.image.path, 'rb') as photo_file:
                buttons = [
                    [InlineKeyboardButton("✅ Подтверждаю выполнение", callback_data=f"mod_photo_action_0_approve"),
                     InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_photo_action_0_reject")]
                ]
                keyboard = InlineKeyboardMarkup(buttons)
                
                deadline_date = task.deadline_date.strftime('%d-%m-%Y') if task else "Не указана"
                time_range = f"{task.start_time.strftime('%H:%M')} - {task.end_time.strftime('%H:%M')}" if task else "Не указано"
                
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=await photo_file.read(),
                    caption=f"Фото от {volunteer_username} (проект: {project_title})\nЗадание: {task.text if task else 'Нет задания'}\nСрок выполнение: {deadline_date}\nВремя: {time_range}",
                    reply_markup=keyboard
                )

            keyboard = get_pagination_keyboard(page, total_pages)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Фото, ожидающие проверки (страница {page + 1} из {total_pages}):",
                reply_markup=keyboard
            )
            return MODERATE_PHOTO
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Больше нет фото для проверки.",
                reply_markup=get_org_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error showing next photo: {e}\n{traceback.format_exc()}")
        await query.message.reply_text(f"Ошибка при загрузке следующего фото: {str(e)}")
        return ConversationHandler.END

async def feedback_rating(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_feedback":
        await query.message.reply_text("Отзыв отменён.", reply_markup=get_org_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        rating = int(query.data.split('_')[1])
        context.user_data['feedback_rating'] = rating
        await query.message.reply_text("Напишите комментарий (или отправьте /skip чтобы пропустить):")
        return FEEDBACK
    except Exception as e:
        logger.error(f"Error in feedback_rating: {e}")
        await query.message.reply_text("Ошибка. Попробуйте снова.")
        return FEEDBACK

async def feedback_comment(update, context):
    if update.message.text == "/skip":
        comment = None
    else:
        comment = update.message.text.strip()

    task = context.user_data.get('selected_task')
    volunteer = context.user_data.get('selected_volunteer')
    rating = context.user_data.get('feedback_rating')

    if not all([task, volunteer, rating]):
        await update.message.reply_text("Ошибка: недостаточно данных для сохранения отзыва.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        # Получаем assignment и обновляем его
        assignment = await sync_to_async(TaskAssignment.objects.get)(task=task, volunteer=volunteer)
        assignment.rating = rating
        assignment.feedback = comment
        await sync_to_async(assignment.save)()

        # Обновляем рейтинг волонтера
        volunteer.rating = min(100, volunteer.rating + rating * 2)  # Более консервативное увеличение
        await sync_to_async(volunteer.save)()

        await update.message.reply_text("Отзыв сохранён!", reply_markup=get_org_keyboard())
    except Exception as e:
        logger.error(f"Error saving feedback: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("Ошибка при сохранении отзыва.")

    context.user_data.clear()
    return ConversationHandler.END

# Регистрация обработчиков
def register_handlers(application):
    create_project_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(create_project_start, pattern=r"^create_project"),
            CommandHandler("org", org_menu)
        ],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_project_title)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_project_description)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_project_city)],
            TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_project_tags)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(create_project_cancel, pattern=r"^cancel_task")
        ],
        per_message=False
    )
    application.add_handler(create_project_conv)

    send_task_conv = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(send_task_start, pattern=r"^send_task")
    ],
    states={
        SELECT_PROJECT: [CallbackQueryHandler(select_project, pattern=r"^(task_project_|cancel_task)")],
        SELECT_RECIPIENTS: [CallbackQueryHandler(select_recipients, pattern=r"^(task_recipients_|cancel_task)")],
        SELECT_VOLUNTEERS: [CallbackQueryHandler(select_volunteers, pattern=r"^(task_volunteer_|task_volunteers_done|cancel_task)")],
        TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text)],
        TASK_DEADLINE_DATE: [
            CallbackQueryHandler(task_deadline_date_year, pattern=r"^deadline_date_year_"),
            CallbackQueryHandler(task_deadline_date_month, pattern=r"^deadline_date_month_"),
            CallbackQueryHandler(task_deadline_date_day, pattern=r"^deadline_date_day_")
        ],
        TASK_DEADLINE_START_TIME: [CallbackQueryHandler(task_deadline_start_time, pattern=r"^deadline_start_time_")],
        TASK_DEADLINE_END_TIME: [CallbackQueryHandler(task_deadline_end_time, pattern=r"^deadline_end_time_")],
        TASK_PHOTO: [CallbackQueryHandler(task_photo, pattern=r"^(task_photo_|cancel_task)")],
        TASK_PHOTO_UPLOAD: [MessageHandler(filters.PHOTO, task_photo_upload)],
        CONFIRM_TASK: [CallbackQueryHandler(confirm_task, pattern=r"^(task_confirm_send|cancel_task)")]
    },
    fallbacks=[
        CallbackQueryHandler(create_project_cancel, pattern=r"^cancel_task")
    ],
    per_message=False
)
    application.add_handler(send_task_conv)

    moderate_photo_conv = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(check_photos, pattern=r"^check_photos"),
        CommandHandler("moderate_photos", moderate_photos_command)
    ],
    states={
        MODERATE_PHOTO: [
            CallbackQueryHandler(handle_photo_moderation_selection, pattern=r"^(photo_prev_|photo_next_|cancel_moderate)"),
            CallbackQueryHandler(handle_photo_moderation_action, pattern=r"^mod_photo_action_\d+_(approve|reject)")
        ],
        MODERATE_PHOTO_ACTION: [
            CallbackQueryHandler(handle_rating_selection, pattern=r"^rating_"),
            CallbackQueryHandler(handle_photo_moderation_selection, pattern=r"^(photo_prev_|photo_next_|cancel_moderate)")
        ]
    },
    fallbacks=[
        CallbackQueryHandler(handle_photo_moderation_selection, pattern=r"^cancel_moderate")
    ],
    per_message=False
)
    application.add_handler(moderate_photo_conv)
    

    application.add_handler(CallbackQueryHandler(manage_volunteers, pattern=r"^manage_volunteers$"))
