"""Слои в шаблонах: буква → «Parlament (E)» (подключено как builtin в TEMPLATES)."""

from django import template

from apps.actors.models import layer_name

register = template.Library()


@register.filter
def layer(code):
    return layer_name(code)


@register.filter
def layers(codes):
    return ", ".join(layer_name(code) for code in codes or [])
