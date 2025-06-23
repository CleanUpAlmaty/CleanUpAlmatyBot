from django.urls import path
from . import views

app_name = 'about_site'  

urlpatterns = [
    path('', views.home, name='home'),
    path('services/', views.services, name='services'),
    path('instruction/', views.instruction, name='instruction'),
    path('guide/admin/', views.admin_guide, name='admin_guide'),
    path('guide/volunteer/', views.volunteer_guide, name='volunteer_guide'),
    path('guide/organizer/', views.organizer_guide, name='organizer_guide'),
]