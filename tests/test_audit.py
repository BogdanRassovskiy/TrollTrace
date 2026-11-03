"""Этап 2: журнал аудита — кто, когда, что изменил, старое → новое."""

import datetime

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory
from django.urls import reverse
from django.views.generic import UpdateView

from apps.actors.models import Actor
from apps.panel import audit
from apps.panel.models import AuditLog

from .factories import ActorFactory, NarrativeFactory, SettingFactory, SourceFactory

pytestmark = pytest.mark.django_db


def entries(action=None):
    qs = AuditLog.objects.exclude(action__in=["login", "logout"]).order_by("pk")
    return qs.filter(action=action) if action else qs


# --- Сервис ---------------------------------------------------------------------------------


class TestService:
    def test_diff_only_changed_fields(self):
        assert audit.diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {
            "b": [2, 3],
            "c": [None, 4],
        }

    def test_snapshot_skips_technical_fields(self):
        source = SourceFactory()
        snap = audit.snapshot(source)
        assert snap["name"] == source.name
        assert "created_at" not in snap and "updated_at" not in snap

    def test_log_change_records_who_what_old_new(self, researcher):
        request = RequestFactory().post("/", REMOTE_ADDR="10.0.0.7")
        request.user = researcher
        actor = ActorFactory(party="ANO")
        before = audit.snapshot(actor)
        actor.party = "SPD"
        actor.save()

        entry = audit.log_change(request, actor, before)
        assert entry.username == "researcher"
        assert entry.action == AuditLog.Action.UPDATE
        assert entry.object_id == str(actor.pk) and entry.object_repr == actor.name
        assert entry.content_type.model == "actor"
        assert entry.changes == {"party": ["ANO", "SPD"]}
        assert entry.ip_address == "10.0.0.7"
        assert entry.timestamp is not None

    def test_noop_change_not_logged(self, researcher):
        actor = ActorFactory()
        assert audit.log_change(None, actor, audit.snapshot(actor), user=researcher) is None
        assert not entries().exists()

    def test_audited_form_mixin(self, researcher):
        """Будущие страницы панели (UpdateView) пишут журнал через миксин."""

        class ActorUpdate(audit.AuditedFormMixin, UpdateView):
            model = Actor
            fields = ["name", "party"]
            success_url = "/"

        actor = ActorFactory(party="ODS")
        request = RequestFactory().post("/", {"name": actor.name, "party": "STAN"})
        request.user = researcher
        response = ActorUpdate.as_view()(request, pk=actor.pk)

        assert response.status_code == 302
        assert entries(AuditLog.Action.UPDATE).get().changes == {"party": ["ODS", "STAN"]}


# --- Журнал только дополняется --------------------------------------------------------------


class TestAppendOnly:
    def test_sql_update_and_delete_blocked(self):
        entry = audit.log(None, AuditLog.Action.RUN, note="x")
        for sql in [
            "UPDATE panel_auditlog SET note = 'подделка' WHERE id = %s",
            "DELETE FROM panel_auditlog WHERE id = %s",
        ]:
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
                cur.execute(sql, [entry.pk])
        entry.refresh_from_db()
        assert entry.note == "x"

    def test_queryset_update_blocked(self):
        audit.log(None, AuditLog.Action.RUN)
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditLog.objects.update(note="подделка")

    def test_deleting_user_keeps_entries(self, make_user):
        user = make_user("temp", "researcher")
        audit.log(None, AuditLog.Action.RUN, user=user)
        user.delete()
        entry = AuditLog.objects.get(action=AuditLog.Action.RUN)
        assert entry.user is None and entry.username == "temp"


# --- Админка --------------------------------------------------------------------------------


def admin_url(obj, view="change"):
    meta = obj._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_{view}", args=[obj.pk])


def actor_form(actor, **overrides):
    data = {
        "name": actor.name,
        "actor_type": actor.actor_type,
        "layer": actor.layer,
        "party": actor.party,
        "role": actor.role,
        "activity_cohort": actor.activity_cohort,
        "status": actor.status,
        "inclusion_criterion": actor.inclusion_criterion,
        "inclusion_reference": actor.inclusion_reference,
        "positions-TOTAL_FORMS": "0",
        "positions-INITIAL_FORMS": "0",
        "inclusion_periods-TOTAL_FORMS": "0",
        "inclusion_periods-INITIAL_FORMS": "0",
        "accounts-TOTAL_FORMS": "0",
        "accounts-INITIAL_FORMS": "0",
    }
    return data | overrides


