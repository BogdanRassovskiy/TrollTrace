"""Политики по институциональному определению (README §2 «Акторы»), кроме депутатов PSP:
чешские евродепутаты (открытые данные Европарламента), члены правительства (vlada.gov.cz)
и партийные политики (ручной список data/party_politicians.json, сверенный с сайтами партий):
председатели партий, в том числе непарламентских, и политики без публичной функции, которых
исследователь отслеживает из-за их аудитории. Политиком они остаются, но в сводке функций
честно указано «bez veřejné funkce».

Один человек — один актор, у него может быть несколько функций одновременно (ActorPosition):
депутат и министр, евродепутат и председатель партии. Существующий актор находится
по имени среди политиков, дубль не создаётся. Функция, пропавшая из официального списка,
закрывается датой синхронизации; актор без единой открытой функции исключается из выборки
(его документы остаются).
"""

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

import lxml.html
from django.db import transaction
from django.utils import timezone

from apps.panel import audit

from .models import Actor, ActorPosition

NOTE = "sync_politicians"
EP_API = "https://data.europarl.europa.eu/api/v2"
EP_CURRENT = (
    f"{EP_API}/meps/show-current?country-of-representation=CZ&format=application%2Fld%2Bjson"
)
EP_DETAIL = f"{EP_API}/meps/{{id}}?format=application%2Fld%2Bjson"
EP_BODY = f"{EP_API}/corporate-bodies/{{id}}?format=application%2Fld%2Bjson&language=cs"
EP_PROFILE = "https://www.europarl.europa.eu/meps/cs/{id}"
EP_PARLIAMENT_PREFIX = "org/ep-"
GOVERNMENT_URL = "https://vlada.gov.cz/cz/vlada/"
PARTY_POLITICIANS_PATH = Path(__file__).resolve().parent / "data" / "party_politicians.json"

# Zkratky stran v datech EP → názvy klubů PSP, aby šly politiky srovnávat podle strany.
# Nejednoznačné (např. společná kandidátka „TOP09/STAN“) zůstávají, jak jsou.
PARTY_ALIASES = {"ANO 2011": "ANO", "AUTO": "Motoristé", "PIR": "Piráti", "Ind.": ""}


@dataclass
class PersonRecord:
    name: str
    kind: str
    title: str
    organization: str
    source_url: str
    start: datetime.date | None = None
    external_id: str = ""
    party: str = ""
    note: str = ""


@dataclass
class SyncResult:
    created: int = 0
    matched: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)


def _date(value) -> datetime.date | None:
    return datetime.date.fromisoformat(value) if value else None


def find_politician(name: str) -> Actor | None:
    return (
        Actor.objects.filter(name=name, actor_type=Actor.Type.POLITICIAN)
        .order_by("layer", "pk")
        .first()
    )


def sync_inclusion(actor: Actor, reason: str, on: datetime.date | None = None) -> None:
    """Открытая функция есть — актор в выборке; нет ни одной — исключается
    (кроме включённых решением исследователя: manual_inclusion)."""
    has_open = actor.positions.filter(end_date__isnull=True).exists()
    if has_open and (actor.status != Actor.Status.ACTIVE or actor.open_period is None):
        actor.include(reason, on=on)
    elif not has_open and actor.status == Actor.Status.ACTIVE and not actor.manual_inclusion:
        actor.exclude("Bez funkce podle institucionální definice (README §2)", on=on)


def upsert_position(actor: Actor, record: PersonRecord) -> ActorPosition:
    lookup = {"actor": actor, "kind": record.kind, "end_date__isnull": True}
    if record.external_id:
        lookup["external_id"] = record.external_id
    else:
        lookup["title"] = record.title
    position = ActorPosition.objects.filter(**lookup).first()
    values = {
        "title": record.title[:200],
        "organization": record.organization[:200],
        "source_url": record.source_url,
        "external_id": record.external_id,
        "note": record.note,
    }
    if position is None:
        position = ActorPosition.objects.create(
            actor=actor, kind=record.kind, start_date=record.start, **values
        )
        audit.log_create(None, position, note=NOTE)
        return position
    before = audit.snapshot(position)
    for key, value in values.items():
        setattr(position, key, value)
    if record.start and position.start_date != record.start:
        position.start_date = record.start
    position.save()
    audit.log_change(None, position, before, note=NOTE)
    return position


