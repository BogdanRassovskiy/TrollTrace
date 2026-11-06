from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardIndexView.as_view(), name="index"),
    path("akter/<int:pk>/", views.ActorDetailView.as_view(), name="actor"),
    path("srovnani/", views.CompareView.as_view(), name="compare"),
    path("narativy/", views.NarrativeListView.as_view(), name="narratives"),
    path("narativy/<slug:code>/", views.NarrativeDetailView.as_view(), name="narrative"),
    path("mapa/", views.TreeMapView.as_view(), name="map"),
    path("prevzeti/", views.ReuseListView.as_view(), name="reuse"),
    path("prevzeti/<int:pk>/", views.ReuseDetailView.as_view(), name="reuse_detail"),
]
