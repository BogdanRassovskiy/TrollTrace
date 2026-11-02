from django.conf import settings

from .middleware import GUEST_USERNAME
from .roles import can_view_dashboard, is_researcher, role_label


def project(request):
    """Общие для всех шаблонов значения: версия и роль текущего пользователя."""
    user = request.user
    return {
        "PROJECT_VERSION": settings.TROLLTRACE["VERSION"],
        "user_is_researcher": is_researcher(user),
        "user_can_view_dashboard": can_view_dashboard(user),
        "user_role": role_label(user) if user.is_authenticated else "",
        "open_access_guest": settings.OPEN_ACCESS and user.get_username() == GUEST_USERNAME,
    }
