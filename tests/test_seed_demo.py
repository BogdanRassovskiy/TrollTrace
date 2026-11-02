"""Этап 1: seed-команда с демо-данными."""

import pytest
from django.core.management import CommandError, call_command

from apps.actors.models import Actor, ActorInclusionPeriod
from apps.analytics.models import WeeklyAggregate
from apps.documents.models import Document
from apps.narratives.models import Label, Narrative

pytestmark = pytest.mark.django_db


def seed(*args):
    call_command("seed_demo", "--actors", "40", "--weeks", "10", *args, stdout=None)


def test_seed_creates_consistent_demo_data():
    seed()
    assert Actor.objects.count() == 40
    assert set(Actor.objects.values_list("layer", flat=True)) == {"A", "B", "C", "D", "E"}
    assert Narrative.objects.count() == 5
    assert Narrative.objects.get(code="N003").current_version == 2
    assert Document.objects.exists()
    assert Label.objects.count() == Document.objects.count()

    # Агрегаты только по неделям, когда актор входил в выборку.
    for row in WeeklyAggregate.objects.filter(actor__isnull=False).select_related("actor")[:500]:
        assert (
            ActorInclusionPeriod.objects.filter(actor=row.actor, start_date__lte=row.week)
            .exclude(end_date__lt=row.week)
            .exists()
        )
    assert WeeklyAggregate.objects.filter(group_key__startswith="layer:").exists()


def test_seed_refuses_to_overwrite_without_reset():
    seed()
    with pytest.raises(CommandError):
        seed()
    seed("--reset")
    assert Actor.objects.count() == 40
