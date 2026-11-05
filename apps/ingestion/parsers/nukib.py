"""NÚKIB — aktuality (nukib.gov.cz), vrstva I."""

from . import register
from .base import SiteParser, inside


@register
class NUKIB(SiteParser):
    key = "nukib"
    name = "NÚKIB — aktuality"
    hosts = ("nukib.gov.cz",)
    blocks = inside('//*[@itemprop="articleBody"]')
    date = '//time[@itemprop="dateCreated"]/@datetime'  # bez JSON-LD i meta article:*
    author = '//meta[@name="author"]/@content'
