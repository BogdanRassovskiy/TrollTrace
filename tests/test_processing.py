"""Этап 5: воронка обработки — релевантность, эмбеддинги, MinHash/LSH, kNN-заимствования."""

import datetime
import random
from io import StringIO

import numpy as np
import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.actors.models import Actor, Layer, Platform, Source
from apps.documents.models import EMBEDDING_DIMENSIONS, Document, DocumentQuality, ReuseLink
from apps.narratives.models import Topic
from apps.processing import embeddings, minhash, relevance, tasks
from apps.processing.models import EmbeddingCenter
from apps.processing.pipeline import process_pending, reset

pytestmark = pytest.mark.django_db

PRAVDA = (
    "Evropská unie chystá další balík sankcí, který podle bruselských úředníků zasáhne "
    "hlavně obyčejné občany členských států. Ceny energií porostou a domácnosti zaplatí "
    "za rozhodnutí, o kterých nikdo nehlasoval. Kritici upozorňují, že vláda v Praze jen "
    "poslušně plní pokyny z Bruselu a na zájmy vlastních lidí zapomíná. "
)
OTHER = (
    "Fotbalisté Slavie v neděli porazili Plzeň dvěma góly a vedou tabulku. Trenér po "
    "zápase ocenil výkon obrany i brankáře, který chytil penaltu ve druhém poločase. "
    "Příští týden čeká tým pohárový zápas v Ostravě, kam pojede i řada fanoušků. "
)


@pytest.fixture(autouse=True)
def center():
    """Нулевой центр: в тестах хэширующий эмбеддер уже даёт ≈0 у несвязанных текстов."""
    return EmbeddingCenter.objects.create(
        model_name=embeddings.model_name(),
        vector=np.zeros(EMBEDDING_DIMENSIONS),
        n_documents=embeddings.CENTER_SAMPLE,
    )


@pytest.fixture
def topic():
    return Topic.objects.create(
        code="eu",
        name="Evropská unie",
        description="Brusel, sankce, evropské instituce, suverenita členských států.",
        keywords=["brusel", "sankc", "evropsk unie"],
    )


def make_source(layer: str, name: str) -> tuple[Source, Actor]:
    source = Source.objects.create(
        name=name, layer=layer, platform=Platform.WEB, url=f"https://{name.lower()}.example/"
    )
    actor = Actor.objects.create(name=name, actor_type=Actor.Type.MEDIA, layer=layer)
    return source, actor


def make_doc(layer, name, text, *, title="", days=0, **kwargs) -> Document:
    source, actor = make_source(layer, name)
    when = timezone.now() - datetime.timedelta(days=10) + datetime.timedelta(days=days)
    return Document.objects.create(
        source=source, actor=actor, url=f"https://{name.lower()}.example/{days}",
        title=title, text=text, published_at=when, collected_at=when, **kwargs,
    )  # fmt: skip


def run():
    return process_pending()


def refreshed(document):
    document.refresh_from_db()
    return document


# --- Релевантность ---------------------------------------------------------------------


def test_cheap_filters_mark_irrelevant_without_embedding(topic):
    empty = make_doc(Layer.MAINSTREAM, "Prazdny", "")
    blocked = make_doc(Layer.MAINSTREAM, "Blok", PRAVDA)
    DocumentQuality.objects.create(
        document=blocked, score=1, quality_rules_version="q1", has_title=True,
        has_published_at=True, date_plausible=True, text_ok=False, text_short=False,
        text_empty=False, lang_ok=True, boilerplate_dup=False, blocked_page=True,
        extraction_ratio_ok=True,
    )  # fmt: skip
    sport = make_doc(Layer.MAINSTREAM, "Sport", PRAVDA, rubric="Sport / Fotbal")
    sport.source.collector_config = {"irrelevant_rubrics": ["sport"]}
    sport.source.save()

    stats = run()

    assert stats.processed == 3 and stats.embedded == 0
    for doc, reason in [(empty, "prázdný"), (blocked, "kvalita 1"), (sport, "rubrika")]:
        doc = refreshed(doc)
        assert doc.is_relevant is False and doc.embedding is None
        assert reason in doc.relevance_details["reason"] and doc.processed_at


def test_keywords_make_document_relevant(topic):
    doc = make_doc(Layer.MAINSTREAM, "Zpravy", PRAVDA)
    run()
    doc = refreshed(doc)
    assert doc.is_relevant is True
    assert "klíčová slova" in doc.relevance_details["reason"]
    assert set(doc.relevance_details["keywords"]["eu"]["text"]) >= {"brusel", "sankc"}
    assert doc.relevance_details["topic"] == "eu"


