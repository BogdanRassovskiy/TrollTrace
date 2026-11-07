"""Маркеры нарративов: короткие оценочные ярлыки из документов (README §3).

Вторая дорога к нарративу рядом с близостью по смыслу. «Kyjevský režim», «junta»,
«banderovci» — сжатая ссылка на уже известное утверждение. Найденный в тексте маркер
отправляет документ к модели даже при низкой близости. Метку и позицию маркер не ставит:
решает модель по всему тексту (цитата Захаровой в ČT24 — «neutral», а не «pro»).

Жизненный цикл без ручного утверждения:

1. Предложение. Ставя «pro», модель возвращает дословный ярлык из текста (поле `marker`).
   Код проверяет, что фраза в тексте есть, и строит вид для поиска (`key`, начала слов без
   диакритики). Каждое предложение хранится (MarkerProposal): откуда маркер взялся.
2. Испытание. Фраза, предложенная в `marker-min-documents` документах от
   `marker-min-authors` авторов, начинает срабатывать в потоке — не больше
   `marker-trial-checks` документов. Узел дерева: нарратив, к которому относится больше
   2/3 предложений, иначе общий родитель всех предлагавших; нет общего родителя — отклонён.
3. Решение по точности: доля документов с маркером, где модель нашла нарратив (в любой
   позиции, с уверенностью для метки). ≥ `marker-min-precision` — активен, иначе отклонён.
   У активного точность считается за последние `marker-recent-checks` проверок; падение
   ниже порога — приостановлен.

Исследователь видит сводку и может маркер запретить, вернуть на испытание или перенести
к другому узлу. Каждая смена статуса — MarkerEvent с причиной и числами. Архив по новым
маркерам проходится только по кнопке (как ретроразметка нарратива).
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, F
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import Document
from apps.narratives.models import (
    LabelDecision,
    MarkerEvent,
    MarkerProposal,
    MarkerStatus,
    Narrative,
    NarrativeMarker,
    NarrativeStatus,
    Stance,
)
from apps.panel.defaults import get_setting

from .relevance import stem_pattern
from .text import fold, words

logger = logging.getLogger(__name__)

MAX_WORDS = 4
MIN_WORD = 5  # хотя бы одно слово маркера не короче (иначе «kati», «ten kat» …)
MAJORITY = 2 / 3
MAX_FORMS = 10
MATCHING = (MarkerStatus.TRIAL, MarkerStatus.ACTIVE)


# Окончания падежей (чешские и русские, без диакритики), длинные проверяются первыми.
# «im» / «им» нет намеренно: «režim» и «режим» — основа, а не окончание.
ENDINGS = sorted(
    {  # проходят через fold, как и текст: «ий» → «ии»
        "ovych", "ovymi", "eho", "emu", "ych", "ymi", "ami", "ech", "ich", "imi", "ove", "ovi",
        "ou", "ym", "em", "y", "a", "e", "i", "o", "u",
        "ого", "его", "ому", "ему", "ыми", "ими", "ами", "ями", "ая", "яя", "ую", "юю", "ое",
        "ее", "ые", "ие", "ых", "их", "ый", "ий", "ой", "ей", "ом", "ем", "ам", "ям", "ах",
        "ях", "ов", "ев", "ы", "и", "а", "я", "у", "ю", "е", "о",
    },
    key=len,
    reverse=True,
)  # fmt: skip
ENDINGS = sorted({fold(e) for e in ENDINGS}, key=len, reverse=True)
MIN_STEM = 3


def stem(word: str) -> str:
    """Основа без окончания: «kyjevského» и «kyjevský» → «kyjevsk», «režimu» → «rezim»."""
    for ending in ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM:
            return word[: -len(ending)]
    return word


def make_key(phrase: str) -> str:
    """Фраза → вид для поиска; пустая строка, если на маркер не годится."""
    parts = words(phrase)
    if not 1 <= len(parts) <= MAX_WORDS or any(p.isdigit() for p in parts):
        return ""
    if max(len(p) for p in parts) < MIN_WORD:
        return ""
    return " ".join(stem(p) for p in parts)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", fold(text)).strip()


def in_text(phrase: str, document: Document) -> bool:
    """Фраза дословно есть в документе (без учёта регистра, диакритики и пробелов)."""
    needle = _squash(phrase)
    return bool(needle) and needle in _squash(f"{document.title}\n{document.text}")


def author_of(document: Document) -> str:
    return f"a{document.actor_id}" if document.actor_id else f"s{document.source_id}"


# --- Поиск в тексте --------------------------------------------------------------


@dataclass
class Matcher:
    """Маркер в отборе кандидатов. `remaining` — сколько ещё документов можно отправить
    к модели на испытании (None — без ограничения)."""

    marker_id: int
    key: str
    pattern: re.Pattern
    remaining: int | None = None

    def open(self) -> bool:
        return self.remaining is None or self.remaining > 0


@dataclass
class MarkerIndex:
    by_narrative: dict = field(default_factory=dict)

    def for_narrative(self, narrative_id: int) -> list[Matcher]:
        return self.by_narrative.get(narrative_id, [])


def _trial_start(marker: NarrativeMarker):
    return marker.status_changed_at or marker.created_at


def _trial_checks(marker: NarrativeMarker):
    """Решения испытания: документы с маркером с начала текущего испытания."""
    return LabelDecision.objects.filter(
        markers__contains=[marker.pk],
        narrative=marker.narrative,
        created_at__gte=_trial_start(marker),
    )


def load_index(narrative_ids=None) -> MarkerIndex:
    markers = NarrativeMarker.objects.filter(status__in=MATCHING)
    if narrative_ids is not None:
        markers = markers.filter(narrative_id__in=narrative_ids)
    limit = int(get_setting("marker-trial-checks"))
    index = MarkerIndex()
    for marker in markers:
        remaining = None
        if marker.status == MarkerStatus.TRIAL:
            remaining = max(0, limit - _trial_checks(marker).count())
        index.by_narrative.setdefault(marker.narrative_id, []).append(
            Matcher(marker.pk, marker.key, stem_pattern(marker.key), remaining)
        )
    return index


def find(matchers: list[Matcher], folded: str) -> list[Matcher]:
    return [m for m in matchers if m.open() and m.pattern.search(folded)]


# --- Предложения модели ------------------------------------------------------------


def record(decision: LabelDecision, phrase: str) -> NarrativeMarker | None:
    """Ярлык из ответа модели по положительному решению → предложение маркера."""
    phrase = re.sub(r"\s+", " ", str(phrase or "")).strip().strip("„“\"'«»")[:150]
    document = decision.document
    if not phrase or not in_text(phrase, document):
        return None
    key = make_key(phrase)
    if not key:
        return None
    marker = NarrativeMarker.objects.filter(key=key).first() or same_phrase(phrase)
    if marker is None:
        marker = NarrativeMarker.objects.create(
            key=key, narrative=decision.narrative, phrase=phrase, first_document=document
        )
    _, created = MarkerProposal.objects.get_or_create(
        marker=marker,
        document=document,
        defaults={
            "decision": decision,
            "narrative": decision.narrative,
            "phrase": phrase,
            "author": author_of(document),
        },
    )
    if not created:
        return marker
    if phrase not in marker.forms and len(marker.forms) < MAX_FORMS:
        marker.forms = [*marker.forms, phrase]
    counts = marker.proposals.aggregate(
        n=Count("document", distinct=True), authors=Count("author", distinct=True)
    )
    marker.n_proposals, marker.n_authors = counts["n"], counts["authors"]
    marker.save(update_fields=["forms", "n_proposals", "n_authors", "updated_at"])
    if marker.status == MarkerStatus.PROPOSED:
        consider_trial(marker)
    return marker


def same_phrase(phrase: str) -> NarrativeMarker | None:
    """Jiný tvar už známého markeru («киевским режимом» k «киевск режим»): stejný počet slov
    a vzor existujícího markeru frázi najde od začátku."""
    folded = " ".join(words(phrase))
    size = len(folded.split())
    for marker in NarrativeMarker.objects.all():
        if len(marker.key.split()) == size and stem_pattern(marker.key).match(folded):
            return marker
    return None


def _live(narrative: Narrative) -> Narrative:
    """Слитый дубль → основной нарратив."""
    seen = set()
    while narrative.merged_into_id and narrative.pk not in seen:
        seen.add(narrative.pk)
        narrative = narrative.merged_into
    return narrative


def _ancestors(narrative: Narrative) -> list[Narrative]:
    chain, node = [], narrative
    while node is not None and node not in chain:
        chain.append(node)
        node = node.parent
    return chain


def resolve_node(marker: NarrativeMarker) -> tuple[Narrative | None, str]:
    """Узел, на который указывает маркер, по предложениям; (None, причина) — неоднозначно."""
    votes = Counter()
    for proposal in marker.proposals.select_related("narrative"):
        votes[_live(proposal.narrative)] += 1
    if not votes:
        return None, "Žádný návrh."
    (top, n), total = votes.most_common(1)[0], sum(votes.values())
    if n / total > MAJORITY:
        return top, f"Návrhy u „{top.name}“: {n} z {total}."
    chains = [_ancestors(narrative) for narrative in votes]
    common = [node for node in chains[0] if all(node in chain for chain in chains[1:])]
    if common:
        node = common[0]
        names = ", ".join(f"{x.code} ({k})" for x, k in votes.most_common())
        return node, f"Ukazuje na více uzlů ({names}) — patří společnému nadřazenému uzlu."
    names = ", ".join(f"{x.code} ({k})" for x, k in votes.most_common())
    return None, f"Ukazuje na různé větve stromu bez společného uzlu: {names}."


def consider_trial(marker: NarrativeMarker) -> bool:
    """Достаточно предложений → испытание (или отклонение, если узел неоднозначен)."""
    if marker.n_proposals < int(get_setting("marker-min-documents")):
        return False
    if marker.n_authors < int(get_setting("marker-min-authors")):
        return False
    node, why = resolve_node(marker)
    if node is None:
        set_status(marker, MarkerStatus.REJECTED, why)
        return False
    if node.status == NarrativeStatus.ARCHIVED:
        set_status(marker, MarkerStatus.REJECTED, f"Narativ „{node.name}“ je v archivu.")
        return False
    reason = f"Navržen v {marker.n_proposals} dokumentech od {marker.n_authors} autorů. {why}"
    set_status(marker, MarkerStatus.TRIAL, reason, narrative=node)
    return True


# --- Решения по точности -----------------------------------------------------------


def _counts(decisions) -> tuple[int, int]:
    minimum = float(get_setting("labeling-min-confidence"))
    rows = list(decisions.values_list("present", "confidence"))
    confirmed = sum(1 for present, conf in rows if present and (conf or 0) >= minimum)
    return len(rows), confirmed


def evaluate(marker: NarrativeMarker) -> str:
    """Пересчёт точности; переход испытание → активен / отклонён, активен → приостановлен."""
    marker.refresh_from_db()
    decided = LabelDecision.objects.filter(
        markers__contains=[marker.pk],
        narrative=marker.narrative,
        present__isnull=False,
        error="",
    )
    marker.n_checks, marker.n_confirmed = _counts(decided)
    marker.precision = round(marker.n_confirmed / marker.n_checks, 3) if marker.n_checks else None
    marker.save(update_fields=["n_checks", "n_confirmed", "precision", "updated_at"])
    threshold = float(get_setting("marker-min-precision"))
    limit = int(get_setting("marker-trial-checks"))
    if marker.status == MarkerStatus.TRIAL:
        trial = decided.filter(created_at__gte=_trial_start(marker))
        n, ok = _counts(trial)
        if n < limit:
            return marker.status
        share = ok / n
        stats = {"checks": n, "confirmed": ok, "precision": round(share, 3)}
        if share >= threshold:
            set_status(marker, MarkerStatus.ACTIVE,
                       f"Zkouška: narativ potvrzen v {ok} z {n} dokumentů "
                       f"({share:.0%} ≥ {threshold:.0%}).", stats=stats)  # fmt: skip
        else:
            set_status(marker, MarkerStatus.REJECTED,
                       f"Zkouška: narativ potvrzen jen v {ok} z {n} dokumentů "
                       f"({share:.0%} < {threshold:.0%}).", stats=stats)  # fmt: skip
    elif marker.status == MarkerStatus.ACTIVE:
        recent_n = int(get_setting("marker-recent-checks"))
        ids = list(decided.order_by("-decided_at", "-pk").values_list("pk", flat=True)[:recent_n])
        n, ok = _counts(LabelDecision.objects.filter(pk__in=ids))
        if n >= limit and ok / n < threshold:
            stats = {"checks": n, "confirmed": ok, "precision": round(ok / n, 3)}
            set_status(marker, MarkerStatus.SUSPENDED,
                       f"Přesnost za posledních {n} kontrol klesla na {ok / n:.0%} "
                       f"(< {threshold:.0%}).", stats=stats)  # fmt: skip
    return marker.status


def after_decisions(decisions: list[LabelDecision], answers: list[dict]) -> None:
    """Po решениях модели по документу: новые предложения и пересчёт задействованных маркеров."""
    minimum = float(get_setting("labeling-min-confidence"))
    for decision, answer in zip(decisions, answers, strict=True):
        phrase = (answer or {}).get("marker")
        if (
            phrase
            and decision.present
            and decision.stance == Stance.PRO
            and (decision.confidence or 0) >= minimum
        ):
            record(decision, phrase)
    ids = {pk for decision in decisions for pk in decision.markers or []}
    for marker in NarrativeMarker.objects.filter(pk__in=ids, status__in=MATCHING):
        evaluate(marker)


# --- Статусы и действия исследователя ------------------------------------------------


def _snapshot(marker: NarrativeMarker) -> dict:
    return {
        "proposals": marker.n_proposals,
        "authors": marker.n_authors,
        "checks": marker.n_checks,
        "confirmed": marker.n_confirmed,
        "precision": marker.precision,
    }


def set_status(marker, status, reason, *, narrative=None, user=None, stats=None) -> MarkerEvent:
    before = marker.status
    with transaction.atomic():
        marker.status = status
        if narrative is not None:
            marker.narrative = narrative
        marker.status_changed_at = timezone.now()
        marker.save(update_fields=["status", "narrative", "status_changed_at", "updated_at"])
        return MarkerEvent.objects.create(
            marker=marker,
            status_before=before,
            status=status,
            narrative=marker.narrative,
            reason=reason[:2000],
            stats={**_snapshot(marker), **(stats or {})},
            user=user,
        )


def ban(marker: NarrativeMarker, user, reason: str = "") -> MarkerEvent:
    return set_status(marker, MarkerStatus.BANNED, reason or "Zakázán výzkumníkem.", user=user)


def retry(marker: NarrativeMarker, user, narrative: Narrative | None = None) -> MarkerEvent:
    """Znovu do zkoušky (případně u jiného uzlu): počítají se jen nové kontroly."""
    target = narrative or marker.narrative
    note = "Znovu do zkoušky (výzkumník)."
    if target.pk != marker.narrative_id:
        note = f"Přesunut k „{target.name}“ a znovu do zkoušky (výzkumník)."
    return set_status(marker, MarkerStatus.TRIAL, note, narrative=target, user=user)


def reattach(duplicate: Narrative, keeper: Narrative, *, user=None) -> int:
    """Sloučení narativů: markery dubletu přejdou k hlavnímu narativu (stav zůstává)."""
    moved = 0
    for marker in NarrativeMarker.objects.filter(narrative=duplicate):
        set_status(marker, marker.status, f"Narativ sloučen do „{keeper.name}“.",
                   narrative=keeper, user=user)  # fmt: skip
        moved += 1
    return moved


# --- Архив и сводка ------------------------------------------------------------------


def archive_pending():
    return NarrativeMarker.objects.filter(
        status=MarkerStatus.ACTIVE, archive_queued_at__isnull=True
    ).select_related("narrative")


def queue_archive(markers=None) -> dict:
    """Прогон релевантного архива по активным маркерам, по которым он ещё не проходился:
    найденные документы встают в очередь решений модели. Повтор безопасен."""
    from .embeddings import get_center
    from .labeling import load_prototypes, queue_candidates

    markers = list(archive_pending() if markers is None else markers)
    if not markers:
        return {"markers": 0, "queued": 0}
    narratives = list({m.narrative_id: m.narrative for m in markers}.values())
    prototypes = load_prototypes(narratives, marker_ids={m.pk for m in markers})
    center = get_center()
    queued = 0
    documents = Document.objects.filter(is_relevant=True, embedding__isnull=False)
    for document in documents.iterator(chunk_size=500):
        queued += queue_candidates(document, prototypes, center, markers_only=True)
    NarrativeMarker.objects.filter(pk__in=[m.pk for m in markers]).update(
        archive_queued_at=timezone.now()
    )
    return {"markers": len(markers), "queued": queued}


def notify_changes(since) -> int:
    """Souhrn automatických změn stavu do panelu (jen když se něco změnilo)."""
    from apps.panel.models import Notification

    events = list(
        MarkerEvent.objects.filter(created_at__gte=since, user__isnull=True)
        .exclude(status=F("status_before"))
        .select_related("marker", "narrative")
    )
    if not events:
        return 0
    groups = {}
    for event in events:
        groups.setdefault(event.status, []).append(event)
    parts = []
    for status in (MarkerStatus.ACTIVE, MarkerStatus.TRIAL, MarkerStatus.SUSPENDED,
                   MarkerStatus.REJECTED):  # fmt: skip
        items = groups.get(status, [])
        if not items:
            continue
        examples = ", ".join(f"„{e.marker.phrase}“ → {e.narrative.code}" for e in items[:6])
        more = f" a další ({len(items) - 6})" if len(items) > 6 else ""
        parts.append(f"{MarkerStatus(status).label}: {len(items)} — {examples}{more}.")
    pending = archive_pending().count()
    if pending:
        parts.append(f"Aktivních markerů bez průchodu archivem: {pending}.")
    Notification.objects.create(
        notification_type=Notification.Type.MARKER,
        title="Markery narativů: změny stavu",
        text=" ".join(parts)[:2000],
        url=reverse("panel:markers"),
    )
    return len(events)


def overview() -> dict:
    return dict(NarrativeMarker.objects.values_list("status").annotate(n=Count("id")).order_by())
