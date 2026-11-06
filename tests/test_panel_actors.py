"""Панель исследователя (этап 9): акторы, кодбук, настройки, задачи, журнал аудита."""

import datetime

import pytest

from apps.actors.models import Actor, ActorAccount, ActorInclusionPeriod, Platform, Source
from apps.analytics import spikes
from apps.analytics.models import WeeklyAggregate
from apps.narratives.models import NarrativeStatus
from apps.panel import actors as service
from apps.panel.defaults import ensure_default_settings
from apps.panel.forms import ActorCreateForm
from apps.panel.models import AuditLog, Job, Notification, Setting

from .factories import (
    ActorAccountFactory,
    ActorFactory,
    DocumentFactory,
    LabelFactory,
    NarrativeFactory,
    SourceFactory,
    TopicFactory,
)

pytestmark = pytest.mark.django_db

TODAY = datetime.date.today()


def form_data(**kwargs):
    data = {
        "name": "Nový Kanál",
        "actor_type": "channel",
        "layer": "C",
        "inclusion_criterion": "kanál z výzkumu",
        "start_date": TODAY.isoformat(),
        "source_mode": "none",
    }
    data.update(kwargs)
    return data


def actions(model):
    return list(AuditLog.objects.filter(content_type__model=model).values_list("action", flat=True))


# --- добавление ----------------------------------------------------------------------------


def test_create_actor_with_new_source(researcher_client, monkeypatch):
    monkeypatch.setattr(service, "check_available", lambda url: (True, "zdroj odpověděl HTTP 200"))
    response = researcher_client.post(
        "/panel/akteri/novy/",
        form_data(
            source_mode="new",
            new_source_url="https://t.me/s/novykanal",
            new_source_platform=Platform.TELEGRAM,
        ),
    )
    assert response.status_code == 302
    actor = Actor.objects.get(name="Nový Kanál")
    assert actor.open_period.start_date == TODAY and actor.manual_inclusion
    source = Source.objects.get(url="https://t.me/s/novykanal")
    assert source.layer == "C" and actor.accounts.get().source == source
    jobs = Job.objects.order_by("pk")
    assert [j.job_type for j in jobs] == [Job.Type.RETRO_COLLECT, Job.Type.AGGREGATE]
    assert jobs[0].params == {"actor": actor.pk, "sources": [source.pk]}
    assert actions("actor") == ["create"] and actions("source") == ["create"]
    assert actions("actoraccount") == ["create"] and actions("actorinclusionperiod") == ["create"]
    assert actions("job") == ["run", "run"]


def test_create_rejects_taken_account_and_existing_source():
    source = SourceFactory(url="https://www.psp.cz/")
    owner = ActorFactory(name="Poslanec A")
    ActorAccountFactory(actor=owner, source=source, account_id="6520")
    form = ActorCreateForm(form_data(source_mode="existing", source=source.pk, account_id="6520"))
    assert not form.is_valid() and "Poslanec A" in str(form.errors["account_id"])
    form = ActorCreateForm(
        form_data(source_mode="new", new_source_url="https://www.psp.cz", new_source_platform="web")
    )
    assert not form.is_valid() and "už existuje" in str(form.errors["new_source_url"])


def test_create_warns_on_unavailable_source_until_confirmed():
    data = form_data(
        source_mode="new", new_source_url="https://down.example/", new_source_platform="web"
    )
    check = lambda url: (False, "robots.txt sběr této adresy zakazuje")  # noqa: E731
    form = ActorCreateForm(data, check_available=check)
    assert not form.is_valid()
    assert any("robots.txt" in e for e in form.non_field_errors())
    assert ActorCreateForm({**data, "confirm": "on"}, check_available=check).is_valid()


def test_check_available_respects_robots():
    import httpx

    from apps.ingestion.http import PoliteClient

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, text="ok")

    client = PoliteClient(delay=0, transport=httpx.MockTransport(handler))
    ok, message = service.check_available("https://blocked.example/feed", client)
    assert not ok and "robots.txt" in message


# --- архивация и повторное включение -------------------------------------------------------


