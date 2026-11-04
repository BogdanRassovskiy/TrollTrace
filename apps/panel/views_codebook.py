"""Кодбук, настройки и фоновые задачи в панели (README §7.2)."""

import difflib
import json

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.narratives.models import (
    Label,
    Narrative,
    NarrativeCandidate,
    NarrativeStatus,
    NarrativeVersion,
    Topic,
)
from apps.narratives.retro import start_relabel

from . import actors as service
from . import audit
from .forms import STANCE_KEYS, NarrativeForm, SettingForm, TopicForm
from .models import AuditLog, Job, Setting
from .views import ResearcherRequiredMixin

# --- кодбук --------------------------------------------------------------------------------


class CodebookView(ResearcherRequiredMixin, TemplateView):
    template_name = "panel/codebook.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        counts = dict(
            Label.objects.current().values_list("narrative").annotate(n=Count("pk")).order_by()
        )
        narratives = Narrative.objects.select_related("topic")
        by_topic: dict = {}
        for narrative in narratives.order_by("status", "code"):
            narrative.n_labels = counts.get(narrative.pk, 0)
            by_topic.setdefault(narrative.topic, []).append(narrative)
        context |= {
            "topics": [(t, by_topic.get(t, [])) for t in Topic.objects.order_by("name")],
            "candidates": NarrativeCandidate.objects.filter(
                status=NarrativeCandidate.Status.NEW
            ).count(),
            "recent": NarrativeVersion.objects.select_related("narrative", "changed_by").order_by(
                "-changed_at"
            )[:15],
        }
        return context


class NarrativeEditView(ResearcherRequiredMixin, View):
    """Создание и правка нарратива. Изменение определения = новая версия кодбука."""

    template_name = "panel/narrative_form.html"

    def get_object(self, code):
        return get_object_or_404(Narrative, code=code) if code else Narrative()

    def get(self, request, code=None):
        narrative = self.get_object(code)
        return render(
            request,
            self.template_name,
            {"form": NarrativeForm(instance=narrative), "narrative": narrative},
        )

    def post(self, request, code=None):
        narrative = self.get_object(code)
        before = audit.stored_snapshot(narrative) if narrative.pk else {}
        version = narrative.current_version
        form = NarrativeForm(request.POST, instance=narrative)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "narrative": narrative})
        with transaction.atomic():
            narrative = form.save(user=request.user)
            if before:
                audit.log_change(request, narrative, before, note=form.cleaned_data["change_note"])
            else:
                audit.log_create(request, narrative, note=form.cleaned_data["change_note"])
        if narrative.current_version != version:
            messages.success(
                request,
                f"Uložena verze v{narrative.current_version}. Nové dokumenty "
                "se anotují podle ní; archiv lze dohledat tlačítkem níže.",
            )
        else:
            messages.info(request, "Definice se nezměnila, nová verze nevznikla.")
        return redirect("panel:narrative_history", code=narrative.code)


def _word_diff(old: str, new: str) -> str:
    """Пословное сравнение: удалённое зачёркнуто, добавленное выделено."""
    a, b = old.split(), new.split()
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            parts.append(escape(" ".join(a[i1:i2])))
            continue
        if i2 > i1:
            parts.append(f'<del class="old">{escape(" ".join(a[i1:i2]))}</del>')
        if j2 > j1:
            parts.append(f'<ins class="new">{escape(" ".join(b[j1:j2]))}</ins>')
    return mark_safe(" ".join(parts))  # noqa: S308 — части экранированы выше


def _as_text(field: str, value) -> str:
    if field == "stance_guide":
        value = value or {}
        return "\n".join(f"{label}: {value[key]}" for key, label in STANCE_KEYS if value.get(key))
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


COMPARED_FIELDS = [
    "name",
    "definition",
    "kind",
    "topic",
    "status",
    "examples",
    "counter_examples",
    "stance_guide",
    "cues",
    "attribution",
]


