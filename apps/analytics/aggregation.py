"""Расчёт агрегатов «актор/группа × нарратив × неделя» (README §4 шаг 8, §5).

Знаменатель: все документы актора за неделю (и нерелевантные — они и есть «всё, что
актор сказал»), кроме:
- документов вне периода включения актора (исключение актора не меняет прошлые недели, а
  документы до включения в выборку не попадают в знаменатель);
- материалов медиа вне сохраняемых рубрик (`in_scope = False`, README §5);
- документов с оценкой качества ниже порога. Считается для трёх порогов: основной
  (`analysis-min-quality`) и проверки устойчивости — все документы (1) и только ≥ 4.
Документы, удалённые у источника, считаются: публикация состоялась.

Числитель: документы с действующей меткой активного нарратива (Label.objects.current()),
ручная метка важнее модельной. Хранятся число в любой позиции и отдельно pro / proti /
neutrálně; основная доля — нарратив присутствует в любой позиции.

Группы: слой (`layer:D`), партия (`party:ANO`, только слои анализа) и все слои анализа
(`all`, без слоя I). Доля группы = сумма числителей / сумма знаменателей её акторов.

z-оценка — относительно собственной истории ряда: среднее и разброс доли за предыдущие
`aggregation-z-window` недель, где актор что-то публиковал (не меньше
`aggregation-z-min-weeks`). Разброс снизу ограничен биномиальной погрешностью доли, иначе
первое упоминание после серии нулей давало бы бесконечность.

Сглаживание: (n + α·m) / (N + α), m — доля слоя актора в ту же неделю, α —
`aggregation-prior-strength` (README §5: для редко пишущих).

Пересчёт инкрементальный: затронутые пары «актор × неделя» (новые документы, решения
модели, метки, оценки качества с прошлого запуска); полный — раз в сутки и при смене
набора активных нарративов.
"""

import datetime
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.actors.models import ANALYSIS_LAYERS, Actor, ActorInclusionPeriod
from apps.documents.models import Document, DocumentQuality
from apps.narratives.models import Label, LabelDecision, Narrative, NarrativeStatus, Stance
from apps.narratives.tree import branch_stance, chains
from apps.panel.defaults import get_setting

from .models import WeeklyAggregate

logger = logging.getLogger(__name__)

ROBUSTNESS_THRESHOLDS = (1, 4)
MAX_INCREMENTAL_PAIRS = 3000
ALL_GROUP = "all"


def thresholds() -> list[int]:
    return sorted({int(get_setting("analysis-min-quality")), *ROBUSTNESS_THRESHOLDS})


def monday(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=day.weekday())


def _documents():
    """Документы, входящие в агрегаты, с днём публикации и оценкой качества."""
    day = TruncDate(Coalesce("published_at", "collected_at"), tzinfo=ZoneInfo(settings.TIME_ZONE))
    inside = ActorInclusionPeriod.objects.filter(
        actor=OuterRef("actor_id"), start_date__lte=OuterRef("day")
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=OuterRef("day")))
    quality = DocumentQuality.objects.filter(document=OuterRef("pk"), is_current=True)
    return (
        Document.objects.filter(actor__isnull=False)
        .exclude(in_scope=False)
        .annotate(day=day, quality=Subquery(quality.values("score")[:1]))
        .filter(Exists(inside))
    )


@dataclass
class Bucket:
    """Документы одной пары «актор × неделя»: [(id, качество, engagement)]."""

    documents: list = field(default_factory=list)


def _collect(pairs: set | None) -> dict:
    """{(actor_id, week): Bucket}; pairs=None — все документы."""
    documents = _documents()
    if pairs is not None:
        if not pairs:
            return {}
        actors = {a for a, _ in pairs}
        weeks = [w for _, w in pairs]
        documents = documents.filter(
            actor_id__in=actors,
            day__gte=min(weeks),
            day__lt=max(weeks) + datetime.timedelta(days=7),
        )
    buckets: dict = defaultdict(Bucket)
    rows = documents.values_list("pk", "actor_id", "day", "quality", "engagement")
    for pk, actor_id, day, quality, engagement in rows.iterator(chunk_size=5000):
        key = (actor_id, monday(day))
        if pairs is None or key in pairs:
            buckets[key].documents.append((pk, quality or 1, engagement or {}))
    return buckets


