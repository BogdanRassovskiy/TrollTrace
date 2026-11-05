"""Агрегаты «актор/группа × нарратив × неделя» и API для графиков (этап 7)."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.actors.models import ActorInclusionPeriod, Layer
from apps.analytics import aggregation
from apps.analytics.models import WeeklyAggregate
from apps.documents.models import DocumentQuality
from apps.narratives.models import Label, LabelDecision, NarrativeStatus
from apps.panel.models import Setting

from .factories import ActorFactory, DocumentFactory, LabelFactory, NarrativeFactory

pytestmark = pytest.mark.django_db

WEEK = datetime.date(2026, 10, 12)  # pondělí


def at(week: datetime.date, day: int = 1) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(week, datetime.time(12)) +
                               datetime.timedelta(days=day))  # fmt: skip


def actor(layer=Layer.ALTERNATIVE, party="", start=datetime.date(2026, 1, 1), end=None):
    a = ActorFactory(layer=layer, party=party)
    ActorInclusionPeriod.objects.create(actor=a, start_date=start, end_date=end)
    return a


def document(who, week=WEEK, score=5, **kwargs):
    d = DocumentFactory(actor=who, published_at=at(week), **kwargs)
    DocumentQuality.objects.create(
        document=d, score=score, quality_rules_version="test", has_title=True,
        has_published_at=True, date_plausible=True, text_ok=True, text_short=False,
        text_empty=False, lang_ok=True, boilerplate_dup=False, blocked_page=False,
        extraction_ratio_ok=True,
    )  # fmt: skip
    return d


def label(d, n, stance="pro", origin=Label.Origin.MODEL):
    return LabelFactory(document=d, narrative=n, stance=stance, origin=origin)


def row(n, quality=3, **kwargs):
    return WeeklyAggregate.objects.get(narrative=n, min_quality=quality, **kwargs)


def test_denominator_and_numerator_rules():
    n = NarrativeFactory()
    a = actor(start=datetime.date(2026, 10, 13))
    labelled = document(a)
    document(a, is_relevant=False)  # nerelevantní: je ve jmenovateli
    document(a, score=2)  # pod prahem 3, jen ve variantě „všechny dokumenty“
    document(a, in_scope=False)  # rubrika mimo rámec
    early = DocumentFactory(actor=a, published_at=at(WEEK, day=0))  # před zařazením aktéra
    label(labelled, n)
    label(early, n)
    aggregation.update(full=True)
    main = row(n, actor=a, week=WEEK)
    assert (main.n_documents, main.n_pro, main.n_total, main.share) == (1, 1, 2, 0.5)
    assert row(n, quality=1, actor=a, week=WEEK).n_total == 3
    assert row(n, quality=4, actor=a, week=WEEK).n_total == 2


def test_stances_human_label_wins_and_only_active_narratives():
    n = NarrativeFactory()
    candidate = NarrativeFactory(status=NarrativeStatus.CANDIDATE)
    a = actor()
    first, second = document(a), document(a)
    label(first, n, "pro")
    label(first, n, "contra", origin=Label.Origin.HUMAN)
    label(second, n, "neutral")
    label(second, candidate, "pro")
    aggregation.update(full=True)
    main = row(n, actor=a, week=WEEK)
    assert (main.n_documents, main.n_pro, main.n_contra, main.n_neutral) == (2, 0, 1, 1)
    assert not WeeklyAggregate.objects.filter(narrative=candidate).exists()


def test_groups_layer_party_and_all_without_security_layer():
    n = NarrativeFactory()
    mp = actor(layer=Layer.PARLIAMENT, party="ANO")
    media = actor(layer=Layer.MAINSTREAM)
    bis = actor(layer=Layer.SECURITY)
    label(document(mp), n)
    document(mp)
    document(media)
    label(document(bis), n)
    aggregation.update(full=True)
    assert row(n, group_key="party:ANO", week=WEEK).share == 0.5
    assert row(n, group_key="layer:E", week=WEEK).n_total == 2
    everything = row(n, group_key="all", week=WEEK)
    assert (everything.n_documents, everything.n_total) == (1, 3)  # bez vrstvy I
    assert row(n, group_key="layer:I", week=WEEK).n_documents == 1
    smoothed = row(n, actor=media, week=WEEK).share_smoothed  # 0 dokumentů, vrstva D má 0
    assert smoothed == 0
    mp_row = row(n, actor=mp, week=WEEK)  # (1 + 5 · 0,5) / (2 + 5) = 0,5
    assert mp_row.share_smoothed == pytest.approx(0.5)


def test_exclusion_keeps_past_weeks():
    n = NarrativeFactory()
    a = actor(start=datetime.date(2026, 1, 1), end=WEEK + datetime.timedelta(days=3))
    label(document(a), n)
    later = WEEK + datetime.timedelta(weeks=1)
    document(a, week=later)  # po vyřazení
    aggregation.update(full=True)
    assert row(n, actor=a, week=WEEK).n_total == 1
    assert not WeeklyAggregate.objects.filter(actor=a, week=later).exists()


def test_incremental_update_recomputes_changed_pairs_only():
    n = NarrativeFactory()
    a, b = actor(), actor()
    document(a)
    document(b)
    aggregation.update(full=True)
    untouched = row(n, actor=b, week=WEEK)
    since = timezone.now()
    d = document(a)
    LabelDecision.objects.create(
        document=d, narrative=n, narrative_version=n.latest_version, text_hash=d.text_hash,
        similarity=0.3, present=True, stance="pro", confidence=0.9, decided_at=timezone.now(),
    )  # fmt: skip
    label(d, n)
    stats = aggregation.update(since)
    assert not stats.full and stats.pairs == 1
    assert row(n, actor=a, week=WEEK).n_documents == 1
    assert row(n, actor=b, week=WEEK).pk == untouched.pk  # nepřepočítáno
    assert row(n, group_key="all", week=WEEK).n_documents == 1
    assert aggregation.update(timezone.now()).pairs == 0


def test_new_active_narrative_forces_full_rebuild():
    n = NarrativeFactory()
    document(actor())
    aggregation.update(full=True)
    later = NarrativeFactory(status=NarrativeStatus.CANDIDATE)
    later.status = NarrativeStatus.ACTIVE
    later.save()
    assert aggregation.update(timezone.now()).full
    assert WeeklyAggregate.objects.filter(narrative=later).exists()
    assert WeeklyAggregate.objects.filter(narrative=n).exists()


def test_z_scores_against_own_history():
    weeks = [WEEK + datetime.timedelta(weeks=i) for i in range(6)]
    points = [(w, 1, 10) for w in weeks[:5]] + [(weeks[5], 6, 10)]
    z = aggregation.z_scores(points)
    assert z[:4] == [None] * 4  # málo historie
    assert z[4] == pytest.approx(0, abs=1e-9) and z[5] > 3
    flat = aggregation.z_scores([(w, 0, 10) for w in weeks[:5]] + [(weeks[5], 1, 10)])
    assert flat[5] is not None and 0 < flat[5] < 100  # dolní mez rozptylu: žádné nekonečno
    Setting(key="aggregation-z-min-weeks", value=6).save()
    assert aggregation.z_scores(points)[5] is None


# --- API ------------------------------------------------------------------------------


def test_series_api_shape_and_filters(observer_client):
    n = NarrativeFactory(code="pomoc")
    mp = actor(layer=Layer.PARLIAMENT, party="ANO")
    media = actor(layer=Layer.MAINSTREAM)
    label(document(mp), n, "contra")
    document(media, week=WEEK + datetime.timedelta(weeks=1))
    aggregation.update(full=True)
    data = observer_client.get("/api/series/", {"narrative": "pomoc"}).json()
    assert data["weeks"] == [WEEK.isoformat(), (WEEK + datetime.timedelta(weeks=1)).isoformat()]
    by_key = {s["key"]: s for s in data["series"]}
    assert by_key[f"actor:{mp.pk}"]["n"] == [1, None] and by_key[f"actor:{mp.pk}"]["share"][0] == 1
    assert by_key[f"actor:{mp.pk}"]["layer"] == "E" and by_key["all"]["kind"] == "group"
    # разбивка по позициям приходит всегда — для подсказки графика
    assert by_key[f"actor:{mp.pk}"]["contra"] == [1, None]
    assert by_key[f"actor:{mp.pk}"]["pro"] == [0, None] and by_key["all"]["neutral"][1] == 0
    pro = observer_client.get("/api/series/", {"narrative": "pomoc", "stance": "pro",
                                               "layer": "E", "groups": "0"}).json()  # fmt: skip
    assert [s["key"] for s in pro["series"]] == [f"actor:{mp.pk}"]
    assert pro["series"][0]["n"] == [0] and pro["weeks"] == [WEEK.isoformat()]
    only = observer_client.get("/api/series/", {"narrative": "pomoc", "groups": "only"}).json()
    assert all(s["kind"] == "group" for s in only["series"])
    assert observer_client.get("/api/series/", {"narrative": "x"}).status_code == 400
    bad = observer_client.get("/api/series/", {"narrative": "pomoc", "min_quality": 2})
    assert bad.status_code == 400 and "práh" in bad.json()["error"]


def test_series_api_350_actors_in_few_queries(observer_client):
    n = NarrativeFactory(code="velky")
    actors = [ActorFactory(layer=Layer.PARLIAMENT) for _ in range(350)]
    weeks = [WEEK + datetime.timedelta(weeks=i) for i in range(26)]
    WeeklyAggregate.objects.bulk_create(
        WeeklyAggregate(actor=a, narrative=n, week=w, min_quality=3, n_documents=1, n_total=4,
                        share=0.25)
        for a in actors for w in weeks
    )  # fmt: skip
    with CaptureQueriesContext(connection) as queries:
        data = observer_client.get("/api/series/", {"narrative": "velky"}).json()
    assert len(data["series"]) == 350 and len(data["weeks"]) == 26
    assert len(queries) <= 12  # bez dotazu na řadu


def test_documents_behind_point(observer_client):
    n = NarrativeFactory(code="pomoc")
    mp = actor(layer=Layer.PARLIAMENT, party="ANO")
    d = document(mp)
    label(d, n, "pro")
    document(mp)
    response = observer_client.get(
        "/api/documents/", {"narrative": "pomoc", "group": "party:ANO",
                            "week": (WEEK + datetime.timedelta(days=2)).isoformat()},
    )  # fmt: skip
    data = response.json()
    assert data["week"] == WEEK.isoformat() and data["count"] == 1
    assert data["documents"][0]["id"] == d.pk and data["documents"][0]["stance"] == "pro"
    missing = observer_client.get("/api/documents/", {"narrative": "pomoc", "week": "2026-10-12"})
    assert missing.status_code == 400


def test_api_requires_login(client):
    assert client.get("/api/narratives/").status_code == 302


def test_narratives_api(observer_client):
    NarrativeFactory(code="a")
    NarrativeFactory(code="b", status=NarrativeStatus.CANDIDATE)
    data = observer_client.get("/api/narratives/").json()
    assert [n["code"] for n in data["narratives"]] == ["a"]
    assert data["thresholds"] == [1, 3, 4]
