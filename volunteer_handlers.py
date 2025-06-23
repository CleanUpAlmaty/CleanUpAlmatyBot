import logging
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone
import aiofiles
import aiofiles.os as aio_os
import os
import traceback

from core.models import User, Project, Photo, VolunteerProject, Task, TaskAssignment

# Настройка логирования
logger = logging.getLogger(__name__)

# Количество проектов на странице
PROJECTS_PER_PAGE = 5

# Максимальное количество проектов для волонтёра
MAX_PROJECTS_PER_VOLUNTEER = 1

# Состояния для ConversationHandler
TASK_CONFIRM, TASK_COMPLETED, TASK_PHOTO_UPLOAD = range(3)

# Основная клавиатура для волонтёров
def get_volunteer_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список проектов", callback_data="list_projects"),
         InlineKeyboardButton("➕ Присоединиться к проекту", callback_data="join_project")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
         InlineKeyboardButton("🚪 Выйти из проекта", callback_data="leave_project")]
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
def get_volunteer_project(volunteer):
    logger.info(f"Fetching volunteer project for {volunteer.username}")
    volunteer_project = VolunteerProject.objects.filter(volunteer=volunteer).select_related('project').first()
    if volunteer_project:
        logger.info(f"Volunteer project found: {volunteer_project.project.title}")
        return volunteer_project, volunteer_project.project.title
    logger.info(f"No volunteer project found for {volunteer.username}")
    return None, None

@sync_to_async
def create_photo(volunteer, project, file_path, task=None):
    logger.info(f"Creating photo for volunteer {volunteer.username} in project {project.title}")
    photo = Photo.objects.create(volunteer=volunteer, project=project, image=file_path, status='pending', task=task)
    logger.info(f"Photo created: {photo.id}")
    return photo

@sync_to_async
def get_approved_projects(volunteer, city=None, tag=None):
    logger.info(f"Fetching approved projects for volunteer {volunteer.username} (city={city}, tag={tag})")
    projects = Project.objects.filter(status='approved')
    if city:
        projects = projects.filter(city__iexact=city)
    if tag:
        projects = projects.filter(tags__name__in=[tag])
    
    joined_project_ids = VolunteerProject.objects.filter(volunteer=volunteer).values_list('project__id', flat=True)
    projects = projects.exclude(id__in=joined_project_ids)
    
    result = [(project, project.title, project.city, [tag.name for tag in project.tags.all()]) for project in projects]
    logger.info(f"Found {len(result)} approved projects for volunteer {volunteer.username}: {[p[1] for p in result]}")
    return result

@sync_to_async
def create_volunteer_project(volunteer, project):
    logger.info(f"Creating volunteer project for {volunteer.username} in project {project.title}")
    current_projects = VolunteerProject.objects.filter(volunteer=volunteer)
    if current_projects.count() >= MAX_PROJECTS_PER_VOLUNTEER:
        logger.warning(f"Volunteer {volunteer.username} has reached the maximum number of projects: {MAX_PROJECTS_PER_VOLUNTEER}")
        return None, None

    try:
        with transaction.atomic():
            volunteer_project = VolunteerProject.objects.create(volunteer=volunteer, project=project)
            logger.info(f"Volunteer project created: {volunteer_project.id}")
        transaction.commit()  # Явное завершение транзакции
        return volunteer_project, project.title
    except Exception as e:
        logger.error(f"Failed to create VolunteerProject for {volunteer.username} in project {project.title}: {e}\n{traceback.format_exc()}")
        return None, None

@sync_to_async
def get_volunteer_projects(volunteer):
    logger.info(f"Fetching projects for volunteer {volunteer.username}")
    volunteer_projects = VolunteerProject.objects.filter(volunteer=volunteer).select_related('project')
    result = [(vp, vp.project.title) for vp in volunteer_projects]
    logger.info(f"Found {len(result)} projects for volunteer {volunteer.username}: {[r[1] for r in result]}")
    return result

