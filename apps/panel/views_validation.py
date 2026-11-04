"""Валидация в панели исследователя (README §7.2): выборки, ход разметки, согласие,
precision/recall, споры и экспорт для главы о методологии."""

import csv

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.narratives.models import LabelDecision
from apps.validation import sampling, stats
from apps.validation.forms import AdjudicationForm, SampleForm
from apps.validation.models import (
    Adjudication,
    Annotation,
    Report,
    ReuseVerdict,
    Review,
    Sample,
    Value,
)

from . import audit
from .models import AuditLog
from .views import ResearcherRequiredMixin

VALUE_LABELS = dict(Value.choices) | dict(ReuseVerdict.choices)


class ValidationListView(ResearcherRequiredMixin, View):
    template_name = "panel/validation.html"

    def context(self, form):
        samples = Sample.objects.annotate(
            n_items=Count("items", distinct=True),
            n_reviews=Count("items__reviews", distinct=True),
            n_annotators=Count("items__reviews__annotator", distinct=True),
        )
        return {"samples": samples, "form": form, "strata": sampling.STRATUM_LABELS}

    def get(self, request):
        return render(request, self.template_name, self.context(SampleForm()))

    def post(self, request):
        form = SampleForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, self.context(form))
        data = form.cleaned_data
        common = {
            "name": data["name"],
            "size": data["size"],
            "seed": data["seed"],
            "user": request.user,
            "hide_source": data["hide_source"],
        }
        with transaction.atomic():
            if data["kind"] == Sample.Kind.REUSE:
                sample = sampling.create_reuse_sample(**common)
            else:
                sample = sampling.create_narrative_sample(versions=data["versions"], **common)
            sample.description = data["description"]
            sample.save(update_fields=["description"])
            audit.log_create(request, sample, note=f"vybráno položek: {sample.items.count()}")
        messages.success(request, f"Vzorek „{sample}“ vytvořen: {sample.items.count()} položek.")
        return redirect("panel:validation_sample", pk=sample.pk)


def _pseudonyms(sample) -> dict:
    """Разметчики в экспорте и отчёте — A1, A2… (порядок по id, стабилен)."""
    ids = sorted(
        Review.objects.filter(item__sample=sample).values_list("annotator_id", flat=True).distinct()
    )
    return {pk: f"A{i + 1}" for i, pk in enumerate(ids)}


def _models(sample) -> list[str]:
    versions = sample.narrative_versions.values_list("pk", flat=True)
    return sorted(
        LabelDecision.objects.filter(narrative_version_id__in=versions)
        .exclude(model_name="")
        .values_list("model_name", flat=True)
        .distinct()
    )


class SampleDetailView(ResearcherRequiredMixin, View):
    template_name = "panel/validation_sample.html"

    def get(self, request, pk):
        sample = get_object_or_404(Sample, pk=pk)
        model = request.GET.get("model", "")
        report = stats.evaluate(sample, model)
        progress = (
            Review.objects.filter(item__sample=sample)
            .values("annotator__username")
            .annotate(n=Count("pk"), unclear=Count("pk", filter=Q(unclear=True)))
            .order_by("annotator__username")
        )
        disputes = stats.disagreements(sample)
        items = sample.items.in_bulk([item_id for item_id, _ in disputes[:50]])
        versions = {v.pk: v for v in sample.narrative_versions.select_related("narrative")}
        return render(
            request,
            self.template_name,
            {
                "sample": sample,
                "report": report,
                "progress": progress,
                "model": model,
                "models": _models(sample) if sample.kind == Sample.Kind.NARRATIVES else [],
                "disputes": [(items[i], versions.get(v)) for i, v in disputes[:50] if i in items],
                "n_disputes": len(disputes),
                "strata": sampling.STRATUM_LABELS,
                "reports": sample.reports.select_related("created_by"),
                "selection": [
                    (
                        label,
                        sample.params.get("population", {}).get(code),
                        sample.params.get("drawn", {}).get(code),
                    )
                    for code, label in sampling.STRATUM_LABELS.items()
                    if code in sample.params.get("drawn", {})
                ],
                "n_items": sample.items.count(),
            },
        )

    def post(self, request, pk):
        sample = get_object_or_404(Sample, pk=pk)
        action = request.POST.get("action")
        with transaction.atomic():
            if action in {"close", "reopen"}:
                before = audit.stored_snapshot(sample)
                sample.status = Sample.Status.CLOSED if action == "close" else Sample.Status.OPEN
                sample.save(update_fields=["status", "updated_at"])
                audit.log_change(request, sample, before)
                messages.success(request, f"Vzorek: {sample.get_status_display().lower()}.")
            elif action == "report":
                model = request.POST.get("model", "")
                report = Report.objects.create(
                    sample=sample,
                    model_name=model,
                    metrics=stats.evaluate(sample, model),
                    note=request.POST.get("note", ""),
                    created_by=request.user,
                )
                audit.log_create(request, report)
                messages.success(request, "Zpráva uložena; najdete ji níže, ke stažení jako JSON.")
            else:
                messages.error(request, "Neznámá akce.")
        return redirect("panel:validation_sample", pk=pk)


