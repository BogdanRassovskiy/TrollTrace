"""Дозревание кандидатов вручную: `mature_candidates [--dry-run] [--days N] [--limit N]`.

--days переопределяет паузу (например, 0 — разобрать всю текущую очередь сразу).
--recluster — сначала пересобрать нерешённые кластеры (после смены порога склейки).
"""

from django.core.management.base import BaseCommand

from apps.narratives.models import NarrativeCandidate
from apps.processing import novelty


class Command(BaseCommand):
    help = "Dozrávání kandidátů: malé shluky bez nových dokumentů přiřadí k narativu nebo vyprší."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Jen ukázat, nic neměnit.")
        parser.add_argument("--days", type=int, help="Pauza ve dnech místo nastavení.")
        parser.add_argument("--limit", type=int, help="Nejvýše tolik shluků.")
        parser.add_argument("--recluster", action="store_true", help="Nejdřív přeskupit shluky.")

    def handle(self, *args, dry_run=False, days=None, limit=None, recluster=False, **options):
        if recluster and not dry_run:
            before = NarrativeCandidate.objects.filter(status="new").count()
            after = novelty.recluster()
            self.stdout.write(f"Přeskupeno: {before} otevřených shluků → {after}.")
        stats = novelty.mature(days=days, limit=limit, dry_run=dry_run)
        if not dry_run:
            novelty.notify_maturation(stats)
        prefix = "Zkouška (nic nezměněno): " if dry_run else ""
        self.stdout.write(
            f"{prefix}přiřazeno {stats['distributed']}, vypršelo {stats['expired']}, "
            f"návrhů {stats['suggested']}, přeskočeno {stats['skipped']}, "
            f"cena {stats['cost_usd']} USD."
        )
        for code, n in sorted(stats["targets"].items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {n:3d} → {code}")
        if stats.get("error"):
            self.stderr.write(f"Přerušeno: {stats['error']}")
