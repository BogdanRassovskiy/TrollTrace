from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("dashboard/", include("apps.dashboard.urls")),
    path("panel/", include("apps.panel.urls")),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:  # фото акторов и прочие загрузки; в prod раздаёт веб-сервер
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
