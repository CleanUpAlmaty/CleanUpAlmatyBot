from django.contrib import admin
from django.db.models import Count
from .models import User, Project, VolunteerProject, Photo, Task, TaskAssignment, timezone

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'telegram_id', 'phone_number', 'organization_name', 'rating', 'is_staff', 'is_organizer')
    list_filter = ('is_staff', 'is_organizer', 'organization_name')
    search_fields = ('username', 'telegram_id', 'phone_number', 'organization_name')
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal info', {'fields': ('telegram_id', 'phone_number', 'organization_name', 'rating')}),
        ('Permissions', {'fields': ('is_staff', 'is_organizer', 'groups', 'user_permissions')}),
    )
    actions = ['approve_organizer', 'reject_organizer']

    def approve_organizer(self, request, queryset):
        queryset.update(is_organizer=True)
        for user in queryset:
            if user.telegram_id:
                pass
    approve_organizer.short_description = "Одобрить статус организатора"

    def reject_organizer(self, request, queryset):
        queryset.update(is_organizer=False, organization_name=None)
        for user in queryset:
            if user.telegram_id:
                pass
    reject_organizer.short_description = "Отклонить статус организатора"

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('title', 'city', 'status', 'creator', 'volunteer_count')
    list_filter = ('status', 'city')
    search_fields = ('title', 'city')
    actions = ['approve_projects', 'reject_projects']

    def volunteer_count(self, obj):
        return obj.volunteer_projects.count()
    volunteer_count.short_description = "Количество волонтёров"

    def approve_projects(self, request, queryset):
        queryset.update(status='approved')
    approve_projects.short_description = "Одобрить выбранные проекты"

    def reject_projects(self, request, queryset):
        queryset.update(status='rejected')
    reject_projects.short_description = "Отклонить выбранные проекты"

@admin.register(Photo)
class PhotoAdmin(admin.ModelAdmin):
    list_display = ('volunteer', 'project', 'status', 'uploaded_at', 'image_preview')
    list_filter = ('status', 'uploaded_at')
    search_fields = ('volunteer__username', 'project__title')
    actions = ['approve_photos', 'reject_photos']
    readonly_fields = ('image_preview',)

    def image_preview(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            return f'<img src="{obj.image.url}" width="100" height="100" />'
        return "No image"
    image_preview.allow_tags = True
    image_preview.short_description = "Превью"

    def approve_photos(self, request, queryset):
        for photo in queryset:
            photo.status = 'approved'
            photo.moderated_at = timezone.now()
            
            # Обновляем рейтинг волонтера (если фото с оценкой)
            if photo.rating:
                photo.volunteer.update_rating(photo.rating * 2)  # Умножаем на 2 для значимости
            
            photo.save()
            self.message_user(request, f"Фото {photo.id} одобрено!")
    approve_photos.short_description = "Одобрить выбранные фото (+ рейтинг)"

    def reject_photos(self, request, queryset):
        queryset.update(status='rejected', moderated_at=timezone.now())
        self.message_user(request, f"Отклонено фото: {queryset.count()} шт.")
    reject_photos.short_description = "Отклонить выбранные фото"

@admin.register(VolunteerProject)
class VolunteerProjectAdmin(admin.ModelAdmin):
    list_display = ('volunteer', 'project', 'joined_at')
    list_filter = ('joined_at',)
    search_fields = ('volunteer__username', 'project__title')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'creator', 'status', 'created_at', 'deadline_date', 'start_time', 'end_time')
    list_filter = ('status', 'created_at')
    search_fields = ('project__title', 'creator__username')

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            volunteer_count=Count('assignments')
        )

    def volunteer_count(self, obj):
        return obj.volunteer_count
    volunteer_count.short_description = 'Количество волонтёров'

@admin.register(TaskAssignment)
class TaskAssignmentAdmin(admin.ModelAdmin):
    list_display = ('task', 'volunteer', 'accepted', 'completed', 'completed_at', 'rating', 'feedback')
    list_filter = ('accepted', 'completed')
    search_fields = ('task__id', 'volunteer__username')