"""Этап 2: вход и роли. Наблюдатель видит только рабочую часть (README §7.2 «Доступ»)."""

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.core.roles import OBSERVER, RESEARCHER, can_view_dashboard, is_researcher
from apps.panel.models import AuditLog

pytestmark = pytest.mark.django_db

DASHBOARD = reverse("dashboard:index")
PANEL = reverse("panel:index")
AUDIT = reverse("panel:audit")
ADMIN = reverse("admin:index")


@pytest.mark.parametrize("url", [DASHBOARD, PANEL, AUDIT])
def test_anonymous_redirected_to_login(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response.url == f"{reverse('login')}?next={url}"


def test_healthz_stays_public(client):
    assert client.get(reverse("core:healthz")).status_code == 200


def test_observer_sees_only_working_part(observer_client):
    response = observer_client.get(DASHBOARD)
    assert response.status_code == 200
    assert "Панель исследователя" not in response.content.decode()  # ссылки в навигации нет
    assert observer_client.get(PANEL).status_code == 403
    assert observer_client.get(AUDIT).status_code == 403


def test_observer_has_no_admin(observer_client):
    response = observer_client.get(ADMIN)
    assert response.status_code == 302  # не staff → на вход в админку


def test_researcher_sees_everything(researcher_client):
    for url in [DASHBOARD, PANEL, AUDIT, ADMIN]:
        assert researcher_client.get(url).status_code == 200, url
    assert "Панель исследователя" in researcher_client.get(DASHBOARD).content.decode()


def test_user_without_role_gets_403(client, make_user):
    client.force_login(make_user("nobody"))
    assert client.get(DASHBOARD).status_code == 403
    assert client.get(PANEL).status_code == 403


def test_superuser_is_researcher(django_user_model):
    admin = django_user_model.objects.create_superuser("root", password="x")
    assert is_researcher(admin) and can_view_dashboard(admin)


def test_role_permissions():
    researcher = Group.objects.get(name=RESEARCHER)
    observer = Group.objects.get(name=OBSERVER)
    assert researcher.permissions.filter(codename="change_actor").exists()
    assert researcher.permissions.filter(codename="view_auditlog").exists()
    assert not researcher.permissions.filter(codename__in=["change_auditlog", "delete_auditlog"])
    assert set(observer.permissions.values_list("codename", flat=True)) <= set(
        Permission.objects.filter(codename__startswith="view_").values_list("codename", flat=True)
    )


def test_login_page_and_login_flow(client, make_user):
    make_user("ivana", "researcher", password="tajne-heslo-1")
    assert client.get(reverse("login")).status_code == 200
    response = client.post(reverse("login"), {"username": "ivana", "password": "tajne-heslo-1"})
    assert response.status_code == 302
    assert response.url == DASHBOARD


def test_login_logout_and_failure_are_audited(client, make_user):
    make_user("ivana", "researcher", password="tajne-heslo-1")
    client.post(reverse("login"), {"username": "ivana", "password": "spatne"})
    client.post(reverse("login"), {"username": "ivana", "password": "tajne-heslo-1"})
    client.post(reverse("logout"))
    actions = list(AuditLog.objects.order_by("pk").values_list("action", "username"))
    assert actions == [("login_failed", "ivana"), ("login", "ivana"), ("logout", "ivana")]


class TestAssignRoleCommand:
    def test_create_user_with_role(self):
        call_command("assign_role", "vedouci", "observer", "--create", "--password", "x-12345")
        entry = AuditLog.objects.get(action=AuditLog.Action.ROLE)
        assert entry.changes == {"role": ["Без роли", OBSERVER]}
        assert entry.object_repr == "vedouci"

    def test_switch_role(self, make_user):
        user = make_user("ivana", "observer")
        call_command("assign_role", "ivana", "researcher")
        user.refresh_from_db()
        assert is_researcher(user) and user.is_staff
        assert list(user.groups.values_list("name", flat=True)) == [RESEARCHER]

    def test_missing_user(self):
        with pytest.raises(CommandError):
            call_command("assign_role", "ghost", "observer")
