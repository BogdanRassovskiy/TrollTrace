"""Общая часть коллекторов: учёт результатов запуска и выбор актора."""

from collections import Counter

from apps.actors.models import Actor, ActorAccount, Source
from apps.ingestion.http import PoliteClient
from apps.ingestion.store import Item, Outcome, save_item

MAX_ERROR_SAMPLES = 20


class Collector:
    """Коллектор одного источника. collect() наполняет self.stats и self.errors."""

    def __init__(self, source: Source, client: PoliteClient):
        self.source = source
        self.client = client
        self.config = source.collector_config or {}
        self.stats: Counter = Counter()
        self.errors: list[str] = []

    def collect(self) -> None:
        raise NotImplementedError

    def save(self, actor: Actor, item: Item) -> Outcome:
        outcome, _doc = save_item(self.source, actor, item)
        self.stats[outcome.value] += 1
        return outcome

    def error(self, message: str) -> None:
        self.stats["errors"] += 1
        if len(self.errors) < MAX_ERROR_SAMPLES:
            self.errors.append(message[:500])

    def active_accounts(self):
        """Аккаунты актуальных акторов: у исключённых сбор новых данных останавливается."""
        return ActorAccount.objects.filter(
            source=self.source, actor__status=Actor.Status.ACTIVE
        ).select_related("actor")
