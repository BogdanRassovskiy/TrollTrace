"""Strom kódové knihy: návrhy modelu (nadnarativy, zařazení narativů) a jejich schválení."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.narratives.models import Narrative, NarrativeLevel, Topic, TreeDraft
from apps.narratives.tree import label_documents, nest
from apps.narratives.tree_draft import apply, topic_nodes

from . import audit
from .views import ResearcherRequiredMixin


class TreeView(ResearcherRequiredMixin, View):
    """Přehled témat: aktuální strom a návrhy modelu."""

    def get(self, request):
        topics, docs = [], label_documents()
        for topic in Topic.objects.order_by("name"):
            nodes = topic_nodes(topic)
            topics.append(
                {
                    "topic": topic,
                    "rows": nest(nodes, docs),
                    "drafts": topic.tree_drafts.filter(status=TreeDraft.Status.NEW),
                    "can_propose": len(nodes) >= 2,
                }
            )
        return render(request, "panel/tree.html", {"topics": topics})

    def post(self, request):
        from apps.narratives.tasks import propose_tree_task
        from apps.processing import llm

        topic = get_object_or_404(Topic, pk=request.POST.get("topic"))
        if not llm.is_configured():
            messages.error(request, "Chybí OPENROUTER_API_KEY — návrh stromu dělá model.")
            return redirect("panel:tree")
        transaction.on_commit(lambda: propose_tree_task.delay(topic.pk, request.user.pk))
        messages.info(
            request,
            f"Návrh stromu pro „{topic.name}“ se připravuje (desítky sekund). Přijde upozornění.",
        )
        return redirect("panel:tree")


def _parent_choices(draft: TreeDraft, nodes) -> list[tuple[str, str]]:
    choices = [("", "— kořen (bez nadřazeného) —")]
    names = {n.code: n.name for n in nodes}
    for group in draft.proposal.get("groups", []):
        label = names.get(group["from_existing"]) or group["name"]
        prefix = "Nadnarativ (stávající)" if group["from_existing"] else "Nadnarativ (nový)"
        choices.append((group["key"], f"{prefix}: {label}"))
    for node in nodes:
        choices.append((node.code, f"Pod narativ: {node.name}"))
    return choices


def _nest_rows(groups: list[dict], rows: list[dict]) -> list[dict]:
    """Раскладывает строки черновика по выбранным родителям для вложенного просмотра:
    group["items"], row["children"]; возвращает строки без родителя (корни темы).
    Строки, недостижимые из групп и корней (цикл в правке), тоже попадают в корни."""
    by_parent: dict[str, list[dict]] = {}
    for row in rows:
        row["children"] = []
        by_parent.setdefault(row["parent"], []).append(row)
    placed: set[str] = set()

    def take(key: str) -> list[dict]:
        result = []
        for row in by_parent.get(key, []):
            code = row["node"].code
            if code in placed:
                continue
            placed.add(code)
            row["children"] = take(code)
            result.append(row)
        return result

    for group in groups:
        group["items"] = take(group["key"])
    keys = {g["key"] for g in groups} | {r["node"].code for r in rows}
    roots = [r for r in rows if r["parent"] not in keys]
    for row in roots:
        placed.add(row["node"].code)
        row["children"] = take(row["node"].code)
    roots += [r for r in rows if r["node"].code not in placed]
    return roots


class TreeDraftView(ResearcherRequiredMixin, View):
    """Návrh pro jedno téma: úprava skupin a zařazení, pak použití nebo zamítnutí."""

    def context(self, draft, errors=None, data=None):
        nodes = topic_nodes(draft.topic)
        by_code = {n.code: n for n in nodes}
        promoted = {g["from_existing"] for g in draft.proposal.get("groups", [])}
        submitted = data is not None  # v odeslaném formuláři chybí nezaškrtnuté políčko
        data = data or {}
        groups = []
        for group in draft.proposal.get("groups", []):
            key = group["key"]
            groups.append(
                {
                    **group,
                    "existing": by_code.get(group["from_existing"]),
                    "accepted": f"accept-{key}" in data if submitted else True,
                    "name": data.get(f"name-{key}", group["name"]),
                    "definition": data.get(f"definition-{key}", group["definition"]),
                }
            )
        rows = []
        for item in draft.proposal.get("assignments", []):
            node = by_code.get(item["code"])
            if node is None or node.code in promoted:
                continue
            rows.append(
                {
                    "node": node,
                    "reason": item["reason"],
                    "parent": data.get(f"parent-{node.code}", item["parent"]),
                }
            )
        roots = _nest_rows(groups, rows)
        return {
            "draft": draft,
            "groups": groups,
            "rows": rows,
            "roots": roots,
            "choices": _parent_choices(draft, nodes),
            "errors": errors or [],
            "levels": NarrativeLevel,
        }

    def get(self, request, pk):
        draft = get_object_or_404(TreeDraft, pk=pk)
        return render(request, "panel/tree_draft.html", self.context(draft))

    def post(self, request, pk):
        draft = get_object_or_404(TreeDraft, pk=pk, status=TreeDraft.Status.NEW)
        if request.POST.get("action") == "reject":
            draft.status, draft.decided_at, draft.decided_by = (
                TreeDraft.Status.REJECTED, timezone.now(), request.user,
            )  # fmt: skip
            draft.save(update_fields=["status", "decided_at", "decided_by"])
            messages.info(request, "Návrh zamítnut, kódová kniha se nezměnila.")
            return redirect("panel:tree")

        context = self.context(draft, data=request.POST)
        if request.POST.get("action") != "apply":  # odeslání bez tlačítka nic neuloží
            return render(request, "panel/tree_draft.html", context)
        accepted = [g for g in context["groups"] if g["accepted"]]
        keys = {g["key"] for g in accepted}
        errors = [f"Nadnarativ „{g['name'] or g['key']}“ nemá název." for g in accepted
                  if not g["existing"] and not g["name"].strip()]  # fmt: skip
        parents = {}
        for row in context["rows"]:
            ref = row["parent"]
            group_keys = {g["key"] for g in context["groups"]}
            if ref in group_keys and ref not in keys:
                errors.append(f"„{row['node'].name}“ je zařazen pod nepřijatý nadnarativ.")
            parents[row["node"].code] = ref
        # podnarativ smí viset jen pod narativem, který sám není podnarativem
        for code, ref in parents.items():
            if ref in parents and parents[ref] in parents:
                errors.append(f"„{code}“: větev by měla víc než tři úrovně.")
            if ref == code:
                errors.append(f"„{code}“ nemůže být pod sebou samým.")
        if errors:
            return render(request, "panel/tree_draft.html", {**context, "errors": errors})
        groups = [
            {**g, "name": g["name"].strip(), "definition": g["definition"].strip()}
            for g in accepted
        ]
        try:
            result = apply(draft, groups, parents, user=request.user)
        except ValidationError as exc:
            return render(request, "panel/tree_draft.html", {**context, "errors": exc.messages})
        for code in result["created"]:
            audit.log_create(request, Narrative.objects.get(code=code), note="Strom kódové knihy")
        messages.success(
            request,
            f"Strom „{draft.topic.name}“ uložen: nových nadnarativů {len(result['created'])}, "
            f"přesunutých narativů {len(result['moved'])}. Každá změna je nová verze "
            "kódové knihy; štítky dokumentů zůstaly beze změny.",
        )
        return redirect("panel:tree")
