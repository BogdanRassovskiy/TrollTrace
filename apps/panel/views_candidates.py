"""Кандидаты в нарративы: просмотр с фрагментами текста и решение одной кнопкой."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.narratives.models import NarrativeCandidate
from apps.processing import novelty

from . import audit
from .candidates import fragments, open_candidates
from .models import AuditLog
from .views import ResearcherRequiredMixin

Status = NarrativeCandidate.Status
DECISIONS = {"accept": Status.ACCEPTED, "reject": Status.REJECTED}


class CandidateListView(ResearcherRequiredMixin, ListView):
    template_name = "panel/candidates.html"
    context_object_name = "candidates"
    paginate_by = 50

    def get_queryset(self):
        if self.request.GET.get("stav") == "rozhodnuto":
            return NarrativeCandidate.objects.exclude(
                status__in=[Status.NEW, Status.DEFERRED]
            ).order_by("-decided_at", "-pk")
        return open_candidates()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["decided"] = self.request.GET.get("stav") == "rozhodnuto"
        context["n_open"] = open_candidates().count()
        context["querystring"] = "stav=rozhodnuto" if context["decided"] else ""
        return context


class CandidateDetailView(ResearcherRequiredMixin, TemplateView):
    template_name = "panel/candidate.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        candidate = get_object_or_404(NarrativeCandidate, pk=self.kwargs["pk"])
        queue = list(open_candidates().values_list("pk", flat=True))
        context |= {
            "candidate": candidate,
            "narrative": candidate.resulting_narrative,
            "nearest": candidate.nearest_narratives.select_related("topic"),
            "fragments": fragments(candidate),
            "claims": candidate.claims.select_related("document__source").order_by("pk"),
            "is_open": candidate.status in (Status.NEW, Status.DEFERRED),
            "position": queue.index(candidate.pk) + 1 if candidate.pk in queue else None,
            "n_open": len(queue),
        }
        return context


class CandidateDecideView(ResearcherRequiredMixin, View):
    """Принять в нарративы / отклонить. После решения — следующий кандидат в очереди."""

    def post(self, request, pk):
        candidate = get_object_or_404(NarrativeCandidate, pk=pk)
        status = DECISIONS.get(request.POST.get("decision"))
        if status is None or candidate.status not in (Status.NEW, Status.DEFERRED):
            messages.error(request, "O kandidátovi už bylo rozhodnuto nebo chybí rozhodnutí.")
            return redirect("panel:candidate", pk=pk)
        queue = list(open_candidates().values_list("pk", flat=True))
        before = candidate.get_status_display()
        candidate.decision_reason = request.POST.get("reason", "").strip()[:2000]
        novelty.decide(candidate, status, request.user)
        audit.log(
            request,
            AuditLog.Action.DECISION,
            candidate,
            changes={"status": [before, candidate.get_status_display()]},
            note=candidate.decision_reason,
        )
        if status == Status.ACCEPTED and candidate.resulting_narrative:
            messages.success(
                request,
                f"Přijato do narativů: {candidate.resulting_narrative.code}. Narativ je teď "
                "aktivní; definici můžete upravit v kódové knize.",
            )
        elif status == Status.ACCEPTED:
            messages.warning(request, "Přijato, ale narativ se nepodařilo vytvořit (chybí téma).")
        else:
            messages.success(request, "Kandidát zamítnut.")
        following = [p for p in queue[queue.index(pk) + 1 :] if p != pk] if pk in queue else queue
        if following:
            return redirect("panel:candidate", pk=following[0])
        return redirect("panel:candidates")
