from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from admissions import views as admission_views

urlpatterns = [
    path("", admission_views.home, name="home"),
    path("login/", auth_views.LoginView.as_view(template_name="admissions/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admissions/", include("admissions.urls")),
    path("django-admin/", admin.site.urls),
]
