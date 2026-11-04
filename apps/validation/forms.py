"""Формы валидации в панели исследователя."""

import datetime

from django import forms

from apps.narratives.models import NarrativeStatus, NarrativeVersion

from .models import Sample


def current_versions():
    """Текущие версии отслеживаемых нарративов: по ним размечают новую выборку."""
    versions = NarrativeVersion.objects.select_related("narrative").filter(
        narrative__status__in=[NarrativeStatus.ACTIVE, NarrativeStatus.CANDIDATE]
    )
    return [
        v for v in versions.order_by("narrative__code") if v.version == v.narrative.current_version
    ]


class VersionChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.narrative.name} ({obj.narrative.code} v{obj.version})"


class SampleForm(forms.Form):
    name = forms.CharField(label="Název", max_length=200)
    kind = forms.ChoiceField(
        label="Druh",
        choices=Sample.Kind.choices,
        widget=forms.RadioSelect,
        initial=Sample.Kind.NARRATIVES,
    )
    versions = VersionChoiceField(
        label="Narativy",
        queryset=NarrativeVersion.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Jen pro vzorek narativů. Anotuje se podle aktuální verze; pozdější změna "
        "definice vzorek nemění.",
    )
    size = forms.IntegerField(label="Velikost vzorku", min_value=10, max_value=2000, initial=200)
    seed = forms.IntegerField(
        label="Semínko náhody",
        initial=lambda: int(datetime.date.today().strftime("%Y%m%d")),
        help_text="Stejné semínko a stejná data dají stejný vzorek (reprodukovatelnost).",
    )
    hide_source = forms.BooleanField(
        label="Skrýt anotátorům zdroj a autora", required=False, initial=True
    )
    description = forms.CharField(
        label="Popis pro anotátory", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        versions = current_versions()
        self.fields["versions"].queryset = (
            NarrativeVersion.objects.filter(pk__in=[v.pk for v in versions])
            .select_related("narrative")
            .order_by("narrative__code")
        )
        self.fields["versions"].initial = [
            v.pk for v in versions if v.narrative.status == NarrativeStatus.ACTIVE
        ]

    def clean(self):
        data = super().clean()
        if data.get("kind") == Sample.Kind.NARRATIVES and not data.get("versions"):
            self.add_error("versions", "Vyberte aspoň jeden narativ.")
        return data


class AdjudicationForm(forms.Form):
    """Решение по одному спору: поле на каждый нарратив (или одно для заимствований)."""

    note = forms.CharField(
        label="Zdůvodnění", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, keys, choices, initial_values, **kwargs):
        super().__init__(*args, **kwargs)
        self.keys = keys
        for key, label in keys:
            self.fields[f"k{key}"] = forms.ChoiceField(
                label=label,
                required=False,
                choices=[("", "— bez rozhodnutí —"), *choices],
                initial=initial_values.get(key, ""),
            )

    def decisions(self):
        return {key: self.cleaned_data[f"k{key}"] for key, _ in self.keys}
