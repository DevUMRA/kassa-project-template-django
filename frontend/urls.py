from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path('', views.app, name='app'),
    path('login/', auth_views.LoginView.as_view(
        template_name='frontend/login.html', redirect_authenticated_user=True,
    ), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]
