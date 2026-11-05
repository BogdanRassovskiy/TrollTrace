"""Черновик дерева кодбука от модели: нарративы темы → nadnarativy (README §3).

Модель только предлагает: группы (новые nadnarativy или повышение широкого нарратива
до nadnarativu) и место каждого нарратива. Применяет исследователь в панели, поправив
названия и родителей; каждое изменение — версия кодбука с комментарием.
"""

import json

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.text import slugify

from apps.panel.defaults import get_setting

from .models import Label, Narrative, NarrativeLevel, NarrativeStatus, Topic, TreeDraft
from .tree import move

MAX_TOKENS = 8000  # návrh pro téma je krátký JSON

SYSTEM = """You organise a research codebook of disinformation narratives into a strict tree.
Levels: nadnarativ (broad umbrella claim) → narativ (specific claim) → podnarativ (a narrower
variant of one narativ). Every node has at most one parent, one level up, in the same topic.

Rules:
- Group narratives by the claim they make, not by vocabulary or actors mentioned.
- A nadnarativ is itself a claim (e.g. "Ukrajinský stát je zločinný"), not a topic label.
- If an existing narrative is already broad enough to be an umbrella, reuse it as the group
  (from_existing = its code) instead of inventing a near-duplicate.
- Put a narrative under another narrative (as podnarativ) only if it is clearly a narrower
  case of that narrative's claim. Otherwise put it under a group.
- The goal is a usable coarse grouping, not fine semantics: place every narrative under
  some group if it fits even loosely. Leave a narrative as root (parent = "") only when it
  fits no group at all and no sensible umbrella can be named for it.
- A group with one member is fine when its umbrella claim is clear (more will come).
- Typically 3–6 groups per topic. Names and definitions in Czech, short and neutral in tone
  (describe what the claim says, do not endorse it).
- stance_guide (Czech, one sentence each): pro = the text asserts the umbrella claim;
  contra = the text explicitly refutes it; neutral = reports it without taking a side;
  absent = what is NOT this nadnarativ.
Return every given narrative code exactly once in assignments."""


def _schema() -> dict:
    guide = {
        "type": "object",
        "additionalProperties": False,
        "required": ["pro", "contra", "neutral", "absent"],
        "properties": {k: {"type": "string"} for k in ("pro", "contra", "neutral", "absent")},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["groups", "assignments"],
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "name", "definition", "from_existing", "stance_guide"],
                    "properties": {
                        "key": {"type": "string"},
                        "name": {"type": "string"},
                        "definition": {"type": "string"},
                        "from_existing": {"type": "string"},
                        "stance_guide": guide,
                    },
                },
            },
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "parent", "reason"],
                    "properties": {
                        "code": {"type": "string"},
                        "parent": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }


def topic_nodes(topic: Topic) -> list[Narrative]:
    return list(
        Narrative.objects.filter(topic=topic)
        .exclude(status=NarrativeStatus.ARCHIVED)
        .order_by("code")
    )


def _prompt(topic: Topic, nodes: list[Narrative]) -> str:
    counts = dict(
        Label.objects.current()
        .filter(narrative__in=nodes)
        .values_list("narrative")
        .annotate(n=Count("pk"))
        .order_by()
    )
    items = [
        {
            "code": n.code,
            "name": n.name,
            "definition": n.definition[:600],
            "status": n.status,
            "labels": counts.get(n.pk, 0),
        }
        for n in nodes
    ]
    return (
        f"Topic: {topic.name}\n{topic.description}\n\n"
        f"Narratives ({len(items)}):\n{json.dumps(items, ensure_ascii=False, indent=1)}"
    )


def propose(topic: Topic) -> TreeDraft:
    """Запрос к модели; результат — TreeDraft со статусом «čeká na rozhodnutí»."""
    from apps.processing import llm

    nodes = topic_nodes(topic)
    if len(nodes) < 2:
        raise ValueError(f"Téma „{topic}“ má méně než dva narativy — není co seskupovat.")
    model = str(get_setting("labeling-model"))
    completion = llm.complete_json(
        model, SYSTEM, _prompt(topic, nodes), _schema(), max_tokens=MAX_TOKENS
    )
    proposal = normalize(completion.data, nodes)
    return TreeDraft.objects.create(
        topic=topic, proposal=proposal, model_name=completion.model, cost_usd=completion.cost_usd
    )


