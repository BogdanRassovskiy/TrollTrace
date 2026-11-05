"""JSON API для графиков (README §7.1): данные только из агрегатной таблицы, одним запросом.

/api/series/     — ряды акторов и групп по одному нарративу: недели + массивы по неделям
                   (n, n_total, доля, сглаженная доля, z). Компактно: 350 рядов × 26 недель.
/api/actor/      — ряды одного актора по всем активным нарративам (страница актора).
/api/documents/  — документы за точкой графика (актор или группа × неделя).
/api/narratives/ — активные нарративы для выбора.
"""

import datetime

from django.http import JsonResponse
from django.views import View

from apps.actors.models import Actor
from apps.core.roles import ViewerRequiredMixin
from apps.narratives.models import Label, Narrative, NarrativeStatus, Stance
from apps.panel.defaults import get_setting

from . import aggregation
from .models import WeeklyAggregate

STANCES = ("all", *Stance.values)
MAX_DOCUMENTS = 200


class BadRequest(Exception):
    pass


def _bad(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=400)


def _date(value: str | None, name: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest(f"Parametr „{name}“: datum ve tvaru RRRR-MM-DD.") from exc


def _common(request) -> tuple[Narrative, int, str]:
    code = request.GET.get("narrative")
    narrative = Narrative.objects.filter(code=code).first() if code else None
    if narrative is None:
        raise BadRequest("Neznámý nebo chybějící parametr „narrative“ (kód narativu).")
    try:
        quality = int(request.GET.get("min_quality") or get_setting("analysis-min-quality"))
    except ValueError as exc:
        raise BadRequest("Parametr „min_quality“ musí být číslo.") from exc
    if quality not in aggregation.thresholds():
        raise BadRequest(f"Agregáty existují jen pro práh kvality {aggregation.thresholds()}.")
    stance = request.GET.get("stance") or "all"
    if stance not in STANCES:
        raise BadRequest(f"Parametr „stance“: jedno z {', '.join(STANCES)}.")
    return narrative, quality, stance


STANCE_KEYS = ("pro", "contra", "neutral")


def _numerator(stance: str) -> str:
    return "n_documents" if stance == "all" else f"n_{stance}"


class SeriesView(ViewerRequiredMixin, View):
    """Parametry: narrative (povinný), min_quality, stance, layer (víckrát), party (víckrát),
    actor (ID, víckrát), groups (1 = i skupiny, only = jen skupiny, 0 = bez skupin),
    excluded (1 = i vyřazení aktéři), from, to."""

    def get(self, request):
        try:
            narrative, quality, stance = _common(request)
            start, end = _date(request.GET.get("from"), "from"), _date(request.GET.get("to"), "to")
        except BadRequest as exc:
            return _bad(str(exc))
        groups = request.GET.get("groups", "1")
        rows = WeeklyAggregate.objects.filter(narrative=narrative, min_quality=quality)
        if start:
            rows = rows.filter(week__gte=aggregation.monday(start))
        if end:
            rows = rows.filter(week__lte=end)

        actors = Actor.objects.all()
        if request.GET.get("excluded") != "1":
            actors = actors.filter(status=Actor.Status.ACTIVE)
        if layers := request.GET.getlist("layer"):
            actors = actors.filter(layer__in=layers)
        if parties := request.GET.getlist("party"):
            actors = actors.filter(party__in=parties)
        if ids := request.GET.getlist("actor"):
            actors = actors.filter(pk__in=[i for i in ids if i.isdigit()])
        meta = {a.pk: a for a in actors} if groups != "only" else {}
        group_rows = rows.filter(actor__isnull=True) if groups != "0" else rows.none()
        if layers or parties:
            wanted = {f"layer:{x}" for x in layers} | {f"party:{x}" for x in parties}
            group_rows = group_rows.filter(group_key__in=wanted)

        numerator = _numerator(stance)
        fields = ("actor_id", "group_key", "week", numerator, "n_total", "share_smoothed",
                  "z_score", "n_pro", "n_contra", "n_neutral")  # fmt: skip
        data = list(rows.filter(actor_id__in=meta).values_list(*fields)) + list(
            group_rows.values_list(*fields)
        )
        weeks = sorted({row[2] for row in data})
        index = {week: i for i, week in enumerate(weeks)}
        series: dict = {}
        for actor_id, group_key, week, n, total, smoothed, z, *by_stance in data:
            key = f"actor:{actor_id}" if actor_id else group_key
            item = series.get(key)
            if item is None:
                item = series[key] = self._series(key, meta.get(actor_id), len(weeks))
            i = index[week]
            item["n"][i], item["total"][i] = n, total
            # разбивка по позициям — для подсказки графика (всегда, при любом фильтре)
            for name, count in zip(STANCE_KEYS, by_stance, strict=True):
                item[name][i] = count
            item["share"][i] = round(n / total, 4) if total else None
            item["smoothed"][i] = (
                round(smoothed, 4) if stance == "all" and smoothed is not None else None
            )
            item["z"][i] = z if stance == "all" else None
        if stance != "all":  # z pro zvolený postoj se počítá na místě
            for item in series.values():
                pairs = zip(item["n"], item["total"], strict=True)
                points = [(weeks[i], n, t) for i, (n, t) in enumerate(pairs) if t]
                for (week, _, _), z in zip(points, aggregation.z_scores(points), strict=True):
                    item["z"][index[week]] = z
        ordered = sorted(series.values(), key=lambda s: (s["kind"] != "group", s["name"]))
        return JsonResponse(
            {
                "narrative": {
                    "code": narrative.code,
                    "name": narrative.name,
                    "status": narrative.status,
                },
                "min_quality": quality,
                "stance": stance,
                "weeks": [w.isoformat() for w in weeks],
                "series": ordered,
            }  # fmt: skip
        )

    @staticmethod
    def _series(key: str, actor: Actor | None, size: int) -> dict:
        if actor is not None:
            head = {"key": key, "kind": "actor", "id": actor.pk, "name": actor.name,
                    "layer": actor.layer, "party": actor.party, "status": actor.status}  # fmt: skip
        else:
            head = {"key": key, "kind": "group", "name": key}
        names = ("n", "total", "share", "smoothed", "z", *STANCE_KEYS)
        return head | {name: [None] * size for name in names}


class ActorSeriesView(ViewerRequiredMixin, View):
    """Ряды одного актора по всем активным нарративам. Parametry: actor, min_quality, stance."""

    def get(self, request):
        actor_id = request.GET.get("actor") or ""
        actor = Actor.objects.filter(pk=actor_id).first() if actor_id.isdigit() else None
        if actor is None:
            return _bad("Neznámý nebo chybějící parametr „actor“.")
        quality = request.GET.get("min_quality") or get_setting("analysis-min-quality")
        stance = request.GET.get("stance") or "all"
        if not str(quality).isdigit() or int(quality) not in aggregation.thresholds():
            return _bad(f"Agregáty existují jen pro práh kvality {aggregation.thresholds()}.")
        if stance not in STANCES:
            return _bad(f"Parametr „stance“: jedno z {', '.join(STANCES)}.")
        numerator = _numerator(stance)
        rows = list(
            WeeklyAggregate.objects.filter(
                actor=actor, min_quality=int(quality), narrative__status=NarrativeStatus.ACTIVE
            ).values_list(
                "narrative_id", "week", numerator, "n_total", "share_smoothed", "z_score"
            )  # fmt: skip
        )
        weeks = sorted({r[1] for r in rows})
        index = {week: i for i, week in enumerate(weeks)}
        narratives = {n.pk: n for n in Narrative.objects.filter(pk__in={r[0] for r in rows})}
        series: dict = {}
        for narrative_id, week, n, total, smoothed, z in rows:
            item = series.get(narrative_id)
            if item is None:
                narrative = narratives[narrative_id]
                item = series[narrative_id] = {
                    "code": narrative.code, "name": narrative.name,
                    **{k: [None] * len(weeks) for k in ("n", "total", "share", "smoothed", "z")},
                }  # fmt: skip
            i = index[week]
            item["n"][i], item["total"][i] = n, total
            item["share"][i] = round(n / total, 4) if total else None
            item["smoothed"][i] = round(smoothed, 4) if stance == "all" and smoothed else None
            item["z"][i] = z if stance == "all" else None
        if stance != "all":
            for item in series.values():
                pairs = zip(item["n"], item["total"], strict=True)
                points = [(weeks[i], n, t) for i, (n, t) in enumerate(pairs) if t]
                for (week, _, _), z in zip(points, aggregation.z_scores(points), strict=True):
                    item["z"][index[week]] = z
        return JsonResponse(
            {
                "actor": {"id": actor.pk, "name": actor.name, "layer": actor.layer},
                "min_quality": int(quality),
                "stance": stance,
                "spike_z": float(get_setting("spike-z-threshold")),
                "weeks": [w.isoformat() for w in weeks],
                "series": sorted(series.values(), key=lambda s: s["code"]),
            }
        )


class DocumentsView(ViewerRequiredMixin, View):
    """Dokumenty za bodem grafu. Parametry: narrative, week (pondělí nebo libovolný den
    týdne), actor (ID) nebo group (klíč skupiny), min_quality, stance."""

    def get(self, request):
        try:
            narrative, quality, stance = _common(request)
            day = _date(request.GET.get("week"), "week")
        except BadRequest as exc:
            return _bad(str(exc))
        if day is None:
            return _bad("Chybí parametr „week“.")
        week = aggregation.monday(day)
        actors = self._actors(request)
        if actors is None:
            return _bad("Zadejte „actor“ (ID aktéra) nebo „group“ (např. layer:D, party:ANO).")
        documents = aggregation._documents().filter(
            actor_id__in=actors, day__gte=week, day__lt=week + datetime.timedelta(days=7)
        )
        documents = documents.filter(quality__gte=quality)
        labels = Label.objects.current().filter(narrative=narrative, document__in=documents)
        if stance != "all":
            labels = labels.filter(stance=stance)
        found: dict = {}
        for label in labels.select_related("document__actor", "document__source").order_by(
            "document__published_at"
        ):
            if label.origin == Label.Origin.HUMAN or label.document_id not in found:
                found[label.document_id] = label
        items = [
            {
                "id": label.document_id,
                "title": label.document.title,
                "url": label.document.url,
                "published_at": label.document.published_at.isoformat()
                if label.document.published_at
                else None,
                "actor": label.document.actor.name,
                "source": label.document.source.name,
                "layer": label.document.actor.layer or label.document.source.layer,
                "stance": label.stance,
                "origin": label.origin,
                "confidence": label.confidence,
                "carrier": (label.details or {}).get("carrier", ""),
                "evidence": (label.details or {}).get("evidence", ""),
            }  # fmt: skip
            for label in list(found.values())[:MAX_DOCUMENTS]
        ]
        return JsonResponse({"week": week.isoformat(), "count": len(found), "documents": items})

    @staticmethod
    def _actors(request) -> list[int] | None:
        if (actor := request.GET.get("actor")) and actor.isdigit():
            return [int(actor)]
        group = request.GET.get("group") or ""
        actors = Actor.objects.all()
        if group == aggregation.ALL_GROUP:
            return list(actors.filter(layer__in=aggregation.ANALYSIS_LAYERS).values_list(
                "pk", flat=True))  # fmt: skip
        kind, _, value = group.partition(":")
        if kind == "layer" and value:
            return list(actors.filter(layer=value).values_list("pk", flat=True))
        if kind == "party" and value:
            return list(actors.filter(party=value, layer__in=aggregation.ANALYSIS_LAYERS)
                        .values_list("pk", flat=True))  # fmt: skip
        return None


class NarrativesView(ViewerRequiredMixin, View):
    def get(self, request):
        narratives = Narrative.objects.filter(status=NarrativeStatus.ACTIVE).select_related("topic")
        return JsonResponse(
            {
                "narratives": [
                    {
                        "code": n.code,
                        "name": n.name,
                        "kind": n.kind,
                        "topic": n.topic.name,
                        "version": n.current_version,
                    }  # fmt: skip
                    for n in narratives.order_by("code")
                ],
                "min_quality": int(get_setting("analysis-min-quality")),
                "thresholds": aggregation.thresholds(),
            }
        )
