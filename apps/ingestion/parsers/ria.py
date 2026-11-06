"""RIA Novosti (ria.ru), vrstva Z. Ruská státní agentura, texty v ruštině."""

from . import register
from .base import SiteParser

BODY = '//div[contains(@class, "article__body")]'


@register
class Ria(SiteParser):
    key = "ria"
    name = "RIA Novosti"
    hosts = ("ria.ru",)
    # Hlavní titulek je div.article__title (rámování redakce, např. „Каллас дерзко ответила…“);
    # h1.article__second-title je věcný podtitulek — jde do textu jako perex.
    title = '//*[contains(@class, "article__title")]'
    perex = '//h1[contains(@class, "article__second-title")]'
    blocks = (
        f'{BODY}/div[@class="article__block"]/div[@class="article__text"]'
        f' | {BODY}/div[@class="article__block"]/h2 | {BODY}/div[@class="article__block"]/h3'
        f' | {BODY}//div[contains(@class, "article__quote-text")]'
    )
    # Shrnutí od „РИА ИИ“ (strojové), odkazy na jiné články, fotografie, obsah, tabulky
    # a poznámky pod čarou („* … запрещенные в России“).
    drop = (
        f'{BODY}/div[contains(@class, "article__summary")]'
        f' | {BODY}//div[contains(@class, "article__article")] | {BODY}//aside'
        f' | {BODY}//div[contains(@class, "article__media")]'
        f' | {BODY}//div[@class="article__contents"] | {BODY}//div[@class="article__table"]'
        f' | {BODY}//div[@class="article__text"][starts-with(normalize-space(.), "*")]'
    )
    author = '//meta[@property="article:author"]/@content'
    # Rubrika je štítek s odkazem na oddíl (/world/, /politics/); ostatní štítky jsou místa
    # (/location_…), osoby (/person/…) a události (/category_…). Bez oddílu — rubrika výpisu.
    rubric = (
        '(//div[contains(@class, "article__tags")]//a[not(contains(@href, "_"))'
        ' and not(contains(@href, "/person/")) and not(contains(@href, "/organization/"))])[1]'
    )
    listing_pattern = r"^/\d{8}/[a-z0-9-]+-\d+\.html$"
