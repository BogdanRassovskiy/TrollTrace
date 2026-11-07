"""Маркеры нарративов в панели: сводка по статусам, происхождение и проверки каждого маркера.

Статусы меняют правила автоматически (apps/processing/markers.py); исследователь может
маркер запретить, вернуть на испытание или перенести к другому узлу, и запустить прогон
архива по новым активным маркерам.
"""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import ListView, TemplateView

from apps.narratives.models import (
    LabelDecision,
    MarkerStatus,
    Narrative,
    NarrativeMarker,
    NarrativeStatus,
)
from apps.narratives.tree import flatten
from apps.processing import markers

from . import audit
from .models import AuditLog, Job
from .views import ResearcherRequiredMixin

TABS = {
    "": ("Aktivní", [MarkerStatus.ACTIVE]),
    "zkouska": ("Ve zkoušce", [MarkerStatus.TRIAL]),
    "navrzene": ("Navržené", [MarkerStatus.PROPOSED]),
    "vyrazene": (
        "Vyřazené",
        [MarkerStatus.REJECTED, MarkerStatus.SUSPENDED, MarkerStatus.BANNED],
    ),
}


class MarkerListView(ResearcherRequiredMixin, ListView):
    template_name = "panel/markers.html"
    context_object_name = "markers"
    paginate_by = 100

    def tab(self) -> str:
        tab = self.request.GET.get("stav", "")
        return tab if tab in TABS else ""

    def get_queryset(self):
        queryset = NarrativeMarker.objects.filter(status__in=TABS[self.tab()][1]).select_related(
            "narrative", "first_document__source"
        )
        if self.tab() == "navrzene":
            return queryset.order_by("-n_proposals", "-n_authors", "phrase")
        return queryset.order_by("narrative__code", "phrase")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        counts = markers.overview()
        tab = self.tab()
        context |= {
            "tab": tab,
            "tabs": [
                (key, label, sum(counts.get(s, 0) for s in statuses))
                for key, (label, statuses) in TABS.items()
            ],
            "archive_pending": markers.archive_pending().count(),
            "querystring": f"stav={tab}" if tab else "",
        }
        return context

    def post(self, request):
        """«Dohledat v archivu»: релевантный архив по активным маркерам без прогона."""
        from apps.processing.tasks import queue_marker_archive

        pending = list(markers.archive_pending().values_list("pk", flat=True))
        if not pending:
            messages.info(request, "Všechny aktivní markery už archivem prošly.")
            return redirect("panel:markers")
        with transaction.atomic():
            job = Job.objects.create(
                job_type=Job.Type.RELABEL, params={"markers": pending}, created_by=request.user
            )
            audit.log(request, AuditLog.Action.RUN, job,
                      note=f"Dohledat v archivu podle markerů: {len(pending)}")  # fmt: skip

            def enqueue():
                result = queue_marker_archive.delay(job.pk)
                Job.objects.filter(pk=job.pk).update(celery_task_id=result.id or "")

            transaction.on_commit(enqueue)
        messages.success(request, f"Průchod archivem naplánován (úloha #{job.pk}).")
        return redirect("panel:markers")


class MarkerDetailView(ResearcherRequiredMixin, TemplateView):
    template_name = "panel/marker.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        marker = get_object_or_404(
            NarrativeMarker.objects.select_related("narrative"), pk=self.kwargs["pk"]
        )
        checks = (
            LabelDecision.objects.filter(markers__contains=[marker.pk])
            .select_related("document__source", "narrative")
            .order_by("-created_at")
        )
        nodes = Narrative.objects.exclude(status=NarrativeStatus.ARCHIVED).select_related(
            "topic", "parent"
        )
        context |= {
            "marker": marker,
            "proposals": marker.proposals.select_related("document__source", "narrative")[:100],
            "checks": checks[:100],
            "events": marker.events.select_related("narrative", "user"),
            "targets": [
                (
                    n.code,
                    "\u00a0\u00a0" * 2 * depth
                    + ("↳ " if depth else "")
                    + f"{n.topic.name} · {n.get_level_display()}: {n.name[:90]}",
                )
                for n, depth in flatten(list(nodes.order_by("topic__name")))
            ],  # fmt: skip
            "can_retry": marker.status != MarkerStatus.TRIAL,
            "can_ban": marker.status != MarkerStatus.BANNED,
        }
        return context

    def post(self, request, pk):
        marker = get_object_or_404(NarrativeMarker, pk=pk)
        action = request.POST.get("action")
        before = marker.get_status_display()
        if action == "ban" and marker.status != MarkerStatus.BANNED:
            event = markers.ban(marker, request.user, request.POST.get("reason", "").strip())
            messages.success(request, "Marker je zakázán: model ho už nebude navrhovat.")
        elif action == "retry":
            target = Narrative.objects.filter(code=request.POST.get("target")).first()
            if target is not None and target.status == NarrativeStatus.ARCHIVED:
                messages.error(request, "Narativ je v archivu.")
                return redirect("panel:marker", pk=pk)
            event = markers.retry(marker, request.user, target)
            messages.success(request, "Marker je znovu ve zkoušce.")
        else:
            messages.error(request, "Neznámá akce.")
            return redirect("panel:marker", pk=pk)
        audit.log(
            request,
            AuditLog.Action.DECISION,
            marker,
            changes={"status": [before, marker.get_status_display()]},
            note=event.reason,
        )
        return redirect("panel:marker", pk=pk)
