"""Дерево кодбука: nadnarativ → narativ → podnarativ под темой (README §3).

Строгое дерево: у узла один родитель уровнем выше. Размечается самый конкретный подходящий
узел; верхние уровни считаются сложением поддерева, документ в узле — один раз.
"""

from django.db import transaction

from .models import Label, Narrative, NarrativeStatus


def children_map(narratives=None) -> dict[int | None, list[Narrative]]:
    """{id родителя (None — корни): [дочерние узлы]} для неархивных нарративов."""
    narratives = (
        narratives
        if narratives is not None
        else Narrative.objects.exclude(status=NarrativeStatus.ARCHIVED).select_related("topic")
    )
    result: dict[int | None, list[Narrative]] = {}
    for narrative in sorted(narratives, key=lambda n: (n.level, n.name)):
        result.setdefault(narrative.parent_id, []).append(narrative)
    return result


def subtree_ids(narratives=None) -> dict[int, set[int]]:
    """{id узла: id всех узлов его поддерева, включая сам узел}."""
    children = children_map(narratives)
    result: dict[int, set[int]] = {}

    def walk(node: Narrative) -> set[int]:
        ids = {node.pk}
        for child in children.get(node.pk, []):
            ids |= walk(child)
        result[node.pk] = ids
        return ids

    known = {n.pk for nodes in children.values() for n in nodes}
    for parent_id, nodes in children.items():
        if parent_id is None or parent_id not in known:  # корни (родитель в архиве — тоже)
            for node in nodes:
                walk(node)
    return result


def flatten(topic_narratives: list[Narrative]) -> list[tuple[Narrative, int]]:
    """Узлы темы в порядке обхода дерева с глубиной (для таблиц кодбука)."""
    ids = {n.pk for n in topic_narratives}
    children: dict[int | None, list[Narrative]] = {}
    for narrative in sorted(topic_narratives, key=lambda n: (n.status, n.name)):
        parent = narrative.parent_id if narrative.parent_id in ids else None
        children.setdefault(parent, []).append(narrative)
    result: list[tuple[Narrative, int]] = []

    def walk(node: Narrative, depth: int):
        result.append((node, depth))
        for child in children.get(node.pk, []):
            walk(child, depth + 1)

    for root in sorted(children.get(None, []), key=lambda n: (n.level, n.status, n.name)):
        walk(root, 0)
    return result


def path(narrative: Narrative) -> list[Narrative]:
    """Путь от корня ветки до узла."""
    nodes = [narrative]
    while nodes[0].parent_id is not None:
        nodes.insert(0, nodes[0].parent)
    return nodes


def move(narrative: Narrative, parent: Narrative | None, *, level: int | None = None,
         user=None, note: str = "") -> Narrative:  # fmt: skip
    """Перенос узла (новая версия кодбука). Разметку не трогает."""
    narrative.parent = parent
    if parent is not None:
        narrative.topic = parent.topic
        narrative.level = parent.level + 1
    elif level is not None:
        narrative.level = level
    narrative.full_clean()
    narrative.save(changed_by=user, change_note=note or "Přesun ve stromu kódové knihy")
    return narrative


@transaction.atomic
def merge(duplicate: Narrative, keeper: Narrative, *, user=None, note: str = "") -> int:
    """Сливает дубль в основной нарратив. Возвращает число перенесённых меток.

    Метки дубля копируются на текущую версию основного (с пометкой merged_from в details),
    если у документа ещё нет метки основного нарратива от того же источника (модель / человек).
    Сами метки дубля остаются в истории; дубль уходит в архив с merged_into.
    Дочерние узлы дубля переходят к основному.
    """
    if duplicate.pk == keeper.pk:
        raise ValueError("Narativ nelze sloučit sám se sebou.")
    note = note or f"Sloučen do {keeper.code}"
    version = keeper.latest_version
    existing = set(
        Label.objects.current().filter(narrative=keeper).values_list("document_id", "origin")
    )
    moved = []
    labels = Label.objects.current().filter(narrative=duplicate)
    for label in labels.select_related("narrative", "narrative_version"):
        if (label.document_id, label.origin) in existing:
            continue
        existing.add((label.document_id, label.origin))
        moved.append(
            Label(
                document_id=label.document_id,
                narrative=keeper,
                narrative_version=version,
                stance=label.stance,
                origin=label.origin,
                confidence=label.confidence,
                model_name=label.model_name,
                annotator_id=label.annotator_id,
                details={**label.details, "merged_from": origin_of(label)},
            )
        )
    Label.objects.bulk_create(moved)
    for child in duplicate.children.all():
        move(child, keeper, user=user, note=note)
    duplicate.status = NarrativeStatus.ARCHIVED
    duplicate.merged_into = keeper
    duplicate.save(changed_by=user, change_note=note)
    return len(moved)


def origin_of(label: Label) -> str:
    return f"{label.narrative.code} v{label.narrative_version.version}"
