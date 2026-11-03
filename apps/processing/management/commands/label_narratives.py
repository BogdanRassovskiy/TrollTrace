"""Разметка нарративов вручную: очередь кандидатов, решения модели, сравнение моделей."""

import json
import random
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.documents.models import Document
from apps.narratives.models import LabelDecision
from apps.processing import labeling, llm
from apps.processing.embeddings import get_center


class Command(BaseCommand):
    help = (
        "Anotace narativů (README §4, krok 7): kandidáti z celého relevantního korpusu, "
        "rozhodnutí modelu OpenRouter, srovnání modelů na stejném vzorku."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue", action="store_true", help="Zařadit kandidáty všech relevantních dokumentů."
        )
        parser.add_argument("--limit", type=int, help="Posoudit nejvýše N dokumentů.")
        parser.add_argument(
            "--retry",
            action="store_true",
            help="Vrátit do fronty rozhodnutí, která skončila chybou.",
        )
        parser.add_argument(
            "--compare", nargs="+", metavar="MODEL",
            help="Srovnat modely OpenRouter na vzorku kandidátů (nic se neukládá).",
        )  # fmt: skip
        parser.add_argument("--sample", type=int, default=20, help="Velikost vzorku pro --compare.")
        parser.add_argument("--out", help="Uložit odpovědi modelů ze srovnání do JSON.")

    def handle(self, *args, **options):
        if options["queue"]:
            prototypes, center = labeling.load_prototypes(), get_center()
            if center is None:
                raise CommandError("Chybí střed prostoru embeddingů: spusťte process_documents.")
            queued = sum(
                labeling.queue_candidates(document, prototypes, center)
                for document in Document.objects.filter(
                    is_relevant=True, embedding__isnull=False
                ).iterator(chunk_size=500)
            )
            self.stdout.write(f"Nových kandidátů ve frontě: {queued}")
        if options["retry"]:
            self.stdout.write(f"Znovu ve frontě po chybě: {labeling.retry_errors()}")
        if options["compare"]:
            return self.compare(options["compare"], options["sample"], options["out"])
        waiting = labeling.pending().count()
        if not llm.is_configured():
            self.stdout.write(
                self.style.WARNING(
                    f"Kandidátů čeká {waiting}. Model nelze zavolat: v .env chybí "
                    "OPENROUTER_API_KEY."
                )
            )
            return
        stats = labeling.label_pending(limit=options["limit"])
        message = (
            f"Posouzeno dokumentů {stats.documents}, rozhodnutí {stats.decisions}, nových štítků "
            f"{stats.labels}, chyb {stats.errors}. Útrata za měsíc: {stats.cost_usd} USD."
        )
        self.stdout.write(self.style.SUCCESS(message))
        if stats.stopped:
            self.stdout.write(self.style.WARNING(f"Zastaveno: {stats.stopped}."))

    def compare(self, models: list[str], size: int, out: str | None):
        if not llm.is_configured():
            raise CommandError("V .env chybí OPENROUTER_API_KEY.")
        document_ids = sorted(set(LabelDecision.objects.values_list("document_id", flat=True)))
        if not document_ids:
            raise CommandError("Nejsou žádní kandidáti: spusťte nejdřív --queue.")
        sample = random.Random(42).sample(document_ids, min(size, len(document_ids)))
        answers: dict[str, dict] = {model: {} for model in models}
        totals = {model: {"cost": 0.0, "seconds": 0.0, "errors": 0} for model in models}
        for document_id in sample:
            decisions = list(
                LabelDecision.objects.filter(document_id=document_id)
                .select_related("document", "narrative", "narrative_version")
                .order_by("narrative__code", "-created_at")
            )
            unique = list({d.narrative_id: d for d in reversed(decisions)}.values())
            document = unique[0].document
            for model in models:
                started = time.monotonic()
                try:
                    results, completion = labeling.decide_document(
                        document, unique, model, save=False
                    )
                except llm.LLMError as exc:
                    totals[model]["errors"] += 1
                    self.stderr.write(f"{model} / dokument {document_id}: {exc}")
                    continue
                totals[model]["seconds"] += time.monotonic() - started
                totals[model]["cost"] += completion.cost_usd or 0
                for decision, answer in zip(unique, results, strict=True):
                    key = f"{document_id}:{decision.narrative.code}"
                    answers[model][key] = {
                        "present": answer.get("present"), "stance": answer.get("stance"),
                        "confidence": answer.get("confidence"),
                        "evidence": answer.get("evidence"), "reason": answer.get("reason"),
                        "title": document.title[:120],
                    }  # fmt: skip

        self.stdout.write(f"Vzorek: {len(sample)} dokumentů.\n")
        for model in models:
            given = answers[model].values()
            present = [a for a in given if a["present"]]
            stances = Counter(a["stance"] for a in present)
            self.stdout.write(
                f"{model}: rozhodnutí {len(given)}, narativ přítomen {len(present)} "
                f"({dict(stances)}), chyb {totals[model]['errors']}, "
                f"cena {totals[model]['cost']:.4f} USD, čas {totals[model]['seconds']:.0f} s"
            )
        self.stdout.write("\nShoda dvojic modelů (přítomnost; přítomnost + postoj):")
        for left, right in combinations(models, 2):
            keys = answers[left].keys() & answers[right].keys()
            if not keys:
                continue
            same = sum(answers[left][k]["present"] == answers[right][k]["present"] for k in keys)
            both = sum(
                answers[left][k]["present"] == answers[right][k]["present"]
                and (not answers[left][k]["present"]
                     or answers[left][k]["stance"] == answers[right][k]["stance"])
                for k in keys
            )  # fmt: skip
            self.stdout.write(
                f"  {left} × {right}: {same / len(keys):.0%}; "
                f"{both / len(keys):.0%} (n={len(keys)})"
            )
        if out:
            Path(out).write_text(json.dumps(answers, ensure_ascii=False, indent=2), "utf-8")
            self.stdout.write(f"\nOdpovědi uloženy: {out}")