def test_exclude_preview_and_archive(researcher_client):
    actor = ActorFactory(name="Web X")
    actor.include(on=TODAY - datetime.timedelta(days=30))
    doc = DocumentFactory(actor=actor)
    LabelFactory(document=doc)
    page = researcher_client.get(f"/panel/akteri/{actor.pk}/").content.decode()
    assert "<strong>1</strong> dokumentů a <strong>1</strong> štítků" in page
    response = researcher_client.post(
        f"/panel/akteri/{actor.pk}/",
        {"action": "exclude", "reason": "konec sledování", "end_date": TODAY.isoformat()},
    )
    assert response.status_code == 302
    actor.refresh_from_db()
    assert actor.status == Actor.Status.EXCLUDED and actor.documents.count() == 1
    period = actor.inclusion_periods.get()
    assert period.end_date == TODAY and period.exclusion_reason == "konec sledování"
    entry = AuditLog.objects.get(content_type__model="actor")
    assert entry.action == AuditLog.Action.ARCHIVE and "konec sledování" in entry.note
    assert Job.objects.get().job_type == Job.Type.AGGREGATE


def test_reinclusion_opens_new_period(researcher_client):
    actor = ActorFactory()
    actor.include(on=TODAY - datetime.timedelta(days=60))
    actor.exclude("pauza", on=TODAY - datetime.timedelta(days=10))
    url = f"/panel/akteri/{actor.pk}/"
    early = researcher_client.post(
        url,
        {
            "action": "include",
            "reason": "znovu",
            "start_date": (TODAY - datetime.timedelta(days=20)).isoformat(),
        },
    )
    assert "nové musí začít později" in early.content.decode()
    researcher_client.post(
        url, {"action": "include", "reason": "znovu", "start_date": TODAY.isoformat()}
    )
    actor.refresh_from_db()
    assert actor.status == Actor.Status.ACTIVE
    assert ActorInclusionPeriod.objects.filter(actor=actor).count() == 2
    assert AuditLog.objects.filter(content_type__model="actor").get().action == "restore"


def test_edit_and_add_account(researcher_client):
    actor = ActorFactory(name="Stará", party="X")
    source = SourceFactory()
    url = f"/panel/akteri/{actor.pk}/"
    researcher_client.post(
        url,
        {
            "action": "edit",
            "name": "Nová",
            "actor_type": actor.actor_type,
            "layer": "B",
            "party": "X",
        },
    )
    actor.refresh_from_db()
    assert actor.name == "Nová"
    assert AuditLog.objects.get(content_type__model="actor").changes["name"] == ["Stará", "Nová"]
    researcher_client.post(url, {"action": "account", "source": source.pk, "account_id": "@kanal"})
    assert actor.accounts.get().account_id == "kanal"
    page = researcher_client.post(
        url, {"action": "account", "source": source.pk, "account_id": "KANAL"}
    )
    assert "Tento účet už aktér má." in page.content.decode()


# --- импорт CSV ----------------------------------------------------------------------------


def test_csv_import_preview_then_confirm(researcher_client):
    source = SourceFactory(url="https://www.psp.cz/")
    ActorAccountFactory(source=source, account_id="1")
    ActorFactory(name="Existující", party="ANO")
    csv = (
        "name;actor_type;layer;party;source_url;account_id;start_date\n"
        "Jana Nová;politician;E;ODS;https://www.psp.cz;7001;2026-01-05\n"
        "Existující;politician;E;ANO;;;\n"
        "Petr Obsazený;politician;E;;https://www.psp.cz/;1;\n"
        "Kdo;neznamy;Z;;;;\n"
    ).encode("cp1250")
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("poslanci.csv", csv, content_type="text/csv")
    page = researcher_client.post("/panel/akteri/import/", {"file": upload}).content.decode()
    assert "bez chyb <strong>1</strong>" in page
    assert "už existuje" in page and "účet už patří" in page and "neznámý typ" in page
    assert not Actor.objects.filter(name="Jana Nová").exists()
    researcher_client.post("/panel/akteri/import/", {"action": "confirm"})
    jana = Actor.objects.get(name="Jana Nová")
    assert jana.open_period.start_date == datetime.date(2026, 1, 5)
    assert jana.accounts.get().account_id == "7001"
    assert AuditLog.objects.filter(note="Import CSV: poslanci.csv").count() == 3
    assert Job.objects.get().job_type == Job.Type.AGGREGATE


def test_csv_missing_columns():
    rows, problems = service.parse_csv("jmeno,typ\nA,B\n")
    assert rows == [] and "name" in problems[0]


