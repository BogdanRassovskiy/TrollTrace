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
    help = "Přidělí uživateli roli „výzkumník“ nebo „pozorovatel“."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("role", choices=sorted(ROLES))
        parser.add_argument("--create", action="store_true", help="Vytvořit uživatele.")
        parser.add_argument("--password", help="Heslo nového uživatele (jinak se zeptá).")

    def handle(self, *args, username, role, create, password, **options):
        User = get_user_model()
        with transaction.atomic():
            user = User.objects.filter(username=username).first()
            if user is None:
                if not create:
                    raise CommandError(f"Uživatel {username} neexistuje. Přidejte --create.")
                password = password or getpass.getpass(f"Heslo pro {username}: ")
                if not password:
                    raise CommandError("Heslo nemůže být prázdné.")
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
