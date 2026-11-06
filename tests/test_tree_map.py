"""Mapa narativů: узел × неделя по документам поддерева, позиция ветки, страница и API."""

import datetime

import pytest
from django.urls import reverse

from apps.analytics import tree_map
from apps.narratives.models import Label, NarrativeLevel, NarrativeStatus

from .factories import NarrativeFactory, TopicFactory
from .test_aggregation import WEEK, actor, document, label

pytestmark = pytest.mark.django_db


def test_branch_counts_document_once_and_conflict_is_neutral():
    topic = TopicFactory()
    meta = NarrativeFactory(topic=topic, level=NarrativeLevel.META)
    left = NarrativeFactory(topic=topic, parent=meta)
    right = NarrativeFactory(topic=topic, parent=meta)
    sub = NarrativeFactory(topic=topic, parent=left, level=NarrativeLevel.SUB)
    NarrativeFactory(topic=topic, status=NarrativeStatus.ARCHIVED)
    a = actor()
    both = document(a)
    label(both, left, "pro")
    label(both, right, "contra")  # pro + proti v jedné větvi → neutrálně
    one = document(a, week=WEEK + datetime.timedelta(days=7))
    label(one, sub, "pro")
    label(one, left, "neutral")
    label(one, left, "contra", origin=Label.Origin.HUMAN)  # ruční štítek má přednost
    document(a)  # bez štítku — jen ve jmenovateli
    document(a, score=2)  # pod prahem kvality

    data = tree_map.compute(3)
    assert data["weeks"] == [WEEK.isoformat(), (WEEK + datetime.timedelta(days=7)).isoformat()]
    assert data["total"] == [2, 1]
    s = data["series"]
    # 2. týden: pro z podnarativu + ruční proti u narativu → ve větvi neutrálně
    assert s[meta.code] == {"n": [1, 1], "pro": [0, 0], "contra": [0, 0], "neutral": [1, 1]}
    assert s[left.code] == {"n": [1, 1], "pro": [1, 0], "contra": [0, 0], "neutral": [0, 1]}
    assert s[right.code] == {"n": [1, 0], "pro": [0, 0], "contra": [1, 0], "neutral": [0, 0]}
    assert s[sub.code] == {"n": [0, 1], "pro": [0, 1], "contra": [0, 0], "neutral": [0, 0]}
    nodes = {n["code"]: n for n in data["nodes"]}
    assert nodes[sub.code]["parent"] == left.code and nodes[meta.code]["parent"] is None
    assert len(nodes) == 4  # archivovaný uzel na mapě není
    assert tree_map.compute(1)["total"] == [3, 1]


def test_map_page_and_api(observer_client):
    page = observer_client.get(reverse("dashboard:map"))
    assert page.status_code == 200 and "Mapa narativů" in page.content.decode()
    response = observer_client.get(reverse("analytics:tree_map"), {"min_quality": 3})
    assert response.status_code == 200 and set(response.json()) >= {"weeks", "nodes", "series"}
    assert observer_client.get(reverse("analytics:tree_map"), {"min_quality": 2}).status_code == 400


def test_aggregates_and_documents_roll_up_the_branch(observer_client):
    from apps.analytics import aggregation
    from apps.analytics.models import WeeklyAggregate

    topic = TopicFactory()
    meta = NarrativeFactory(topic=topic, level=NarrativeLevel.META)
    left = NarrativeFactory(topic=topic, parent=meta)
    right = NarrativeFactory(topic=topic, parent=meta)
    a = actor()
    both = document(a)
    label(both, left, "pro")
    label(both, right, "contra")
    label(document(a), right, "pro")
    aggregation.update(full=True)

    row = WeeklyAggregate.objects.get(narrative=meta, actor=a, min_quality=3)
    assert (row.n_documents, row.n_pro, row.n_contra, row.n_neutral) == (2, 1, 0, 1)
    assert WeeklyAggregate.objects.get(narrative=left, actor=a, min_quality=3).n_documents == 1
    assert not aggregation.needs_full(aggregation.active_narratives())
    from apps.narratives import tree

    tree.move(right, None)  # přesun ve stromu → součty větví se přepočítají celé
    assert aggregation.needs_full(aggregation.active_narratives())

    url = reverse("analytics:documents")
    week = WEEK.isoformat()
    data = observer_client.get(url, {"narrative": meta.code, "week": week, "actor": a.pk}).json()
    assert data["count"] == 1  # right už není ve větvi
    data = observer_client.get(url, {"narrative": left.code, "week": week, "actor": a.pk,
                                     "stance": "pro"}).json()  # fmt: skip
    assert data["count"] == 1 and data["documents"][0]["narrative"] == left.code


def test_dashboard_pages_carry_the_tree(observer_client):
    topic = TopicFactory()
    meta = NarrativeFactory(topic=topic, level=NarrativeLevel.META)
    child = NarrativeFactory(topic=topic, parent=meta)
    NarrativeFactory(topic=topic, parent=meta, status=NarrativeStatus.CANDIDATE)

    page = observer_client.get(reverse("dashboard:compare"))
    tree = {n["code"]: n for n in page.context["chart_config"]["tree"]}
    assert tree[child.code]["parent"] == meta.code and len(tree) == 2  # jen aktivní
    assert page.context["chart_config"]["narrative"] == meta.code  # výchozí = kořen
    assert 'id="narrative-picker"' in page.content.decode()

    page = observer_client.get(reverse("dashboard:narratives"))
    (topic_row,) = page.context["codebook"]
    (root,) = topic_row[1]
    assert root["node"] == meta and len(root["children"]) == 2  # kandidát v kódové knize je
    assert page.context["chart_config"]["narratives"][0]["topic"] == topic.code

    a = actor()
    page = observer_client.get(reverse("dashboard:actor", args=[a.pk]))
    assert 'id="actor-tree"' in page.content.decode()