def _labels(document_ids: list[int], narratives: list[Narrative]) -> dict:
    """{(document_id, narrative_id): stance}; ruční štítek má přednost před modelem.

    Strom kódové knihy: štítek uzlu se započítá i všem jeho aktivním předkům, dokument
    v uzlu jednou; pro i proti v jedné větvi = neutrálně (tree.branch_stance).
    """
    found: dict = {}
    ids = [n.pk for n in narratives]
    for start in range(0, len(document_ids), 5000):
        chunk = document_ids[start : start + 5000]
        labels = Label.objects.current().filter(document_id__in=chunk, narrative_id__in=ids)
        for document_id, narrative_id, stance, origin in labels.values_list(
            "document_id", "narrative_id", "stance", "origin"
        ):
            key = (document_id, narrative_id)
            if origin == Label.Origin.HUMAN or key not in found:
                found[key] = stance
    chain = chains(narratives)
    branch: dict = defaultdict(set)
    for (document_id, narrative_id), stance in found.items():
        for node_id in chain.get(narrative_id, [narrative_id]):
            branch[(document_id, node_id)].add(stance)
    return {key: branch_stance(stances) for key, stances in branch.items()}


def _engagement(items) -> dict:
    total: dict = defaultdict(float)
    for engagement in items:
        for key, value in engagement.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                total[key] += value
    return {k: (int(v) if float(v).is_integer() else v) for k, v in total.items()}


def _actor_rows(buckets: dict, narratives: list[Narrative]) -> list[WeeklyAggregate]:
    ids = [pk for bucket in buckets.values() for pk, _, _ in bucket.documents]
    labels = _labels(ids, narratives)
    rows = []
    for (actor_id, week), bucket in buckets.items():
        for threshold in thresholds():
            documents = [d for d in bucket.documents if d[1] >= threshold]
            if not documents:
                continue
            for narrative in narratives:
                stances = [
                    (labels[(pk, narrative.pk)], engagement)
                    for pk, _, engagement in documents
                    if (pk, narrative.pk) in labels
                ]
                count = len(stances)
                rows.append(
                    WeeklyAggregate(
                        actor_id=actor_id,
                        narrative=narrative,
                        week=week,
                        min_quality=threshold,
                        n_documents=count,
                        n_pro=sum(1 for s, _ in stances if s == Stance.PRO),
                        n_contra=sum(1 for s, _ in stances if s == Stance.CONTRA),
                        n_neutral=sum(1 for s, _ in stances if s == Stance.NEUTRAL),
                        n_total=len(documents),
                        share=count / len(documents),
                        engagement=_engagement(e for _, e in stances),
                    )
                )
    return rows


def _group_keys(actor: Actor) -> list[str]:
    keys = [f"layer:{actor.layer}"] if actor.layer else []
    if actor.layer in ANALYSIS_LAYERS:
        keys.append(ALL_GROUP)
        if actor.party:
            keys.append(f"party:{actor.party}")
    return keys


def _group_rows(weeks: set) -> list[WeeklyAggregate]:
    """Группы за недели — из строк акторов (уже пересчитанных)."""
    actors = {a.pk: a for a in Actor.objects.all()}
    sums: dict = defaultdict(lambda: [0, 0, 0, 0, 0, defaultdict(float)])
    fields = ("actor_id", "narrative_id", "week", "min_quality", "n_documents", "n_pro",
              "n_contra", "n_neutral", "n_total", "engagement")  # fmt: skip
    rows = WeeklyAggregate.objects.filter(actor__isnull=False, week__in=weeks).values_list(*fields)
    for actor_id, narrative_id, week, quality, n, pro, contra, neutral, total, engagement in rows:
        for key in _group_keys(actors[actor_id]):
            acc = sums[(key, narrative_id, week, quality)]
            for i, value in enumerate((n, pro, contra, neutral, total)):
                acc[i] += value
            for k, v in (engagement or {}).items():
                acc[5][k] += v
    return [
        WeeklyAggregate(
            group_key=key, narrative_id=narrative_id, week=week, min_quality=quality,
            n_documents=n, n_pro=pro, n_contra=contra, n_neutral=neutral, n_total=total,
            share=n / total, share_smoothed=n / total, engagement=_engagement([engagement]),
        )
        for (key, narrative_id, week, quality), (n, pro, contra, neutral, total, engagement)
        in sums.items()
        if total
    ]  # fmt: skip


