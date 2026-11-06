"""Стратифицированный отбор выборки для ручной разметки (README §4, шаг 9).

Нарративы — страты по тому, что сделала автоматика с документом:
- `pozitivni` — модель нашла хотя бы один из нарративов выборки (проверка precision);
- `kandidat` — документ был кандидатом, модель сказала «нет» (ошибки модели);
- `relevantni` — релевантный документ, кандидатом не стал (ошибки отбора кандидатов);
- `ostatni` — нерелевантный документ (ошибки фильтра релевантности).
Популяция — документы, которые входят в агрегаты (период включения, качество ≥ порога).
Без случайных документов из нижних страт recall нельзя оценить: позитивы, пропущенные
отбором кандидатов, не попали бы в выборку никогда.

Заимствования — найденные дословные связи, найденные парафразы и «почти пары»: ближайший
по эмбеддингу документ слоя-источника без связи (оценка пропусков у порога).

Отбор воспроизводим: фиксированное зерно, список id сортируется перед выборкой.
"""

import random

from django.db import connection, transaction
from pgvector.django import CosineDistance

from apps.analytics.aggregation import _documents
from apps.documents.models import Document, ReuseLink
from apps.narratives.models import LabelDecision
from apps.panel.defaults import get_setting

from .models import Sample, SampleItem

NARRATIVE_STRATA = {"pozitivni": 0.4, "kandidat": 0.3, "relevantni": 0.2, "ostatni": 0.1}
REUSE_STRATA = {"doslovne": 0.4, "parafraze": 0.3, "blizke": 0.3}
STRATUM_LABELS = {
    "pozitivni": "model narativ našel",
    "kandidat": "kandidát, model narativ nenašel",
    "relevantni": "relevantní, nebyl kandidátem",
    "ostatni": "nerelevantní",
    "doslovne": "nalezené doslovné převzetí",
    "parafraze": "nalezená parafráze",
    "blizke": "blízká dvojice bez nalezené vazby",
}
NEAR_ATTEMPTS = 4  # сколько документов-целей пробуем на одну «почти пару»


def allocate(sizes: dict, total: int, shares: dict) -> dict:
    """Размер выборки по стратам: по долям, недобор маленьких страт отдаётся остальным."""
    allocation = {name: 0 for name in shares}
    remaining = total
    open_strata = {name for name in shares if sizes.get(name, 0) > 0}
    while remaining > 0 and open_strata:
        share_sum = sum(shares[name] for name in open_strata)
        given = 0
        for name in sorted(open_strata):
            want = max(1, round(remaining * shares[name] / share_sum))
            take = min(want, sizes[name] - allocation[name], remaining - given)
            allocation[name] += take
            given += take
        remaining -= given
        open_strata = {n for n in open_strata if allocation[n] < sizes[n]}
        if given == 0:
            break
    return allocation


def _eligible_ids() -> set[int]:
    threshold = int(get_setting("analysis-min-quality"))
    documents = _documents().exclude(text="")
    if threshold > 1:
        documents = documents.filter(quality__gte=threshold)
    return set(documents.values_list("pk", flat=True))


def narrative_strata(version_ids: list[int]) -> dict[str, list[int]]:
    eligible = _eligible_ids()
    decisions = LabelDecision.objects.filter(
        narrative_version_id__in=version_ids, document_id__in=eligible, error=""
    )
    positive = set(decisions.filter(present=True).values_list("document_id", flat=True))
    candidate = set(decisions.values_list("document_id", flat=True)) - positive
    relevant = (
        set(Document.objects.filter(pk__in=eligible, is_relevant=True).values_list("pk", flat=True))
        - positive
        - candidate
    )
    other = eligible - positive - candidate - relevant
    return {
        "pozitivni": sorted(positive),
        "kandidat": sorted(candidate),
        "relevantni": sorted(relevant),
        "ostatni": sorted(other),
    }


def _draw(strata: dict, total: int, shares: dict, rng: random.Random) -> list[tuple]:
    """[(stratum, id, weight)] по стратам; вес = размер страты / число отобранных."""
    sizes = {name: len(ids) for name, ids in strata.items()}
    allocation = allocate(sizes, total, shares)
    drawn = []
    for name in shares:
        n = allocation.get(name, 0)
        if n:
            weight = sizes[name] / n
            drawn += [(name, pk, weight) for pk in rng.sample(strata[name], n)]
    return drawn


