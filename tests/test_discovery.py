"""Автоматическое добавление местных политиков, функция которых названа в тексте."""

import datetime
import io

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.actors import discovery
from apps.actors.models import Actor, ActorMention, ActorPosition
from apps.processing.pipeline import process_pending

from .factories import DocumentFactory

pytestmark = pytest.mark.django_db

TEXT = (
    "Starosta obce Horní Lhota Jan Novák (STAN) uvedl, že most se opraví. "
    "S tím nesouhlasí Jana Svobodová, zastupitelka města Ústí nad Labem. "
    "Bývalý starosta Lhoty pod Horami Petr Dvořák to označil za chybu."
)


def doc(text, **kwargs):
    when = timezone.make_aware(datetime.datetime(2026, 10, 20, 9, 0))
    return DocumentFactory(text=text, title="", published_at=when, **kwargs)


def test_finds_explicit_local_functions():
    mentions = {m.name: m for m in discovery.find_mentions(TEXT)}
    assert set(mentions) == {"Jan Novák", "Jana Svobodová", "Petr Dvořák"}
    novak = mentions["Jan Novák"]
    assert (novak.title, novak.place, novak.party) == (
        "starosta obce Horní Lhota", "Horní Lhota", "STAN"
    )  # fmt: skip
    assert mentions["Jana Svobodová"].title == "zastupitelka města Ústí nad Labem"
    assert mentions["Petr Dvořák"].former and mentions["Petr Dvořák"].place == "Lhoty pod Horami"


@pytest.mark.parametrize(
    "text",
    [
        "Se starostou obce Lhota Janem Novákem jsme mluvili.",  # nepřímý pád
        "Starosta Jan Novák řekl, že most se opraví.",  # bez místa
        "Zastupitelstvo obce Lhota Jan Novák svolal.",  # není funkce osoby
        "Starosta obce Horní Lhota uvedl, že most se opraví.",  # bez jména
        "starosta obce Lhota Novák Dvořák",  # neznámé křestní jméno
    ],
)
def test_ignores_vague_mentions(text):
    assert discovery.find_mentions(text) == []


def test_record_adds_marked_actor_with_evidence():
    document = doc(TEXT)
    assert discovery.record_document(document) == 3
    assert discovery.record_document(document) == 0  # opakování je bezpečné

    novak = Actor.objects.get(name="Jan Novák")
    assert novak.added_automatically and not novak.manual_inclusion
    assert (novak.actor_type, novak.status, novak.party) == ("politician", "active", "STAN")
    assert novak.role == "starosta obce Horní Lhota" and novak.office_status == "office"
    assert novak.inclusion_criterion.startswith("Přidán automaticky: v textu uveden jako")
    assert novak.inclusion_periods.count() == 1
    mention = novak.mentions.get()
    assert mention.document == document and "Horní Lhota Jan Novák" in mention.quote
    position = novak.positions.get()
    assert (position.kind, position.source_url) == (ActorPosition.Kind.REGIONAL, document.url)

    dvorak = Actor.objects.get(name="Petr Dvořák")  # bývalý starosta: funkce uzavřená
    assert dvorak.positions.get().end_date == datetime.date(2026, 10, 20)
    assert dvorak.role == "dříve starosta Lhoty pod Horami; bez veřejné funkce"
    assert dvorak.office_status == "none"


def test_existing_politician_is_reused_and_excluded_stays_excluded():
    deputy = Actor.objects.create(name="Jan Novák", actor_type=Actor.Type.POLITICIAN)
    ActorPosition.objects.create(actor=deputy, kind="regional", title="starosta obce Horní Lhota")
    wrong = Actor.objects.create(
        name="Jana Svobodová", actor_type="politician", added_automatically=True
    )
    wrong.exclude("Chybně rozpoznaná osoba")

    assert discovery.record_document(doc(TEXT)) == 1  # jen Dvořák je nový
    assert Actor.objects.filter(name="Jan Novák").count() == 1
    assert deputy.positions.count() == 1 and not Actor.objects.get(pk=deputy.pk).added_automatically
    assert deputy.mentions.count() == 1
    wrong.refresh_from_db()
    assert wrong.status == "excluded" and not wrong.positions.exists()


def test_pipeline_and_backfill_command():
    doc(TEXT)
    assert process_pending().actors == 3
    ActorMention.objects.all().delete()
    out = io.StringIO()
    call_command("discover_actors", stdout=out)
    assert "nových zmínek 3, automaticky přidaných aktérů 0" in out.getvalue()
