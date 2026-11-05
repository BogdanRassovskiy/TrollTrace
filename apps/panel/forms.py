"""Формы панели исследователя (README §7.2)."""

import datetime
import json

from django import forms
from django.urls import reverse
from django.utils.html import format_html

from apps.actors.models import Actor, ActorAccount, Layer, Platform, Source
from apps.ingestion import parsers
from apps.narratives.models import Narrative, Topic

from . import actors as service


class LinesField(forms.CharField):
    """Список строк в JSON-поле ↔ textarea «по одной на строку»."""

    widget = forms.Textarea(attrs={"rows": 4})

    def prepare_value(self, value):
        if isinstance(value, list):
            return "\n".join(str(v) for v in value)
        return value

    def to_python(self, value):
        text = super().to_python(value)
        return [line.strip() for line in text.splitlines() if line.strip()]


# --- акторы --------------------------------------------------------------------------------


class ActorCreateForm(forms.Form):
    SOURCE_NONE, SOURCE_EXISTING, SOURCE_NEW = "none", "existing", "new"

    name = forms.CharField(label="Jméno / název", max_length=200)
    actor_type = forms.ChoiceField(label="Typ", choices=Actor.Type.choices)
    layer = forms.ChoiceField(label="Vrstva", choices=[("", "—")] + Layer.choices, required=False)
    party = forms.CharField(label="Strana", max_length=100, required=False)
    is_hybrid = forms.BooleanField(label="Hybridní aktér", required=False)
    inclusion_criterion = forms.CharField(
        label="Důvod zařazení",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Kritérium výběru (README §2), např. „poslanec PSP 2025–2029“.",
    )
    inclusion_reference = forms.URLField(
        label="Důvod: odkaz", required=False, max_length=500, assume_scheme="https"
    )
    start_date = forms.DateField(
        label="Zařazen od",
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    source_mode = forms.ChoiceField(
        label="Zdroj dokumentů",
        choices=[
            (SOURCE_NONE, "bez zdroje (doplním později)"),
            (SOURCE_EXISTING, "existující zdroj"),
            (SOURCE_NEW, "nový zdroj"),
        ],
        initial=SOURCE_NONE,
        widget=forms.RadioSelect,
    )
    source = forms.ModelChoiceField(
        label="Existující zdroj", queryset=Source.objects.order_by("layer", "name"), required=False
    )
    account_id = forms.CharField(
        label="Identifikátor účtu",
        max_length=300,
        required=False,
        help_text="Handle Telegramu, ID poslance apod. Prázdné — aktér je celý zdroj.",
    )
    account_url = forms.URLField(
        label="Odkaz na účet", max_length=500, assume_scheme="https", required=False
    )
    new_source_url = forms.URLField(
        label="Adresa nového zdroje", max_length=500, assume_scheme="https", required=False
    )
    new_source_name = forms.CharField(
        label="Název zdroje",
        max_length=200,
        required=False,
        help_text="Prázdné — podle jména aktéra.",
    )
    new_source_platform = forms.ChoiceField(
        label="Platforma", choices=Platform.choices, required=False
    )
    new_source_layer = forms.ChoiceField(
        label="Vrstva zdroje", choices=[("", "jako aktér")] + Layer.choices, required=False
    )
    confirm = forms.BooleanField(
        label="Uložit i přes upozornění (možný duplikát, nedostupný zdroj)", required=False
    )

    def __init__(self, *args, check_available=service.check_available, **kwargs):
        super().__init__(*args, **kwargs)
        self._check_available = check_available
        self.warnings: list[str] = []

    def clean(self):
        data = super().clean()
        mode = data.get("source_mode")
        handle = service.clean_handle(data.get("account_id", ""))
        data["account_id"] = handle
        if (
            data.get("name")
            and Actor.objects.filter(
                name__iexact=data["name"], party__iexact=data.get("party", "")
            ).exists()
        ):
            self.warnings.append("Aktér se stejným jménem a stranou už existuje.")
        if mode == self.SOURCE_EXISTING:
            source = data.get("source")
            if source is None:
                self.add_error("source", "Vyberte zdroj.")
            else:
                conflict = service.account_conflict(source, handle, data.get("account_url", ""))
                if conflict is not None:
                    self.add_error("account_id", _taken(conflict))
        elif mode == self.SOURCE_NEW:
            url = data.get("new_source_url")
            if not url:
                self.add_error("new_source_url", "Zadejte adresu zdroje.")
            elif (existing := service.find_source(url)) is not None:
                self.add_error(
                    "new_source_url",
                    f"Zdroj „{existing}“ už existuje — vyberte ho jako existující.",
                )
            else:
                ok, message = self._check_available(url)
                if not ok:
                    self.warnings.append(f"Kontrola dostupnosti: {message}.")
                data["availability"] = message
                if data.get("new_source_platform") == Platform.WEB and parsers.guess(url) is None:
                    # Правило этапа 3.5: у каждого веб-источника свой парсер.
                    self.warnings.append(
                        "Pro tento web zatím není parser (apps/ingestion/parsers). Zdroj se "
                        "založí se sběrem vypnutým; zapněte ho po doplnění parseru."
                    )
            if not (data.get("new_source_layer") or data.get("layer")):
                self.add_error("new_source_layer", "Zdroj musí mít vrstvu.")
            if not data.get("new_source_platform"):
                self.add_error("new_source_platform", "Vyberte platformu.")
        if self.warnings and not data.get("confirm") and not self.errors:
            raise forms.ValidationError(
                [*self.warnings, "Zkontrolujte to a potvrďte uložení zaškrtnutím níže."]
            )
        return data

    def draft(self) -> service.ActorDraft:
        data = self.cleaned_data
        mode = data["source_mode"]
        new_source = None
        if mode == self.SOURCE_NEW:
            new_source = service.NewSource(
                url=data["new_source_url"],
                name=data["new_source_name"],
                platform=data["new_source_platform"],
                layer=data["new_source_layer"],
            )
        return service.ActorDraft(
            name=data["name"],
            actor_type=data["actor_type"],
            layer=data["layer"],
            party=data["party"],
            is_hybrid=data["is_hybrid"],
            inclusion_criterion=data["inclusion_criterion"],
            inclusion_reference=data["inclusion_reference"],
            start_date=data["start_date"],
            source=data.get("source") if mode == self.SOURCE_EXISTING else None,
            new_source=new_source,
            account_id=data["account_id"] if mode != self.SOURCE_NONE else "",
            account_url=data["account_url"] if mode != self.SOURCE_NONE else "",
        )


def _taken(account: ActorAccount):
    url = reverse("panel:actor", args=[account.actor_id])
    return format_html('Účet už patří aktérovi <a href="{}">{}</a>.', url, account.actor)


class ActorEditForm(forms.ModelForm):
    class Meta:
        model = Actor
        fields = [
            "name",
            "actor_type",
            "layer",
            "party",
            "is_hybrid",
            "activity_cohort",
            "security_portfolio",
            "is_pseudonymized",
            "inclusion_criterion",
            "inclusion_reference",
        ]
        widgets = {"inclusion_criterion": forms.Textarea(attrs={"rows": 2})}


class AccountForm(forms.ModelForm):
    class Meta:
        model = ActorAccount
        fields = ["source", "account_id", "url"]

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.fields["source"].queryset = Source.objects.order_by("layer", "name")

    def clean(self):
        data = super().clean()
        data["account_id"] = service.clean_handle(data.get("account_id", ""))
        if data.get("source") is not None:
            conflict = service.account_conflict(
                data["source"], data["account_id"], data.get("url", "")
            )
            if conflict is not None:
                if conflict.actor_id == self.actor.pk:
                    raise forms.ValidationError("Tento účet už aktér má.")
                raise forms.ValidationError(_taken(conflict))
        return data


class ExcludeForm(forms.Form):
    reason = forms.CharField(label="Důvod vyřazení", widget=forms.Textarea(attrs={"rows": 2}))
    end_date = forms.DateField(
        label="Vyřazen ke dni",
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor

    def clean_end_date(self):
        end = self.cleaned_data["end_date"]
        period = self.actor.open_period
        if period is not None and end < period.start_date:
            raise forms.ValidationError(
                f"Období zařazení začíná {period.start_date:%-d. %-m. %Y}, konec nesmí být dřív."
            )
        return end


class IncludeForm(forms.Form):
    reason = forms.CharField(label="Důvod zařazení", widget=forms.Textarea(attrs={"rows": 2}))
    start_date = forms.DateField(
        label="Zařazen od",
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor

    def clean_start_date(self):
        start = self.cleaned_data["start_date"]
        last = service.last_period_end(self.actor)
        if last == datetime.date.max:
            raise forms.ValidationError("Aktér už má otevřené období zařazení.")
        if last is not None and start <= last:
            raise forms.ValidationError(
                f"Předchozí období skončilo {last:%-d. %-m. %Y}; nové musí začít později "
                "(staré období se nepřepisuje)."
            )
        return start


class CsvImportForm(forms.Form):
    file = forms.FileField(label="Soubor CSV", help_text="UTF-8, oddělovač čárka nebo středník.")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Soubor je větší než 2 MB.")
        return upload


# --- кодбук --------------------------------------------------------------------------------

STANCE_KEYS = [
    ("pro", "Postoj pro"),
    ("contra", "Postoj proti"),
    ("neutral", "Neutrálně"),
    ("absent", "Co narativem není"),
]


class NarrativeForm(forms.ModelForm):
    examples = LinesField(
        label="Příklady (postoj pro)", required=False, help_text="Jedna formulace na řádek."
    )
    counter_examples = LinesField(label="Příklady (postoj proti)", required=False)
    cues = LinesField(
        label="Signální slova",
        required=False,
        help_text=Narrative._meta.get_field("cues").help_text,
    )
    change_note = forms.CharField(
        label="Komentář ke změně",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Proč se definice mění: uloží se do historie kódové knihy.",
    )

    class Meta:
        model = Narrative
        fields = [
            "code",
            "topic",
            "kind",
            "name",
            "definition",
            "examples",
            "counter_examples",
            "cues",
            "attribution",
            "status",
        ]
        widgets = {
            "definition": forms.Textarea(attrs={"rows": 4}),
            "attribution": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        guide = self.instance.stance_guide or {}
        for key, label in STANCE_KEYS:
            self.fields[f"stance_{key}"] = forms.CharField(
                label=label,
                required=False,
                initial=guide.get(key, ""),
                widget=forms.Textarea(attrs={"rows": 2}),
            )
        if self.instance.pk:
            self.fields["code"].disabled = True  # на код ссылаются метки и агрегаты

    def clean_code(self):
        code = self.cleaned_data["code"]
        if code == "novy":
            raise forms.ValidationError("Kód „novy“ je vyhrazen pro adresu nového narativu.")
        return code

    def stance_fields(self):
        return [self[f"stance_{key}"] for key, _ in STANCE_KEYS]

    def save(self, commit=True, *, user=None):
        narrative = super().save(commit=False)
        narrative.stance_guide = {
            key: self.cleaned_data[f"stance_{key}"].strip()
            for key, _ in STANCE_KEYS
            if self.cleaned_data[f"stance_{key}"].strip()
        }
        narrative.save(changed_by=user, change_note=self.cleaned_data["change_note"])
        return narrative


class TopicForm(forms.ModelForm):
    keywords = LinesField(
        label="Klíčová slova", required=False, help_text=Topic._meta.get_field("keywords").help_text
    )

    class Meta:
        model = Topic
        fields = ["code", "name", "description", "keywords"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


# --- настройки -----------------------------------------------------------------------------


class SettingForm(forms.Form):
    """Поле значения выбирается по типу текущего значения; сложные значения — JSON."""

    change_note = forms.CharField(
        label="Komentář ke změně",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Proč se práh mění (kalibrace, pilot): uloží se do historie.",
    )

    def __init__(self, *args, setting, **kwargs):
        super().__init__(*args, **kwargs)
        self.setting = setting
        value = setting.value
        if isinstance(value, bool):
            field = forms.BooleanField(label="Hodnota", required=False)
        elif isinstance(value, int):
            field = forms.IntegerField(label="Hodnota")
        elif isinstance(value, float):
            field = forms.FloatField(label="Hodnota")
        elif isinstance(value, str):
            field = forms.CharField(label="Hodnota", required=False)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            field = forms.CharField(
                label="Hodnota", required=False, help_text="Položky oddělené čárkou."
            )
            value = ", ".join(value)
            self.kind = "list"
        else:
            field = forms.CharField(
                label="Hodnota (JSON)", widget=forms.Textarea(attrs={"rows": 4})
            )
            value = json.dumps(value, ensure_ascii=False, indent=2)
            self.kind = "json"
        self.fields = {"value": field, **self.fields}
        self.fields["value"].initial = value
        self.kind = getattr(self, "kind", "scalar")

    def clean_value(self):
        value = self.cleaned_data["value"]
        if self.kind == "list":
            return [v.strip() for v in value.split(",") if v.strip()]
        if self.kind == "json":
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(
                    f"Neplatný JSON: {exc.msg} (řádek {exc.lineno})."
                ) from exc
        if isinstance(self.setting.value, int) and isinstance(value, float):
            return int(value)
        return value
