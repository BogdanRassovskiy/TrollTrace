"""Кандидаты в нарративы: просмотр с фрагментами текста и решение одной кнопкой."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.narratives.models import Narrative, NarrativeCandidate, NarrativeStatus
from apps.narratives.tree import flatten
from apps.processing import novelty

from . import audit
from .candidates import fragments, open_candidates
from .models import AuditLog
from .views import ResearcherRequiredMixin

Status = NarrativeCandidate.Status
DECISIONS = {"accept": Status.ACCEPTED, "reject": Status.REJECTED, "merge": Status.MERGED}
TABS = {
    "": "Čekají na rozhodnutí",
    "inkubator": "Inkubátor",
    "dozrani": "Dozrání",
    "rozhodnuto": "Rozhodnuto",
}


class CandidateListView(ResearcherRequiredMixin, ListView):
    """Вкладки: очередь (видимые), инкубатор (малые), дозревание (автоматические решения),
    решённые исследователем."""

    template_name = "panel/candidates.html"
    context_object_name = "candidates"
    paginate_by = 50

    def tab(self) -> str:
        tab = self.request.GET.get("stav", "")
        return tab if tab in TABS else ""

    def get_queryset(self):
        tab = self.tab()
        if tab == "rozhodnuto":
            return NarrativeCandidate.objects.filter(
                status__in=[Status.ACCEPTED, Status.MERGED, Status.REJECTED]
            ).order_by("-decided_at", "-pk")
        if tab == "dozrani":
            return (
                NarrativeCandidate.objects.filter(status__in=[Status.DISTRIBUTED, Status.EXPIRED])
                .select_related("resulting_narrative")
                .order_by("-decided_at", "-pk")
            )
        return open_candidates(incubator=tab == "inkubator")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tab = self.tab()
        counts = {
            "": open_candidates().count(),
            "inkubator": open_candidates(incubator=True).count(),
            "dozrani": NarrativeCandidate.objects.filter(
                status__in=[Status.DISTRIBUTED, Status.EXPIRED]
            ).count(),
        }
        context |= {
            "tab": tab,
            "tabs": [(key, label, counts.get(key)) for key, label in TABS.items()],
            "decided": tab in ("rozhodnuto", "dozrani"),
            "n_open": counts[""],
            "querystring": f"stav={tab}" if tab else "",
        }
        return context


class CandidateDetailView(ResearcherRequiredMixin, TemplateView):
    template_name = "panel/candidate.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        candidate = get_object_or_404(NarrativeCandidate, pk=self.kwargs["pk"])
        queue = list(open_candidates().values_list("pk", flat=True))
        if candidate.pk not in queue:  # inkubátor: procházet v jeho pořadí
            incubator = list(open_candidates(incubator=True).values_list("pk", flat=True))
            queue = incubator if candidate.pk in incubator else queue
        nodes = Narrative.objects.filter(status=NarrativeStatus.ACTIVE).select_related(
            "topic", "parent"
        )
        context |= {
            "targets": [
                (
                    n.code,
                    "\u00a0\u00a0" * 2 * depth
                    + ("↳ " if depth else "")
                    + f"{n.topic.name} · {n.get_level_display()}: {n.name[:90]}",
                )
                for n, depth in flatten(list(nodes.order_by("topic__name")))
            ],  # fmt: skip
            "candidate": candidate,
            # собственный нарратив кластера; у объединённого resulting_narrative — чужой узел
            "narrative": None
            if candidate.status in (Status.MERGED, Status.DISTRIBUTED)
            else candidate.resulting_narrative,
            "nearest": candidate.nearest_narratives.select_related("topic"),
            "fragments": fragments(candidate),
            "claims": candidate.claims.select_related("document__source").order_by("pk"),
            "is_open": candidate.status in (Status.NEW, Status.DEFERRED, Status.EXPIRED),
            "position": queue.index(candidate.pk) + 1 if candidate.pk in queue else None,
            "n_open": len(queue),
        }
        return context


class CandidateDecideView(ResearcherRequiredMixin, View):
    """Принять в нарративы / отклонить. После решения — следующий кандидат в очереди."""

    def post(self, request, pk):
        candidate = get_object_or_404(NarrativeCandidate, pk=pk)
        decision = request.POST.get("decision")
        before = candidate.get_status_display()
        if decision == "reopen" and candidate.status == Status.EXPIRED:
            candidate.status, candidate.decided_at = Status.NEW, None
            candidate.decision_reason = "Znovu otevřen výzkumníkem."
            candidate.save(update_fields=["status", "decided_at", "decision_reason"])
            audit.log(request, AuditLog.Action.DECISION, candidate,
                      changes={"status": [before, candidate.get_status_display()]})  # fmt: skip
            messages.success(request, "Kandidát je znovu otevřen.")
            return redirect("panel:candidate", pk=pk)
        status = DECISIONS.get(decision)
        if status is None or candidate.status not in (Status.NEW, Status.DEFERRED, Status.EXPIRED):
            messages.error(request, "O kandidátovi už bylo rozhodnuto nebo chybí rozhodnutí.")
            return redirect("panel:candidate", pk=pk)
        target = None
        if status == Status.MERGED:
            target = Narrative.objects.filter(
                code=request.POST.get("target"), status=NarrativeStatus.ACTIVE
            ).first()
            if target is None:
                messages.error(request, "Vyberte aktivní narativ, ke kterému shluk patří.")
                return redirect("panel:candidate", pk=pk)
        queue = list(open_candidates().values_list("pk", flat=True))
        queue += list(open_candidates(incubator=True).values_list("pk", flat=True))
        candidate.decision_reason = request.POST.get("reason", "").strip()[:2000]
        novelty.decide(candidate, status, request.user, target=target)
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
        elif status == Status.MERGED:
            messages.success(
                request,
                f"Přidáno k narativu „{target.name}“: dokumenty shluku posoudí model podle "
                "tohoto narativu.",
            )
        else:
            messages.success(request, "Kandidát zamítnut.")
        following = [p for p in queue[queue.index(pk) + 1 :] if p != pk] if pk in queue else queue
        if following:
            return redirect("panel:candidate", pk=following[0])
        return redirect("panel:candidates")
