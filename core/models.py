from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from taggit.managers import TaggableManager
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver
from telegram.ext import Application
from asgiref.sync import async_to_sync
import os

bot = Application.builder().token('7633935996:AAH1VW2r-6akFzay6nQW2wSkYa8j7JgWQvI').build()

def photo_upload_path(instance, filename):
    """Generate path for uploaded photos: photos/year/month/day/filename"""
    date = timezone.now().strftime("%Y/%m/%d")
    return os.path.join('photos', date, filename)

def task_image_upload_path(instance, filename):
    """Generate path for task images: tasks/year/month/day/filename"""
    date = timezone.now().strftime("%Y/%m/%d")
    return os.path.join('tasks', date, filename)

class User(AbstractUser):
    telegram_id = models.CharField(max_length=50, unique=True, blank=True, null=True)
    phone_number = models.CharField(
        max_length=15,
        unique=True,
        blank=True,
        null=True,
        validators=[RegexValidator(regex=r'^\+?\d{10,15}$', message="Номер телефона должен быть в формате: '+1234567890'.")],
        help_text="Номер телефона в формате +1234567890"
    )
    organization_name = models.CharField(max_length=255, blank=True, null=True, 
                                       help_text="Название организации (для организаторов)")
    rating = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Рейтинг пользователя от 0 до 100"
    )
    is_organizer = models.BooleanField(
        default=False,
        help_text="Является ли пользователь организатором"
    )

    def update_rating(self, points):
        """Обновляет рейтинг пользователя с учетом ограничений"""
        self.rating = max(0, min(100, self.rating + points))
        self.save()

    def __str__(self):
        return f"{self.username} (ID: {self.telegram_id}, Phone: {self.phone_number}, Org: {self.organization_name})"

    class Meta:
        verbose_name = 'Пользователь'
        verbose_name_plural = 'Пользователи'
        ordering = ['-rating', 'username']

class Project(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Ожидает проверки'),
        ('approved', 'Одобрен'),
        ('rejected', 'Отклонён'),
    )
    title = models.CharField(max_length=255, help_text="Название проекта")
    description = models.TextField(help_text="Описание проекта")
    city = models.CharField(max_length=100, help_text="Город реализации проекта")
    creator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_projects',
        limit_choices_to={'is_organizer': True},
        help_text="Организатор проекта"
    )
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending',
        db_index=True,
        help_text="Статус модерации проекта"
    )
    tags = TaggableManager(help_text="Теги проекта")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    volunteers = models.ManyToManyField(
        User,
        through='VolunteerProject',
        related_name='projects',
        blank=True,
        help_text="Волонтеры проекта"
    )

    def approve(self):
        """Одобряет проект"""
        self.status = 'approved'
        self.save()

    def reject(self):
        """Отклоняет проект"""
        self.status = 'rejected'
        self.save()

    def __str__(self):
        return f"{self.title} (Creator: {self.creator.username})"

    class Meta:
        verbose_name = 'Проект'
        verbose_name_plural = 'Проекты'
        ordering = ['-created_at']

class VolunteerProject(models.Model):
    volunteer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='volunteer_projects',
        limit_choices_to={'is_organizer': False},
        help_text="Волонтер"
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='volunteer_projects',
        help_text="Проект"
    )
    joined_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_active = models.BooleanField(default=True, help_text="Активно ли участие")

    class Meta:
        unique_together = ('volunteer', 'project')
        verbose_name = 'Участие волонтера'
        verbose_name_plural = 'Участия волонтеров'
        ordering = ['-joined_at']

    def __str__(self):
        status = "активно" if self.is_active else "неактивно"
        return f"{self.volunteer.username} in {self.project.title} ({status})"

