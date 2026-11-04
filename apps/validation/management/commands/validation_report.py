"""Результаты валидации в терминале или в JSON.

python manage.py validation_report 3
python manage.py validation_report 3 --model openai/gpt-6-luna --json > validace.json
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.validation import stats
from apps.validation.models import Sample


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.3f}"


class Command(BaseCommand):
    help = "Shoda anotátorů a přesnost / úplnost automatiky pro validační vzorek."

    def add_arguments(self, parser):
        parser.add_argument("sample", type=int)
        parser.add_argument("--model", default="", help="Hodnotit jen rozhodnutí tohoto modelu.")
        parser.add_argument("--json", action="store_true", help="Výstup jako JSON.")

    def handle(self, *args, sample, model, json: bool, **options):
        try:
            obj = Sample.objects.get(pk=sample)
        except Sample.DoesNotExist as exc:
            raise CommandError(f"Vzorek {sample} neexistuje.") from exc
        report = stats.evaluate(obj, model)
        if json:
            self.stdout.write(_dumps(report))
            return
        self.stdout.write(
            f"{obj} — položek {report['items']}, anotátorů {report['annotators']}, "
            f"otevřených sporů {report['disagreements_open']}"
        )
        for r in report["results"]:
            p = r["presence"]
            self.stdout.write(
                f"{r['label']}: α={_fmt(r['alpha'])} (přítomnost {_fmt(r['alpha_presence'])}), "
                f"ref. hodnot {r['gold_items']}, P={_fmt(p['precision'])} R={_fmt(p['recall'])} "
                f"F1={_fmt(p['f1'])}, populace P={_fmt(r['presence_weighted']['precision'])} "
                f"R={_fmt(r['presence_weighted']['recall'])}, postoj {_fmt(r['stance_accuracy'])}"
            )


def _dumps(report) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
