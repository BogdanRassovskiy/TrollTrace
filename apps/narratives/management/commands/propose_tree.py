"""Návrh stromu kódové knihy modelem (nadnarativy a zařazení narativů); schvaluje se v panelu.

python manage.py propose_tree               # všechna témata s alespoň dvěma narativy
python manage.py propose_tree --topic ukrajina
"""

from django.core.management.base import BaseCommand, CommandError

from apps.narratives.models import Topic
from apps.narratives.tree_draft import propose, topic_nodes


class Command(BaseCommand):
    help = "Navrhne strom kódové knihy (nadnarativy) pro témata; návrhy schválíte v panelu."

    def add_arguments(self, parser):
        parser.add_argument("--topic", action="append", default=[], help="Kód tématu.")

    def handle(self, *args, topic, **options):
        from apps.processing import llm

        if not llm.is_configured():
            raise CommandError("Chybí OPENROUTER_API_KEY — návrh stromu dělá model.")
        topics = Topic.objects.order_by("code")
        if topic:
            topics = topics.filter(code__in=topic)
        for item in topics:
            if len(topic_nodes(item)) < 2:
                continue
            try:
                draft = propose(item)
            except llm.LLMError as exc:
                self.stderr.write(f"{item.name}: model selhal — {exc}")
                continue
            groups = len(draft.proposal["groups"])
            cost = f", {draft.cost_usd:.4f} USD" if draft.cost_usd else ""
            self.stdout.write(f"{item.name}: návrh č. {draft.pk}, nadnarativů {groups}{cost}")