class TestAdminAudit:
    def test_change(self, researcher_client):
        actor = ActorFactory(party="ANO")
        response = researcher_client.post(admin_url(actor), actor_form(actor, party="SPD"))
        assert response.status_code == 302, response.content.decode()[:2000]
        entry = entries(AuditLog.Action.UPDATE).get()
        assert entry.username == "researcher"
        assert entry.changes == {"party": ["ANO", "SPD"]}

    def test_create_and_inline(self, researcher_client):
        source = SourceFactory()
        data = actor_form(
            Actor(name="Nový kanál", actor_type="channel", layer="C"),
            **{
                "accounts-TOTAL_FORMS": "1",
                "accounts-0-source": str(source.pk),
                "accounts-0-account_id": "novy_kanal",
                "inclusion_periods-TOTAL_FORMS": "1",
                "inclusion_periods-0-start_date": "2026-01-05",
            },
        )
        response = researcher_client.post(reverse("admin:actors_actor_add"), data)
        assert response.status_code == 302, response.content.decode()[:2000]
        created = {e.content_type.model: e for e in entries(AuditLog.Action.CREATE)}
        assert set(created) == {"actor", "actoraccount", "actorinclusionperiod"}
        assert created["actoraccount"].changes["account_id"] == [None, "novy_kanal"]

    def test_inline_change_and_delete(self, researcher_client):
        actor = ActorFactory()
        period = actor.include(on=datetime.date(2026, 1, 5))
        prefix = "inclusion_periods"
        base = actor_form(
            actor,
            **{
                f"{prefix}-TOTAL_FORMS": "1",
                f"{prefix}-INITIAL_FORMS": "1",
                f"{prefix}-0-id": str(period.pk),
                f"{prefix}-0-actor": str(actor.pk),
                f"{prefix}-0-start_date": "2026-01-05",
                f"{prefix}-0-end_date": "2026-02-01",
                f"{prefix}-0-exclusion_reason": "test",
            },
        )
        assert researcher_client.post(admin_url(actor), base).status_code == 302
        change = entries(AuditLog.Action.UPDATE).get()
        assert change.changes == {
            "end_date": [None, "2026-02-01"],
            "exclusion_reason": ["", "test"],
        }

        base[f"{prefix}-0-DELETE"] = "on"
        assert researcher_client.post(admin_url(actor), base).status_code == 302
        deleted = entries(AuditLog.Action.DELETE).get()
        assert deleted.content_type.model == "actorinclusionperiod"
        assert deleted.changes["start_date"] == ["2026-01-05", None]

    def test_delete(self, researcher_client):
        source = SourceFactory(name="Smazat")
        response = researcher_client.post(admin_url(source, "delete"), {"post": "yes"})
        assert response.status_code == 302
        entry = entries(AuditLog.Action.DELETE).get()
        assert entry.object_repr.startswith("Smazat") and entry.changes["name"] == ["Smazat", None]

    def test_bulk_delete_action(self, researcher_client):
        sources = SourceFactory.create_batch(3)
        response = researcher_client.post(
            reverse("admin:actors_source_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [s.pk for s in sources],
                "post": "yes",
            },
        )
        assert response.status_code == 302
        assert entries(AuditLog.Action.DELETE).count() == 3

    def test_versioned_model_records_author(self, researcher_client, researcher):
        setting = SettingFactory(value=3)
        response = researcher_client.post(
            admin_url(setting),
            {
                "key": setting.key,
                "value": "4",
                "description": "",
                "versions-TOTAL_FORMS": "0",
                "versions-INITIAL_FORMS": "0",
            },
        )
        assert response.status_code == 302, response.content.decode()[:2000]
        assert setting.versions.get(version=2).changed_by == researcher
        assert entries(AuditLog.Action.UPDATE).get().changes == {
            "current_version": [1, 2],
            "value": [3, 4],
        }

    def test_audit_log_read_only_in_admin(self, researcher_client):
        entry = audit.log(None, AuditLog.Action.RUN)
        assert researcher_client.get(reverse("admin:panel_auditlog_add")).status_code == 403
        assert (
            researcher_client.post(admin_url(entry, "delete"), {"post": "yes"}).status_code == 403
        )


# --- Страница журнала -----------------------------------------------------------------------


class TestAuditPage:
    def test_lists_and_filters(self, researcher_client, researcher):
        actor = ActorFactory(name="Hledaný aktér")
        narrative = NarrativeFactory()
        audit.log(
            None, AuditLog.Action.UPDATE, actor, changes={"party": ["A", "B"]}, user=researcher
        )
        audit.log(None, AuditLog.Action.CREATE, narrative, user=researcher)

        page = researcher_client.get(reverse("panel:audit")).content.decode()
        assert "Hledaný aktér" in page and narrative.code in page and "party" in page

        filtered = researcher_client.get(reverse("panel:audit"), {"action": "create"})
        assert "Hledaný aktér" not in filtered.content.decode()
        searched = researcher_client.get(reverse("panel:audit"), {"q": "hledaný"})
        assert [e.object_repr for e in searched.context["entries"]] == ["Hledaný aktér"]


class TestUserAdminAudit:
    def test_role_change_via_admin_is_audited(self, client, django_user_model, make_user):
        from django.contrib.auth.models import Group

        from apps.core.roles import OBSERVER, RESEARCHER

        root = django_user_model.objects.create_superuser("root", password="x")
        client.force_login(root)
        target = make_user("ivana", "observer")
        response = client.post(
            reverse("admin:auth_user_change", args=[target.pk]),
            {
                "username": "ivana",
                "is_active": "on",
                "groups": [Group.objects.get(name=RESEARCHER).pk],
                "date_joined_0": "2026-01-01",
                "date_joined_1": "10:00:00",
            },
        )
        assert response.status_code == 302, response.content.decode()[:2000]
        entry = entries(AuditLog.Action.ROLE).get()
        assert entry.username == "root"
        assert entry.changes == {"groups": [[OBSERVER], [RESEARCHER]]}

    def test_password_change_audited_without_hash(self, client, django_user_model, make_user):
        root = django_user_model.objects.create_superuser("root", password="x")
        client.force_login(root)
        target = make_user("ivana")
        response = client.post(
            reverse("admin:auth_user_password_change", args=[target.pk]),
            {
                "password1": "Nove-heslo-2026",
                "password2": "Nove-heslo-2026",
                "usable_password": "true",
            },
        )
        assert response.status_code == 302, response.content.decode()[:2000]
        entry = entries(AuditLog.Action.UPDATE).get()
        assert entry.note == "Heslo změněno" and entry.changes == {}
        assert "pbkdf2" not in str(list(AuditLog.objects.values_list("changes", flat=True)))
