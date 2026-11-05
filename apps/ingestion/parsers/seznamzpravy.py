"""Seznam Zprávy (seznamzpravy.cz), vrstva D."""

from . import register
from .seznam import SeznamPlatform


@register
class SeznamZpravy(SeznamPlatform):
    key = "seznamzpravy"
    name = "Seznam Zprávy"
    hosts = ("seznamzpravy.cz",)