# --- z-оценка и сглаживание --------------------------------------------------------


def z_scores(points: list[tuple]) -> list[float | None]:
    """points: [(week, n, total)] по возрастанию недель → z относительно истории ряда."""
    window = int(get_setting("aggregation-z-window"))
    minimum = int(get_setting("aggregation-z-min-weeks"))
    result = []
    for i, (week, n, total) in enumerate(points):
        start = week - datetime.timedelta(weeks=window)
        history = [(hn, ht) for hw, hn, ht in points[:i] if hw >= start]
        if len(history) < minimum or not total:
            result.append(None)
            continue
        shares = [hn / ht for hn, ht in history]
        mean = sum(shares) / len(shares)
        sd = math.sqrt(sum((s - mean) ** 2 for s in shares) / (len(shares) - 1))
        pooled = (sum(hn for hn, _ in history) + 0.5) / (sum(ht for _, ht in history) + 1)
        sd = max(sd, math.sqrt(pooled * (1 - pooled) / total))
        result.append(round((n / total - mean) / sd, 3))
    return result


def _fill_z(queryset) -> int:
    series: dict = defaultdict(list)
    for row in queryset.only("pk", "actor_id", "group_key", "narrative_id", "min_quality",
                             "week", "n_documents", "n_total", "z_score"):  # fmt: skip
        series[(row.actor_id, row.group_key, row.narrative_id, row.min_quality)].append(row)
    changed = []
    for rows in series.values():
        rows.sort(key=lambda r: r.week)
        for row, z in zip(rows, z_scores([(r.week, r.n_documents, r.n_total) for r in rows]),
                          strict=True):  # fmt: skip
            if row.z_score != z:
                row.z_score = z
                changed.append(row)
    WeeklyAggregate.objects.bulk_update(changed, ["z_score"], batch_size=5000)
    return len(changed)


def _fill_smoothed(weeks: set) -> None:
    alpha = float(get_setting("aggregation-prior-strength"))
    layers = dict(Actor.objects.values_list("pk", "layer"))
    groups = {
        (key, narrative_id, week, quality): share
        for key, narrative_id, week, quality, share in WeeklyAggregate.objects.filter(
            actor__isnull=True, week__in=weeks
        ).values_list("group_key", "narrative_id", "week", "min_quality", "share")
    }
    changed = []
    for row in WeeklyAggregate.objects.filter(actor__isnull=False, week__in=weeks).only(
        "pk", "actor_id", "narrative_id", "week", "min_quality", "n_documents", "n_total",
        "share", "share_smoothed",
    ):  # fmt: skip
        prior = groups.get((f"layer:{layers.get(row.actor_id)}", row.narrative_id, row.week,
                            row.min_quality), row.share)  # fmt: skip
        value = round((row.n_documents + alpha * prior) / (row.n_total + alpha), 6)
        if row.share_smoothed != value:
            row.share_smoothed = value
            changed.append(row)
    WeeklyAggregate.objects.bulk_update(changed, ["share_smoothed"], batch_size=5000)


# --- Запуск ------------------------------------------------------------------------


@dataclass
class AggregateStats:
    full: bool = False
    pairs: int = 0
    actor_rows: int = 0
    group_rows: int = 0
    z_updated: int = 0

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def active_narratives() -> list[Narrative]:
    return list(Narrative.objects.filter(status=NarrativeStatus.ACTIVE).order_by("pk"))


TREE_KEY = "analytics:aggregates-tree"


