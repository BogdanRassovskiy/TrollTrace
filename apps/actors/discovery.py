"""Автоматическое добавление местных политиков, упомянутых в текстах (README §2 «Акторы»).

Старост и заступителей тысячи, и большинство из них публично не высказывается: перебирать
все общины бессмысленно. Если же в тексте прямо сказано «starosta obce Lhota Jan Novák» или
«Jan Novák (STAN), zastupitel města Brna», человек добавляется в акторы с пометкой
«přidán automaticky», функцией и ссылкой на документ (ActorMention).

Распознаётся только именительный падеж функции рядом с полным именем (известное křestní
jméno + фамилия) и названием общины, города или края. Косвенные падежи («se starostou …»)
и упоминания без названия места не дают актора — ложные срабатывания дороже пропусков.
"""

import datetime
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.panel import audit

from .models import Actor, ActorMention, ActorPosition

GIVEN_NAMES_PATH = Path(__file__).resolve().parent / "data" / "given_names.txt"

TITLE = (
    r"(?:místo)?starost(?:a|ka)|primátor(?:ka)?|hejtman(?:ka)?|zastupitel(?:ka)?|"
    r"náměst(?:ek|kyně) (?:primátora|primátorky|hejtmana|hejtmanky|starosty|starostky)"
)
FORMER = r"(?:bývalý|bývalá|někdejší|dřívější)\s+"
UPPER, LOWER = "A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ", "a-záčďéěíňóřšťúůýž"
WORD = rf"[{UPPER}][{LOWER}]+"
SURNAME = rf"{WORD}(?:-{WORD})?"
PARTY = r"(?:\s*\((?P<party>[^()\n]{1,30})\))?"

# «starosta obce Lhota Jan Novák», «bývalá starostka Lhoty Jana Nová (ANO)»
TITLE_FIRST = re.compile(rf"(?i:\b(?P<former>{FORMER})?(?P<title>{TITLE}))\b")
# «Jan Novák (STAN), zastupitel města Brna»
NAME_FIRST = re.compile(
    rf"\b(?P<given>{WORD})\s+(?P<surname>{SURNAME}){PARTY},\s+"
    rf"(?i:(?P<former>{FORMER})?(?P<title>{TITLE}))\b"
)
TOKEN = re.compile(r"[ \t]+(\d+|[^\W\d_]+(?:-[^\W\d_]+)*)")
PARTY_AFTER = re.compile(r"\s*\(([^()\n]{1,30})\)")
QUALIFIERS = {
    "obce", "města", "městyse", "městysu", "městské", "části", "městského", "obvodu",
    "statutárního", "hlavního", "kraje",
}  # fmt: skip
CONNECTORS = {"nad", "pod", "u", "v", "ve", "na", "při"}


@cache
def given_names() -> frozenset[str]:
    lines = GIVEN_NAMES_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


@dataclass(frozen=True)
class Mention:
    name: str
    title: str  # funkce včetně místa, jak je v textu: «starostka obce Lhota»
    place: str
    party: str
    former: bool
    quote: str


def _tokens(text: str, pos: int) -> tuple[list[str], int]:
    """Slova za funkcí až po interpunkci nebo běžné slovo s malým písmenem."""
    tokens = []
    while match := TOKEN.match(text, pos):
        word = match.group(1)
        if word[0].islower() and word not in QUALIFIERS and word not in CONNECTORS:
            break
        tokens.append(word)
        pos = match.end()
    while tokens and tokens[-1] in CONNECTORS:
        tokens.pop()
    return tokens, pos


def _place(tokens: list[str]) -> tuple[str, str]:
    """(«obce Horní Lhota», «Horní Lhota»). Místo musí mít slovo s velkým písmenem."""
    if not any(t[0].isupper() for t in tokens if not t.isdigit()):
        return "", ""
    start = 0
    while tokens[start] in QUALIFIERS:
        start += 1
    return " ".join(tokens), " ".join(tokens[start:])


