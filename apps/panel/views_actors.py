"""Страница «Aktéři» панели (README §7.2): список, карточка, добавление, CSV, архивация."""

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import ListView

from apps.actors.models import Actor, ActorAccount, ActorInclusionPeriod, Layer

from . import actors as service
from . import audit
from .forms import (
    AccountForm,
    ActorCreateForm,
    ActorEditForm,
    CsvImportForm,
    ExcludeForm,
    IncludeForm,
)
from .models import AuditLog
from .views import ResearcherRequiredMixin

IMPORT_SESSION_KEY = "panel:actor-import"


def _jobs_message(request, jobs) -> None:
    if jobs:
        names = ", ".join(f"{j.get_job_type_display().lower()} #{j.pk}" for j in jobs)
        messages.info(request, f"Naplánované úlohy: {names}. Průběh je v sekci Úlohy.")


class ActorListView(ResearcherRequiredMixin, ListView):
    template_name = "panel/actors.html"
    context_object_name = "actors"
    paginate_by = 100

    def get_queryset(self):
        params = self.request.GET
        qs = Actor.objects.annotate(
            n_accounts=Count("accounts", distinct=True),
            n_periods=Count("inclusion_periods", distinct=True),
        ).order_by("name")
        if params.get("q"):
            qs = qs.filter(Q(name__icontains=params["q"]) | Q(party__icontains=params["q"]))
        if params.get("layer"):
            qs = qs.filter(layer=params["layer"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("auto") == "1":
            qs = qs.filter(added_automatically=True)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.copy()
        query.pop("page", None)
        context |= {
            "filters": self.request.GET,
            "layers": Layer.choices,
            "statuses": Actor.Status.choices,
            "querystring": query.urlencode(),
            "counts": dict(Actor.objects.values_list("status").annotate(n=Count("pk"))),
        }
        return context


class ActorPanelDetailView(ResearcherRequiredMixin, View):
    """Карточка актора: периоды, аккаунты, правка, архивация / повторное включение."""

    template_name = "panel/actor_detail.html"

    def get_context(self, actor, **forms):
        types = [
            ContentType.objects.get_for_model(m)
            for m in (Actor, ActorAccount, ActorInclusionPeriod)
        ]
        ids = {str(actor.pk)} | {str(pk) for pk in actor.accounts.values_list("pk", flat=True)}
        ids |= {str(pk) for pk in actor.inclusion_periods.values_list("pk", flat=True)}
        history = (
            AuditLog.objects.filter(content_type__in=types, object_id__in=ids)
            .select_related("content_type")
            .order_by("-timestamp")[:50]
        )
        excluded = actor.status == Actor.Status.EXCLUDED
        return {
            "actor": actor,
            "periods": actor.inclusion_periods.order_by("start_date"),
            "accounts": actor.accounts.select_related("source"),
            "consequences": None if excluded else service.consequences(actor),
            "history": history,
            "edit_form": forms.get("edit_form") or ActorEditForm(instance=actor),
            "account_form": forms.get("account_form") or AccountForm(actor=actor),
            "exclude_form": forms.get("exclude_form") or ExcludeForm(actor=actor),
            "include_form": forms.get("include_form") or IncludeForm(actor=actor),
            "open": forms.get("open", ""),
        }

    def get(self, request, pk):
        actor = get_object_or_404(Actor, pk=pk)
        return render(request, self.template_name, self.get_context(actor))

    def post(self, request, pk):
        actor = get_object_or_404(Actor, pk=pk)
        action = request.POST.get("action")
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            messages.error(request, "Neznámá akce.")
            return redirect("panel:actor", pk=actor.pk)
        return handler(request, actor)

    def do_edit(self, request, actor):
        before = audit.stored_snapshot(actor)
        form = ActorEditForm(request.POST, instance=actor)
        if not form.is_valid():
            return render(
                request, self.template_name, self.get_context(actor, edit_form=form, open="edit")
            )
        with transaction.atomic():
            form.save()
            audit.log_change(request, actor, before)
        messages.success(request, "Změny aktéra jsou uloženy.")
        return redirect("panel:actor", pk=actor.pk)

    def do_account(self, request, actor):
        form = AccountForm(request.POST, actor=actor)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                self.get_context(actor, account_form=form, open="account"),
            )
        with transaction.atomic():
            account = form.save(commit=False)
            account.actor = actor
            account.save()
            audit.log_create(request, account)
        messages.success(request, f"Účet {account} je přidán.")
        return redirect("panel:actor", pk=actor.pk)

    def do_exclude(self, request, actor):
        if actor.status == Actor.Status.EXCLUDED:
            messages.error(request, "Aktér už je vyřazen.")
            return redirect("panel:actor", pk=actor.pk)
        form = ExcludeForm(request.POST, actor=actor)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                self.get_context(actor, exclude_form=form, open="exclude"),
            )
        job = service.exclude_actor(
            request, actor, form.cleaned_data["reason"], on=form.cleaned_data["end_date"]
        )
        messages.success(request, f"Aktér {actor} je vyřazen. Data zůstávají v databázi.")
        _jobs_message(request, [job])
        return redirect("panel:actor", pk=actor.pk)

    def do_include(self, request, actor):
        if actor.status == Actor.Status.ACTIVE:
            messages.error(request, "Aktér je už zařazen.")
            return redirect("panel:actor", pk=actor.pk)
        form = IncludeForm(request.POST, actor=actor)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                self.get_context(actor, include_form=form, open="include"),
            )
        jobs = service.include_actor(
            request, actor, form.cleaned_data["reason"], on=form.cleaned_data["start_date"]
        )
        messages.success(request, f"Aktér {actor} je znovu zařazen (nové období zařazení).")
        _jobs_message(request, jobs)
        return redirect("panel:actor", pk=actor.pk)