class AdjudicateView(ResearcherRequiredMixin, View):
    """Спор: все ответы разметчиков (с псевдонимами) и решение модели — исследователь
    выбирает эталон. Решение записывается в журнал аудита."""

    template_name = "panel/validation_adjudicate.html"

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.sample = get_object_or_404(Sample, pk=kwargs["pk"])
        self.item = get_object_or_404(self.sample.items, pk=kwargs["item"])

    def keys(self):
        if self.sample.kind == Sample.Kind.REUSE:
            return [(0, "Převzetí")]
        return [
            (v.pk, v.name)
            for v in self.sample.narrative_versions.select_related("narrative").order_by(
                "narrative__code"
            )
        ]

    def form(self, data=None):
        existing = {a.narrative_version_id or 0: a.value for a in self.item.adjudications.all()}
        choices = ReuseVerdict.choices if self.sample.kind == Sample.Kind.REUSE else Value.choices
        return AdjudicationForm(data, keys=self.keys(), choices=choices, initial_values=existing)

    def context(self, form):
        names = _pseudonyms(self.sample)
        reviews = self.item.reviews.select_related("annotator").prefetch_related("annotations")
        answers = []
        for review in reviews:
            by_key = {a.narrative_version_id: a.value for a in review.annotations.all()}
            if self.sample.kind == Sample.Kind.REUSE:
                by_key = {0: review.reuse_verdict}
            answers.append(
                {
                    "name": f"{names[review.annotator_id]} ({review.annotator})",
                    "unclear": review.unclear,
                    "note": review.note,
                    "values": [VALUE_LABELS.get(by_key.get(k, ""), "—") for k, _ in self.keys()],
                }
            )
        if self.sample.kind == Sample.Kind.REUSE:
            predicted = stats.reuse_predictions(self.sample)
        else:
            predicted = stats.model_predictions(self.sample)
        model_row = [
            VALUE_LABELS.get(predicted.get((self.item.pk, k)) or "", "čeká na model")
            for k, _ in self.keys()
        ]
        return {
            "sample": self.sample,
            "item": self.item,
            "form": form,
            "answers": answers,
            "keys": self.keys(),
            "model_row": model_row,
        }

    def get(self, request, pk, item):
        return render(request, self.template_name, self.context(self.form()))

    def post(self, request, pk, item):
        form = self.form(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, self.context(form))
        note = form.cleaned_data["note"]
        with transaction.atomic():
            for key, value in form.decisions().items():
                version_id = key or None
                existing = Adjudication.objects.filter(
                    item=self.item, narrative_version_id=version_id
                ).first()
                if not value:
                    if existing:
                        audit.log_delete(request, existing)
                        existing.delete()
                    continue
                before = audit.stored_snapshot(existing) if existing else {}
                adjudication, _ = Adjudication.objects.update_or_create(
                    item=self.item,
                    narrative_version_id=version_id,
                    defaults={"value": value, "note": note, "decided_by": request.user},
                )
                audit.log(
                    request,
                    AuditLog.Action.DECISION,
                    adjudication,
                    changes=audit.diff(before, audit.snapshot(adjudication)),
                    note=note,
                )
        messages.success(request, f"Rozhodnutí u položky #{self.item.position} uloženo.")
        following = stats.disagreements(self.sample)
        if following:
            return redirect("panel:validation_adjudicate", pk=pk, item=following[0][0])
        return redirect("panel:validation_sample", pk=pk)