class Task(models.Model):
    STATUS_CHOICES = (
        ('open', 'Открыта'),
        ('in_progress', 'В работе'),
        ('completed', 'Выполнена'),
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='tasks',
        help_text="Проект"
    )
    creator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_tasks',
        limit_choices_to={'is_organizer': True},
        help_text="Создатель задания"
    )
    text = models.TextField(help_text="Текст задания")
    task_image = models.ImageField(
        upload_to=task_image_upload_path,
        null=True,
        blank=True,
        help_text="Изображение для задания"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    deadline_date = models.DateField(null=True, blank=True, help_text="Дата дедлайна")
    start_time = models.TimeField(null=True, blank=True, help_text="Время начала")
    end_time = models.TimeField(null=True, blank=True, help_text="Время окончания")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='open',
        db_index=True,
        help_text="Статус задания"
    )
    volunteers = models.ManyToManyField(
        User,
        through='TaskAssignment',
        related_name='tasks',
        blank=True,
        help_text="Волонтеры, выполняющие задание"
    )

    def is_expired(self):
        """Проверяет, истек ли срок выполнения задания"""
        if not self.deadline_date:
            return False
        now = timezone.now()
        return now.date() > self.deadline_date or (
            now.date() == self.deadline_date and 
            self.end_time and now.time() > self.end_time
        )

    def __str__(self):
        return f"Task {self.id} for {self.project.title} by {self.creator.username}"

    class Meta:
        verbose_name = 'Задание'
        verbose_name_plural = 'Задания'
        ordering = ['-created_at']

class Photo(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Ожидает проверки'),
        ('approved', 'Одобрен'),
        ('rejected', 'Отклонён'),
    )
    volunteer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='photos',
        limit_choices_to={'is_organizer': False},
        help_text="Волонтер"
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='photos',
        help_text="Проект"
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='task_photos',
        help_text="Связанное задание"
    )
    image = models.ImageField(
        upload_to=photo_upload_path,
        help_text="Фотоотчет"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
        help_text="Статус модерации"
    )
    rating = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Оценка от 1 до 5 звезд"
    )
    feedback = models.TextField(
        null=True,
        blank=True,
        help_text="Комментарий организатора"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    moderated_at = models.DateTimeField(null=True, blank=True, help_text="Дата модерации")

    def approve(self, rating=None, feedback=None):
        """Одобряет фото с оценкой и комментарием"""
        self.status = 'approved'
        self.rating = rating
        self.feedback = feedback
        self.moderated_at = timezone.now()
        self.save()
        # Обновляем рейтинг волонтера
        if rating:
            self.volunteer.update_rating(rating * 2)

    def reject(self, feedback=None):
        """Отклоняет фото с комментарием"""
        self.status = 'rejected'
        self.feedback = feedback
        self.moderated_at = timezone.now()
        self.save()

    def is_moderated(self):
        """Проверяет, было ли фото проверено"""
        return self.status != 'pending'

    def __str__(self):
        base = f"Photo {self.id} by {self.volunteer.username}"
        if self.rating:
            return f"{base} [Rating: {self.rating}★]"
        return f"{base} [Status: {self.get_status_display()}]"

    class Meta:
        verbose_name = 'Фотоотчет'
        verbose_name_plural = 'Фотоотчеты'
        ordering = ['-uploaded_at']

class TaskAssignment(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='assignments',
        help_text="Задание"
    )
    volunteer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='assignments',
        limit_choices_to={'is_organizer': False},
        help_text="Волонтер"
    )
    accepted = models.BooleanField(default=False, help_text="Принял ли волонтер задание")
    completed = models.BooleanField(default=False, help_text="Выполнено ли задание")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="Дата выполнения")
    rating = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        help_text="Оценка выполнения"
    )
    feedback = models.TextField(null=True, blank=True, help_text="Отзыв о выполнении")

    class Meta:
        unique_together = ('task', 'volunteer')
        verbose_name = 'Назначение задания'
        verbose_name_plural = 'Назначения заданий'
        ordering = ['-completed_at']

    def __str__(self):
        status = "выполнено" if self.completed else "не выполнено"
        return f"Assignment: {self.volunteer.username} -> {self.task} ({status})"

@receiver(post_save, sender=TaskAssignment)
def update_completed_at(sender, instance, **kwargs):
    """Обновляет дату выполнения при завершении задания"""
    if instance.completed and not instance.completed_at:
        instance.completed_at = timezone.now()
        instance.save()

@receiver(post_save, sender=User)
def user_status_changed(sender, instance, created, **kwargs):
    """Уведомляет при изменении статуса пользователя"""
    if created:  
        return
        
    try:
        if instance.is_organizer != instance._original_is_organizer:  
            from organization_handlers import notify_organizer_status
            from asgiref.sync import async_to_sync
            async_to_sync(notify_organizer_status)(instance, bot)
    except AttributeError:
        pass

def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._original_is_organizer = self.is_organizer