"""Данные карты дерева нарративов (рабочая часть, «Mapa narativů»).

Узел × неделя: документы с меткой узла или любого узла его поддерева, каждый документ
в узле один раз. Позиция документа в узле: одна позиция — она и есть; pro и proti внутри
ветки — neutrálně (обычно текст цитирует обе стороны); pro/proti вместе с neutrálně —
pro/proti. Ручная метка важнее модельной (как в агрегатах).

Документы — те же, что в агрегатах (`aggregation._documents`: период включения актора,
in_scope, порог качества), только слои анализа. Знаменатель недели — все такие документы.

Структура дерева — текущая версия кодбука, применённая ко всему периоду.
"""

import datetime
from collections import defaultdict

from apps.actors.models import ANALYSIS_LAYERS
from apps.narratives.models import Label, Narrative, NarrativeStatus

from .aggregation import _documents, monday

STANCES = ("pro", "contra", "neutral")


def _stance(found: set[str]) -> str:
    if "pro" in found and "contra" in found:
        return "neutral"
    for stance in ("pro", "contra"):
        if stance in found:
            return stance
    return "neutral"


def compute(min_quality: int, layers: list[str] | None = None) -> dict:
    layers = [x for x in (layers or ANALYSIS_LAYERS) if x in ANALYSIS_LAYERS]
    nodes = list(
        Narrative.objects.exclude(status=NarrativeStatus.ARCHIVED)
        .select_related("topic")
        .order_by("topic__name", "level", "name")
    )
    by_id = {n.pk: n for n in nodes}

    def ancestors(narrative: Narrative) -> list[int]:
        ids, current = [], narrative
        while current is not None and current.pk in by_id and current.pk not in ids:
            ids.append(current.pk)
            current = by_id.get(current.parent_id)
        return ids

    chain = {n.pk: ancestors(n) for n in nodes}

    documents = _documents().filter(actor__layer__in=layers)
    if min_quality > 1:
        documents = documents.filter(quality__gte=min_quality)
    week_of: dict[int, datetime.date] = {}
    totals: dict[datetime.date, int] = defaultdict(int)
    for pk, day in documents.values_list("pk", "day").iterator(chunk_size=5000):
        week = monday(day)
        week_of[pk] = week
        totals[week] += 1

    # (документ, узел) → позиции; сначала ручные метки, модельные — только где ручной нет
    human: dict[tuple[int, int], str] = {}
    model: dict[tuple[int, int], str] = {}
    labels = Label.objects.current().filter(narrative_id__in=by_id)
    for document_id, narrative_id, stance, origin in labels.values_list(
        "document_id", "narrative_id", "stance", "origin"
    ).iterator(chunk_size=5000):
        if document_id in week_of:
            target = human if origin == Label.Origin.HUMAN else model
            target[(document_id, narrative_id)] = stance
    leaf = {**model, **human}
    in_node: dict[tuple[int, int], set[str]] = defaultdict(set)
    for (document_id, narrative_id), stance in leaf.items():
        for node_id in chain[narrative_id]:
            in_node[(document_id, node_id)].add(stance)

    weeks = _weeks(min(totals), max(totals)) if totals else []
    index = {w: i for i, w in enumerate(weeks)}
    series = {
        n.code: {key: [0] * len(weeks) for key in ("n", *STANCES)}
        for n in nodes
    }  # fmt: skip
    for (document_id, node_id), found in in_node.items():
        row = series[by_id[node_id].code]
        i = index[week_of[document_id]]
        row["n"][i] += 1
        row[_stance(found)][i] += 1

    return {
        "weeks": [w.isoformat() for w in weeks],
        "total": [totals.get(w, 0) for w in weeks],
        "min_quality": min_quality,
        "layers": layers,
        "nodes": [
            {
                "code": n.code,
                "name": n.name,
                "definition": n.definition[:500],
                "level": n.level,
                "level_name": n.get_level_display(),
                "status": n.status,
                "status_name": n.get_status_display(),
                "topic": n.topic.code,
                "topic_name": n.topic.name,
                "parent": by_id[n.parent_id].code if n.parent_id in by_id else None,
            }
            for n in nodes
        ],
        "series": series,
    }


def _weeks(first: datetime.date, last: datetime.date) -> list[datetime.date]:
    weeks, week = [], first
    while week <= last:
        weeks.append(week)
        week += datetime.timedelta(days=7)
    return weeks
