"""Демо-данные для разработки: акторы по слоям A–E, кодбук, документы, агрегаты, мониторинг.

Всё вымышленное: имена «Demo …», адреса в зоне .example. Агрегаты синтетические, но с
осмысленной формой: событийные нарративы появляются сначала в слое A и доходят до парламента
с задержкой (H1), культурные ведут политики (H2). Хватает для режима сравнения на ~350 линий.

    python manage.py seed_demo                 # 350 акторов, 26 недель
    python manage.py seed_demo --reset         # удалить ВСЕ исследовательские данные и пересоздать
"""

import datetime
import math
import random
import statistics
import zlib
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.actors.models import Actor, ActorAccount, ActorInclusionPeriod, Layer, Platform, Source
from apps.analytics.models import WeeklyAggregate
from apps.documents.models import Document, DocumentQuality, DocumentVersion, ReuseLink
from apps.documents.utils import normalize_url, text_hash
from apps.ingestion.models import GoldenPage, Level, SourceHealthCheck, SourceIncident
from apps.narratives.models import (
    Debunk,
    Label,
    Narrative,
    NarrativeCandidate,
    Stance,
    Topic,
)
from apps.panel.defaults import ensure_default_settings
from apps.panel.models import Job, Notification

PARTIES = ["ANO", "ODS", "STAN", "Piráti", "SPD", "KDU-ČSL", "TOP 09", "Stačilo!", "Motoristé"]
ANTI_SYSTEM_PARTIES = {"SPD", "Stačilo!"}
ROLES = ["Poslanec PSP"] * 8 + ["Člen vlády", "Předseda strany", "Europoslanec"]

# Доля акторов по слоям; остаток — парламент (E).
LAYER_SHARES = {
    Layer.ATTRIBUTED: 0.012,
    Layer.ALTERNATIVE: 0.07,
    Layer.TELEGRAM: 0.17,
    Layer.MAINSTREAM: 0.05,
}
# Среднее число документов актора в неделю (знаменатель).
WEEKLY_VOLUME = {
    Layer.ATTRIBUTED: 90,
    Layer.ALTERNATIVE: 25,
    Layer.TELEGRAM: 35,
    Layer.MAINSTREAM: 70,
    Layer.PARLIAMENT: 3,
}
# Склонность слоя к нарративам (множитель вероятности).
LAYER_AFFINITY = {
    Layer.ATTRIBUTED: 1.6,
    Layer.ALTERNATIVE: 1.2,
    Layer.TELEGRAM: 1.0,
    Layer.MAINSTREAM: 0.25,
    Layer.PARLIAMENT: 0.5,
}

TOPICS = [
    ("ukrajina", "Válka na Ukrajině"),
    ("eu", "Evropská unie"),
    ("energetika", "Energetika"),
    ("migrace", "Migrace"),
]
# (код, тема, формулировка, тип: event — H1, cultural — H2, пик доли)
NARRATIVES = [
    ("N001", "ukrajina", "Помощь Украине разоряет чешских граждан", "event", 0.22),
    ("N002", "eu", "Брюссель диктует Чехии свою волю", "cultural", 0.12),
    ("N003", "eu", "Евро разорит чехов", "cultural", 0.08),
    ("N004", "energetika", "Санкции против России вызвали энергетический кризис", "event", 0.18),
    ("N005", "migrace", "Миграционный пакт навяжет Чехии мигрантов", "event", 0.15),
]
# Задержка появления по слоям в неделях от старта нарратива.
ONSET_LAG = {
    "event": {
        Layer.ATTRIBUTED: 0,
        Layer.TELEGRAM: 1,
        Layer.ALTERNATIVE: 2,
        Layer.PARLIAMENT: 4,
        Layer.MAINSTREAM: 5,
    },
    "cultural": {
        Layer.PARLIAMENT: 0,
        Layer.ALTERNATIVE: 1,
        Layer.TELEGRAM: 1,
        Layer.ATTRIBUTED: 2,
        Layer.MAINSTREAM: 3,
    },
}
QUALITY_RULES_VERSION = "demo-1"