def tree_signature(narratives: list[Narrative]) -> str:
    return ",".join(f"{n.pk}:{n.parent_id or ''}" for n in sorted(narratives, key=lambda n: n.pk))


def needs_full(narratives: list[Narrative]) -> bool:
    """Набор активных нарративов или дерево изменились (принят кандидат, архив, перенос узла:
    суммы ветвей считаются заново). Подпись дерева — в кэше; нет её — полный пересчёт."""
    stored = set(WeeklyAggregate.objects.values_list("narrative_id", flat=True).distinct())
    if stored != {n.pk for n in narratives}:
        return True
    return cache.get(TREE_KEY) != tree_signature(narratives)


def affected_pairs(since: datetime.datetime) -> set:
    """Пары «актор × неделя», где с `since` что-то изменилось."""
    changed = (
        Q(processed_at__gte=since) | Q(collected_at__gte=since) | Q(last_checked_at__gte=since)
    )
    changed |= Q(pk__in=LabelDecision.objects.filter(decided_at__gte=since).values("document_id"))
    changed |= Q(pk__in=Label.objects.filter(created_at__gte=since).values("document_id"))
    changed |= Q(
        pk__in=DocumentQuality.objects.filter(computed_at__gte=since).values("document_id")
    )
    day = TruncDate(Coalesce("published_at", "collected_at"), tzinfo=ZoneInfo(settings.TIME_ZONE))
    rows = (
        Document.objects.filter(changed, actor__isnull=False)
        .annotate(day=day)
        .values_list("actor_id", "day")
        .distinct()
    )
    return {(actor_id, monday(day)) for actor_id, day in rows}


def update(since: datetime.datetime | None = None, *, full: bool = False) -> AggregateStats:
    """Инкрементальный пересчёт с момента `since`; без него или при full — полный."""
    narratives = active_narratives()
    full = full or since is None or needs_full(narratives)
    stats = AggregateStats(full=full)
    pairs = None if full else affected_pairs(since)
    if pairs is not None and len(pairs) > MAX_INCREMENTAL_PAIRS:
        full, pairs, stats.full = True, None, True  # velká změna: levnější přepočítat vše
    if pairs is not None and not pairs:
        return stats
    buckets = _collect(pairs)
    rows = _actor_rows(buckets, narratives)
    with transaction.atomic():
        if full:
            WeeklyAggregate.objects.all().delete()
            weeks = {w for _, w in buckets}
        else:
            stats.pairs = len(pairs)
            weeks = {w for _, w in pairs}
            stale = Q()
            for actor_id, week in pairs:
                stale |= Q(actor_id=actor_id, week=week)
            WeeklyAggregate.objects.filter(stale).delete()
            WeeklyAggregate.objects.filter(actor__isnull=True, week__in=weeks).delete()
        WeeklyAggregate.objects.bulk_create(rows, batch_size=5000)
        groups = _group_rows(weeks)
        WeeklyAggregate.objects.bulk_create(groups, batch_size=5000)
        stats.actor_rows, stats.group_rows = len(rows), len(groups)
        if full:
            series = WeeklyAggregate.objects.all()
        else:
            actors = {a for a, _ in pairs}
            series = WeeklyAggregate.objects.filter(Q(actor_id__in=actors) | Q(actor__isnull=True))
        stats.z_updated = _fill_z(series)
        _fill_smoothed(weeks)
    if full:
        stats.pairs = len(buckets)
        cache.set(TREE_KEY, tree_signature(narratives), None)
    return stats


WATERMARK_KEY = "analytics:aggregates-since"


def update_incremental() -> AggregateStats:
    """Для beat: пересчёт с прошлого запуска (метка в кэше; нет метки — полный)."""
    from django.core.cache import cache

    started = timezone.now()
    since = cache.get(WATERMARK_KEY)
    stats = update(datetime.datetime.fromisoformat(since) if since else None)
    # С запасом: документы, обработанные во время пересчёта, попадут в следующий.
    cache.set(WATERMARK_KEY, (started - datetime.timedelta(minutes=5)).isoformat(), None)
    return stats
