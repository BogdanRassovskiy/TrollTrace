"""Strom kódové knihy: nadnarativ → narativ → podnarativ, sloučení duplikátů, návrh modelu."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.narratives import tree, tree_draft
from apps.narratives.models import Label, Narrative, NarrativeLevel, NarrativeStatus, TreeDraft
from apps.processing import llm

from .factories import DocumentFactory, LabelFactory, NarrativeFactory, TopicFactory

pytestmark = pytest.mark.django_db
META, NARR, SUB = NarrativeLevel.META, NarrativeLevel.NARRATIVE, NarrativeLevel.SUB


@pytest.fixture
def topic():
    return TopicFactory(code="ukrajina", name="Válka na Ukrajině")


def node(topic, code, level=NARR, parent=None, **kwargs):
    return NarrativeFactory(code=code, topic=topic, level=level, parent=parent, **kwargs)


# --- model -------------------------------------------------------------------------------


def test_tree_rules_level_topic_and_root(topic):
    meta = node(topic, "zlocinny-stat", META)
    child = node(topic, "valecne-zlociny", NARR, meta)
    sub = node(topic, "azov", SUB, child)
    assert [n.code for n in tree.path(sub)] == ["zlocinny-stat", "valecne-zlociny", "azov"]

    wrong = Narrative(code="x", topic=topic, name="X", definition="D", level=SUB, parent=meta)
    with pytest.raises(ValidationError, match="o úroveň výš"):
        wrong.full_clean()
    other = node(TopicFactory(), "jine-tema", META)
    with pytest.raises(ValidationError, match="stejném tématu"):
        Narrative(code="y", topic=topic, name="Y", definition="D", parent=other).full_clean()
    meta.level = NARR
    with pytest.raises(ValidationError, match="Podřízené uzly"):
        meta.full_clean()


def test_move_is_a_new_version_and_keeps_labels(topic):
    meta = node(topic, "zlocinny-stat", META)
    child = node(topic, "valecne-zlociny")
    label = LabelFactory(narrative=child)
    tree.move(child, meta, note="Zařazení")
    child.refresh_from_db()
    assert (child.parent, child.level, child.current_version) == (meta, NARR, 2)
    assert child.latest_version.parent == meta and child.latest_version.change_note == "Zařazení"
    assert list(Label.objects.current().filter(narrative=child)) == [label]


def test_subtree_and_flatten(topic):
    meta = node(topic, "a-meta", META)
    child = node(topic, "b-child", NARR, meta)
    sub = node(topic, "c-sub", SUB, child)
    root = node(topic, "d-root")
    node(topic, "e-archiv", status=NarrativeStatus.ARCHIVED)
    ids = tree.subtree_ids()
    assert ids[meta.pk] == {meta.pk, child.pk, sub.pk} and ids[root.pk] == {root.pk}
    nodes = [meta, child, sub, root]
    assert [(n.code, d) for n, d in tree.flatten(nodes)] == [
        ("a-meta", 0), ("b-child", 1), ("c-sub", 2), ("d-root", 0),
    ]  # fmt: skip


def test_merge_moves_labels_children_and_archives(topic):
    keeper, duplicate = node(topic, "k49-a"), node(topic, "k49-b")
    child = node(topic, "k49-b-sub", SUB, duplicate)
    shared, only_dup = DocumentFactory(), DocumentFactory()
    LabelFactory(narrative=keeper, document=shared)
    LabelFactory(narrative=duplicate, document=shared)
    LabelFactory(narrative=duplicate, document=only_dup, stance="pro")

    assert tree.merge(duplicate, keeper, note="Duplikát") == 1
    duplicate.refresh_from_db()
    assert (duplicate.status, duplicate.merged_into) == (NarrativeStatus.ARCHIVED, keeper)
    moved = Label.objects.get(narrative=keeper, document=only_dup)
    assert moved.stance == "pro" and moved.details["merged_from"] == "k49-b v1"
    child.refresh_from_db()
    assert child.parent == keeper


# --- návrh modelem -------------------------------------------------------------------------


def proposal_answer(**overrides):
    data = {
        "groups": [
            {"key": "g1", "name": "Ukrajinský stát je zločinný", "definition": "Tvrzení…",
             "from_existing": "", "stance_guide": {"pro": "tvrdí", "contra": "vyvrací",
                                                   "neutral": "popisuje", "absent": "jiné"}},
            {"key": "g2", "name": "", "definition": "", "from_existing": "pomoc",
             "stance_guide": {"pro": "", "contra": "", "neutral": "", "absent": ""}},
        ],
        "assignments": [
            {"code": "k49", "parent": "g1", "reason": "násilí"},
            {"code": "k76", "parent": "k49", "reason": "užší případ"},
            {"code": "k106", "parent": "pomoc", "reason": "rozkrádání pomoci"},
            {"code": "neznamy", "parent": "g1", "reason": "?"},
        ],
    }  # fmt: skip
    return data | overrides


def test_normalize_drops_unknown_and_keeps_missing_as_roots(topic):
    nodes = [node(topic, c) for c in ("k49", "k76", "k106", "pomoc", "k42")]
    result = tree_draft.normalize(proposal_answer(), nodes)
    parents = {a["code"]: a["parent"] for a in result["assignments"]}
    assert parents == {"k49": "g1", "k76": "k49", "k106": "g2", "k42": ""}
    assert result["groups"][1]["from_existing"] == "pomoc"


def test_propose_and_apply_with_researcher_edits(topic, monkeypatch, researcher):
    for code in ("k49", "k76", "k106", "pomoc"):
        node(topic, code)
    seen = {}

    def fake(model, system, user, schema, **kwargs):
        seen.update(user=user, max_tokens=kwargs.get("max_tokens"))
        return llm.Completion(proposal_answer(), model, 0.003, 100, 100)

    monkeypatch.setattr(llm, "complete_json", fake)
    draft = tree_draft.propose(topic)
    assert '"code": "k49"' in seen["user"] and seen["max_tokens"] == tree_draft.MAX_TOKENS
    assert draft.status == TreeDraft.Status.NEW and draft.cost_usd == 0.003

    groups = [g for g in draft.proposal["groups"]]
    groups[0]["name"] = "Ukrajinský stát páchá zločiny"  # úprava výzkumníkem
    parents = {"k49": "g1", "k76": "k49", "k106": "g2"}
    result = tree_draft.apply(draft, groups, parents, user=researcher)

    meta = Narrative.objects.get(name="Ukrajinský stát páchá zločiny")
    assert result["created"] == [meta.code] and meta.code.startswith("n-")
    assert (meta.level, meta.stance_guide["pro"]) == (META, "tvrdí")
    pomoc = Narrative.objects.get(code="pomoc")
    assert (pomoc.level, pomoc.parent) == (META, None)
    k49, k76 = Narrative.objects.get(code="k49"), Narrative.objects.get(code="k76")
    assert (k49.parent, k49.level) == (meta, NARR) and (k76.parent, k76.level) == (k49, SUB)
    assert Narrative.objects.get(code="k106").parent == pomoc
    assert k76.latest_version.changed_by == researcher
    draft.refresh_from_db()
    assert draft.status == TreeDraft.Status.APPLIED


# --- panel -------------------------------------------------------------------------------


def test_tree_pages_and_apply_from_panel(topic, researcher_client):
    for code in ("k49", "k76", "k106", "pomoc"):
        node(topic, code)
    draft = TreeDraft.objects.create(
        topic=topic, proposal=tree_draft.normalize(proposal_answer(), tree_draft.topic_nodes(topic))
    )
    page = researcher_client.get(reverse("panel:tree")).content.decode()
    assert "Navrhnout seskupení modelem" in page and f"č. {draft.pk}" in page
    url = reverse("panel:tree_draft", args=[draft.pk])
    assert "Ukrajinský stát je zločinný" in researcher_client.get(url).content.decode()

    # nepřijatý nadnarativ, pod kterým zůstal narativ → chyba, nic se neuloží
    data = {"action": "apply", "accept-g2": "on", "name-g1": "X", "definition-g1": "D",
            "parent-k49": "g1", "parent-k76": "k49", "parent-k106": "g2"}  # fmt: skip
    response = researcher_client.post(url, data)
    assert "nepřijatý nadnarativ" in response.content.decode()
    assert Narrative.objects.get(code="k49").parent is None

    data |= {"accept-g1": "on"}
    response = researcher_client.post(url, data, follow=True)
    assert response.redirect_chain, response.context["errors"]
    assert "Strom „Válka na Ukrajině“ uložen" in response.content.decode()
    assert Narrative.objects.get(code="k76").level == SUB
    codebook = researcher_client.get(reverse("panel:codebook")).content.decode()
    assert "Nadnarativ" in codebook and "Podnarativ" in codebook


def test_narrative_form_sets_level_from_parent_and_merge_in_panel(topic, researcher_client):
    meta = node(topic, "meta", META)
    child = node(topic, "child")
    data = {"code": "child", "topic": topic.pk, "level": SUB, "parent": meta.pk,
            "kind": "narrative", "name": child.name, "definition": child.definition,
            "status": "active", "change_note": "Zařazení pod nadnarativ"}  # fmt: skip
    researcher_client.post(reverse("panel:narrative_edit", args=["child"]), data)
    child.refresh_from_db()
    assert (child.parent, child.level) == (meta, NARR)

    duplicate = node(topic, "dup")
    LabelFactory(narrative=duplicate)
    response = researcher_client.post(
        reverse("panel:narrative_history", args=["dup"]),
        {"action": "merge", "keeper": "child", "note": "Totéž tvrzení"},
        follow=True,
    )
    assert "převedeno štítků 1" in response.content.decode()
    assert Narrative.objects.get(code="dup").merged_into == child


def test_nest_counts_documents_once_per_branch(topic):
    meta = node(topic, "a-meta", META)
    child = node(topic, "b-child", NARR, meta)
    sub = node(topic, "c-sub", SUB, child)
    shared = DocumentFactory()
    LabelFactory(narrative=child, document=shared)
    LabelFactory(narrative=sub, document=shared)
    LabelFactory(narrative=sub)
    nodes = [meta, child, sub]
    (root,) = tree.nest(nodes, tree.label_documents(nodes))
    assert (root["node"], root["docs"]) == (meta, 2)
    (branch,) = root["children"]
    assert branch["docs"] == 2 and branch["children"][0]["docs"] == 2
    assert tree.nest(nodes)[0]["docs"] is None


def test_codebook_and_draft_are_nested_lists(topic, researcher_client):
    meta = node(topic, "meta", META)
    node(topic, "child", NARR, meta)
    page = researcher_client.get(reverse("panel:codebook")).content.decode()
    assert 'data-key="meta"' in page and "tree-list.js" in page

    for code in ("k49", "k76", "k106", "pomoc"):
        node(topic, code)
    nodes = tree_draft.topic_nodes(topic)
    draft = TreeDraft.objects.create(
        topic=topic, proposal=tree_draft.normalize(proposal_answer(), nodes)
    )
    url = reverse("panel:tree_draft", args=[draft.pk])
    response = researcher_client.get(url)
    (g1, g2) = response.context["groups"]
    assert [r["node"].code for r in g1["items"]] == ["k49"]
    assert [r["node"].code for r in g1["items"][0]["children"]] == ["k76"]
    assert [r["node"].code for r in g2["items"]] == ["k106"]
    # odeslání bez tlačítka (Enter ve formuláři) strom nepoužije
    researcher_client.post(url, {"accept-g1": "on", "name-g1": "X", "parent-k49": "g1"})
    draft.refresh_from_db()
    assert draft.status == TreeDraft.Status.NEW