def normalize(data: dict, nodes: list[Narrative]) -> dict:
    """Чистит ответ модели: неизвестные коды убираются, пропущенные нарративы остаются
    корнями, родитель-нарратив допускается только для узла уровня «narativ»."""
    codes = {n.code for n in nodes}
    groups, keys = [], set()
    for group in data.get("groups") or []:
        key = str(group.get("key") or "").strip()
        if not key or key in keys or key in codes:
            continue
        existing = str(group.get("from_existing") or "")
        keys.add(key)
        groups.append(
            {
                "key": key,
                "name": str(group.get("name") or "")[:300],
                "definition": str(group.get("definition") or ""),
                "from_existing": existing if existing in codes else "",
                "stance_guide": {
                    k: str(v)
                    for k, v in (group.get("stance_guide") or {}).items()
                    if k in ("pro", "contra", "neutral", "absent") and v
                },
            }  # fmt: skip
        )
    promoted = {g["from_existing"]: g["key"] for g in groups if g["from_existing"]}
    assignments, seen = [], set()
    for item in data.get("assignments") or []:
        code = str(item.get("code") or "")
        if code not in codes or code in seen or code in promoted:
            continue
        seen.add(code)
        parent = str(item.get("parent") or "")
        parent = promoted.get(parent, parent)  # «pod kód povýšeného» = pod jeho skupinu
        if parent not in keys and (parent not in codes or parent == code):
            parent = ""
        assignments.append(
            {"code": code, "parent": parent, "reason": str(item.get("reason") or "")}
        )
    for code in sorted(codes - seen - set(promoted)):
        assignments.append({"code": code, "parent": "", "reason": "Model narativ nezařadil."})
    # podnarativ jen pod narativem, který sám visí pod skupinou nebo je kořenem (max. 3 úrovně)
    by_code = {a["code"]: a for a in assignments}
    for item in assignments:
        if item["parent"] in by_code and by_code[item["parent"]]["parent"] in by_code:
            item["parent"] = by_code[item["parent"]]["parent"]
    return {"groups": groups, "assignments": assignments}


def _code(name: str) -> str:
    base = f"n-{slugify(name)}"[:46].rstrip("-")
    code, i = base, 2
    while Narrative.objects.filter(code=code).exists():
        code, i = f"{base}-{i}", i + 1
    return code


@transaction.atomic
def apply(draft: TreeDraft, groups: list[dict], parents: dict[str, str], *, user) -> dict:
    """Применяет черновик в редакции исследователя.

    groups — принятые группы [{key, name, definition, from_existing, stance_guide}];
    parents — {код нарратива: ключ группы | код нарратива | ""}.
    Возвращает {"created": [...], "moved": [...]}.
    """
    note = f"Strom kódové knihy: návrh č. {draft.pk} schválen výzkumníkem"
    by_key: dict[str, Narrative] = {}
    created = []
    for group in groups:
        if group.get("from_existing"):
            node = Narrative.objects.get(code=group["from_existing"], topic=draft.topic)
            node.parent, node.level = None, NarrativeLevel.META
            node.full_clean()
            node.save(changed_by=user, change_note=f"{note}: povýšen na nadnarativ")
        else:
            node = Narrative(
                code=_code(group["name"]),
                topic=draft.topic,
                level=NarrativeLevel.META,
                name=group["name"],
                definition=group["definition"],
                stance_guide=group.get("stance_guide") or {},
                attribution="Nadnarativ ze seskupení narativů kódové knihy (návrh modelu, "
                "schválil výzkumník).",
                status=NarrativeStatus.ACTIVE,
            )
            node.full_clean()
            node.save(changed_by=user, change_note=f"{note}: nový nadnarativ")
            created.append(node.code)
        by_key[group["key"]] = node

    nodes = {n.code: n for n in topic_nodes(draft.topic)}
    # сначала narativy (под группы и в корень), потом podnarativy — родитель уже на месте
    order = sorted(parents.items(), key=lambda kv: kv[1] in nodes)
    moved = []
    for code, parent_ref in order:
        node = nodes.get(code)
        if node is None or code in {g.get("from_existing") for g in groups}:
            continue
        if parent_ref in by_key:
            parent = by_key[parent_ref]
        elif parent_ref in nodes:
            parent = Narrative.objects.get(pk=nodes[parent_ref].pk)
        else:
            parent = None
        level = NarrativeLevel.NARRATIVE if parent is None else parent.level + 1
        if node.parent_id == (parent.pk if parent else None) and node.level == level:
            continue
        move(node, parent, level=level, user=user, note=note)
        moved.append(code)
    draft.status, draft.decided_at, draft.decided_by = (
        TreeDraft.Status.APPLIED, timezone.now(), user,
    )  # fmt: skip
    draft.save(update_fields=["status", "decided_at", "decided_by"])
    return {"created": created, "moved": moved}
