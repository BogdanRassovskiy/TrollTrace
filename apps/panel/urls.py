from django.urls import path

from . import views, views_actors, views_codebook

app_name = "panel"

urlpatterns = [
    path("", views.PanelIndexView.as_view(), name="index"),
    path("audit/", views.AuditLogView.as_view(), name="audit"),
    path("sources/", views.SourcesHealthView.as_view(), name="sources"),
    path("sources/<int:pk>/", views.SourceDetailView.as_view(), name="source_detail"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path("akteri/", views_actors.ActorListView.as_view(), name="actors"),
    path("akteri/novy/", views_actors.ActorCreateView.as_view(), name="actor_create"),
    path("akteri/import/", views_actors.ActorImportView.as_view(), name="actor_import"),
    path("akteri/<int:pk>/", views_actors.ActorPanelDetailView.as_view(), name="actor"),
    path("kodova-kniha/", views_codebook.CodebookView.as_view(), name="codebook"),
    path(
        "kodova-kniha/narativ/novy/",
        views_codebook.NarrativeEditView.as_view(),
        name="narrative_create",
    ),
    path(
        "kodova-kniha/narativ/<slug:code>/",
        views_codebook.NarrativeHistoryView.as_view(),
        name="narrative_history",
    ),
    path(
        "kodova-kniha/narativ/<slug:code>/upravit/",
        views_codebook.NarrativeEditView.as_view(),
        name="narrative_edit",
    ),
    path("kodova-kniha/tema/nove/", views_codebook.TopicEditView.as_view(), name="topic_create"),
    path("kodova-kniha/tema/<int:pk>/", views_codebook.TopicEditView.as_view(), name="topic_edit"),
    path("nastaveni/", views_codebook.SettingsView.as_view(), name="settings"),
    path("nastaveni/<slug:key>/", views_codebook.SettingEditView.as_view(), name="setting"),
    path("ulohy/", views_codebook.JobListView.as_view(), name="jobs"),
    path(
        "notifications/read/", views.NotificationMarkReadView.as_view(), name="notifications-read"
    ),
]
