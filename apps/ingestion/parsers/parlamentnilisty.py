"""ParlamentníListy.cz (parlamentnilisty.cz), vrstva B.

Bez souhlasu s cookies web přesměruje každý článek na /nastaveni-souhlasu. Souhlas je
uložený v `collector_config.cookies` zdroje (cookie pl-cmp-consent-content, platí rok);
po vypršení sběr hlásí chybu „obnovte cookies zdroje“ a stránku souhlasu neukládá.
"""

from . import register
from .base import SiteParser, inside

BODY = '//div[@class="article-container"]'


@register
class ParlamentniListy(SiteParser):
    key = "parlamentnilisty"
    name = "ParlamentníListy.cz"
    hosts = ("parlamentnilisty.cz",)
    title = "jsonld"  # na stránce je víc h1 („Politici voličům“, „Další články“)
    perex = '//p[contains(@class, "brief")]'
    blocks = inside(BODY)
    # Vložené ankety, fotogalerie a „Psali jsme:“ (odkazy na jiné články); seznam zdrojů
    # na konci textu („Tento článek přímo v textu aktivně odkazuje na tyto zdroje:“).
    drop = f'{BODY}//section | {BODY}/div[@class="related-articles"]'
    # Zdroje, na které text odkazuje — web uvádí jen domény.
    origin = f'{BODY}/div[@class="related-articles"]/p[starts-with(normalize-space(.), "http")]'
    gate = r"^/nastaveni-souhlasu"
