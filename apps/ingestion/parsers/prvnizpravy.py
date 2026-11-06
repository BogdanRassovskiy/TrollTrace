"""První zprávy (prvnizpravy.cz), vrstva B. Bez RSS — sběr ze stránky-seznamu."""

import datetime
import re

from apps.ingestion.extract import PRAGUE

from . import register
from .base import SiteParser, lines_text

ARTICLE = '//div[@class="zpravy_full"]'
# Měsíc v 1. pádě („6.listopad 2026 - 08:06“).
MONTHS = {
    name: i
    for i, name in enumerate(
        ["leden", "únor", "březen", "duben", "květen", "červen",
         "červenec", "srpen", "září", "říjen", "listopad", "prosinec"],
        1,
    )
}  # fmt: skip
DATE = re.compile(
    r"(?P<day>\d{1,2})\.\s*(?P<month>[^\W\d_]+)\s+(?P<year>\d{4})(?:\s*-\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)


@register
class PrvniZpravy(SiteParser):
    key = "prvnizpravy"
    name = "První zprávy"
    hosts = ("prvnizpravy.cz",)
    title = f"{ARTICLE}/h1"
    perex = f'{ARTICLE}/p[contains(@class, "description")]'
    # Text bez odstavců, řádky oddělené <br><br>; vnořené <div> jsou další odstavce.
    blocks = f'{ARTICLE}/div[@class="text"]'
    # Bannery a odkaz „Psali jsme: …“ na jiný článek.
    drop = (
        f'{ARTICLE}//div[contains(@class, "banner")] | {ARTICLE}//div[starts-with(@id, "banner")]'
        f' | {ARTICLE}//div[starts-with(normalize-space(.), "Psali jsme:")]'
    )
    date = f'{ARTICLE}/div[@class="time"]'
    rubric = f'{ARTICLE}/div[@class="category"]'
    author = ""  # autor jen zkratkou v závorce na konci textu („Chmelík, prvnizpravy.cz“)
    listing_pattern = r"^/(?:zpravy|sloupky)/(?:[a-z0-9-]+/)?[a-z0-9-]+/$"

    def parse_date(self, value: str) -> datetime.datetime | None:
        match = DATE.search(value)
        if match is None or match["month"].lower() not in MONTHS:
            return None
        return datetime.datetime(
            int(match["year"]), MONTHS[match["month"].lower()], int(match["day"]),
            int(match["hour"] or 0), int(match["minute"] or 0), tzinfo=PRAGUE,
        )  # fmt: skip

    def block_text(self, node) -> str:
        return lines_text(node)
