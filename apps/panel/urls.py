from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.PanelIndexView.as_view(), name="index"),
]