# --- кодбук --------------------------------------------------------------------------------


def test_narrative_edit_creates_version_and_diff(researcher_client):
    narrative = NarrativeFactory(code="valka", definition="Válka je vina Západu.")
    data = {
        "topic": narrative.topic_id,
        "kind": narrative.kind,
        "name": narrative.name,
        "definition": "Válka je vina NATO a Západu.",
        "examples": "NATO provokovalo\nZápad lže",
        "status": "active",
        "stance_pro": "šíří tvrzení",
        "stance_absent": "jen téma války",
        "change_note": "zpřesnění",
    }
    response = researcher_client.post("/panel/kodova-kniha/narativ/valka/upravit/", data)
    assert response.status_code == 302
    narrative.refresh_from_db()
    assert narrative.current_version == 2 and narrative.examples == [
        "NATO provokovalo",
        "Západ lže",
    ]
    assert narrative.stance_guide == {"pro": "šíří tvrzení", "absent": "jen téma války"}
    assert narrative.latest_version.change_note == "zpřesnění"
    assert AuditLog.objects.get(content_type__model="narrative").note == "zpřesnění"
    page = researcher_client.get("/panel/kodova-kniha/narativ/valka/").content.decode()
    assert '<ins class="new">NATO a</ins>' in page
    assert "Porovnání v1 → v2" in page


def test_narrative_create_and_relabel(researcher_client):
    topic = TopicFactory()
    researcher_client.post(
        "/panel/kodova-kniha/narativ/novy/",
        {
            "code": "ze-zpravy",
            "topic": topic.pk,
            "kind": "narrative",
            "name": "Nový narativ",
            "definition": "Tvrzení.",
            "status": "active",
            "change_note": "ze zprávy BIS",
        },
    )
    narrative = topic.narratives.get(code="ze-zpravy")
    assert narrative.current_version == 1
    researcher_client.post("/panel/kodova-kniha/narativ/ze-zpravy/")
    job = Job.objects.get(job_type=Job.Type.RELABEL)
    assert job.params["narrative"] == "ze-zpravy"
    assert AuditLog.objects.filter(action="run").exists()
    page = researcher_client.get("/panel/kodova-kniha/").content.decode()
    assert "Nový narativ" in page and "ze zprávy BIS" in page


def test_topic_form(researcher_client):
    researcher_client.post(
        "/panel/kodova-kniha/tema/nove/", {"code": "euro", "name": "Euro", "keywords": "euro\n=ecb"}
    )
    topic = TopicFactory._meta.model.objects.get(code="euro")
    assert topic.keywords == ["euro", "=ecb"]
    assert actions("topic") == ["create"]


# --- настройки и задачи --------------------------------------------------------------------


def test_setting_versioned_edit(researcher_client):
    ensure_default_settings()
    response = researcher_client.post(
        "/panel/nastaveni/relevance-threshold/", {"value": "0.4", "change_note": "pilot"}
    )
    assert response.status_code == 302
    setting = Setting.objects.get(key="relevance-threshold")
    assert (
        setting.value == 0.4
        and setting.versions.get(version=setting.current_version).change_note == "pilot"
    )
    assert AuditLog.objects.get(content_type__model="setting").changes["value"][1] == 0.4
    researcher_client.post("/panel/nastaveni/reuse-layers/", {"value": "A, B", "change_note": "x"})
    assert Setting.objects.get(key="reuse-layers").value == ["A", "B"]
    page = researcher_client.post(
        "/panel/nastaveni/minhash-shingle-size/", {"value": "pět", "change_note": "x"}
    ).content.decode()
    assert "celé číslo" in page or "Zadejte" in page
    assert "relevance" in researcher_client.get("/panel/nastaveni/").content.decode()


def test_jobs_page_rebuild_and_cancel(researcher_client, researcher):
    researcher_client.post("/panel/ulohy/", {"action": "rebuild"})
    job = Job.objects.get()
    assert job.created_by == researcher and job.status == Job.Status.PENDING
    researcher_client.post("/panel/ulohy/", {"action": "cancel", "job": job.pk})
    job.refresh_from_db()
    assert job.status == Job.Status.CANCELLED
    page = researcher_client.get("/panel/ulohy/", {"job": job.pk}).content.decode()
    assert "Zrušeno výzkumníkem" in page and "Fronty zpracování" in page