class Command(BaseCommand):
    help = "Заполняет БД демо-данными для разработки (вымышленные акторы и синтетические агрегаты)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--actors", type=int, default=350, help="Число акторов (по умолчанию 350)."
        )
        parser.add_argument("--weeks", type=int, default=26, help="Число недель (по умолчанию 26).")
        parser.add_argument(
            "--seed", type=int, default=42, help="Зерно генератора случайных чисел."
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Удалить ВСЕ исследовательские данные (журнал аудита и настройки остаются).",
        )

    def handle(self, *args, actors, weeks, seed, reset, **options):
        if actors < 10 or weeks < 8:
            raise CommandError("Нужно минимум 10 акторов и 8 недель.")
        if Actor.objects.exists() and not reset:
            raise CommandError(
                "В БД уже есть акторы. Запустите с --reset, чтобы пересоздать данные."
            )

        self.rng = random.Random(seed)
        today = timezone.localdate()
        last_monday = today - datetime.timedelta(days=today.weekday())
        self.weeks = [last_monday - datetime.timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]

        with transaction.atomic():
            if reset:
                self._reset()
            ensure_default_settings()
            sources = self._create_sources(actors)
            actor_list = self._create_actors(actors, sources)
            narratives = self._create_codebook()
            rows = self._create_aggregates(actor_list, narratives)
            documents = self._create_documents(actor_list, narratives, rows)
            self._create_monitoring(sources)
            self._create_panel_data(narratives, documents)

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: {len(actor_list)} акторов, {Source.objects.count()} источников, "
                f"{len(narratives)} нарративов, {len(documents)} документов, "
                f"{WeeklyAggregate.objects.count()} строк агрегатов за {weeks} недель."
            )
        )

    # --- Очистка -------------------------------------------------------------------------

    def _reset(self):
        for model in [
            WeeklyAggregate,
            Label,
            ReuseLink,
            NarrativeCandidate,
            DocumentQuality,
            DocumentVersion,
            Document,
            ActorAccount,
            ActorInclusionPeriod,
            Actor,
            SourceHealthCheck,
            GoldenPage,
            SourceIncident,
            Source,
            Debunk,
            Narrative,
            Topic,
            Notification,
            Job,
        ]:
            model.objects.all().delete()

    # --- Источники и акторы --------------------------------------------------------------

    def _layer_counts(self, total: int) -> dict[str, int]:
        counts = {layer: max(1, round(total * share)) for layer, share in LAYER_SHARES.items()}
        counts[Layer.PARLIAMENT] = total - sum(counts.values())
        return counts

    def _create_sources(self, total_actors: int) -> dict:
        counts = self._layer_counts(total_actors)
        sources = {"telegram": None, "psp": None, "sites": defaultdict(list)}
        sites = [
            (Layer.ATTRIBUTED, "Demo Pravda CZ", "pravda"),
            (Layer.ALTERNATIVE, "Demo Alternativa", "alt"),
            (Layer.MAINSTREAM, "Demo Zprávy", "zpravy"),
        ]
        for layer, title, slug in sites:
            for i in range(1, counts[layer] + 1):
                source = Source(
                    name=f"{title} {i}",
                    layer=layer,
                    platform=Platform.WEB,
                    url=f"https://{slug}-{i}.demo.example/",
                    feed_url=f"https://{slug}-{i}.demo.example/rss",
                )
                if layer == Layer.ATTRIBUTED:
                    source.attribution_by = "Demo: внешняя атрибуция"
                    source.attribution_url = "https://attribution.demo.example/report"
                if layer == Layer.ALTERNATIVE:
                    source.external_rating = "demo-rating"
                    source.external_rating_source = "Demo: внешний рейтинг"
                source.save()
                sources["sites"][layer].append(source)
        sources["telegram"] = Source.objects.create(
            name="Telegram (публичные каналы)",
            layer=Layer.TELEGRAM,
            platform=Platform.TELEGRAM,
            url="https://t.me/",
            stale_after_hours=24 * 7,
            min_text_length=80,
        )
        sources["psp"] = Source.objects.create(
            name="Стенограммы Poslanecké sněmovny (демо)",
            layer=Layer.PARLIAMENT,
            platform=Platform.PARLIAMENT,
            url="https://psp.demo.example/",
            stale_after_hours=24 * 14,
        )
        return sources

    def _create_actors(self, total: int, sources: dict) -> list[Actor]:
        counts = self._layer_counts(total)
        rng = self.rng
        actors: list[Actor] = []

        def add(actor: Actor, source: Source, account_id: str, url: str = ""):
            actor.inclusion_criterion = "Демо-данные"
            actor.save()
            ActorAccount.objects.create(actor=actor, source=source, account_id=account_id, url=url)
            actors.append(actor)
            return actor

        for layer, actor_type in [
            (Layer.ATTRIBUTED, Actor.Type.ATTRIBUTED_NETWORK),
            (Layer.ALTERNATIVE, Actor.Type.MEDIA),
            (Layer.MAINSTREAM, Actor.Type.MEDIA),
        ]:
            for source in sources["sites"][layer]:
                add(Actor(name=source.name, actor_type=actor_type, layer=layer), source, "")

        for i in range(1, counts[Layer.TELEGRAM] + 1):
            handle = f"demo_kanal_{i:03d}"
            add(
                Actor(
                    name=f"Demo kanál {i:03d}", actor_type=Actor.Type.CHANNEL, layer=Layer.TELEGRAM
                ),
                sources["telegram"],
                handle,
                f"https://t.me/{handle}",
            )

        for i in range(1, counts[Layer.PARLIAMENT] + 1):
            party = rng.choice(PARTIES)
            hybrid = party in ANTI_SYSTEM_PARTIES and rng.random() < 0.25
            actor = add(
                Actor(
                    name=f"Demo poslanec {i:03d}",
                    actor_type=Actor.Type.POLITICIAN,
                    layer=Layer.PARLIAMENT,
                    party=party,
                    role=rng.choice(ROLES),
                    is_hybrid=hybrid,
                ),
                sources["psp"],
                f"poslanec-{i:03d}",
            )
            if hybrid:  # гибридные политики ведут и Telegram-канал
                ActorAccount.objects.create(
                    actor=actor, source=sources["telegram"], account_id=f"demo_poslanec_{i:03d}"
                )

        # Периоды включения: все с первой недели; часть исключена по ходу, часть возвращена.
        start = self.weeks[0]
        for actor in actors:
            actor.include("Демо: начальная выборка", on=start)
            if rng.random() < 0.03:
                out_week = rng.randrange(len(self.weeks) // 3, len(self.weeks) - 2)
                actor.exclude("Демо: исключён из выборки", on=self.weeks[out_week])
                if rng.random() < 0.35:
                    actor.include("Демо: повторное включение", on=self.weeks[out_week + 2])
        return actors

    # --- Кодбук --------------------------------------------------------------------------

    def _create_codebook(self) -> list[dict]:
        topics = {code: Topic.objects.create(code=code, name=name) for code, name in TOPICS}
        narratives = []
        for code, topic, name, kind, peak in NARRATIVES:
            narrative = Narrative(
                code=code,
                topic=topics[topic],
                name=name,
                definition=f"Демо-определение: {name.lower()}. Позиция фиксируется отдельно.",
                examples=[f"Пример формулировки для {code}"],
            )
            narrative.save(change_note="Демо: начальная версия кодбука")
            onset = self.rng.randrange(2, max(3, len(self.weeks) // 3))
            narratives.append({"obj": narrative, "kind": kind, "peak": peak, "onset": onset})

        # Пример правки кодбука: у N003 будет две версии.
        n003 = narratives[2]["obj"]
        n003.definition += " Уточнено: включая выход из еврозоны как альтернативу."
        n003.save(change_note="Демо: уточнение определения")

        Debunk.objects.create(
            narrative=narratives[0]["obj"],
            organization=Debunk.Organization.OTHER,
            url="https://factcheck.demo.example/n001",
            title="Демо-ссылка на внешнее опровержение",
        )
        return narratives

    # --- Агрегаты ------------------------------------------------------------------------

    def _intensity(self, narrative: dict, layer: str, week_index: int) -> float:
        """Ожидаемая доля документов о нарративе в слое на неделе."""
        lag = ONSET_LAG[narrative["kind"]][layer]
        dt = week_index - narrative["onset"] - lag
        if narrative["kind"] == "event":
            if dt < 0:
                return 0.002
            return narrative["peak"] * (dt / 2) * math.exp(1 - dt / 2) + 0.01  # пик через 2 недели
        base = narrative["peak"] * (0.6 + 0.4 * math.sin(week_index / 3))
        return base if dt >= 0 else base * 0.3

    def _poisson(self, lam: float) -> int:
        if lam > 30:
            return max(0, round(self.rng.gauss(lam, math.sqrt(lam))))
        threshold, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= threshold:
                return k
            k += 1

    def _binomial(self, n: int, p: float) -> int:
        if n > 40:
            return min(n, max(0, round(self.rng.gauss(n * p, math.sqrt(n * p * (1 - p))))))
        return sum(self.rng.random() < p for _ in range(n))

    def _create_aggregates(
        self, actors: list[Actor], narratives: list[dict]
    ) -> list[WeeklyAggregate]:
        rng = self.rng
        periods = defaultdict(list)
        for period in ActorInclusionPeriod.objects.all():
            periods[period.actor_id].append(period)

        def included(actor: Actor, week: datetime.date) -> bool:
            return any(
                p.start_date <= week and (p.end_date is None or week <= p.end_date)
                for p in periods[actor.pk]
            )

        rows: list[WeeklyAggregate] = []
        totals_by_actor: dict[int, int] = defaultdict(int)
        for actor in actors:
            activity = rng.lognormvariate(0, 0.6)
            affinity = LAYER_AFFINITY[actor.layer] * rng.lognormvariate(0, 0.5)
            if actor.party in ANTI_SYSTEM_PARTIES or actor.is_hybrid:
                affinity *= 2.5
            for wi, week in enumerate(self.weeks):
                if not included(actor, week):
                    continue
                n_total = self._poisson(WEEKLY_VOLUME[actor.layer] * activity)
                if n_total == 0:
                    continue
                totals_by_actor[actor.pk] += n_total
                for narrative in narratives:
                    p = min(0.9, self._intensity(narrative, actor.layer, wi) * affinity)
                    n = self._binomial(n_total, p)
                    rows.append(
                        WeeklyAggregate(
                            actor=actor,
                            narrative=narrative["obj"],
                            week=week,
                            n_documents=n,
                            n_total=n_total,
                            share=n / n_total,
                            engagement={"views": n * rng.randint(50, 2000)},
                        )
                    )

        self._assign_cohorts(actors, totals_by_actor)

        # Группы: слой и партия — сумма числителей и знаменателей по участникам.
        groups: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        actor_by_id = {a.pk: a for a in actors}
        for row in rows:
            actor = actor_by_id[row.actor_id]
            keys = [f"layer:{actor.layer}"] + ([f"party:{actor.party}"] if actor.party else [])
            for key in keys:
                acc = groups[(key, row.narrative_id, row.week)]
                acc[0] += row.n_documents
                acc[1] += row.n_total
        group_rows = [
            WeeklyAggregate(
                group_key=key,
                narrative_id=narrative_id,
                week=week,
                n_documents=n,
                n_total=total,
                share=n / total,
            )
            for (key, narrative_id, week), (n, total) in groups.items()
        ]

        self._fill_z_scores(rows, lambda r: (r.actor_id, r.narrative_id))
        self._fill_z_scores(group_rows, lambda r: (r.group_key, r.narrative_id))
        WeeklyAggregate.objects.bulk_create(rows, batch_size=5000)
        WeeklyAggregate.objects.bulk_create(group_rows, batch_size=5000)
        return rows

    @staticmethod
    def _fill_z_scores(rows: list[WeeklyAggregate], key):
        """z-оценка доли относительно собственной истории актора/группы (README §5)."""
        series = defaultdict(list)
        for row in rows:
            series[key(row)].append(row)
        for items in series.values():
            shares = [r.share for r in items]
            if len(shares) < 2:
                continue
            mean, stdev = statistics.fmean(shares), statistics.pstdev(shares)
            for r in items:
                r.z_score = round((r.share - mean) / stdev, 3) if stdev > 0 else 0.0

    @staticmethod
    def _assign_cohorts(actors: list[Actor], totals: dict[int, int]):
        """Терцили активности внутри слоя (README §2: переменная стратификации, не отбора)."""
        by_layer = defaultdict(list)
        for actor in actors:
            by_layer[actor.layer].append(actor)
        cohorts = [Actor.ActivityCohort.LOW, Actor.ActivityCohort.MEDIUM, Actor.ActivityCohort.HIGH]
        for members in by_layer.values():
            members.sort(key=lambda a: totals.get(a.pk, 0))
            for i, actor in enumerate(members):
                actor.activity_cohort = cohorts[min(2, i * 3 // len(members))]
        Actor.objects.bulk_update(actors, ["activity_cohort"])

    # --- Документы -----------------------------------------------------------------------

    def _create_documents(self, actors, narratives, rows) -> list[Document]:
        """По несколько документов на актора: с метками, версиями и оценкой качества."""
        rng = self.rng
        tz = timezone.get_current_timezone()
        accounts = {a.actor_id: a for a in ActorAccount.objects.select_related("source")}
        hits = defaultdict(list)
        for row in rows:
            if row.n_documents:
                hits[row.actor_id].append(row)
        narrative_by_id = {n["obj"].pk: n["obj"] for n in narratives}

        documents, picks = [], []
        now = timezone.now()
        for actor in actors:
            account = accounts[actor.pk]
            for row in rng.sample(hits[actor.pk], k=min(3, len(hits[actor.pk]))):
                narrative = narrative_by_id[row.narrative_id]
                published = datetime.datetime.combine(
                    row.week + datetime.timedelta(days=rng.randrange(7)),
                    datetime.time(rng.randrange(6, 23), rng.randrange(60)),
                    tzinfo=tz,
                )
                n = len(documents) + 1
                if account.source.platform == Platform.TELEGRAM:
                    url = f"https://t.me/{account.account_id}/{n}"
                else:
                    url = f"{account.source.url}clanek/{n}?utm_source=rss"
                text = f"[DEMO] {narrative.name}. Ukázkový text dokumentu pro vývoj. " * 8
                documents.append(
                    Document(
                        actor=actor,
                        source=account.source,
                        url=url,
                        url_normalized=normalize_url(url),
                        title=f"[DEMO] {narrative.name}",
                        text=text,
                        text_hash=text_hash(text),
                        language="cs",
                        published_at=min(published, now),
                        date_source=rng.choice(
                            [Document.DateSource.PAGE] * 4 + [Document.DateSource.FEED]
                        ),
                        collected_at=min(
                            published + datetime.timedelta(minutes=rng.randrange(5, 240)), now
                        ),
                        is_relevant=True,
                        engagement={"views": rng.randint(10, 50_000)},
                    )
                )
                picks.append(narrative)
        Document.objects.bulk_create(documents, batch_size=2000)

        current_version = {n.pk: n.latest_version.pk for n in narrative_by_id.values()}
        versions, qualities, labels = [], [], []
        for doc, narrative in zip(documents, picks, strict=True):
            raw = zlib.compress(
                f"<html><body><h1>{doc.title}</h1><p>{doc.text}</p></body></html>".encode()
            )
            versions.append(
                DocumentVersion(
                    document=doc,
                    version=1,
                    title=doc.title,
                    text=doc.text,
                    text_hash=doc.text_hash,
                    raw_html_compressed=raw,
                )
            )
            qualities.append(self._quality(doc))
            labels.append(
                Label(
                    document=doc,
                    narrative=narrative,
                    narrative_version_id=current_version[narrative.pk],
                    stance=rng.choice([Stance.PRO, Stance.PRO, Stance.CONTRA, Stance.NEUTRAL]),
                    origin=Label.Origin.MODEL,
                    confidence=round(rng.uniform(0.55, 0.99), 3),
                    model_name="demo-classifier",
                )
            )
        DocumentVersion.objects.bulk_create(versions, batch_size=2000)
        DocumentQuality.objects.bulk_create(qualities, batch_size=2000)
        Label.objects.bulk_create(labels, batch_size=2000)

        # Несколько правленых статей: вторая версия текста.
        for doc in rng.sample(documents, k=max(1, len(documents) // 40)):
            doc.record_version(text=doc.text + " Aktualizace: doplněno vyjádření.", title=doc.title)

        self._create_reuse_links(documents, picks)
        return documents

    def _quality(self, doc: Document) -> DocumentQuality:
        """Флаги → оценка; распределение примерно как у здорового сбора."""
        roll = self.rng.random()
        flags = dict(
            has_title=True,
            has_published_at=True,
            date_plausible=True,
            text_ok=True,
            text_short=False,
            text_empty=False,
            lang_ok=True,
            boilerplate_dup=False,
            blocked_page=False,
            selectors_ok=None,
            extraction_ratio_ok=True,
        )
        if doc.date_source != Document.DateSource.PAGE:
            score = 4
        elif roll < 0.82:
            score = 5
        elif roll < 0.92:
            score, flags["text_ok"], flags["text_short"] = 3, False, True
        elif roll < 0.97:
            score, flags["lang_ok"], flags["extraction_ratio_ok"] = 2, False, False
        else:
            score, flags["boilerplate_dup"] = 1, True
        return DocumentQuality(
            document=doc, score=score, quality_rules_version=QUALITY_RULES_VERSION, **flags
        )

    def _create_reuse_links(self, documents, picks):
        """Слой A → более поздние документы других слоёв по тому же нарративу."""
        originals = defaultdict(list)
        for doc, narrative in zip(documents, picks, strict=True):
            if doc.source.layer == Layer.ATTRIBUTED:
                originals[narrative.pk].append(doc)
        links = []
        for doc, narrative in zip(documents, picks, strict=True):
            if doc.source.layer == Layer.ATTRIBUTED or self.rng.random() > 0.3:
                continue
            earlier = [o for o in originals[narrative.pk] if o.published_at < doc.published_at]
            if not earlier:
                continue
            verbatim = self.rng.random() < 0.4
            links.append(
                ReuseLink(
                    source_document=self.rng.choice(earlier),
                    target_document=doc,
                    link_type=ReuseLink.Type.VERBATIM if verbatim else ReuseLink.Type.PARAPHRASE,
                    method=ReuseLink.Method.MINHASH if verbatim else ReuseLink.Method.KNN,
                    similarity=round(
                        self.rng.uniform(0.7, 0.98) if verbatim else self.rng.uniform(0.85, 0.95), 3
                    ),
                )
            )
        ReuseLink.objects.bulk_create(links, ignore_conflicts=True)

    # --- Мониторинг и панель -------------------------------------------------------------

    def _create_monitoring(self, sources: dict):
        today = timezone.localdate()
        all_sources = list(Source.objects.all())
        checks = [
            SourceHealthCheck(
                source=source,
                date=today - datetime.timedelta(days=d),
                check_type=SourceHealthCheck.CheckType.VOLUME,
                status=Level.OK,
                metrics={"n": self.rng.randint(5, 120)},
            )
            for source in all_sources
            for d in range(14)
        ]
        SourceHealthCheck.objects.bulk_create(checks)

        broken = sources["sites"][Layer.ALTERNATIVE][0]
        SourceIncident.objects.create(
            source=broken,
            incident_type=SourceIncident.Type.STALE_FEED,
            level=Level.WARNING,
            started_at=timezone.now() - datetime.timedelta(days=2),
            description="Демо: RSS не обновлялся 48 часов.",
        )
        recovered = sources["sites"][Layer.MAINSTREAM][0]
        SourceIncident.objects.create(
            source=recovered,
            incident_type=SourceIncident.Type.SELECTOR_MISSING,
            level=Level.CRITICAL,
            started_at=timezone.now() - datetime.timedelta(days=20),
            ended_at=timezone.now() - datetime.timedelta(days=18),
            description="Демо: смена вёрстки, селектор даты не находился два дня.",
        )
        for i in (1, 2):
            GoldenPage.objects.create(
                source=recovered,
                url=f"{recovered.url}clanek/golden-{i}",
                expected_title=f"Kontrolní článek {i}",
                expected_text_prefix="Ukázkový začátek textu kontrolního článku…",
            )

    def _create_panel_data(self, narratives, documents):
        Notification.objects.create(
            notification_type=Notification.Type.COLLECTION,
            level=Notification.Level.WARNING,
            title="Источник не обновлялся 48 ч",
            text="Демо-уведомление об инциденте сбора.",
        )
        Notification.objects.create(
            notification_type=Notification.Type.SPIKE,
            title=f"Всплеск по {narratives[0]['obj'].code}",
            text="Демо-уведомление о всплеске у актора.",
        )
        Job.objects.create(
            job_type=Job.Type.AGGREGATE,
            status=Job.Status.SUCCEEDED,
            progress=1,
            message="Демо: пересчёт агрегатов",
            started_at=timezone.now() - datetime.timedelta(minutes=3),
            finished_at=timezone.now(),
        )
        window_end = self.weeks[-1]
        candidate = NarrativeCandidate.objects.create(
            description="Демо-кластер: «Чехия потеряет суверенитет из-за общего долга ЕС»",
            keywords=["dluh", "EU", "suverenita"],
            n_documents=27,
            n_authors=9,
            window_start=window_end - datetime.timedelta(days=13),
            window_end=window_end,
            layers=[Layer.TELEGRAM, Layer.ALTERNATIVE],
        )
        candidate.example_documents.set(documents[:3])
        candidate.nearest_narratives.set([narratives[1]["obj"]])
