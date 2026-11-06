"""Слой I: институты безопасности, их заявления-события и результаты event study (README §2.1, §9).

Слой I хранится отдельно и в общие агрегаты A–E по умолчанию не входит (actors.ANALYSIS_LAYERS).
Единица анализа — событие (InstitutionalEvent), кодируется вручную; лента сайта органа даёт
только документы-кандидаты, из которых исследователь создаёт события.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.actors.models import Actor, Layer, Source
from apps.core.models import TimeStampedModel
from apps.documents.models import Document
from apps.narratives.models import Narrative, NarrativeVersion, Stance


class Institution(TimeStampedModel):
    """Орган слоя I. Его заявления по ведомственным каналам — заявления института, не министра."""

    class Group(models.TextChoices):
        INTELLIGENCE = "intelligence", "Zpravodajská služba"
        CYBER = "cyber", "Kybernetická bezpečnost"
        MINISTRY = "ministry", "Ministerstvo (resortní kanály)"
        COORDINATION = "coordination", "Koordinace (Úřad vlády, BRS)"
        FORCE = "force", "Bezpečnostní sbor (policie, armáda)"

    name = models.CharField("název", max_length=200, unique=True)
    abbreviation = models.CharField("zkratka", max_length=30, blank=True)
    group = models.CharField("skupina", max_length=20, choices=Group.choices)
    url = models.URLField("web", max_length=500, blank=True)
    actor = models.OneToOneField(
        Actor,
        verbose_name="aktér",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="institution",
        limit_choices_to={"layer": Layer.SECURITY},
        help_text="Aktér vrstvy I, ke kterému se přiřazují dokumenty z kanálů orgánu.",
    )
    sources = models.ManyToManyField(
        Source,
        verbose_name="kanály a zdroje",
        blank=True,
        related_name="institutions",
        limit_choices_to={"layer": Layer.SECURITY},
    )
    officials = models.ManyToManyField(
        Actor,
        verbose_name="profesní vedoucí",
        blank=True,
        related_name="led_institutions",
        limit_choices_to={"layer": Layer.SECURITY, "actor_type": Actor.Type.OFFICIAL},
        help_text="Ředitelé, náčelníci: vrstva I v jakémkoli kanálu (README §2.1).",
    )
    minister = models.ForeignKey(
        Actor,
        verbose_name="ministr",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="security_ministries",
        limit_choices_to={"security_portfolio": True},
        help_text="Politik s příznakem bezpečnostního resortu; do vrstvy I patří jen "
        "v resortním formátu.",
    )
    notes = models.TextField("poznámky", blank=True)

    class Meta:
        verbose_name = "instituce"
        verbose_name_plural = "instituce"
        ordering = ["group", "name"]

    def __str__(self):
        return self.abbreviation or self.name


class InstitutionalEvent(TimeStampedModel):
    """Veřejné prohlášení orgánu jako událost na časové ose (H4). Kóduje se ručně."""

    class SpeakerRole(models.TextChoices):
        INSTITUTION = "institution", "Instituce (bez jmenovaného mluvčího)"
        DIRECTOR = "director", "Profesní vedoucí (ředitel, náčelník)"
        SPOKESPERSON = "spokesperson", "Mluvčí"
        MINISTER = "minister", "Ministr v resortní roli"

    class Channel(models.TextChoices):
        WEBSITE = "website", "Web orgánu"
        PRESS_RELEASE = "press_release", "Tisková zpráva"
        BRIEFING = "briefing", "Tisková konference / brífink"
        MEDIA = "media", "Rozhovor nebo výrok v médiích"
        SOCIAL = "social", "Oficiální účet na sociální síti"
        REPORT = "report", "Výroční nebo tematická zpráva"
        OTHER = "other", "Jiné"

    class EventType(models.TextChoices):
        ANNUAL_REPORT = "annual_report", "Výroční zpráva"
        WARNING = "warning", "Tematické varování"
        PRESS_RELEASE = "press_release", "Tisková zpráva"
        BRIEFING = "briefing", "Brífink"
        INTERVIEW = "interview", "Rozhovor"
        OTHER = "other", "Jiné"

    class Specificity(models.TextChoices):
        SPECIFIC = "specific", "Konkrétní narativ z kódové knihy"
        GENERAL = "general", "Obecná formulace („ruská propaganda“)"

    institution = models.ForeignKey(
        Institution, verbose_name="instituce", on_delete=models.PROTECT, related_name="events"
    )
    speaker = models.ForeignKey(
        Actor,
        verbose_name="mluvčí (aktér)",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="institutional_events",
    )
    speaker_name = models.CharField(
        "mluvčí (jméno)", max_length=200, blank=True, help_text="Pokud mluvčí není mezi aktéry."
    )
    speaker_role = models.CharField(
        "role mluvčího",
        max_length=20,
        choices=SpeakerRole.choices,
        default=SpeakerRole.INSTITUTION,
        help_text="Ministr patří do vrstvy I jen v resortním formátu (README §2.1).",
    )
    channel = models.CharField("kanál", max_length=20, choices=Channel.choices)
    date = models.DateField("datum", db_index=True)
    event_type = models.CharField("typ", max_length=20, choices=EventType.choices)
    title = models.CharField("název", max_length=300)
    summary = models.TextField("shrnutí", blank=True)
    specificity = models.CharField("konkrétnost", max_length=10, choices=Specificity.choices)
    narrative = models.ForeignKey(
        Narrative,
        verbose_name="narativ",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="institutional_events",
    )
    narrative_version = models.ForeignKey(
        NarrativeVersion,
        verbose_name="verze kódové knihy",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="institutional_events",
        help_text="Doplní se aktuální verzí narativu při uložení.",
    )
    stance = models.CharField(
        "postoj k narativu", max_length=10, choices=Stance.choices, blank=True
    )
    source_url = models.URLField("odkaz na prvotní zdroj", max_length=1000)
    document = models.ForeignKey(
        Document,
        verbose_name="související dokument",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="institutional_events",
    )
    coded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="kódoval(a)",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    coded_at = models.DateTimeField("zakódováno", null=True, blank=True)
    notes = models.TextField("poznámky ke kódování", blank=True)

    class Meta:
        verbose_name = "institucionální událost"
        verbose_name_plural = "institucionální události"
        ordering = ["-date"]
        indexes = [models.Index(fields=["narrative", "date"])]
        constraints = [
            models.CheckConstraint(
                condition=(Q(specificity="specific") & Q(narrative__isnull=False))
                | (Q(specificity="general") & Q(narrative__isnull=True)),
                name="event_specific_has_narrative",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.institution}: {self.title}"

    def clean(self):
        if self.specificity == self.Specificity.SPECIFIC and self.narrative_id is None:
            raise ValidationError({"narrative": "Konkrétní událost musí mít narativ."})
        if self.specificity == self.Specificity.GENERAL and self.narrative_id is not None:
            raise ValidationError(
                {"narrative": "Obecná formulace se s narativem nespojuje (analyzuje se zvlášť)."}
            )
        if self.narrative_id is not None and not self.stance:
            raise ValidationError({"stance": "U konkrétního narativu je potřeba postoj."})
        if (
            self.narrative_version_id
            and self.narrative_id
            and self.narrative_version.narrative_id != self.narrative_id
        ):
            raise ValidationError("Verze kódové knihy patří k jinému narativu.")

    def save(self, *args, **kwargs):
        if self.narrative_id is None:
            self.narrative_version = None
        elif self.narrative_version_id is None:
            self.narrative_version = self.narrative.latest_version
        super().save(*args, **kwargs)


class EventWindowResult(models.Model):
    """Výsledek event study: událost × narativ × vrstva × okno, metriky před/po a kontrola."""

    event = models.ForeignKey(
        InstitutionalEvent,
        verbose_name="událost",
        on_delete=models.CASCADE,
        related_name="window_results",
    )
    narrative = models.ForeignKey(
        Narrative, verbose_name="narativ", on_delete=models.PROTECT, related_name="+"
    )
    layer = models.CharField("vrstva", max_length=1, choices=Layer.choices)
    days_before = models.PositiveSmallIntegerField("okno před (dní)")
    days_after = models.PositiveSmallIntegerField("okno po (dní)")
    metrics_before = models.JSONField("metriky před", default=dict)
    metrics_after = models.JSONField("metriky po", default=dict)
    control_narratives = models.ManyToManyField(
        Narrative, verbose_name="kontrolní narativy", blank=True, related_name="+"
    )
    control_metrics = models.JSONField(
        "metriky kontroly", default=dict, blank=True, help_text="Před/po pro kontrolní narativy."
    )
    settings_version = models.CharField(
        "verze nastavení", max_length=100, help_text="Okna, metriky a pravidla výběru kontroly."
    )
    computed_at = models.DateTimeField("spočteno", auto_now_add=True)

    class Meta:
        verbose_name = "výsledek event study"
        verbose_name_plural = "výsledky event study"
        ordering = ["-computed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "narrative", "layer", "days_before", "days_after",
                        "settings_version"],
                name="event_window_unique",
            ),
        ]  # fmt: skip

    def __str__(self):
        window = f"−{self.days_before}/+{self.days_after}"
        return f"{self.event} × {self.narrative} × {self.get_layer_display()} ({window})"