def _quote(text: str, start: int, end: int, width: int = 100) -> str:
    left, right = max(0, start - width), min(len(text), end + width)
    snippet = " ".join(text[left:right].split())
    return ("…" if left else "") + snippet + ("…" if right < len(text) else "")


def find_mentions(text: str) -> list[Mention]:
    names = given_names()
    found: dict[tuple[str, str], Mention] = {}

    for match in TITLE_FIRST.finditer(text):
        tokens, end = _tokens(text, match.end())
        if len(tokens) < 3 or tokens[-2] not in names or not tokens[-1][0].isupper():
            continue
        where, place = _place(tokens[:-2])
        if not place:
            continue
        party = PARTY_AFTER.match(text, end)
        mention = Mention(
            name=f"{tokens[-2]} {tokens[-1]}",
            title=f"{match.group('title').lower()} {where}",
            place=place,
            party=party.group(1).strip() if party else "",
            former=bool(match.group("former")),
            quote=_quote(text, match.start(), end),
        )
        found.setdefault((mention.name, mention.title), mention)

    for match in NAME_FIRST.finditer(text):
        if match.group("given") not in names:
            continue
        tokens, end = _tokens(text, match.end())
        where, place = _place(tokens)
        if not place:
            continue
        mention = Mention(
            name=f"{match.group('given')} {match.group('surname')}",
            title=f"{match.group('title').lower()} {where}",
            place=place,
            party=(match.group("party") or "").strip(),
            former=bool(match.group("former")),
            quote=_quote(text, match.start(), end),
        )
        found.setdefault((mention.name, mention.title), mention)
    return list(found.values())


def _external_id(mention: Mention) -> str:
    return f"text:{mention.title.lower()}"[:100]


def _record(document, mention: Mention) -> bool:
    """Vrací True, pokud vznikl nový aktér."""
    actor = (
        Actor.objects.filter(name=mention.name, actor_type=Actor.Type.POLITICIAN)
        .order_by("added_automatically", "layer", "pk")
        .first()
    )
    published = document.published_at or document.collected_at or timezone.now()
    day = timezone.localdate(published)
    reason = (
        f"Přidán automaticky: v textu uveden jako „{mention.title}“ "
        f"({document.url}, {day:%d. %m. %Y})."
    )
    created = actor is None
    if created:
        actor = Actor.objects.create(
            name=mention.name,
            actor_type=Actor.Type.POLITICIAN,
            party=mention.party,
            added_automatically=True,
            inclusion_criterion=reason,
            inclusion_reference=document.url[:500],
        )
        actor.include(reason, on=datetime.date.today())
        audit.log_create(None, actor, note=reason, username="discover_actors")
    ActorMention.objects.get_or_create(
        actor=actor,
        document=document,
        title=mention.title[:200],
        defaults={"quote": mention.quote},
    )
    if actor.status == Actor.Status.EXCLUDED:
        return created  # vyřazen výzkumníkem (např. chybně rozpoznaný): funkci nepřidáváme
    has_regional = actor.positions.filter(
        kind=ActorPosition.Kind.REGIONAL, end_date__isnull=True
    ).exists()
    if not actor.added_automatically and has_regional:
        return created  # funkci už vede registr nebo ruční seznam
    _, new_position = ActorPosition.objects.get_or_create(
        actor=actor,
        kind=ActorPosition.Kind.REGIONAL,
        external_id=_external_id(mention),
        defaults={
            "title": mention.title[:200],
            "organization": mention.place[:200],
            "end_date": day if mention.former else None,
            "source_url": document.url[:500],
            "note": f"Automaticky z textu ze dne {day:%d. %m. %Y}: {mention.quote}",
        },
    )
    if new_position:
        actor.refresh_role()
    return created


def record_document(document) -> int:
    """Zapíše zmínky z dokumentu; vrací počet nově přidaných aktérů. Opakování je bezpečné."""
    mentions = find_mentions(f"{document.title}\n{document.text}")
    if not mentions:
        return 0
    with transaction.atomic():
        return sum(_record(document, mention) for mention in mentions)
