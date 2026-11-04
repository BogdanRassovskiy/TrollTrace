"""Пересчёт недельных агрегатов вручную (README §4 шаг 8)."""

import time

from django.core.management.base import BaseCommand

from apps.analytics import aggregation


class Command(BaseCommand):
    help = "Přepočet týdenních agregátů aktér/skupina × narativ × týden."

    def add_arguments(self, parser):
        parser.add_argument(
            "--incremental", action="store_true",
            help="Jen změny od posledního přepočtu (jinak se přepočítá vše).",
        )  # fmt: skip

    def handle(self, *args, **options):
        started = time.monotonic()
        if options["incremental"]:
            stats = aggregation.update_incremental()
        else:
            stats = aggregation.update(full=True)
        kind = "úplný" if stats.full else "přírůstkový"
        self.stdout.write(self.style.SUCCESS(
            f"Přepočet ({kind}): párů aktér × týden {stats.pairs}, řádků aktérů "
            f"{stats.actor_rows}, skupin {stats.group_rows}, z-skóre změněno {stats.z_updated}, "
            f"{time.monotonic() - started:.1f} s."
        ))  # fmt: skip
