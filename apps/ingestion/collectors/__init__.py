"""Коллекторы по платформам. Слой A (Pravda) и B собираются тем же веб-коллектором, что и D."""

from apps.actors.models import Platform, Source

from .psp import PspCollector
from .telegram import TelegramCollector
from .web import WebCollector

COLLECTORS = {
    Platform.WEB: WebCollector,
    Platform.TELEGRAM: TelegramCollector,
    Platform.PARLIAMENT: PspCollector,
}


def collector_for(source: Source):
    try:
        return COLLECTORS[source.platform]
    except KeyError:
        raise ValueError(f"Pro platformu neexistuje kolektor: {source.platform!r}") from None