def test_single_keyword_counts_only_in_title(topic):
    in_text = make_doc(Layer.MAINSTREAM, "Text", OTHER + " Zápas sledoval i velvyslanec v Bruselu.")
    in_title = make_doc(Layer.MAINSTREAM, "Titulek", OTHER, title="Brusel řeší fotbal")
    run()
    assert refreshed(in_text).is_relevant is False
    assert refreshed(in_title).is_relevant is True


def test_semantic_similarity_without_keywords():
    Topic.objects.create(
        code="sport", name="Fotbal", description="Fotbalisté, zápas, trenér, góly, tabulka."
    )
    football = make_doc(Layer.MAINSTREAM, "Sport", OTHER)
    unrelated = make_doc(Layer.MAINSTREAM, "Pocasi", "Zítra bude pršet a foukat silný vítr. " * 8)
    run()
    football, unrelated = refreshed(football), refreshed(unrelated)
    assert football.is_relevant is True and football.relevance_details["reason"] == "podobnost"
    assert football.relevance_score >= 0.3
    assert football.relevance_details["nearest"]["code"] == "sport"
    # Нерелевантный документ остаётся в базе — это знаменатель (README §4, шаг 3).
    assert unrelated.is_relevant is False and unrelated.relevance_details["reason"] == "pod prahem"
    assert unrelated.embedding is not None


def test_without_codebook_relevance_stays_unknown():
    doc = make_doc(Layer.MAINSTREAM, "Zpravy", PRAVDA)
    run()
    doc = refreshed(doc)
    assert doc.is_relevant is None and doc.processed_at is not None


def test_stem_pattern_folds_diacritics_and_inflection():
    pattern = relevance.stem_pattern("evropsk unie")
    assert pattern.search(relevance.fold("Podle Evropské unie"))
    assert pattern.search(relevance.fold("evropská   unie"))
    assert not pattern.search(relevance.fold("neevropská unie"))
    exact = relevance.stem_pattern("=nato")
    assert exact.search(relevance.fold("Summit NATO")) and not exact.search("natočil film")


def test_exact_word_keyword_does_not_match_inside_other_words():
    Topic.objects.create(code="nato", name="NATO", keywords=["=nato", "armad"])
    doc = make_doc(Layer.MAINSTREAM, "Film", OTHER, title="Režisér natočil film o armádě")
    run()
    assert refreshed(doc).relevance_details["keywords"]["nato"]["title"] == ["armad"]


# --- Эмбеддинги ------------------------------------------------------------------------


def test_embedding_is_computed_once_and_again_after_new_version(topic):
    doc = make_doc(Layer.MAINSTREAM, "Zpravy", PRAVDA)
    doc.record_version(text=PRAVDA)
    assert run().embedded == 1
    reset()
    assert run().embedded == 0  # тот же текст и модель — эмбеддинг не пересчитывается

    doc = refreshed(doc)
    doc.record_version(text=PRAVDA + " Doplněno: vláda reagovala.")
    assert refreshed(doc).processed_at is None
    stats = run()
    assert stats.processed == 1 and stats.embedded == 1


def test_center_is_recomputed_while_corpus_grows():
    EmbeddingCenter.objects.all().delete()
    for i in range(embeddings.MIN_CENTER_DOCS):
        make_doc(Layer.MAINSTREAM, f"Z{i}", f"{OTHER} {i}")
    run()
    first = EmbeddingCenter.objects.get()
    assert first.n_documents == embeddings.MIN_CENTER_DOCS
    for i in range(embeddings.MIN_CENTER_DOCS):
        make_doc(Layer.MAINSTREAM, f"Y{i}", f"{PRAVDA} {i}")
    run()
    assert EmbeddingCenter.objects.get().n_documents == 2 * embeddings.MIN_CENTER_DOCS


# --- Дословные заимствования (MinHash / LSH) ------------------------------------------


def test_verbatim_reuse_from_layer_a_in_both_arrival_orders():
    original = make_doc(Layer.ATTRIBUTED, "Pravda", PRAVDA, days=0)
    copy = make_doc(Layer.TELEGRAM, "Kanal", "Přeposíláme: " + PRAVDA, days=1)
    run()
    link = ReuseLink.objects.get()
    assert (link.source_document, link.target_document) == (original, copy)
    assert (link.link_type, link.method) == ("verbatim", "minhash") and link.similarity >= 0.9

    # Обратный порядок: копия уже обработана, оригинал Pravda приходит позже.
    ReuseLink.objects.all().delete()
    Document.objects.filter(pk=original.pk).update(processed_at=None)
    run()
    assert ReuseLink.objects.filter(source_document=original, target_document=copy).exists()


def test_short_post_quoting_a_paragraph_counts_by_containment():
    paragraph = PRAVDA
    original = make_doc(Layer.ATTRIBUTED, "Pravda", paragraph + OTHER * 2, days=0)
    post = make_doc(Layer.TELEGRAM, "Kanal", paragraph, days=1)
    run()
    link = ReuseLink.objects.get(link_type="verbatim")
    assert (link.source_document, link.target_document) == (original, post)
    assert link.similarity >= 0.9  # почти весь пост взят из статьи, хотя Jaccard ≈ 0,4


