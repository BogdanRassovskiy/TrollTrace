"""Фабрики тестовых данных (factory_boy)."""

import datetime

import factory
from django.utils import timezone

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.analytics.models import WeeklyAggregate
from apps.documents.models import Document
from apps.narratives.models import Label, Narrative, Stance, Topic
from apps.panel.models import Setting


class SourceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Source

    name = factory.Sequence(lambda n: f"Zdroj {n}")
    layer = Layer.ALTERNATIVE
    platform = Platform.WEB
    url = factory.Sequence(lambda n: f"https://source-{n}.example/")


class ActorFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Actor

    name = factory.Sequence(lambda n: f"Aktér {n}")
    actor_type = Actor.Type.MEDIA
    layer = Layer.ALTERNATIVE


class ActorAccountFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ActorAccount

    actor = factory.SubFactory(ActorFactory)
    source = factory.SubFactory(SourceFactory)
    account_id = factory.Sequence(lambda n: f"account_{n}")


class DocumentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Document

    actor = factory.SubFactory(ActorFactory)
    source = factory.SubFactory(SourceFactory)
    url = factory.Sequence(lambda n: f"https://source.example/clanek/{n}")
    title = factory.Sequence(lambda n: f"Článek {n}")
    text = "Text článku. " * 40
    published_at = factory.LazyFunction(timezone.now)
    date_source = Document.DateSource.PAGE
    collected_at = factory.LazyFunction(timezone.now)


class TopicFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Topic

    code = factory.Sequence(lambda n: f"topic-{n}")
    name = factory.Sequence(lambda n: f"Téma {n}")


class NarrativeFactory(factory.django.DjangoModelFactory):
    """Создание через save() — сразу появляется версия 1."""

    class Meta:
        model = Narrative

    code = factory.Sequence(lambda n: f"N{n:03d}")
    topic = factory.SubFactory(TopicFactory)
    name = factory.Sequence(lambda n: f"Narativ {n}")
    definition = "Definice narativu."


class LabelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Label

    document = factory.SubFactory(DocumentFactory)
    narrative = factory.SubFactory(NarrativeFactory)
    narrative_version = factory.LazyAttribute(lambda o: o.narrative.latest_version)
    stance = Stance.CONTRA
    origin = Label.Origin.MODEL
    confidence = 0.9


class SettingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Setting

    key = factory.Sequence(lambda n: f"setting-{n}")
    value = 1


def monday(offset_weeks: int = 0) -> datetime.date:
    today = timezone.localdate()
    return today - datetime.timedelta(days=today.weekday(), weeks=offset_weeks)


class WeeklyAggregateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WeeklyAggregate

    actor = factory.SubFactory(ActorFactory)
    narrative = factory.SubFactory(NarrativeFactory)
    week = factory.LazyFunction(monday)
    n_documents = 2
    n_total = 10
    share = 0.2