def test_manual_rebuild_notifies(researcher):
    from apps.analytics.tasks import rebuild_aggregates

    job = Job.objects.create(
        job_type=Job.Type.AGGREGATE, created_by=researcher, params={"full": True}
    )
    rebuild_aggregates(job.pk)
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED
    note = Notification.objects.get(notification_type=Notification.Type.JOB)
    assert note.recipient == researcher and "hotovo" in note.title


def test_retro_collect_task(monkeypatch, researcher):
    from apps.ingestion.models import CollectionRun
    from apps.panel.tasks import retro_collect

    source = SourceFactory(platform="telegram")
    actor = ActorFactory()

    def fake(src):
        return CollectionRun.objects.create(source=src, status="ok", new=7)

    monkeypatch.setattr("apps.ingestion.tasks.run_collector", fake)
    job = Job.objects.create(
        job_type=Job.Type.RETRO_COLLECT,
        created_by=researcher,
        params={"actor": actor.pk, "sources": [source.pk]},
    )
    retro_collect(job.pk)
    job.refresh_from_db()
    assert job.status == Job.Status.SUCCEEDED and "nových dokumentů 7" in job.message
    assert Notification.objects.filter(notification_type="job", recipient=researcher).exists()


def test_psp_actor_skips_retro_collect():
    source = SourceFactory(platform=Platform.PARLIAMENT)
    assert service.queue_retro_collect(None, ActorFactory(), [source]) is None


# --- уведомления о всплесках ---------------------------------------------------------------


def test_spike_notifications_are_not_duplicated():
    narrative = NarrativeFactory(status=NarrativeStatus.ACTIVE)
    actor = ActorFactory()
    week = datetime.date(2026, 10, 12)
    WeeklyAggregate.objects.create(
        actor=actor,
        narrative=narrative,
        week=week,
        n_documents=3,
        n_total=4,
        share=0.75,
        z_score=3.0,
        min_quality=3,
    )
    WeeklyAggregate.objects.create(
        actor=actor,
        narrative=narrative,
        week=week - datetime.timedelta(weeks=5),
        n_documents=3,
        n_total=4,
        share=0.75,
        z_score=3.0,
        min_quality=3,
    )
    assert spikes.notify_spikes() == 1
    assert spikes.notify_spikes() == 0
    note = Notification.objects.get(notification_type=Notification.Type.SPIKE)
    assert actor.name in note.title and "z-skóre 3.0" in note.text


# --- доступ --------------------------------------------------------------------------------


PANEL_URLS = [
    "/panel/akteri/",
    "/panel/akteri/novy/",
    "/panel/akteri/import/",
    "/panel/kodova-kniha/",
    "/panel/nastaveni/",
    "/panel/ulohy/",
]


@pytest.mark.parametrize("url", PANEL_URLS)
def test_panel_pages_for_researcher(researcher_client, url):
    assert researcher_client.get(url).status_code == 200


@pytest.mark.parametrize("url", PANEL_URLS)
def test_panel_pages_forbidden_for_observer(observer_client, url):
    assert observer_client.get(url).status_code == 403


def test_actor_list_filters(researcher_client):
    ActorFactory(name="Aktivní A")
    ActorFactory(name="Vyřazený B", status=Actor.Status.EXCLUDED)
    page = researcher_client.get("/panel/akteri/", {"status": "excluded"}).content.decode()
    assert "Vyřazený B" in page and "Aktivní A" not in page
    assert ActorAccount.objects.count() == 0


def test_tasks_ignore_foreign_or_processed_jobs():
    """Задача по id не трогает Job другого типа или уже обработанный (общий брокер, повтор)."""
    from apps.analytics.tasks import rebuild_aggregates
    from apps.panel.tasks import retro_collect

    relabel = Job.objects.create(job_type=Job.Type.RELABEL, status=Job.Status.SUCCEEDED,
                                 message="Do fronty anotace přidáno kandidátů: 5.")  # fmt: skip
    assert retro_collect(relabel.pk) == {"skipped": relabel.pk}
    assert rebuild_aggregates(relabel.pk) == {"skipped": relabel.pk}
    assert rebuild_aggregates(999999) == {"skipped": 999999}
    relabel.refresh_from_db()
    assert relabel.message == "Do fronty anotace přidáno kandidátů: 5."
    assert not Notification.objects.exists()
