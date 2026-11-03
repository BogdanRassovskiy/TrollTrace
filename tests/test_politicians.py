"""Политики по институциональному определению: евродепутаты, правительство, председатели
партий; несколько функций одного человека (README §2 «Акторы»)."""

import datetime
import io
import json

import httpx
import pytest
from django.core.management import call_command

from apps.actors import registry
from apps.actors.models import Actor, ActorPosition
from apps.ingestion.http import PoliteClient

pytestmark = pytest.mark.django_db

GOVERNMENT_HTML = """<div class="members"><div class="members__intro"><div class="members__article">
<div class="promo__txt"><h2>Premiér Andrej Babiš</h2></div>
<a href="/cz/vlada/clenove-vlady/premier/zivotopis/andrej-babis-1/">O premiérovi</a></div></div>
<ul class="members__list"><li class="members__item"><div class="members__desc">
<h3><a href="/cz/vlada/clenove-vlady/lubomir-metnar-2/">Lubomír Metnar</a></h3>
<p>ministr vnitra</p></div></li>
<li class="members__item"><div class="members__desc">
<h3><a href="/cz/vlada/clenove-vlady/jaromir-zuna-3/">Jaromír Zůna</a></h3>
<p>místopředseda vlády a ministr obrany</p></div></li></ul></div>"""


def record(name, kind, title, **kwargs):
    return registry.PersonRecord(name=name, kind=kind, title=title, organization="X",
                                 source_url="https://x.example/", **kwargs)  # fmt: skip


def deputy(name):
    actor = Actor.objects.create(name=name, actor_type="politician", layer="E", party="ANO")
    ActorPosition.objects.create(actor=actor, kind="deputy", title="poslanec PSP",
                                 start_date=datetime.date(2025, 10, 4))  # fmt: skip
    actor.include("Poslanecký mandát", on=datetime.date(2025, 10, 4))
    return actor


def test_parse_government_page():
    records = registry.parse_government(GOVERNMENT_HTML)
    assert [(r.name, r.title) for r in records] == [
        ("Andrej Babiš", "předseda vlády"),
        ("Lubomír Metnar", "ministr vnitra"),
        ("Jaromír Zůna", "místopředseda vlády a ministr obrany"),
    ]
    assert records[1].source_url == "https://vlada.gov.cz/cz/vlada/clenove-vlady/lubomir-metnar-2/"


def test_one_person_several_functions_without_duplicates():
    metnar = deputy("Lubomír Metnar")
    records = registry.parse_government(GOVERNMENT_HTML)
    result = registry.apply_records(records, ActorPosition.Kind.GOVERNMENT)

    assert (result.created, result.matched) == (2, 1)
    assert Actor.objects.filter(name="Lubomír Metnar").count() == 1
    metnar.refresh_from_db()
    assert metnar.role == "poslanec PSP; ministr vnitra"
    assert metnar.inclusion_periods.count() == 1  # už byl zařazen jako poslanec
    zuna = Actor.objects.get(name="Jaromír Zůna")
    assert (zuna.layer, zuna.status, zuna.inclusion_periods.count()) == ("", "active", 1)

    # Druhý běh beze změn je idempotentní.
    registry.apply_records(records, ActorPosition.Kind.GOVERNMENT)
    assert ActorPosition.objects.filter(kind="government").count() == 3


def test_function_missing_from_list_is_closed_and_actor_without_functions_excluded():
    metnar = deputy("Lubomír Metnar")
    records = registry.parse_government(GOVERNMENT_HTML)
    registry.apply_records(records, ActorPosition.Kind.GOVERNMENT)
    today = datetime.date(2027, 1, 15)
    registry.apply_records(records[:1], ActorPosition.Kind.GOVERNMENT, today=today)  # jen premiér

    zuna = Actor.objects.get(name="Jaromír Zůna")
    assert zuna.positions.get().end_date == today
    assert zuna.status == Actor.Status.EXCLUDED  # žádná jiná funkce
    metnar.refresh_from_db()
    assert metnar.status == Actor.Status.ACTIVE and metnar.role == "poslanec PSP"  # mandát trvá


