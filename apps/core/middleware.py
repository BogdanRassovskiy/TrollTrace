"""Временный открытый доступ: пока защита не настроена, все работают как общий пользователь.

Включается флагом OPEN_ACCESS (в dev по умолчанию включён, в prod — выключен). Анониму
подставляется суперпользователь guest без пароля: страницы, панель и админка открыты всем,
а журнал аудита продолжает писать изменения — от имени guest.
"""

from django.conf import settings
from django.contrib.auth import get_user_model

GUEST_USERNAME = "guest"


def get_guest_user():
    User = get_user_model()
    user = User.objects.filter(username=GUEST_USERNAME).first()
    if user is None:
        user = User(username=GUEST_USERNAME, is_staff=True, is_superuser=True)
        user.set_unusable_password()  # войти под guest по паролю нельзя
        user.save()
    return user


class OpenAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.OPEN_ACCESS and not request.user.is_authenticated:
            request.user = get_guest_user()
        return self.get_response(request)
