"""Вход, выход и неудачные попытки входа попадают в журнал аудита."""

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from . import audit
from .models import AuditLog


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit.log(request, AuditLog.Action.LOGIN, user=user)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        audit.log(request, AuditLog.Action.LOGOUT, user=user)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    audit.log(request, AuditLog.Action.LOGIN_FAILED, username=credentials.get("username", ""))
