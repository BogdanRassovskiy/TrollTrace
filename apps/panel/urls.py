from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.PanelIndexView.as_view(), name="index"),
    path("audit/", views.AuditLogView.as_view(), name="audit"),
    path("sources/", views.SourcesHealthView.as_view(), name="sources"),
    path("sources/<int:pk>/", views.SourceDetailView.as_view(), name="source_detail"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path(
        "notifications/read/", views.NotificationMarkReadView.as_view(), name="notifications-read"
    ),
]