@sync_to_async
def delete_volunteer_project(volunteer_project):
    logger.info(f"Deleting volunteer project {volunteer_project.id}")
    volunteer_project.delete()
    logger.info(f"Volunteer project {volunteer_project.id} deleted")

@sync_to_async
def get_task(task_id):
    try:
        task = Task.objects.select_related('project__creator').get(id=task_id)
        logger.info(f"Task {task_id} loaded with project and creator")
        return task
    except Task.DoesNotExist:
        logger.warning(f"Task {task_id} not found")
        return None

@sync_to_async
def update_task_assignment(task, volunteer, accepted=None, completed=None):
    try:
        assignment = TaskAssignment.objects.get(task=task, volunteer=volunteer)
        if accepted is not None:
            assignment.accepted = accepted
        if completed is not None:
            assignment.completed = completed
            assignment.completed_at = timezone.now()
        assignment.save()
        return assignment
    except TaskAssignment.DoesNotExist:
        logger.error(f"TaskAssignment not found for task {task.id} and volunteer {volunteer.username}")
        return None

@sync_to_async
def get_current_date():
    return timezone.now()

def get_pagination_keyboard(page, total_pages):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"prev_{page}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Следующая ➡️", callback_data=f"next_{page}"))
    return InlineKeyboardMarkup([buttons])

async def volunteer_menu(update, context):
    user = update.message.from_user
    telegram_id = str(user.id)
    logger.info(f"Volunteer menu requested by telegram_id: {telegram_id}")
    db_user = await get_user(telegram_id)
    if not db_user:
        await update.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return

    await update.message.reply_text(
        f"Добро пожаловать, {db_user.username}!\nВыберите действие:",
        reply_markup=get_volunteer_keyboard()
    )

async def list_projects(update, context):
    query = update.callback_query
    await query.answer()

    args = context.args if context.args is not None else []
    city = args[0] if len(args) > 0 else None
    tag = args[1] if len(args) > 1 else None

    page = context.user_data.get('projects_page', 0)

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user:
        await query.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return

    projects = await get_approved_projects(db_user, city=city, tag=tag)
    if not projects:
        await query.message.reply_text("Нет доступных проектов по вашему запросу.")
        return

    total_projects = len(projects)
    total_pages = (total_projects + PROJECTS_PER_PAGE - 1) // PROJECTS_PER_PAGE
    start_idx = page * PROJECTS_PER_PAGE
    end_idx = min(start_idx + PROJECTS_PER_PAGE, total_projects)
    current_projects = projects[start_idx:end_idx]

    project_list = "\n".join([f"{i+1+start_idx}. {project[1]} ({project[2]}) - Теги: {', '.join(project[3])}" for i, project in enumerate(current_projects)])
    reply_text = f"Доступные проекты (страница {page+1} из {total_pages}):\n{project_list}\n\nЧтобы присоединиться, используйте 'Присоединиться к проекту'"

    keyboard = get_pagination_keyboard(page, total_pages)
    await query.message.reply_text(reply_text, reply_markup=keyboard)

async def handle_pagination(update, context):
    query = update.callback_query
    await query.answer()

    action, page = query.data.split('_')
    page = int(page)

    if action == "prev":
        page -= 1
    elif action == "next":
        page += 1

    context.user_data['projects_page'] = page
    await list_projects(update, context)