def apply_records(records: list[PersonRecord], kind: str, today=None) -> SyncResult:
    """Сводит официальный список функций одного вида с базой (идемпотентно)."""
    today = today or timezone.localdate()
    result = SyncResult()
    current = set()
    with transaction.atomic():
        for record in records:
            actor = find_politician(record.name)
            if actor is None:
                actor = Actor.objects.create(
                    name=record.name,
                    actor_type=Actor.Type.POLITICIAN,
                    party=record.party[:100],
                    inclusion_criterion=f"{record.title} (institucionální definice, README §2)",
                    inclusion_reference=record.source_url,
                )
                audit.log_create(None, actor, note=NOTE)
                result.created += 1
            else:
                result.matched += 1
                if record.party and not actor.party:
                    before = audit.snapshot(actor)
                    actor.party = record.party[:100]
                    actor.save(update_fields=["party", "updated_at"])
                    audit.log_change(None, actor, before, note=NOTE)
            position = upsert_position(actor, record)
            current.add(position.pk)
            sync_inclusion(actor, record.title, on=record.start or today)
            actor.refresh_role()

        stale = ActorPosition.objects.filter(kind=kind, end_date__isnull=True).exclude(
            pk__in=current
        )
        for position in stale.select_related("actor"):
            before = audit.snapshot(position)
            position.end_date = max(today, position.start_date or today)
            position.save(update_fields=["end_date"])
            audit.log_change(None, position, before, note=NOTE)
            sync_inclusion(position.actor, "", on=today)
            position.actor.refresh_role()
            result.closed += 1
    return result


# --- Evropský parlament -------------------------------------------------------------------


def _ep_json(client, url: str) -> dict:
    response = client.get(url, headers={"Accept": "application/ld+json"})
    if not response.ok:
        raise RuntimeError(f"{url}: HTTP {response.status}")
    return json.loads(response.content)


def mep_records(client) -> list[PersonRecord]:
    """Текущие чешские евродепутаты: мандат, национальная партия, политическая группа."""
    parties: dict[str, str] = {}
    records = []
    for item in _ep_json(client, EP_CURRENT)["data"]:
        mep_id = item["identifier"]
        detail = _ep_json(client, EP_DETAIL.format(id=mep_id))["data"][0]
        start, party_org = None, ""
        for membership in detail.get("hasMembership", []):
            period = membership.get("memberDuring") or {}
            if period.get("endDate"):
                continue
            org = membership.get("organization") or ""
            if membership.get("role") == "def/ep-roles/MEMBER_PARLIAMENT" and org.startswith(
                EP_PARLIAMENT_PREFIX
            ):
                start = _date(period.get("startDate"))
            if membership.get("membershipClassification") == (
                "def/ep-entities/NATIONAL_POLITICAL_GROUP"
            ):
                party_org = org.removeprefix("org/")
        if party_org and party_org not in parties:
            body = _ep_json(client, EP_BODY.format(id=party_org))["data"][0]
            label = body.get("label") or ""
            parties[party_org] = PARTY_ALIASES.get(label, label)
        group = item.get("api:political-group", "")
        records.append(
            PersonRecord(
                name=f"{detail.get('givenName', '')} {detail.get('familyName', '')}".strip(),
                kind=ActorPosition.Kind.MEP,
                title="europoslankyně"
                if str(detail.get("hasGender", "")).endswith("FEMALE")
                else "europoslanec",
                organization="Evropský parlament",
                source_url=EP_PROFILE.format(id=mep_id),
                start=start,
                external_id=mep_id,
                party=parties.get(party_org, ""),
                note=f"Politická skupina EP: {group}" if group else "",
            )
        )
    return records


# --- Vláda ČR -----------------------------------------------------------------------------


