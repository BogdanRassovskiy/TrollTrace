"""Tadesco (tadesco.org), vrstva B. Šablona tagDiv (Newspaper)."""

from . import register
from .base import SiteParser, inside

BODY = '//div[contains(@class, "tdb_single_content")]//div[contains(@class, "tdb-block-inner")]'


@register
class Tadesco(SiteParser):
    key = "tadesco"
    name = "Tadesco"
    hosts = ("tadesco.org",)
    title = '//h1[contains(@class, "tdb-title-text")]'
    blocks = inside(BODY)
    drop = f"{BODY}//figure"
    author = '//div[contains(@class, "tdb_single_author")]//a[contains(@class, "tdb-author-name")]'
    rubric = '//div[contains(@class, "tdb_single_categories")]//a'
    # Domovská stránka a hlavní rubriky (RSS zakazuje robots.txt, sitemap je bez dat).
    listing_xpath = '//h3[contains(@class, "entry-title")]/a/@href'