def compare_versions(old: NarrativeVersion, new: NarrativeVersion) -> list[dict]:
    rows = []
    for field in COMPARED_FIELDS:
        label = NarrativeVersion._meta.get_field(field).verbose_name
        if field == "topic":
            before, after = str(old.topic), str(new.topic)
        elif field in {"kind", "status"}:
            before = getattr(old, f"get_{field}_display")()
            after = getattr(new, f"get_{field}_display")()
        else:
            before, after = (
                _as_text(field, getattr(old, field)),
                _as_text(field, getattr(new, field)),
            )
        lines = [_word_diff(x, y) for x, y in _pair_lines(before, after)]
        rows.append({"label": label, "changed": before != after, "lines": lines, "text": after})
    return rows


def _pair_lines(before: str, after: str):
    """Строки списков сравниваются попарно по совпадению, остальное — как вставка/удаление."""
    a, b = before.splitlines() or [""], after.splitlines() or [""]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if tag == "equal":
            yield from ((x, x) for x in a[i1:i2])
        elif tag == "replace" and i2 - i1 == j2 - j1:
            yield from zip(a[i1:i2], b[j1:j2], strict=True)
        else:
            yield from ((x, "") for x in a[i1:i2])
            yield from (("", y) for y in b[j1:j2])


class NarrativeHistoryView(ResearcherRequiredMixin, View):
    template_name = "panel/narrative_history.html"

    def get(self, request, code):
        narrative = get_object_or_404(Narrative, code=code)
        versions = list(narrative.versions.select_related("changed_by", "topic"))
        by_number = {v.version: v for v in versions}
        try:
            new_n = int(request.GET.get("b") or narrative.current_version)
            old_n = int(request.GET.get("a") or new_n - 1)
        except ValueError as exc:
            raise Http404 from exc
        new, old = by_number.get(new_n), by_number.get(old_n)
        if old is not None and new is not None and old.version > new.version:
            old, new = new, old
        labels = (
            Label.objects.current()
            .filter(narrative=narrative)
            .values("narrative_version__version")
            .annotate(n=Count("pk"))
        )
        return render(
            request,
            self.template_name,
            {
                "narrative": narrative,
                "versions": versions,
                "old": old,
                "new": new,
                "rows": compare_versions(old, new) if old and new else None,
                "labels_by_version": {r["narrative_version__version"]: r["n"] for r in labels},
                "jobs": Job.objects.filter(
                    job_type=Job.Type.RELABEL, params__narrative=narrative.code
                )[:5],
            },
        )

    def post(self, request, code):
        """«Dohledat v archivu»: ручная ретроразметка по текущей версии."""
        narrative = get_object_or_404(Narrative, code=code)
        if narrative.status == NarrativeStatus.ARCHIVED:
            messages.error(request, "Archivovaný narativ se nesleduje.")
            return redirect("panel:narrative_history", code=code)
        with transaction.atomic():
            job = start_relabel(narrative, request.user)
            audit.log(
                request,
                AuditLog.Action.RUN,
                job,
                note=f"Dohledat v archivu: {narrative.code} v{narrative.current_version}",
            )
        messages.success(request, f"Zpětné anotování naplánováno (úloha #{job.pk}).")
        return redirect("panel:narrative_history", code=code)


class TopicEditView(ResearcherRequiredMixin, View):
    template_name = "panel/topic_form.html"

    def get(self, request, pk=None):
        topic = get_object_or_404(Topic, pk=pk) if pk else Topic()
        return render(
            request, self.template_name, {"form": TopicForm(instance=topic), "topic": topic}
        )

    def post(self, request, pk=None):
        topic = get_object_or_404(Topic, pk=pk) if pk else Topic()
        before = audit.stored_snapshot(topic) if topic.pk else {}
        form = TopicForm(request.POST, instance=topic)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "topic": topic})
        with transaction.atomic():
            topic = form.save()
            if before:
                audit.log_change(request, topic, before)
            else:
                audit.log_create(request, topic)
        messages.success(request, f"Téma „{topic}“ je uloženo.")
        return redirect("panel:codebook")


# --- настройки -----------------------------------------------------------------------------


