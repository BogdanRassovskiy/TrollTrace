"""Rádio Sputnik (radiosputnik.ru), vrstva Z. Skupina „Россия сегодня“, stejný redakční systém
jako RIA Novosti — stejné selektory, vlastní klíč, doména a fixtures."""

from . import register
from .ria import BODY, Ria


@register
class Sputnik(Ria):
    key = "sputnik"
    name = "Rádio Sputnik"
    hosts = ("radiosputnik.ru",)
    # Výzva k odběru na konci každého textu („Радио Sputnik в MAX. Подписывайтесь!“ + odkaz).
    drop = (
        f"{Ria.drop}"
        f' | {BODY}//div[@class="article__text"][string-length(normalize-space(.)) < 80]'
        '[starts-with(normalize-space(.), "Радио Sputnik в") or contains(., "max.ru/")'
        ' or contains(., "t.me/")]'
    )
