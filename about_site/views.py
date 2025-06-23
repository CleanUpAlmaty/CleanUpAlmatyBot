from django.shortcuts import render

def home(request):
    return render(request, 'about_site/index.html')

def services(request):
    return render(request, 'about_site/services.html')

def instruction(request):
    return render(request, 'about_site/instruction.html')

def admin_guide(request):
    return render(request, 'about_site/admin_guide.html')

def volunteer_guide(request):
    return render(request, 'about_site/volunteer_guide.html')

def organizer_guide(request):
    return render(request, 'about_site/organizer_guide.html')
