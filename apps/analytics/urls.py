from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("series/", views.SeriesView.as_view(), name="series"),
    path("actor/", views.ActorSeriesView.as_view(), name="actor"),
    path("documents/", views.DocumentsView.as_view(), name="documents"),
    path("narratives/", views.NarrativesView.as_view(), name="narratives"),
    path("tree-map/", views.TreeMapView.as_view(), name="tree_map"),
    path("narrative-shares/", views.NarrativeSharesView.as_view(), name="narrative_shares"),
]
