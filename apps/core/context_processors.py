from django.conf import settings

from apps.actors.models import Layer

from .middleware import GUEST_USERNAME
from .roles import can_annotate, can_view_dashboard, is_researcher, role_label


def project(request):
    """Общие для всех шаблонов значения: версия и роль текущего пользователя."""
    user = request.user
    return {
        "PROJECT_VERSION": settings.TROLLTRACE["VERSION"],
        "user_is_researcher": is_researcher(user),
        "user_can_view_dashboard": can_view_dashboard(user),
        "user_can_annotate": can_annotate(user),
        "user_role": role_label(user) if user.is_authenticated else "",
        "LAYER_NAMES": dict(Layer.choices),  # для JS (TT.layerName)
        "open_access_guest": settings.OPEN_ACCESS and user.get_username() == GUEST_USERNAME,
    }