def _group(key: str) -> str:
    return key.split("-", 1)[0]


class SettingsView(ResearcherRequiredMixin, TemplateView):
    template_name = "panel/settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        groups: dict = {}
        for setting in Setting.objects.order_by("key"):
            groups.setdefault(_group(setting.key), []).append(setting)
        context["groups"] = sorted(groups.items())
        return context


class SettingEditView(ResearcherRequiredMixin, View):
    template_name = "panel/setting_form.html"

    def context(self, setting, form):
        return {
            "setting": setting,
            "form": form,
            "versions": setting.versions.select_related("changed_by"),
        }

    def get(self, request, key):
        setting = get_object_or_404(Setting, key=key)
        return render(
            request, self.template_name, self.context(setting, SettingForm(setting=setting))
        )

    def post(self, request, key):
        setting = get_object_or_404(Setting, key=key)
        form = SettingForm(request.POST, setting=setting)
        if not form.is_valid():
            return render(request, self.template_name, self.context(setting, form))
        if form.cleaned_data["value"] == setting.value:
            messages.info(request, "Hodnota se nezměnila.")
            return redirect("panel:setting", key=key)
        before = audit.stored_snapshot(setting)
        with transaction.atomic():
            setting.value = form.cleaned_data["value"]
            setting.save(changed_by=request.user, change_note=form.cleaned_data["change_note"])
            audit.log_change(request, setting, before, note=form.cleaned_data["change_note"])
        messages.success(
            request,
            f"Nastavení {key} uloženo jako verze v{setting.current_version}. "
            "Projeví se při dalším běhu příslušné úlohy.",
        )
        return redirect("panel:setting", key=key)


# --- задачи --------------------------------------------------------------------------------


class JobListView(ResearcherRequiredMixin, ListView):
    template_name = "panel/jobs.html"
    context_object_name = "jobs"
    paginate_by = 50

    def get_queryset(self):
        qs = Job.objects.select_related("created_by").order_by("-created_at")
        params = self.request.GET
        if params.get("type"):
            qs = qs.filter(job_type=params["type"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("job"):
            qs = qs.filter(pk=params["job"]) if params["job"].isdigit() else qs.none()
        return qs

    def get_context_data(self, **kwargs):
        from apps.processing import labeling, pipeline

        context = super().get_context_data(**kwargs)
        query = self.request.GET.copy()
        query.pop("page", None)
        context |= {
            "filters": self.request.GET,
            "types": Job.Type.choices,
            "statuses": Job.Status.choices,
            "querystring": query.urlencode(),
            "running": Job.objects.filter(
                status__in=[Job.Status.PENDING, Job.Status.RUNNING]
            ).count(),
            "queues": [
                (
                    "Dokumenty čekající na zpracování (relevance, embeddingy, převzetí)",
                    pipeline.pending().count(),
                ),
                ("Kandidáti na štítek čekající na model", labeling.pending().count()),
                (
                    "Dokumenty čekající na hledání nových tvrzení",
                    labeling.pending_screenings().count(),
                ),
            ],
        }
        return context

    def post(self, request):
        action = request.POST.get("action")
        if action == "rebuild":
            with transaction.atomic():
                job = service.queue_aggregates(request.user, "ruční přepočet z panelu")
            messages.success(request, f"Přepočet agregátů naplánován (úloha #{job.pk}).")
        elif action == "cancel":
            job = get_object_or_404(Job, pk=request.POST.get("job"), status=Job.Status.PENDING)
            with transaction.atomic():
                before = audit.stored_snapshot(job)
                job.status = Job.Status.CANCELLED
                job.message = "Zrušeno výzkumníkem před spuštěním."
                job.save(update_fields=["status", "message"])
                audit.log_change(request, job, before)
            if job.celery_task_id:
                from config.celery import app

                app.control.revoke(job.celery_task_id)
            messages.success(request, f"Úloha #{job.pk} je zrušena.")
        else:
            messages.error(request, "Neznámá akce.")
        return redirect("panel:jobs")
