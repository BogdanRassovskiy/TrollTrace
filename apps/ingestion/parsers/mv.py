"""Ministerstvo vnitra — zpravodajství (mv.gov.cz), vrstva I."""

from . import register
from .base import SiteParser, inside

BODY = '//section[@id="article-chapter-content"]'


@register
class MV(SiteParser):
    key = "mv"
    name = "Ministerstvo vnitra — zpravodajství"
    hosts = ("mv.gov.cz",)
    perex = '(//p[contains(@class, "mb-s-nudge")])[1]'  # lid pod nadpisem, mimo tělo
    blocks = inside(BODY)
    # Tabulky (statistiky po krajích) se rozpadají na jednotlivá čísla; podpis tiskového
    # odboru na konci („Adéla Dittmannová<br>odbor komunikace…“) není text zprávy.
    drop = f'{BODY}//table | {BODY}//p[br and contains(., "odbor komunikace")]'
    author = '//meta[@property="article:author"]/@content'
    # Výpis zpravodajství: články nemají společný začátek adresy, odkazy jsou v nadpisech.
    listing_xpath = "//li//h3/a/@href"
