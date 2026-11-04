"""Агрегатная таблица «актор/группа × нарратив × неделя» (README §4 шаг 8, §5)."""

from django.db import models
from django.db.models import F, Q

from apps.actors.models import Actor
from apps.narratives.models import Narrative


class WeeklyAggregate(models.Model):
    """Строка агрегата. Графики строятся только отсюда, не из сырых постов (README §7.1).

    Строка относится либо к актору, либо к группе (group_key вида «party:SPD», «layer:B»).
    min_quality — порог оценки качества документов, на которых посчитан агрегат: основной
    расчёт ≥ 3, проверки устойчивости — на всех (1) и только на ≥ 4 (README §4.3).
    """

    actor = models.ForeignKey(
        Actor,
        verbose_name="aktér",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="weekly_aggregates",
    )
    group_key = models.CharField("skupina", max_length=100, blank=True)
    narrative = models.ForeignKey(
        Narrative,
        verbose_name="narativ",
        on_delete=models.CASCADE,
        related_name="weekly_aggregates",
    )
    week = models.DateField("týden (pondělí)")
    min_quality = models.PositiveSmallIntegerField("práh kvality", default=3)

    n_documents = models.PositiveIntegerField(
        "dokumentů k narativu", help_text="Narativ přítomen v jakémkoli postoji."
    )
    n_pro = models.PositiveIntegerField("z toho pro", default=0)
    n_contra = models.PositiveIntegerField("z toho proti", default=0)
    n_neutral = models.PositiveIntegerField("z toho neutrálně", default=0)
    n_total = models.PositiveIntegerField("dokumentů celkem (jmenovatel)")
    share = models.FloatField("podíl")
    share_smoothed = models.FloatField(
        "vyhlazený podíl",
        null=True,
        blank=True,
        help_text="Bayesovské vyhlazení k podílu vrstvy v témže týdnu (pro málo píšící aktéry).",
    )
    z_score = models.FloatField(
        "z-skóre", null=True, blank=True, help_text="Vůči vlastní historii řady (výkyvy)."
    )
    engagement = models.JSONField(
        "odezva", default=dict, blank=True, help_text="Odděleně od podílu: poptávka ≠ nabídka."
    )
    computed_at = models.DateTimeField("přepočteno", auto_now=True)

    class Meta:
        verbose_name = "týdenní agregát"
        verbose_name_plural = "týdenní agregáty"
        ordering = ["narrative", "week"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(actor__isnull=False) & Q(group_key=""))
                | (Q(actor__isnull=True) & ~Q(group_key="")),
                name="aggregate_actor_xor_group",
            ),
            models.CheckConstraint(
                condition=Q(week__iso_week_day=1), name="aggregate_week_is_monday"
            ),
            models.CheckConstraint(
                condition=Q(n_documents__lte=F("n_total")), name="aggregate_n_le_total"
            ),
            models.CheckConstraint(
                condition=Q(
                    n_pro__lte=F("n_documents"),
                    n_contra__lte=F("n_documents"),
                    n_neutral__lte=F("n_documents"),
                ),
                name="aggregate_stances_le_n",
            ),  # fmt: skip
            models.CheckConstraint(
                condition=Q(share__gte=0, share__lte=1), name="aggregate_share_0_1"
            ),
            models.CheckConstraint(
                condition=Q(min_quality__gte=1, min_quality__lte=5),
                name="aggregate_min_quality_1_5",
            ),
            models.UniqueConstraint(
                fields=["actor", "narrative", "week", "min_quality"],
                condition=Q(actor__isnull=False),
                name="aggregate_unique_actor_week",
            ),
            models.UniqueConstraint(
                fields=["group_key", "narrative", "week", "min_quality"],
                condition=Q(actor__isnull=True),
                name="aggregate_unique_group_week",
            ),
        ]
        indexes = [
            # Режим сравнения: все акторы по одному нарративу одним запросом.
            models.Index(fields=["narrative", "min_quality", "week"], name="aggregate_compare"),
        ]

    def __str__(self):
        who = self.actor or self.group_key
        return f"{who} × {self.narrative.code} × {self.week}: {self.n_documents}/{self.n_total}"