class ExportView(ResearcherRequiredMixin, View):
    """CSV всей разметки (длинный формат) и JSON результатов — приложение к методологии."""

    def get(self, request, pk, fmt):
        sample = get_object_or_404(Sample, pk=pk)
        if fmt == "json":
            report_id = request.GET.get("report")
            if report_id:
                metrics = get_object_or_404(sample.reports, pk=report_id).metrics
            else:
                metrics = stats.evaluate(sample, request.GET.get("model", ""))
            metrics = {
                **metrics,
                "sample": {"id": sample.pk, "name": sample.name, "params": sample.params},
            }
            response = JsonResponse(metrics, json_dumps_params={"ensure_ascii": False, "indent": 2})
            response["Content-Disposition"] = f'attachment; filename="validace-{sample.pk}.json"'
            return response
        return self.csv(sample)

    def csv(self, sample):
        names = _pseudonyms(sample)
        golds = stats.gold(sample)
        if sample.kind == Sample.Kind.REUSE:
            predicted = stats.reuse_predictions(sample)
        else:
            predicted = stats.model_predictions(sample)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="validace-{sample.pk}.csv"'
        response.write("﻿")  # BOM: Excel откроет UTF-8 правильно
        writer = csv.writer(response)
        writer.writerow(
            [
                "position",
                "stratum",
                "weight",
                "document_id",
                "source_document_id",
                "target_document_id",
                "narrative",
                "version",
                "annotator",
                "value",
                "unclear",
                "gold",
                "gold_source",
                "model",
                "note",
            ]
        )
        if sample.kind == Sample.Kind.REUSE:
            rows = Review.objects.filter(item__sample=sample).select_related("item")
            for r in rows.order_by("item__position", "annotator_id"):
                key = (r.item_id, 0)
                g = golds.get(key, ("", ""))
                writer.writerow(
                    [
                        r.item.position,
                        r.item.stratum,
                        r.item.weight,
                        "",
                        r.item.source_document_id,
                        r.item.target_document_id,
                        "",
                        "",
                        names[r.annotator_id],
                        r.reuse_verdict,
                        int(r.unclear),
                        g[0],
                        g[1],
                        predicted.get(key, ""),
                        r.note,
                    ]
                )
            return response
        reviews = Review.objects.filter(item__sample=sample).select_related("item")
        annotations = {
            (a.review_id, a.narrative_version_id): a.value
            for a in Annotation.objects.filter(review__item__sample=sample)
        }
        versions = list(
            sample.narrative_versions.select_related("narrative").order_by("narrative__code")
        )
        for r in reviews.order_by("item__position", "annotator_id"):
            for v in versions:
                key = (r.item_id, v.pk)
                g = golds.get(key, ("", ""))
                writer.writerow(
                    [
                        r.item.position,
                        r.item.stratum,
                        r.item.weight,
                        r.item.document_id,
                        "",
                        "",
                        v.narrative.code,
                        v.version,
                        names[r.annotator_id],
                        annotations.get((r.pk, v.pk), ""),
                        int(r.unclear),
                        g[0],
                        g[1],
                        predicted.get(key) or "",
                        r.note,
                    ]
                )
        return response
