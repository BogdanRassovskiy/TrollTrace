"""Согласие разметчиков и сравнение с автоматикой (README §4, шаг 9).

- Krippendorff's alpha (номинальная шкала): допускает пропуски и любое число разметчиков.
- Эталон: значение, в котором сошлись все (не меньше двух) разметчики, или решение
  исследователя по спору. Элементы без эталона в precision/recall не входят.
- Precision/recall считаются и по выборке, и с весами страт (размер страты в популяции /
  число отобранных): выборка стратифицирована, без весов редкие позитивы завышены.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db.models import Q

from apps.documents.models import ReuseLink
from apps.narratives.models import LabelDecision

from .models import Adjudication, Annotation, ReuseVerdict, Review, Sample, Value

# --- Krippendorff's alpha ------------------------------------------------------------------


def krippendorff_alpha(units: list[list]) -> float | None:
    """Номинальная alpha. units — значения разных разметчиков по каждому элементу.

    Элементы, где меньше двух значений, не участвуют. None — нечего сравнивать; 1.0 — когда
    во всех данных одна категория (ожидаемое расхождение нулевое, наблюдаемое тоже).
    """
    coincidence: Counter = Counter()
    for values in units:
        m = len(values)
        if m < 2:
            continue
        counts = Counter(values)
        for c, n_c in counts.items():
            for k, n_k in counts.items():
                pairs = n_c * (n_k - 1) if c == k else n_c * n_k
                coincidence[(c, k)] += pairs / (m - 1)
    n = sum(coincidence.values())
    if n <= 1:
        return None
    marginals: Counter = Counter()
    for (c, _k), value in coincidence.items():
        marginals[c] += value
    observed = sum(v for (c, k), v in coincidence.items() if c != k)
    expected = sum(marginals[c] * marginals[k] for c in marginals for k in marginals if c != k)
    if expected == 0:
        return 1.0 if observed == 0 else None
    return 1 - (n - 1) * observed / expected


def percent_agreement(units: list[list]) -> float | None:
    """Доля элементов (с ≥ 2 значениями), где все разметчики совпали."""
    rated = [values for values in units if len(values) >= 2]
    if not rated:
        return None
    return sum(1 for values in rated if len(set(values)) == 1) / len(rated)


def wilson(successes: float, total: float, z: float = 1.96) -> tuple[float, float] | None:
    """95% интервал Уилсона для доли (по невзвешенным числам)."""
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


# --- сбор разметки -------------------------------------------------------------------------


def _key(narrative_version_id):
    return narrative_version_id or 0


def collect(sample: Sample) -> dict:
    """{(item_id, version_id | 0): {annotator_id: value}} без «нельзя оценить»."""
    values: dict = defaultdict(dict)
    if sample.kind == Sample.Kind.REUSE:
        reviews = Review.objects.filter(item__sample=sample, unclear=False).exclude(
            reuse_verdict=""
        )
        for item_id, annotator_id, verdict in reviews.values_list(
            "item_id", "annotator_id", "reuse_verdict"
        ):
            values[(item_id, 0)][annotator_id] = verdict
        return values
    rows = Annotation.objects.filter(
        review__item__sample=sample, review__unclear=False
    ).values_list("review__item_id", "narrative_version_id", "review__annotator_id", "value")
    for item_id, version_id, annotator_id, value in rows:
        values[(item_id, version_id)][annotator_id] = value
    return values


def gold(sample: Sample, values: dict | None = None) -> dict:
    """{(item_id, version_id | 0): (value, "shoda" | "rozhodnutí")}."""
    values = collect(sample) if values is None else values
    result = {}
    for key, by_annotator in values.items():
        distinct = set(by_annotator.values())
        if len(by_annotator) >= 2 and len(distinct) == 1:
            result[key] = (distinct.pop(), "shoda")
    for adj in Adjudication.objects.filter(item__sample=sample):
        result[(adj.item_id, _key(adj.narrative_version_id))] = (adj.value, "rozhodnutí")
    return result


def disagreements(sample: Sample, values: dict | None = None) -> list[tuple]:
    """Пары (item_id, version_id | 0), где разметчики разошлись и решения ещё нет."""
    values = collect(sample) if values is None else values
    decided = {
        (a.item_id, _key(a.narrative_version_id))
        for a in Adjudication.objects.filter(item__sample=sample)
    }
    return sorted(
        key
        for key, by_annotator in values.items()
        if len(set(by_annotator.values())) > 1 and key not in decided
    )


# --- предсказания автоматики ---------------------------------------------------------------


def model_predictions(sample: Sample, model_name: str = "") -> dict:
    """{(item_id, version_id): value | None} — что сказал бы конвейер.

    Нет решения модели → «нарратива нет» (документ не прошёл фильтр релевантности или отбор
    кандидатов: это тоже ошибка или заслуга автоматики). Решение ждёт модель → None, элемент
    в метрики не входит.
    """
    items = dict(sample.items.values_list("document_id", "pk"))
    versions = list(sample.narrative_versions.values_list("pk", flat=True))
    predictions = {(item, version): Value.ABSENT for item in items.values() for version in versions}
    decisions = LabelDecision.objects.filter(
        document_id__in=items, narrative_version_id__in=versions, error=""
    ).order_by("created_at")
    if model_name:
        decisions = decisions.filter(Q(model_name=model_name) | Q(present__isnull=True))
    for document_id, version_id, present, stance in decisions.values_list(
        "document_id", "narrative_version_id", "present", "stance"
    ):
        key = (items[document_id], version_id)
        if present is None:
            predictions[key] = None
        else:
            predictions[key] = (stance or Value.NEUTRAL) if present else Value.ABSENT
    return predictions


def reuse_predictions(sample: Sample) -> dict:
    """{(item_id, 0): verdict}: автоматически найденная связь пары и её тип."""
    items = list(sample.items.values_list("pk", "source_document_id", "target_document_id"))
    links = {}
    pairs = {(s, t) for _pk, s, t in items}
    sources = {s for s, _t in pairs}
    for source, target, link_type in (
        ReuseLink.objects.filter(source_document_id__in=sources)
        .exclude(method=ReuseLink.Method.MANUAL)
        .values_list("source_document_id", "target_document_id", "link_type")
    ):
        # дословная связь важнее парафразы, если найдены обе
        if (source, target) in pairs and links.get((source, target)) != ReuseVerdict.VERBATIM:
            links[(source, target)] = link_type
    return {(pk, 0): links.get((s, t), ReuseVerdict.NONE) for pk, s, t in items}


# --- метрики -------------------------------------------------------------------------------


@dataclass
class Confusion:
    tp: float = 0
    fp: float = 0
    fn: float = 0
    tn: float = 0

    def add(self, truth: bool, predicted: bool, weight: float = 1.0):
        if truth and predicted:
            self.tp += weight
        elif predicted:
            self.fp += weight
        elif truth:
            self.fn += weight
        else:
            self.tn += weight

    def as_dict(self, interval: bool = False) -> dict:
        precision = self.tp / (self.tp + self.fp) if self.tp + self.fp else None
        recall = self.tp / (self.tp + self.fn) if self.tp + self.fn else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else None
        )
        result = {
            "tp": round(self.tp, 2),
            "fp": round(self.fp, 2),
            "fn": round(self.fn, 2),
            "tn": round(self.tn, 2),
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
        }
        if interval:
            result["precision_ci"] = wilson(self.tp, self.tp + self.fp)
            result["recall_ci"] = wilson(self.tp, self.tp + self.fn)
        return result


def _round(value):
    return None if value is None else round(value, 4)


@dataclass
class Evaluation:
    """Результат по одному нарративу (или по заимствованиям целиком)."""

    label: str
    alpha: float | None = None
    alpha_presence: float | None = None
    agreement: float | None = None
    rated: int = 0
    gold: int = 0
    pending_model: int = 0
    raw: Confusion = field(default_factory=Confusion)
    weighted: Confusion = field(default_factory=Confusion)
    matrix: dict = field(default_factory=dict)
    stance_correct: int = 0
    stance_total: int = 0
    by_stratum: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "alpha": _round(self.alpha),
            "alpha_presence": _round(self.alpha_presence),
            "percent_agreement": _round(self.agreement),
            "items_with_2_annotations": self.rated,
            "gold_items": self.gold,
            "pending_model": self.pending_model,
            "presence": self.raw.as_dict(interval=True),
            "presence_weighted": self.weighted.as_dict(),
            "stance_accuracy": _round(self.stance_correct / self.stance_total)
            if self.stance_total
            else None,
            "stance_total": self.stance_total,
            "confusion": self.matrix,
            "by_stratum": {k: v.as_dict() for k, v in sorted(self.by_stratum.items())},
        }


def _evaluate(label, keys, values, golds, predictions, items, negative) -> Evaluation:
    """Общий расчёт: keys — (item_id, version_id) одного нарратива или заимствований."""
    result = Evaluation(label=label)
    units = [list(values[k].values()) for k in keys if k in values]
    result.alpha = krippendorff_alpha(units)
    result.alpha_presence = krippendorff_alpha([[v != negative for v in unit] for unit in units])
    result.agreement = percent_agreement(units)
    result.rated = sum(1 for unit in units if len(unit) >= 2)
    matrix: dict = defaultdict(Counter)
    for key in keys:
        if key not in golds:
            continue
        predicted = predictions.get(key, negative)
        if predicted is None:
            result.pending_model += 1
            continue
        truth = golds[key][0]
        result.gold += 1
        stratum, weight = items[key[0]]
        is_true, is_predicted = truth != negative, predicted != negative
        result.raw.add(is_true, is_predicted)
        result.weighted.add(is_true, is_predicted, weight if weight is not None else 1.0)
        result.by_stratum.setdefault(stratum, Confusion()).add(is_true, is_predicted)
        matrix[truth][predicted] += 1
        if is_true and is_predicted:
            result.stance_total += 1
            result.stance_correct += truth == predicted
    result.matrix = {truth: dict(row) for truth, row in matrix.items()}
    return result


def evaluate(sample: Sample, model_name: str = "") -> dict:
    """Полный отчёт по выборке: согласие и precision/recall по каждому нарративу."""
    values = collect(sample)
    golds = gold(sample, values)
    items = {
        pk: (stratum, weight)
        for pk, stratum, weight in sample.items.values_list("pk", "stratum", "weight")
    }
    annotators = sorted(
        {a for by in values.values() for a in by}
    )  # не имена: в отчёт идёт только число разметчиков
    report = {
        "sample": sample.pk,
        "kind": sample.kind,
        "items": len(items),
        "annotators": len(annotators),
        "model": model_name or "všechny modely",
        "disagreements_open": len(disagreements(sample, values)),
        "results": [],
    }
    if sample.kind == Sample.Kind.REUSE:
        keys = [(pk, 0) for pk in items]
        evaluation = _evaluate(
            "Převzetí", keys, values, golds, reuse_predictions(sample), items, ReuseVerdict.NONE
        )
        report["results"].append(evaluation.as_dict())
        return report
    predictions = model_predictions(sample, model_name)
    for version in sample.narrative_versions.select_related("narrative").order_by(
        "narrative__code"
    ):
        keys = [(pk, version.pk) for pk in items]
        evaluation = _evaluate(
            f"{version.narrative.code} v{version.version}",
            keys,
            values,
            golds,
            predictions,
            items,
            Value.ABSENT,
        )
        entry = evaluation.as_dict()
        entry["narrative"] = version.narrative.code
        entry["version"] = version.version
        report["results"].append(entry)
    return report
