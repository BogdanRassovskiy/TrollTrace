"""Разметка выборки (README §4, шаг 9). Вслепую: разметчик не видит ни решений модели,
ни ответов других разметчиков, а при `hide_source` — ни источника, ни автора."""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import ListView

from apps.core.roles import AnnotatorRequiredMixin

from .models import Annotation, ReuseVerdict, Review, Sample, Value


class SampleListView(AnnotatorRequiredMixin, ListView):
    template_name = "validation/index.html"
    context_object_name = "samples"

    def get_queryset(self):
        return Sample.objects.filter(status=Sample.Status.OPEN).annotate(
            n_items=Count("items", distinct=True),
            n_done=Count(
                "items__reviews",
                filter=Q(items__reviews__annotator=self.request.user),
                distinct=True,
            ),
        )


def _next_position(sample, user) -> int | None:
    item = (
        sample.items.exclude(reviews__annotator=user)
        .order_by("position")
        .values("position")
        .first()
    )
    return item["position"] if item else None


class NextItemView(AnnotatorRequiredMixin, View):
    def get(self, request, pk):
        sample = get_object_or_404(Sample, pk=pk)
        position = _next_position(sample, request.user)
        if position is None:
            return render(request, "validation/done.html", {"sample": sample})
        return redirect("validation:item", pk=pk, position=position)


class AnnotateView(AnnotatorRequiredMixin, View):
    template_name = "validation/item.html"

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.sample = get_object_or_404(Sample, pk=kwargs["pk"])
        self.item = get_object_or_404(
            self.sample.items.select_related(
                "document__source",
                "document__actor",
                "source_document__source",
                "target_document__source",
                "source_document__actor",
                "target_document__actor",
            ),
            position=kwargs["position"],
        )

    def versions(self):
        return list(
            self.sample.narrative_versions.select_related("narrative").order_by("narrative__code")
        )

    def context(self, review, values, errors=None):
        total = self.sample.items.count()
        done = self.sample.items.filter(reviews__annotator=self.request.user).count()
        rows = [(v, values.get(v.pk, "")) for v in self.versions()]
        return {
            "sample": self.sample,
            "item": self.item,
            "review": review,
            "rows": rows,
            "value_choices": Value.choices,
            "verdict_choices": ReuseVerdict.choices,
            "errors": errors or [],
            "total": total,
            "done": done,
            "closed": self.sample.status == Sample.Status.CLOSED,
            "previous": self.item.position - 1 if self.item.position > 1 else None,
            "following": self.item.position + 1 if self.item.position < total else None,
        }

    def get(self, request, pk, position):
        review = Review.objects.filter(item=self.item, annotator=request.user).first()
        values = (
            dict(review.annotations.values_list("narrative_version_id", "value")) if review else {}
        )
        return render(request, self.template_name, self.context(review, values))

    def post(self, request, pk, position):
        if self.sample.status == Sample.Status.CLOSED:
            messages.error(request, "Vzorek je uzavřen, anotace už nelze měnit.")
            return redirect("validation:item", pk=pk, position=position)
        unclear = request.POST.get("unclear") == "on"
        note = request.POST.get("note", "").strip()
        errors, values, verdict = [], {}, ""
        if self.sample.kind == Sample.Kind.REUSE:
            verdict = request.POST.get("verdict", "")
            if verdict not in ReuseVerdict.values and not unclear:
                errors.append("Vyberte, zda jde o převzetí, nebo označte „nelze posoudit“.")
        else:
            for version in self.versions():
                value = request.POST.get(f"v{version.pk}", "")
                if value in Value.values:
                    values[version.pk] = value
                elif not unclear:
                    errors.append(f"Chybí hodnota u narativu „{version.name}“.")
        if errors:
            review = Review.objects.filter(item=self.item, annotator=request.user).first()
            return render(request, self.template_name, self.context(review, values, errors))
        with transaction.atomic():
            review, _ = Review.objects.update_or_create(
                item=self.item,
                annotator=request.user,
                defaults={
                    "unclear": unclear,
                    "note": note,
                    "reuse_verdict": "" if unclear else verdict,
                },
            )
            review.annotations.all().delete()
            if not unclear:
                Annotation.objects.bulk_create(
                    Annotation(review=review, narrative_version_id=version_id, value=value)
                    for version_id, value in values.items()
                )
        return redirect("validation:next", pk=pk)
