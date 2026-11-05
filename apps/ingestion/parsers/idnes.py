"""iDNES.cz (idnes.cz), vrstva D."""

from . import register
from .base import AUTHOR_PREFIX, SiteParser, inside

BODY = '//div[@id="art-text"]'
AUTHORS = '//div[contains(@class, "authors")]'


@register
class IDnes(SiteParser):
    key = "idnes"
    name = "iDNES.cz"
    hosts = ("idnes.cz",)
    title = '//h1[contains(@class, "arttit")] | //div[contains(@class, "art-full")]//h1'
    perex = '//div[@class="opener"]'
    blocks = inside(BODY)
    # Odkazy na galerie a související články (tabulky), boxy iDNES.tv, fotogalerie v textu,
    # video, reklama a u prémiových článků paywall s nabídkou předplatného.
    drop = (
        f"{BODY}//table | {BODY}//*[contains(@class, 'equip') or @data-redistribute"
        " or contains(@class, 'box-idntv') or contains(@class, 'imagelist')"
        " or contains(@class, 'paywall') or contains(@class, 'switch-off')]"
    )

    def authors(self, root, ld):
        # Jména redaktorů jsou v itemprop="name" (vedle zkratky „bur“), agentury jen textem.
        names = self.values(root, f'{AUTHORS}//*[@itemprop="name"]')
        if not names:
            names = [AUTHOR_PREFIX.sub("", n) for n in self.values(root, AUTHORS)]
        return ", ".join(names)