async def join_project(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user:
        await query.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return

    projects = await get_approved_projects(db_user)
    if not projects:
        await query.message.reply_text("Нет доступных проектов для участия.")
        return

    buttons = [
        [InlineKeyboardButton(f"{project[1]} ({project[2]})", callback_data=f"join_{i}")]
        for i, project in enumerate(projects)
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await query.message.reply_text("Выберите проект для участия:", reply_markup=keyboard)

    context.user_data['projects'] = projects
    context.user_data['db_user'] = db_user

async def handle_join_selection(update, context):
    query = update.callback_query
    await query.answer()

    try:
        choice = int(query.data.split('_')[1])
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback_data format: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор проекта.")
        return

    projects = context.user_data.get('projects', [])
    db_user = context.user_data.get('db_user')

    if 0 <= choice < len(projects):
        project = projects[choice][0]
        volunteer_project, project_title = await create_volunteer_project(db_user, project)
        if volunteer_project:
            await asyncio.sleep(1)  # Даём время на фиксацию транзакции
            await query.message.reply_text(f"Вы успешно зарегистрированы в проекте: {project_title}!")
        else:
            await query.message.reply_text(f"Вы не можете присоединиться к проекту: вы уже участвуете в максимальном количестве проектов ({MAX_PROJECTS_PER_VOLUNTEER}).")
    else:
        await query.message.reply_text("Неверный выбор проекта.")

    context.user_data.pop('projects', None)
    context.user_data.pop('db_user', None)

async def leave_project(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user:
        await query.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return

    projects = await get_volunteer_projects(db_user)
    if not projects:
        await query.message.reply_text("Вы не участвуете в проектах.")
        return

    buttons = [
        [InlineKeyboardButton(project[1], callback_data=f"leave_{i}")]
        for i, project in enumerate(projects)
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_leave")])
    keyboard = InlineKeyboardMarkup(buttons)
    await query.message.reply_text("Выберите проект, из которого хотите выйти:", reply_markup=keyboard)

    context.user_data['volunteer_projects'] = projects

async def handle_leave_selection(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_leave":
        await query.message.reply_text("Выход из проекта отменён.", reply_markup=get_volunteer_keyboard())
        context.user_data.clear()
        return

    try:
        choice = int(query.data.split('_')[1])
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback_data format: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный выбор проекта.")
        return

    projects = context.user_data.get('volunteer_projects', [])
    if 0 <= choice < len(projects):
        volunteer_project = projects[choice][0]
        project_title = projects[choice][1]
        await delete_volunteer_project(volunteer_project)
        await query.message.reply_text(f"Вы успешно вышли из проекта: {project_title}!")
    else:
        await query.message.reply_text("Неверный выбор проекта.")

    context.user_data.pop('volunteer_projects', None)

async def profile(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user:
        await query.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return

    volunteer_projects = await sync_to_async(lambda: list(VolunteerProject.objects.filter(volunteer=db_user).select_related('project')))()
    project_titles = [vp.project.title for vp in volunteer_projects]
    projects_text = "\n".join(project_titles) if project_titles else "Вы не участвуете в проектах."

    await query.message.reply_text(
        f"Ваш профиль:\nИмя: {db_user.username}\nРейтинг: {db_user.rating}\nПроекты:\n{projects_text}"
    )

async def task_accept_decline(update, context):
    query = update.callback_query
    await query.answer()

    logger.info(f"Processing task_accept_decline with callback_data: {query.data}")
    user = await get_user(str(query.from_user.id))
    if not user:
        await query.message.reply_text("Пользователь не найден.")
        return ConversationHandler.END

    try:
        task_id = int(query.data.split('_')[2])
        task = await sync_to_async(Task.objects.get)(id=task_id)
        project = await sync_to_async(Project.objects.get)(id=task.project_id)
        project_title = project.title
        assignment = await sync_to_async(TaskAssignment.objects.get)(task=task, volunteer=user)

        if query.data.startswith("task_accept"):
            assignment.accepted = True
            await sync_to_async(assignment.save)()
            # Используем deadline_date, start_time, end_time вместо deadline
            deadline_date_str = task.deadline_date.strftime('%Y-%m-%d') if task.deadline_date else "Не указана"
            time_range = f"{task.start_time.strftime('%H:%M') if task.start_time else '00:00'} - {task.end_time.strftime('%H:%M') if task.end_time else '23:59'}"
            await query.message.reply_text(f"Вы приняли задание для проекта {project_title}. Выполните его до {deadline_date_str} {time_range} и отправьте фото для проверки.")
            # Переход к загрузке фото
            context.user_data['task'] = task  # Сохраняем задачу для следующего шага
            await query.message.reply_text("Пожалуйста, прикрепите фото, подтверждающее выполнение задания:")
            return TASK_PHOTO_UPLOAD
        elif query.data.startswith("task_decline"):
            assignment.accepted = False
            await sync_to_async(assignment.save)()
            await query.message.reply_text(f"Вы отказались от задания для проекта {project_title}.")

        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in task_accept_decline: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка при обработке задания.")
        return ConversationHandler.END

async def task_confirm(update, context):
    query = update.callback_query
    await query.answer()
    logger.info(f"Processing task_confirm with callback_data: {query.data}")

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user:
        await query.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return ConversationHandler.END

    try:
        task_id = int(query.data.split('_')[2])
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid callback_data format: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный формат данных.")
        return ConversationHandler.END

    task = await get_task(task_id)
    if not task:
        await query.message.reply_text("Задание не найдено.")
        return ConversationHandler.END

    if task.deadline and task.deadline < timezone.now():
        await query.message.reply_text("Дедлайн для этого задания истёк.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton("Да", callback_data=f"task_completed_yes_{task.id}"),
         InlineKeyboardButton("Нет", callback_data=f"task_completed_no_{task.id}")]
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await query.message.reply_text("Вы выполнили задание?", reply_markup=keyboard)
    return TASK_COMPLETED

async def task_completed(update, context):
    query = update.callback_query
    await query.answer()
    logger.info(f"Processing task_completed with callback_data: {query.data}")

    user = query.from_user
    telegram_id = str(user.id)
    db_user = await get_user(telegram_id)
    if not db_user:
        await query.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return ConversationHandler.END

    parts = query.data.split('_')
    if len(parts) != 4 or parts[0] != "task" or parts[1] != "completed":
        logger.error(f"Invalid callback_data format: {query.data}")
        await query.message.reply_text("Ошибка: неверный формат данных.")
        return ConversationHandler.END

    action = parts[2]
    try:
        task_id = int(parts[3])
    except ValueError as e:
        logger.error(f"Invalid task_id in callback_data: {query.data}, error: {e}\n{traceback.format_exc()}")
        await query.message.reply_text("Ошибка: неверный формат данных.")
        return ConversationHandler.END

    task = await get_task(task_id)
    if not task:
        await query.message.reply_text("Задание не найдено.")
        return ConversationHandler.END

    if task.deadline and task.deadline < await get_current_date():
        await query.message.reply_text("Дедлайн для этого задания истёк.")
        return ConversationHandler.END

    if action == "yes":
        assignment = await update_task_assignment(task, db_user, completed=True)
        if not assignment:
            await query.message.reply_text("Ошибка: задание не назначено вам.")
            return ConversationHandler.END
        await query.message.reply_text("Пожалуйста, прикрепите фото, подтверждающее выполнение задания:")
        context.user_data['task'] = task  
        return TASK_PHOTO_UPLOAD
    else:
        await update_task_assignment(task, db_user, completed=False)
        await query.message.reply_text("Спасибо за информацию. Если вы выполните задание позже, дайте знать.")
        return ConversationHandler.END

async def task_photo_upload(update, context):
    user = update.message.from_user
    telegram_id = str(user.id)
    logger.info(f"Processing photo upload for user {telegram_id}")
    db_user = await get_user(telegram_id)
    if not db_user:
        await update.message.reply_text("Вы не зарегистрированы. Создайте аккаунт.")
        return ConversationHandler.END

    task = context.user_data.get('task')
    if not task:
        await update.message.reply_text("Задание не найдено.")
        context.user_data.clear()
        return ConversationHandler.END

    logger.info("Checking task deadline")
    current_date = await get_current_date()
    if task.deadline_date and task.deadline_date < current_date.date():
        await update.message.reply_text("Дедлайн для этого задания истёк.")
        context.user_data.clear()
        return ConversationHandler.END

    # Используем sync_to_async для доступа к связанным полям
    logger.info("Accessing task.project")
    project = await sync_to_async(lambda: task.project)()
    logger.info("Project accessed successfully")

    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            current_date = await get_current_date()
            year, month, day = current_date.year, current_date.month, current_date.day
            save_dir = os.path.join("media", f"photos/{year}/{month}/{day}")
            await aio_os.makedirs(save_dir, exist_ok=True)
            file_name = f"{telegram_id}_{photo_file.file_id}.jpg"
            full_path = os.path.join(save_dir, file_name)

            photo_data = await photo_file.download_as_bytearray()
            if not photo_data:
                raise ValueError("Downloaded photo data is empty")
            
            async with aiofiles.open(full_path, 'wb') as f:
                await f.write(photo_data)
            logger.info(f"Photo saved to {full_path}")

            db_file_path = os.path.join(f"photos/{year}/{month}/{day}", file_name)
            
            photo = await create_photo(db_user, project, db_file_path, task)
            logger.info(f"Photo saved with path: {photo.image.path}")
            await update.message.reply_text("Фото загружено и отправлено на проверку организатору.")

            logger.info("Accessing project.creator")
            organizer = await sync_to_async(lambda: project.creator)()
            logger.info(f"Organizer accessed: {organizer.telegram_id}")
            try:
                logger.info(f"Sending photo to organizer {organizer.telegram_id}")
                async with aiofiles.open(full_path, 'rb') as photo_file:
                    photo_bytes = await photo_file.read()
                await context.bot.send_photo(
                    chat_id=organizer.telegram_id,
                    photo=photo_bytes,
                    caption = f'Новое фото от волонтёра {db_user.username} для проекта {project.title} (задание: {task.text}) ожидает проверки.\nНажмите на кнопку "Проверить кнопку" для модерации.'
                )
            except Exception as e:
                logger.error(f"Failed to notify organizer {organizer.username} about new photo: {e}\n{traceback.format_exc()}")
                await update.message.reply_text("Фото загружено, но не удалось уведомить организатора. Свяжитесь с поддержкой.")

            context.user_data.clear()
            return ConversationHandler.END
        except ValueError as e:
            logger.error(f"Photo upload failed: {e}\n{traceback.format_exc()}")
            await update.message.reply_text("Ошибка: загруженное фото пустое. Попробуйте снова.")
            return TASK_PHOTO_UPLOAD
        except Exception as e:
            logger.error(f"Unexpected error uploading photo: {e}\n{traceback.format_exc()}")
            await update.message.reply_text("Ошибка при загрузке фото. Попробуйте снова.")
            return TASK_PHOTO_UPLOAD
    else:
        await update.message.reply_text("Пожалуйста, отправьте фото.")
        return TASK_PHOTO_UPLOAD

async def error_handler(update, context):
    logger.error(f"Update {update} caused error {context.error}\n{traceback.format_exc()}")
    if update and update.effective_message:
        await update.effective_message.reply_text("Произошла ошибка при обработке вашего сообщения. Попробуйте снова.")

def register_handlers(application):
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("projects", list_projects))
    application.add_handler(CommandHandler("join_project", join_project))
    application.add_handler(CallbackQueryHandler(list_projects, pattern=r"^list_projects"))
    application.add_handler(CallbackQueryHandler(join_project, pattern=r"^join_project"))
    application.add_handler(CallbackQueryHandler(profile, pattern=r"^profile"))
    application.add_handler(CallbackQueryHandler(handle_pagination, pattern=r"^(prev|next)_"))
    application.add_handler(CallbackQueryHandler(handle_join_selection, pattern=r"^join_"))
    application.add_handler(CallbackQueryHandler(leave_project, pattern=r"^leave_project"))
    application.add_handler(CallbackQueryHandler(handle_leave_selection, pattern=r"^(leave_|cancel_leave)"))

    task_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(task_accept_decline, pattern=r"^(task_accept_|task_decline_)")
        ],
        states={
            TASK_CONFIRM: [CallbackQueryHandler(task_confirm, pattern=r"^task_confirm_")],
            TASK_COMPLETED: [CallbackQueryHandler(task_completed, pattern=r"^task_completed_(yes|no)_")],
            TASK_PHOTO_UPLOAD: [MessageHandler(filters.PHOTO, task_photo_upload)]
        },
        fallbacks=[
            CallbackQueryHandler(task_completed, pattern=r"^task_completed_no_")
        ]
    )
    application.add_handler(task_conv)