@transaction.atomic
def create_narrative_sample(
    *, name, versions, size, seed, user=None, hide_source=True, shares=None
) -> Sample:
    shares = shares or NARRATIVE_STRATA
    version_ids = [v.pk for v in versions]
    strata = narrative_strata(version_ids)
    rng = random.Random(seed)
    drawn = _draw(strata, size, shares, rng)
    rng.shuffle(drawn)  # страты перемешаны: разметчик не видит, откуда документ
    sample = Sample.objects.create(
        name=name,
        kind=Sample.Kind.NARRATIVES,
        hide_source=hide_source,
        created_by=user,
        params={
            "seed": seed,
            "size": size,
            "shares": shares,
            "population": {k: len(v) for k, v in strata.items()},
            "drawn": {k: sum(1 for s, *_ in drawn if s == k) for k in shares},
            "analysis_min_quality": get_setting("analysis-min-quality"),
            "relevance_threshold": get_setting("relevance-threshold"),
            "relevance_all_layers": get_setting("relevance-all-layers"),
        },
    )
    sample.narrative_versions.set(version_ids)
    SampleItem.objects.bulk_create(
        SampleItem(sample=sample, position=i + 1, stratum=stratum, weight=weight, document_id=pk)
        for i, (stratum, pk, weight) in enumerate(drawn)
    )
    return sample


# --- заимствования -------------------------------------------------------------------------


def _near_pairs(n: int, rng: random.Random, exclude: set) -> list[tuple]:
    """«Почти пары»: для случайных документов вне слоёв-источников — ближайший документ
    слоя-источника в окне, если связи нет. [(source_id, target_id, centered_similarity)]."""
    from apps.processing.embeddings import centered, get_center
    from apps.processing.reuse import counterparts, source_layers

    center = get_center()
    if center is None or n <= 0:
        return []
    layers = source_layers()
    targets = sorted(
        Document.objects.filter(embedding__isnull=False, pk__in=_eligible_ids())
        .exclude(source__layer__in=layers)
        .values_list("pk", flat=True)
    )
    linked = set(ReuseLink.objects.values_list("source_document_id", "target_document_id"))
    pairs = []
    for pk in rng.sample(targets, min(len(targets), n * NEAR_ATTEMPTS)):
        if len(pairs) >= n:
            break
        document = Document.objects.select_related("source").get(pk=pk)
        queryset, is_source = counterparts(document, layers)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        nearest = (
            queryset.filter(embedding__isnull=False)
            .order_by(CosineDistance("embedding", document.embedding))
            .only("id", "embedding")
            .first()
        )
        if nearest is None:
            continue
        source, target = (document.pk, nearest.pk) if is_source else (nearest.pk, document.pk)
        if (source, target) in linked or (source, target) in exclude:
            continue
        vectors = centered([document.embedding, nearest.embedding], center)
        pairs.append((source, target, round(float(vectors[0] @ vectors[1]), 4)))
    return pairs


@transaction.atomic
def create_reuse_sample(*, name, size, seed, user=None, hide_source=True, shares=None) -> Sample:
    shares = shares or REUSE_STRATA
    rng = random.Random(seed)
    links = ReuseLink.objects.exclude(method=ReuseLink.Method.MANUAL).order_by("pk")
    strata = {
        "doslovne": list(
            links.filter(link_type=ReuseLink.Type.VERBATIM).values_list(
                "source_document_id", "target_document_id", "similarity"
            )
        ),
        "parafraze": list(
            links.filter(link_type=ReuseLink.Type.PARAPHRASE).values_list(
                "source_document_id", "target_document_id", "similarity"
            )
        ),
    }
    allocation = allocate({**{k: len(v) for k, v in strata.items()}, "blizke": size}, size, shares)
    drawn = []
    for name_ in ("doslovne", "parafraze"):
        n = allocation[name_]
        if n:
            weight = len(strata[name_]) / n
            drawn += [(name_, pair, weight) for pair in rng.sample(strata[name_], n)]
    taken = {(s, t) for _n, (s, t, _sim), _w in drawn}
    # у «почти пар» популяция не определена — вес не задаётся, метрики по ним без весов
    drawn += [("blizke", pair, None) for pair in _near_pairs(allocation["blizke"], rng, taken)]
    rng.shuffle(drawn)
    sample = Sample.objects.create(
        name=name,
        kind=Sample.Kind.REUSE,
        hide_source=hide_source,
        created_by=user,
        params={
            "seed": seed,
            "size": size,
            "shares": shares,
            "population": {k: len(v) for k, v in strata.items()},
            "drawn": {k: sum(1 for s, *_ in drawn if s == k) for k in shares},
            "reuse_verbatim_jaccard": get_setting("reuse-verbatim-jaccard"),
            "reuse_paraphrase_cosine": get_setting("reuse-paraphrase-cosine"),
        },
    )
    SampleItem.objects.bulk_create(
        SampleItem(
            sample=sample,
            position=i + 1,
            stratum=stratum,
            weight=weight,
            source_document_id=source,
            target_document_id=target,
            similarity=similarity,
        )
        for i, (stratum, (source, target, similarity), weight) in enumerate(drawn)
    )
    return sample
