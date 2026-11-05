"""Novinky.cz (novinky.cz), vrstva D."""

from . import register
from .seznam import SeznamPlatform


@register
class Novinky(SeznamPlatform):
    key = "novinky"
    name = "Novinky.cz"
    hosts = ("novinky.cz",)