class ActorCreateView(ResearcherRequiredMixin, View):
    template_name = "panel/actor_form.html"

    def get(self, request):
        return render(request, self.template_name, {"form": ActorCreateForm()})

    def post(self, request):
        form = ActorCreateForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})
        created = service.create_actor(request, form.draft())
        if form.cleaned_data.get("availability"):
            messages.info(request, f"Kontrola dostupnosti: {form.cleaned_data['availability']}.")
        messages.success(request, f"Aktér {created.actor} je přidán.")
        _jobs_message(request, created.jobs)
        return redirect("panel:actor", pk=created.actor.pk)


class ActorImportView(ResearcherRequiredMixin, View):
    """Импорт CSV в два шага: проверка с предпросмотром, затем запись строк без ошибок."""

    template_name = "panel/actor_import.html"

    def get(self, request):
        request.session.pop(IMPORT_SESSION_KEY, None)
        return render(
            request, self.template_name, {"form": CsvImportForm(), "columns": service.CSV_COLUMNS}
        )

    def post(self, request):
        if request.POST.get("action") == "confirm":
            stored = request.session.pop(IMPORT_SESSION_KEY, None)
            if stored is None:
                messages.error(request, "Náhled vypršel, nahrajte soubor znovu.")
                return redirect("panel:actor_import")
            # проверка повторяется: между предпросмотром и записью данные могли измениться
            rows, _problems = service.parse_csv(stored["content"])
            actors = service.import_rows(request, rows, stored["filename"])
            skipped = sum(1 for r in rows if not r.ok)
            messages.success(
                request,
                f"Importováno aktérů: {len(actors)}"
                + (f", přeskočeno řádků s chybou: {skipped}." if skipped else "."),
            )
            if actors:
                messages.info(request, "Naplánován přepočet agregátů. Průběh je v sekci Úlohy.")
            return redirect("panel:actors")
        form = CsvImportForm(request.POST, request.FILES)
        context = {"form": form, "columns": service.CSV_COLUMNS}
        if form.is_valid():
            upload = form.cleaned_data["file"]
            text = service.decode_csv(upload.read())
            rows, problems = service.parse_csv(text)
            request.session[IMPORT_SESSION_KEY] = {"filename": upload.name, "content": text}
            context |= {
                "rows": rows,
                "problems": problems,
                "filename": upload.name,
                "valid": sum(1 for r in rows if r.ok),
            }
        return render(request, self.template_name, context)
