"""Этап 1: ограничения модели данных на уровне БД и версионность кодбука/настроек."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.actors.models import Actor, ActorAccount, ActorInclusionPeriod
from apps.documents.models import Document, DocumentQuality, ReuseLink
from apps.documents.utils import normalize_url, text_hash
from apps.ingestion.models import Level, SourceIncident
from apps.narratives.models import NarrativeCandidate, NarrativeVersion
from apps.panel.defaults import DEFAULT_SETTINGS, ensure_default_settings
from apps.panel.models import AuditLog, SettingVersion

from .factories import (
    ActorFactory,
    DocumentFactory,
    LabelFactory,
    NarrativeFactory,
    SettingFactory,
    SourceFactory,
    TopicFactory,
    WeeklyAggregateFactory,
    monday,
)

pytestmark = pytest.mark.django_db

D = datetime.date


def violates(func, exc=IntegrityError):
    """Проверяет, что БД отвергла операцию, не ломая внешнюю тестовую транзакцию."""
    with pytest.raises(exc), transaction.atomic():
        func()


# --- Акторы ---------------------------------------------------------------------------------


class TestActorAccount:
    def test_account_unique_per_source_case_insensitive(self):
        source = SourceFactory()
        ActorAccount.objects.create(actor=ActorFactory(), source=source, account_id="Rajchl_Kanal")
        violates(
            lambda: ActorAccount.objects.create(
                actor=ActorFactory(), source=source, account_id="@rajchl_kanal"
            )
        )

    def test_same_handle_on_other_source_is_allowed(self):
        ActorAccount.objects.create(actor=ActorFactory(), source=SourceFactory(), account_id="x")
        ActorAccount.objects.create(actor=ActorFactory(), source=SourceFactory(), account_id="x")
        assert ActorAccount.objects.count() == 2


class TestInclusionPeriods:
    def test_end_before_start_rejected(self):
        actor = ActorFactory()
        violates(
            lambda: ActorInclusionPeriod.objects.create(
                actor=actor, start_date=D(2026, 5, 1), end_date=D(2026, 4, 1)
            )
        )

    def test_overlapping_periods_rejected(self):
        actor = ActorFactory()
        ActorInclusionPeriod.objects.create(
            actor=actor, start_date=D(2026, 1, 1), end_date=D(2026, 3, 1)
        )
        violates(lambda: ActorInclusionPeriod.objects.create(actor=actor, start_date=D(2026, 2, 1)))

    def test_only_one_open_period(self):
        actor = ActorFactory()
        actor.include(on=D(2026, 1, 1))
        violates(lambda: actor.include(on=D(2026, 6, 1)))

    def test_other_actors_periods_do_not_conflict(self):
        ActorFactory().include(on=D(2026, 1, 1))
        ActorFactory().include(on=D(2026, 1, 1))
        assert ActorInclusionPeriod.objects.filter(end_date__isnull=True).count() == 2

    def test_exclude_and_reinclude_keeps_history(self):
        actor = ActorFactory()
        actor.include("выборка", on=D(2026, 1, 1))
        actor.exclude("перестал соответствовать критерию", on=D(2026, 3, 1))
        assert actor.status == Actor.Status.EXCLUDED
        actor.include("снова соответствует", on=D(2026, 5, 1))

        periods = list(actor.inclusion_periods.order_by("start_date"))
        assert [(p.start_date, p.end_date) for p in periods] == [
            (D(2026, 1, 1), D(2026, 3, 1)),
            (D(2026, 5, 1), None),
        ]
        assert periods[0].exclusion_reason == "перестал соответствовать критерию"
        assert actor.status == Actor.Status.ACTIVE

    def test_excluded_actor_keeps_documents(self):
        doc = DocumentFactory()
        doc.actor.include(on=D(2026, 1, 1))
        doc.actor.exclude("причина", on=D(2026, 2, 1))
        assert Document.objects.filter(pk=doc.pk).exists()


# --- Документы ------------------------------------------------------------------------------


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "HTTPS://www.Example.cz/clanek/1/?utm_source=rss&utm_medium=x&id=5#komentare",
                "https://example.cz/clanek/1?id=5",
            ),
            ("https://example.cz/a?b=2&a=1&fbclid=XYZ", "https://example.cz/a?a=1&b=2"),
            ("https://example.cz:443/", "https://example.cz"),
            ("http://example.cz:8080/x", "http://example.cz:8080/x"),
        ],
    )
    def test_normalize(self, url, expected):
        assert normalize_url(url) == expected

    def test_text_hash_ignores_whitespace(self):
        assert text_hash("a  b\n c ") == text_hash("a b c")


class TestDocument:
    def test_duplicate_url_after_normalization_rejected(self):
        doc = DocumentFactory(url="https://alt.example/clanek/1")
        violates(
            lambda: DocumentFactory(
                source=doc.source, url="https://www.alt.example/clanek/1/?utm_source=telegram"
            )
        )

    def test_same_url_in_other_source_allowed(self):
        DocumentFactory(url="https://alt.example/clanek/1")
        DocumentFactory(url="https://alt.example/clanek/1")
        assert Document.objects.count() == 2

    def test_deleted_status_requires_date(self):
        doc = DocumentFactory()
        doc.status = Document.Status.DELETED
        violates(doc.save)

    def test_versions_only_on_text_change(self):
        doc = DocumentFactory()
        v1 = doc.record_version(text="První verze.", title="T", raw_html="<p>První verze.</p>")
        assert doc.record_version(text="První   verze. ", title="T") is None
        v2 = doc.record_version(text="Opravená verze.", title="T")

        assert (v1.version, v2.version) == (1, 2)
        assert doc.versions.count() == 2
        doc.refresh_from_db()
        assert doc.text == "Opravená verze."
        assert doc.versions.get(version=1).raw_html == "<p>První verze.</p>"

    def test_embedding_dimension_enforced(self):
        doc = DocumentFactory()
        doc.embedding = [0.1] * 768
        doc.save()
        doc.embedding = [0.1] * 3
        violates(doc.save, exc=Exception)


def quality(doc, score, *, save=True, **kwargs):
    flags = dict(
        has_title=True,
        has_published_at=True,
        date_plausible=True,
        text_ok=True,
        text_short=False,
        text_empty=False,
        lang_ok=True,
        boilerplate_dup=False,
        blocked_page=False,
        extraction_ratio_ok=True,
    )
    obj = DocumentQuality(
        document=doc, score=score, quality_rules_version="test", **(flags | kwargs)
    )
    if save:
        obj.save()
    return obj


class TestDocumentQuality:
    @pytest.mark.parametrize("score", [0, 6])
    def test_score_range(self, score):
        doc = DocumentFactory()
        violates(lambda: quality(doc, score))

    def test_new_score_moves_previous_to_history(self):
        doc = DocumentFactory()
        old = quality(doc, 5)
        new = quality(doc, 2, lang_ok=False)
        old.refresh_from_db()
        assert not old.is_current and new.is_current
        assert doc.quality_scores.count() == 2

    def test_two_current_scores_rejected_at_db_level(self):
        doc = DocumentFactory()
        quality(doc, 5)
        # bulk_create минует save(): срабатывает только частичный уникальный индекс.
        violates(lambda: DocumentQuality.objects.bulk_create([quality(doc, 4, save=False)]))


class TestReuseLink:
    def test_self_link_rejected(self):
        doc = DocumentFactory()
        violates(
            lambda: ReuseLink.objects.create(
                source_document=doc,
                target_document=doc,
                link_type=ReuseLink.Type.VERBATIM,
                method=ReuseLink.Method.MINHASH,
                similarity=1,
            )
        )

    def test_similarity_range(self):
        a, b = DocumentFactory(), DocumentFactory()
        violates(
            lambda: ReuseLink.objects.create(
                source_document=a,
                target_document=b,
                link_type=ReuseLink.Type.PARAPHRASE,
                method=ReuseLink.Method.KNN,
                similarity=1.5,
            )
        )


# --- Кодбук и метки -------------------------------------------------------------------------


class TestNarrativeVersioning:
    def test_create_makes_version_1(self):
        narrative = NarrativeFactory()
        assert narrative.current_version == 1
        assert narrative.versions.get().definition == narrative.definition

    def test_change_creates_new_version_and_keeps_old(self):
        narrative = NarrativeFactory(definition="Старое определение")
        narrative.definition = "Новое определение"
        narrative.save(change_note="уточнение")

        assert narrative.current_version == 2
        v1, v2 = narrative.versions.order_by("version")
        assert (v1.definition, v2.definition) == ("Старое определение", "Новое определение")
        assert v2.change_note == "уточнение"

    def test_noop_save_does_not_create_version(self):
        narrative = NarrativeFactory()
        narrative.save()
        narrative.save()
        assert narrative.versions.count() == 1

    def test_topic_change_is_versioned(self):
        narrative = NarrativeFactory()
        narrative.topic = TopicFactory()
        narrative.save()
        assert narrative.versions.get(version=2).topic == narrative.topic

    def test_stale_instance_gets_next_free_number(self):
        narrative = NarrativeFactory()
        stale = type(narrative).objects.get(pk=narrative.pk)
        narrative.name = "A"
        narrative.save()
        stale.name = "B"
        stale.save()
        assert sorted(narrative.versions.values_list("version", flat=True)) == [1, 2, 3]

    def test_duplicate_version_number_rejected(self):
        narrative = NarrativeFactory()
        violates(
            lambda: NarrativeVersion.objects.create(
                narrative=narrative,
                version=1,
                topic=narrative.topic,
                name="x",
                definition="x",
                status="active",
            )
        )


class TestLabel:
    def test_label_points_to_codebook_version(self):
        label = LabelFactory()
        assert label.narrative_version.version == 1
        label.narrative.definition = "Изменено"
        label.narrative.save()
        label.refresh_from_db()
        assert label.narrative_version.version == 1  # метка остаётся привязана к своей версии

    def test_version_of_other_narrative_rejected(self):
        other = NarrativeFactory()
        violates(lambda: LabelFactory(narrative_version=other.latest_version), ValidationError)

    def test_confidence_range(self):
        violates(lambda: LabelFactory(confidence=1.2))

    def test_one_label_per_document_version_origin(self):
        label = LabelFactory()
        violates(
            lambda: LabelFactory(
                document=label.document,
                narrative=label.narrative,
                narrative_version=label.narrative_version,
            )
        )


class TestNarrativeCandidate:
    def test_accepted_requires_narrative(self):
        violates(
            lambda: NarrativeCandidate.objects.create(
                description="x",
                n_documents=30,
                n_authors=6,
                window_start=D(2026, 1, 1),
                window_end=D(2026, 1, 14),
                status=NarrativeCandidate.Status.ACCEPTED,
            )
        )


# --- Агрегаты -------------------------------------------------------------------------------


class TestWeeklyAggregate:
    def test_actor_xor_group(self):
        violates(lambda: WeeklyAggregateFactory(group_key="party:SPD"))
        violates(lambda: WeeklyAggregateFactory(actor=None))

    def test_group_row(self):
        row = WeeklyAggregateFactory(actor=None, group_key="layer:B")
        assert row.pk

    def test_week_must_be_monday(self):
        violates(lambda: WeeklyAggregateFactory(week=monday() + datetime.timedelta(days=2)))

    def test_n_not_above_total_and_share_range(self):
        violates(lambda: WeeklyAggregateFactory(n_documents=11, n_total=10))
        violates(lambda: WeeklyAggregateFactory(share=1.5))

    def test_unique_per_actor_week_quality(self):
        row = WeeklyAggregateFactory()
        violates(
            lambda: WeeklyAggregateFactory(actor=row.actor, narrative=row.narrative, week=row.week)
        )
        # Та же неделя с другим порогом качества — отдельная строка (проверка устойчивости).
        WeeklyAggregateFactory(
            actor=row.actor, narrative=row.narrative, week=row.week, min_quality=4
        )


# --- Мониторинг -----------------------------------------------------------------------------


class TestSourceIncident:
    def test_one_open_incident_per_type(self):
        source = SourceFactory()
        make = lambda: SourceIncident.objects.create(  # noqa: E731
            source=source,
            incident_type=SourceIncident.Type.STALE_FEED,
            level=Level.WARNING,
            started_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        )
        first = make()
        violates(make)
        first.ended_at = datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)
        first.save()
        make()  # после закрытия можно открыть новый
        assert SourceIncident.objects.count() == 2


# --- Настройки и аудит ----------------------------------------------------------------------


class TestSettings:
    def test_value_change_versioned(self):
        setting = SettingFactory(value=3)
        setting.value = 4
        setting.save(change_note="строже")
        assert list(setting.versions.order_by("version").values_list("value", flat=True)) == [3, 4]

    def test_description_change_not_versioned(self):
        setting = SettingFactory()
        setting.description = "новое описание"
        setting.save()
        assert setting.current_version == 1

    def test_duplicate_version_rejected(self):
        setting = SettingFactory()
        violates(lambda: SettingVersion.objects.create(setting=setting, version=1, value=0))

    def test_defaults_are_idempotent(self):
        assert ensure_default_settings() == len(DEFAULT_SETTINGS)
        assert ensure_default_settings() == 0


class TestAuditLog:
    def test_append_only(self, django_user_model):
        user = django_user_model.objects.create_user("researcher")
        entry = AuditLog.objects.create(user=user, action=AuditLog.Action.CREATE, object_repr="x")
        assert entry.username == "researcher"
        entry.note = "правка"
        with pytest.raises(PermissionError):
            entry.save()
        with pytest.raises(PermissionError):
            entry.delete()
