"""Рабочая часть (README §7.1): акторы, сравнение, карточка нарратива, заимствования.

Графики строятся только из агрегатной таблицы (через /api/…); страницы отдают каркас,
фильтры и метаданные. Карточка нарратива и просмотр заимствований — по документам и меткам.
"""

import datetime

from django.db.models import Count, Max
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views.generic import DetailView, ListView, TemplateView

from apps.actors.models import Actor, Layer
from apps.analytics import aggregation
from apps.analytics.models import WeeklyAggregate
from apps.core.roles import ViewerRequiredMixin
from apps.documents.models import ReuseLink
from apps.narratives.models import Label, Narrative, NarrativeStatus, Stance
from apps.panel.defaults import get_setting

from .reuse import highlight

SPIKE_WEEKS = 2  # výkyv „teď“ = v posledních dvou týdnech s daty


def _active_narratives():
    return Narrative.objects.filter(status=NarrativeStatus.ACTIVE).order_by("code")


class DashboardIndexView(ViewerRequiredMixin, ListView):
    """Seznam aktérů s filtry (README §7.1): vrstva, strana, role, kohorta, hybridnost, jméno."""

    template_name = "dashboard/index.html"
    context_object_name = "actors"
    paginate_by = 100

    def get_queryset(self):
        g = self.request.GET
        actors = Actor.objects.all()
        if g.get("excluded") != "1":
            actors = actors.filter(status=Actor.Status.ACTIVE)
        if g.get("layer"):
            actors = actors.filter(layer=g["layer"])
        if g.get("party"):
            actors = actors.filter(party=g["party"])
        if g.get("cohort"):
            actors = actors.filter(activity_cohort=g["cohort"])
        if g.get("hybrid") in ("0", "1"):
            actors = actors.filter(is_hybrid=g["hybrid"] == "1")
        if g.get("role"):
            actors = actors.filter(role__icontains=g["role"])
        if g.get("q"):
            actors = actors.filter(name__icontains=g["q"])
        return actors.annotate(n_documents=Count("documents")).order_by("layer", "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        spikes = self._spikes([a.pk for a in context["actors"]])
        for actor in context["actors"]:
            actor.spikes = spikes.get(actor.pk, [])
        query = self.request.GET.copy()
        query.pop("page", None)
        context |= {
            "layers": Layer.choices,
            "parties": Actor.objects.exclude(party="").values_list("party", flat=True)
            .distinct().order_by("party"),
            "cohorts": Actor.ActivityCohort.choices,
            "narratives": _active_narratives(),
            "filters": self.request.GET,
            "query": query.urlencode(),
        }  # fmt: skip
        return context

    @staticmethod
    def _spikes(actor_ids: list[int]) -> dict:
        """{actor_id: [kódy narativů]} s výkyvem v posledních týdnech s daty."""
        last = WeeklyAggregate.objects.aggregate(last=Max("week"))["last"]
        if last is None:
            return {}
        threshold = float(get_setting("spike-z-threshold"))
        rows = WeeklyAggregate.objects.filter(
            actor_id__in=actor_ids,
            min_quality=int(get_setting("analysis-min-quality")),
            narrative__status=NarrativeStatus.ACTIVE,
            week__gt=last - datetime.timedelta(weeks=SPIKE_WEEKS),
            z_score__gte=threshold,
            n_documents__gt=0,
        ).values_list("actor_id", "narrative__name")
        spikes: dict = {}
        for actor_id, name in rows:
            spikes.setdefault(actor_id, set()).add(name)
        return {k: sorted(v) for k, v in spikes.items()}


class ActorDetailView(ViewerRequiredMixin, DetailView):
    model = Actor
    template_name = "dashboard/actor.html"
    context_object_name = "actor"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        actor = self.object
        context |= {
            "positions": actor.positions.order_by("-start_date"),
            "periods": actor.inclusion_periods.order_by("start_date"),
            "n_documents": actor.documents.count(),
            "chart_config": {
                "actor": actor.pk,
                "thresholds": aggregation.thresholds(),
                "min_quality": int(get_setting("analysis-min-quality")),
            },
        }
        return context


class CompareView(ViewerRequiredMixin, TemplateView):
    """Режим сравнения: все акторы по одному нарративу, линии / тепловая карта."""

    template_name = "dashboard/compare.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        narratives = list(_active_narratives())
        code = self.request.GET.get("narrative") or (narratives[0].code if narratives else "")
        context |= {
            "narratives": narratives,
            "chart_config": {
                "narrative": code,
                "thresholds": aggregation.thresholds(),
                "min_quality": int(get_setting("analysis-min-quality")),
                "click_max_lines": int(get_setting("chart-click-max-lines")),
                "layers": dict(Layer.choices),
                "parties": list(
                    Actor.objects.exclude(party="")
                    .values_list("party", flat=True)
                    .distinct()
                    .order_by("party")
                ),  # fmt: skip
            },
        }
        return context


class NarrativeListView(ViewerRequiredMixin, ListView):
    template_name = "dashboard/narratives.html"
    context_object_name = "narratives"

    def get_queryset(self):
        return (
            Narrative.objects.filter(status__in=[NarrativeStatus.ACTIVE, NarrativeStatus.CANDIDATE])
            .select_related("topic")
            .annotate(n_labels=Count("labels"))
            .order_by("status", "code")
        )


def lifecycle(narrative: Narrative) -> dict:
    """Первая фиксация, слои по порядку появления, пик (README §3, метрики жизненного цикла).

    По тем же документам, что и агрегаты (периоды включения, рубрики, основной порог
    качества), с действующими метками в любой позиции.
    """
    quality = int(get_setting("analysis-min-quality"))
    documents = aggregation._documents().filter(quality__gte=quality)
    labels = (
        Label.objects.current()
        .filter(narrative=narrative, document__in=documents)
        .select_related("document__actor", "document__source")
    )
    first = None
    layers: dict = {}
    carriers: dict = {}
    for label in labels:
        document = label.document
        moment = document.published_at or document.collected_at
        if first is None or moment < first["moment"]:
            first = {"moment": moment, "document": document, "stance": label.get_stance_display(),
                     "layer": document.actor.layer or document.source.layer}  # fmt: skip
        layer = document.actor.layer or document.source.layer  # jako ve skupinách agregátů
        if layer not in layers or moment < layers[layer]["moment"]:
            layers[layer] = {"moment": moment, "document": document}
        if label.stance == Stance.PRO:
            carriers.setdefault(document.actor, moment)
            carriers[document.actor] = min(carriers[document.actor], moment)
    names = dict(Layer.choices)
    order = [
        {"layer": names.get(code, code), "code": code, **value}
        for code, value in sorted(layers.items(), key=lambda item: item[1]["moment"])
    ]
    peak = (
        WeeklyAggregate.objects.filter(
            narrative=narrative, group_key=aggregation.ALL_GROUP, min_quality=quality,
            n_documents__gt=0,
        )
        .order_by("-share", "week")
        .first()
    )  # fmt: skip
    pickup = sorted(carriers.items(), key=lambda item: item[1])[:10]
    return {"first": first, "layers": order, "peak": peak, "pickup": pickup}


class NarrativeDetailView(ViewerRequiredMixin, DetailView):
    model = Narrative
    slug_field = "code"
    slug_url_kwarg = "code"
    template_name = "dashboard/narrative.html"
    context_object_name = "narrative"

    def get_object(self, queryset=None):
        narrative = super().get_object(queryset)
        if narrative.status == NarrativeStatus.ARCHIVED:
            raise Http404
        return narrative

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        narrative = self.object
        guide = narrative.stance_guide or {}
        context |= {
            "version": narrative.latest_version,
            "debunks": narrative.debunks.order_by("-published_at"),
            "life": lifecycle(narrative),
            "guide": [
                (label, guide[key])
                for key, label in (
                    ("pro", "Postoj pro"),
                    ("contra", "Postoj proti"),
                    ("neutral", "Neutrálně"),
                    ("absent", "Není narativ"),
                )
                if guide.get(key)
            ],  # fmt: skip
            "chart_config": {
                "narrative": narrative.code,
                "min_quality": int(get_setting("analysis-min-quality")),
            },
        }
        return context


class ReuseListView(ViewerRequiredMixin, ListView):
    template_name = "dashboard/reuse_list.html"
    context_object_name = "links"
    paginate_by = 50

    def get_queryset(self):
        g = self.request.GET
        links = ReuseLink.objects.select_related(
            "source_document__actor", "source_document__source",
            "target_document__actor", "target_document__source",
        ).order_by("-similarity")  # fmt: skip
        if g.get("type"):
            links = links.filter(link_type=g["type"])
        if g.get("layer"):
            links = links.filter(target_document__source__layer=g["layer"])
        if g.get("confirmed") in ("0", "1", "none"):
            value = {"0": False, "1": True, "none": None}[g["confirmed"]]
            links = links.filter(is_confirmed=value)
        return links

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.copy()
        query.pop("page", None)
        context |= {"types": ReuseLink.Type.choices, "layers": Layer.choices,
                    "filters": self.request.GET, "query": query.urlencode()}  # fmt: skip
        return context


class ReuseDetailView(ViewerRequiredMixin, TemplateView):
    template_name = "dashboard/reuse_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        link = get_object_or_404(
            ReuseLink.objects.select_related("source_document__actor", "source_document__source",
                                             "target_document__actor", "target_document__source"),
            pk=kwargs["pk"],
        )  # fmt: skip
        min_words = 5 if link.link_type == ReuseLink.Type.VERBATIM else 3
        source, target, share = highlight(
            link.source_document.text or "", link.target_document.text or "", min_words
        )
        context |= {"link": link, "source_parts": source, "target_parts": target,
                    "matched_share": share, "min_words": min_words}  # fmt: skip
        return context