def test_no_link_outside_window_between_other_layers_or_backwards_in_time():
    make_doc(Layer.ATTRIBUTED, "Pravda", PRAVDA, days=0)
    make_doc(Layer.TELEGRAM, "Pozde", PRAVDA, days=45)  # вне окна 30 дней
    make_doc(Layer.TELEGRAM, "Driv", PRAVDA, days=-1)  # раньше оригинала
    make_doc(Layer.ALTERNATIVE, "Web", OTHER, days=2)
    make_doc(Layer.MAINSTREAM, "Media", OTHER, days=3)  # D ← B: не слой-источник
    run()
    assert not ReuseLink.objects.exists()


def test_minhash_containment_formula():
    assert minhash.containment(1.0, 50, 50) == 1.0
    # B целиком внутри A: |A|=150, |B|=50 → J = 50/150.
    assert minhash.containment(50 / 150, 150, 50) == pytest.approx(1.0)
    assert minhash.containment(0.0, 100, 100) == 0.0
    assert minhash.signature(minhash.shingles("jen pár slov", 5)) is None


# --- Парафразы (kNN) -------------------------------------------------------------------


def shuffled(text: str) -> str:
    """Те же слова в другом порядке: шинглы не совпадают, смысл (мешок слов) — тот же."""
    rng = random.Random(7)
    sentences = []
    for sentence in text.split(". "):
        tokens = sentence.split()
        rng.shuffle(tokens)
        sentences.append(" ".join(tokens))
    return ". ".join(sentences)


def test_paraphrase_found_by_knn_not_minhash():
    original = make_doc(Layer.ATTRIBUTED, "Pravda", PRAVDA, days=0)
    rewrite = make_doc(Layer.ALTERNATIVE, "Web", shuffled(PRAVDA), days=2)
    make_doc(Layer.ALTERNATIVE, "Sport", OTHER, days=2)
    run()
    link = ReuseLink.objects.get()
    assert (link.source_document, link.target_document) == (original, rewrite)
    assert (link.link_type, link.method) == ("paraphrase", "knn") and link.similarity >= 0.7


def test_verbatim_pair_is_not_duplicated_as_paraphrase():
    make_doc(Layer.ATTRIBUTED, "Pravda", PRAVDA, days=0)
    make_doc(Layer.TELEGRAM, "Kanal", PRAVDA, days=1)
    run()
    assert list(ReuseLink.objects.values_list("link_type", flat=True)) == ["verbatim"]


def test_reprocessing_keeps_confirmed_links_and_rebuilds_automatic():
    original = make_doc(Layer.ATTRIBUTED, "Pravda", PRAVDA, days=0)
    copy = make_doc(Layer.TELEGRAM, "Kanal", PRAVDA, days=1)
    run()
    manual = ReuseLink.objects.create(
        source_document=original, target_document=copy, link_type="quote",
        method="manual", similarity=1.0, is_confirmed=True,
    )  # fmt: skip
    reset()
    run()
    assert ReuseLink.objects.filter(pk=manual.pk).exists()
    assert ReuseLink.objects.filter(link_type="verbatim").count() == 1


# --- Задача, команды -------------------------------------------------------------------


def test_task_processes_queue_and_respects_lock(topic):
    make_doc(Layer.MAINSTREAM, "Zpravy", PRAVDA)
    from django.core.cache import cache

    cache.add(tasks.LOCK_KEY, "1", 60)
    assert "skipped" in tasks.process_pending_documents.delay().get()
    cache.delete(tasks.LOCK_KEY)
    result = tasks.process_pending_documents.delay().get()
    assert result["processed"] == 1 and result["relevant"] == 1


def test_task_goes_to_processing_queue(settings):
    route = settings.CELERY_TASK_ROUTES["apps.processing.tasks.*"]
    assert route == {"queue": "processing"}
    assert "process-pending-documents" in settings.CELERY_BEAT_SCHEDULE


def test_import_topics_is_idempotent():
    out = StringIO()
    call_command("import_topics", stdout=out)
    count = Topic.objects.count()
    assert count >= 8 and all(t.keywords for t in Topic.objects.all())
    call_command("import_topics", stdout=out)
    assert Topic.objects.count() == count
    assert "nová 0" in out.getvalue()


def test_process_documents_command_reprocesses_all(topic):
    make_doc(Layer.MAINSTREAM, "Zpravy", PRAVDA)
    call_command("process_documents", stdout=StringIO())
    out = StringIO()
    call_command("process_documents", "--all", stdout=out)
    assert "vráceno dokumentů: 1" in out.getvalue()
    assert "Zpracováno 1: relevantních 1" in out.getvalue()