def test_mep_records_from_ep_open_data():
    def ep(data):
        return httpx.Response(200, json={"data": data})

    def handler(request):
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if "show-current" in url:
            return ep([{"identifier": "23699", "label": "Kateřina KONEČNÁ",
                        "api:political-group": "PfE"}])  # fmt: skip
        if "/meps/23699" in url:
            return ep([{"givenName": "Kateřina", "familyName": "Konečná", "hasMembership": [
                {"role": "def/ep-roles/MEMBER_PARLIAMENT", "organization": "org/ep-10",
                 "memberDuring": {"startDate": "2024-07-16"}},
                {"role": "def/ep-roles/MEMBER_PARLIAMENT", "organization": "org/ep-9",
                 "memberDuring": {"startDate": "2019-07-02", "endDate": "2024-07-15"}},
                {"membershipClassification": "def/ep-entities/NATIONAL_POLITICAL_GROUP",
                 "organization": "org/6769", "memberDuring": {"startDate": "2024-07-16"}},
            ]}])  # fmt: skip
        if "/corporate-bodies/6769" in url:
            return ep([{"label": "KSČM"}])
        return httpx.Response(404)

    client = PoliteClient(delay=0, transport=httpx.MockTransport(handler))
    (mep,) = registry.mep_records(client)
    assert (mep.name, mep.start, mep.party, mep.external_id) == (
        "Kateřina Konečná",
        datetime.date(2024, 7, 16),
        "KSČM",
        "23699",
    )
    assert mep.note == "Politická skupina EP: PfE"

    registry.apply_records([mep], ActorPosition.Kind.MEP)
    actor = Actor.objects.get(name="Kateřina Konečná")
    assert (actor.party, actor.role, actor.layer) == ("KSČM", "europoslanec", "")
    assert actor.inclusion_periods.get().start_date == datetime.date(2024, 7, 16)


def test_party_politicians_file_is_valid():
    records, people = registry.manual_records()
    assert {r.kind for r in records} <= set(registry.MANUAL_KINDS)
    assert all(r.source_url.startswith("https://") and r.title for r in records)
    leaders = [r.external_id for r in records if r.kind == "party_leader"]
    assert len(leaders) == len(set(leaders)) and {"ODS", "SOCDEM", "PRO", "Přísaha"} <= set(leaders)


def write(path, people):
    path.write_text(json.dumps({"checked": "2026-11-03", "people": people}), encoding="utf-8")


def test_politician_without_public_office_is_tracked_and_honestly_marked(tmp_path):
    path = tmp_path / "people.json"
    write(path, [
        {"name": "Jiří Nedvěd", "party": "SOCDEM", "functions": [
            {"kind": "party_leader", "title": "předseda SOCDEM", "organization": "SOCDEM",
             "source_url": "https://socdem.example/"}]},
        {"name": "Jan Novák", "party": "X", "criterion": "Bývalý politik s vlastním publikem",
         "source_url": "https://x.example/"},
    ])  # fmt: skip
    registry.apply_manual(path)

    nedved = Actor.objects.get(name="Jiří Nedvěd")
    assert nedved.actor_type == "politician" and nedved.status == "active"
    assert not nedved.holds_public_office
    assert nedved.role == "předseda SOCDEM; bez veřejné funkce"

    novak = Actor.objects.get(name="Jan Novák")
    assert novak.manual_inclusion and novak.role == "bez veřejné funkce"
    assert novak.inclusion_periods.get().inclusion_reason == "Bývalý politik s vlastním publikem"

    # Nedvěd přestal být předsedou: funkce se uzavře a bez funkce vypadne z výběru;
    # Novák je zařazen rozhodnutím výzkumníka a zůstává.
    write(path, [{"name": "Jan Novák", "party": "X", "source_url": "https://x.example/"}])
    registry.apply_manual(path, today=datetime.date(2027, 1, 1))
    nedved.refresh_from_db()
    novak.refresh_from_db()
    assert (
        nedved.status == "excluded" and nedved.role == "dříve předseda SOCDEM; bez veřejné funkce"
    )
    assert novak.status == "active"


def test_party_function_keyed_by_party_and_several_kinds(tmp_path):
    path = tmp_path / "people.json"
    slachta = {"name": "Robert Šlachta", "party": "Přísaha", "functions": [
        {"kind": "party_leader", "title": "předseda", "organization": "Přísaha",
         "source_url": "https://p.example/"},
        {"kind": "senator", "title": "senátor", "organization": "Senát PČR",
         "source_url": "https://p.example/"}]}  # fmt: skip
    write(path, [slachta])
    registry.apply_manual(path)
    slachta["functions"][0]["title"] = "předseda hnutí Přísaha"
    write(path, [slachta])
    registry.apply_manual(path)

    actor = Actor.objects.get(name="Robert Šlachta")
    assert actor.positions.filter(kind="party_leader").get().title == "předseda hnutí Přísaha"
    assert actor.holds_public_office  # senátor
    assert actor.role == "senátor; předseda hnutí Přísaha"


def test_sync_politicians_refuses_empty_list(monkeypatch):
    from apps.actors.management.commands import sync_politicians

    monkeypatch.setitem(sync_politicians.LISTS, "meps",
                        ("Europoslanci", lambda client: [], ActorPosition.Kind.MEP))  # fmt: skip
    with pytest.raises(Exception, match="prázdný seznam"):
        call_command("sync_politicians", "--only", "meps", stdout=io.StringIO())