def parse_government(content: str, base_url: str = GOVERNMENT_URL) -> list[PersonRecord]:
    root = lxml.html.fromstring(content)
    root.make_links_absolute(base_url)
    records = []
    for headline in root.xpath('//div[contains(@class, "members__intro")]//h2'):
        text = " ".join(headline.text_content().split())
        if text.lower().startswith("premiér "):
            link = headline.xpath('ancestor::div[contains(@class, "members__intro")]//a/@href')
            records.append(
                PersonRecord(
                    name=text[len("premiér ") :],
                    kind=ActorPosition.Kind.GOVERNMENT,
                    title="předseda vlády",
                    organization="Vláda ČR",
                    source_url=link[0] if link else base_url,
                )
            )
    for item in root.xpath('//li[contains(@class, "members__item")]'):
        link = item.xpath(".//h3/a")
        title = item.xpath('.//div[contains(@class, "members__desc")]/p')
        if not link or not title:
            continue
        records.append(
            PersonRecord(
                name=" ".join(link[0].text_content().split()),
                kind=ActorPosition.Kind.GOVERNMENT,
                title=" ".join(title[0].text_content().split()),
                organization="Vláda ČR",
                source_url=link[0].get("href") or base_url,
            )
        )
    return records


def government_records(client) -> list[PersonRecord]:
    response = client.get(GOVERNMENT_URL)
    if not response.ok:
        raise RuntimeError(f"{GOVERNMENT_URL}: HTTP {response.status}")
    records = parse_government(response.text, response.final_url)
    if not records:
        raise RuntimeError("Na stránce vlády nebyli nalezeni členové — změnila se šablona?")
    return records


# --- Stranické funkce a politici mimo instituce --------------------------------------------

# Druhy funkcí, které vede ruční seznam (data/party_politicians.json).
MANUAL_KINDS = [
    ActorPosition.Kind.PARTY_LEADER,
    ActorPosition.Kind.PARTY_FUNCTION,
    ActorPosition.Kind.SENATOR,
    ActorPosition.Kind.REGIONAL,
]


def manual_records(path: Path = PARTY_POLITICIANS_PATH) -> tuple[list[PersonRecord], list[dict]]:
    """Ruční seznam ověřený na webech stran: jednotný registr předsedů neexistuje.

    Vrací (funkce, lidé bez funkce). Lidé bez funkce jsou politici sledovaní rozhodnutím
    výzkumníka: zařazují se s manual_inclusion a v souhrnu mají „bez veřejné funkce“.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    checked = f"Ověřeno {data.get('checked', '')}".strip()
    records, people = [], []
    for item in data["people"]:
        functions = item.get("functions", [])
        if not functions:
            people.append(item)
        for function in functions:
            records.append(
                PersonRecord(
                    name=item["name"],
                    kind=function["kind"],
                    title=function["title"],
                    organization=function.get("organization", ""),
                    source_url=function["source_url"],
                    # Ключ функции — орган или партия: смена формулировки не плодит историю.
                    external_id=function.get("organization", "") or function["title"],
                    party=item.get("party", ""),
                    note=" ".join(filter(None, [function.get("note", ""), checked])),
                )
            )
    return records, people


def party_politician_records(client=None, path: Path = PARTY_POLITICIANS_PATH):
    return manual_records(path)[0]


def apply_manual(path: Path = PARTY_POLITICIANS_PATH, today=None) -> SyncResult:
    records, people = manual_records(path)
    result = SyncResult()
    for kind in MANUAL_KINDS:
        partial = apply_records([r for r in records if r.kind == kind], kind, today=today)
        result.created += partial.created
        result.matched += partial.matched
        result.closed += partial.closed
    with transaction.atomic():
        for item in people:
            actor = find_politician(item["name"])
            criterion = (
                item.get("criterion")
                or "Politik bez veřejné funkce, sledován rozhodnutím výzkumníka"
            )
            if actor is None:
                actor = Actor.objects.create(
                    name=item["name"],
                    actor_type=Actor.Type.POLITICIAN,
                    party=item.get("party", "")[:100],
                    manual_inclusion=True,
                    inclusion_criterion=criterion,
                    inclusion_reference=item.get("source_url", ""),
                )
                audit.log_create(None, actor, note=NOTE)
                actor.include(criterion, on=today or timezone.localdate())
                result.created += 1
            else:
                result.matched += 1
                if not actor.manual_inclusion:
                    before = audit.snapshot(actor)
                    actor.manual_inclusion = True
                    actor.save(update_fields=["manual_inclusion", "updated_at"])
                    audit.log_change(None, actor, before, note=NOTE)
            actor.refresh_role()
    return result
