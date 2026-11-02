"""Назначение роли пользователю (README §7.2 «Доступ»). Изменение пишется в журнал аудита.

python manage.py assign_role ivana researcher
python manage.py assign_role vedouci observer --create      # спросит пароль
"""

import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.roles import ROLES, assign_role, role_label
from apps.panel import audit
from apps.panel.models import AuditLog


class Command(BaseCommand):
    help = "Назначает пользователю роль «исследователь» или «наблюдатель»."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("role", choices=sorted(ROLES))
        parser.add_argument("--create", action="store_true", help="Создать пользователя.")
        parser.add_argument("--password", help="Пароль для нового пользователя (иначе спросит).")

    def handle(self, *args, username, role, create, password, **options):
        User = get_user_model()
        with transaction.atomic():
            user = User.objects.filter(username=username).first()
            if user is None:
                if not create:
                    raise CommandError(f"Пользователя {username} нет. Добавьте --create.")
                password = password or getpass.getpass(f"Пароль для {username}: ")
                if not password:
                    raise CommandError("Пароль не может быть пустым.")
                user = User.objects.create_user(username=username, password=password)
                audit.log(None, AuditLog.Action.CREATE, user, note="CLI: assign_role --create")
            old = role_label(user)
            assign_role(user, role)
            new = role_label(user)
            if old != new:
                audit.log(
                    None,
                    AuditLog.Action.ROLE,
                    user,
                    changes={"role": [old, new]},
                    note="CLI: assign_role",
                )
        self.stdout.write(self.style.SUCCESS(f"{username}: {new}"))
