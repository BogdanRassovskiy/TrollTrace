from django.urls import path

from . import views

app_name = "validation"

urlpatterns = [
    path("", views.SampleListView.as_view(), name="index"),
    path("<int:pk>/", views.NextItemView.as_view(), name="next"),
    path("<int:pk>/polozka/<int:position>/", views.AnnotateView.as_view(), name="item"),
]
