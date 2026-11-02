from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.PanelIndexView.as_view(), name="index"),
    path("audit/", views.AuditLogView.as_view(), name="audit"),
]
