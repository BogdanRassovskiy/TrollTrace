from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("series/", views.SeriesView.as_view(), name="series"),
    path("documents/", views.DocumentsView.as_view(), name="documents"),
    path("narratives/", views.NarrativesView.as_view(), name="narratives"),
]
