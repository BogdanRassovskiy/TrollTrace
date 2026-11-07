"""Разметка нарративов вручную: очередь кандидатов, ретроразметка, проверка на новые
утверждения, решения модели, сравнение моделей."""

import json
import random
import time
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from apps.documents.models import Document
from apps.narratives.models import LabelDecision, Narrative
from apps.narratives.retro import start_relabel
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
        parser.add_argument(
            "--retro", metavar="KÓD",
            help="Zpětně anotovat archiv podle aktuální verze narativu (jinak se nespouští).",
        )  # fmt: skip
        parser.add_argument(
            "--screen-since", metavar="RRRR-MM-DD",
            help="Zařadit ke kontrole na nová tvrzení relevantní dokumenty publikované od data "
            "(nové dokumenty se zařazují samy).",
        )  # fmt: skip
        parser.add_argument(
            "--recluster", action="store_true",
            help="Znovu sestavit nerozhodnuté shluky nových tvrzení (po změně prahů).",
        )  # fmt: skip
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
        if options["retro"]:
            narrative = Narrative.objects.filter(code=options["retro"]).first()
            if narrative is None:
                raise CommandError(f"Narativ {options['retro']} neexistuje.")
            start_relabel(narrative)
            self.stdout.write(f"Zpětné anotování „{narrative.name}“ zařazeno.")
        if options["screen_since"]:
            since = parse_date(options["screen_since"])
            if since is None:
                raise CommandError("Datum ve tvaru RRRR-MM-DD.")
            documents = Document.objects.filter(
                is_relevant=True, embedding__isnull=False, published_at__date__gte=since
            )
            queued = sum(labeling.queue_screening(d) for d in documents.iterator(chunk_size=500))
            self.stdout.write(f"Ke kontrole na nová tvrzení zařazeno dokumentů: {queued}")
        if options["recluster"]:
            from apps.processing import novelty

            self.stdout.write(f"Shluků nových tvrzení po přestavbě: {novelty.recluster()}")
        if options["retry"]:
            self.stdout.write(f"Znovu ve frontě po chybě: {labeling.retry_errors()}")
        if options["compare"]:
            return self.compare(options["compare"], options["sample"], options["out"])
        waiting = labeling.pending().count() + labeling.pending_screenings().count()
        if not llm.is_configured():
            self.stdout.write(
                self.style.WARNING(
                    f"Ve frontě čeká {waiting}. Model nelze zavolat: v .env chybí "
                    "OPENROUTER_API_KEY."
                )
            )
            return
        stats = labeling.label_pending(limit=options["limit"])
        message = (
            f"Posouzeno dokumentů {stats.documents}, rozhodnutí {stats.decisions}, nových štítků "
            f"{stats.labels}, nových tvrzení {stats.claims}, chyb {stats.errors}. "
            f"Útrata za měsíc: {stats.cost_usd} USD."
        )
        self.stdout.write(self.style.SUCCESS(message))
        if stats.stopped:
            self.stdout.write(self.style.WARNING(f"Zastaveno: {stats.stopped}."))

    def compare(self, models: list[str], size: int, out: str | None):
        """Modely na stejném vzorku; první model je etalon. Nic se neukládá."""
        if not llm.is_configured():
            raise CommandError("V .env chybí OPENROUTER_API_KEY.")
        sample = compare_sample(size)
        if not sample:
            raise CommandError("Nejsou žádní kandidáti: spusťte nejdřív --queue.")
        answers: dict[str, dict] = {model: {} for model in models}
        totals = {model: Counter() for model in models}
        for document_id in sample:
            decisions = list(
                LabelDecision.objects.filter(document_id=document_id)
                .select_related("document__source", "narrative", "narrative_version")
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
                    self.stderr.write(f"{model} / dokument {document_id}: {str(exc)[:300]}")
                    continue
                total = totals[model]
                total["seconds"] += time.monotonic() - started
                total["cost"] += completion.cost_usd or 0
                total["calls"] += 1
                total["prompt"] += completion.prompt_tokens or 0
                total["completion"] += completion.completion_tokens or 0
                total["reasoning"] += completion.reasoning_tokens or 0
                for decision, answer in zip(unique, results, strict=True):
                    key = f"{document_id}:{decision.narrative.code}"
                    answers[model][key] = {
                        "present": answer.get("present"), "stance": answer.get("stance"),
                        "confidence": answer.get("confidence"), "marker": answer.get("marker"),
                        "evidence": answer.get("evidence"), "reason": answer.get("reason"),
                        "title": document.title[:120], "layer": document.source.layer,
                    }  # fmt: skip

        self.stdout.write(f"Vzorek: {len(sample)} dokumentů, etalon {models[0]}.\n")
        for model in models:
            given, total = answers[model].values(), totals[model]
            present = [a for a in given if a["present"]]
            calls = total["calls"] or 1
            self.stdout.write(
                f"{model}: rozhodnutí {len(given)}, přítomen {len(present)} "
                f"({dict(Counter(a['stance'] for a in present))}), chyb {total['errors']}, "
                f"cena {total['cost']:.4f} USD ({total['cost'] / calls:.5f}/dok.), "
                f"čas {total['seconds'] / calls:.0f} s/dok., tokeny na dok.: vstup "
                f"{total['prompt'] / calls:.0f}, výstup {total['completion'] / calls:.0f} "
                f"(z toho uvažování {total['reasoning'] / calls:.0f})"
            )
        reference = answers[models[0]]
        self.stdout.write(f"\nShoda s etalonem {models[0]}:")
        for model in models[1:]:
            self.stdout.write(f"  {model}: " + agreement(reference, answers[model]))
        if out:
            Path(out).write_text(json.dumps(answers, ensure_ascii=False, indent=2), "utf-8")
            self.stdout.write(f"\nOdpovědi uloženy: {out}")


def compare_sample(size: int, seed: int = 42) -> list[int]:
    """Vzorek pro srovnání: ~40 % dokumentů, kde model narativ dřív našel (jinak by vzorek
    tvořila skoro jen „není narativ“), ~20 % vrstvy Z (ruské texty), zbytek náhodně."""
    rng = random.Random(seed)
    candidates = LabelDecision.objects.values_list("document_id", flat=True)
    positive = sorted(set(candidates.filter(present=True)))
    russian = sorted(set(candidates.filter(document__source__layer="Z")) - set(positive))
    chosen = rng.sample(positive, min(round(size * 0.4), len(positive)))
    chosen += rng.sample(russian, min(round(size * 0.2), len(russian)))
    rest = sorted(set(candidates) - set(chosen))
    chosen += rng.sample(rest, min(size - len(chosen), len(rest)))
    return chosen


def agreement(reference: dict, other: dict) -> str:
    keys = reference.keys() & other.keys()
    if not keys:
        return "žádná společná rozhodnutí"
    same = sum(reference[k]["present"] == other[k]["present"] for k in keys)
    found = [k for k in keys if reference[k]["present"]]
    hit = sum(bool(other[k]["present"]) for k in found)
    stance = sum(bool(other[k]["present"]) and other[k]["stance"] == reference[k]["stance"]
                 for k in found)  # fmt: skip
    absent = [k for k in keys if not reference[k]["present"]]
    false_yes = sum(bool(other[k]["present"]) for k in absent)
    return (
        f"přítomnost {same / len(keys):.0%} (n={len(keys)}); z {len(found)} nálezů etalonu "
        f"našel {hit}, se stejným postojem {stance}; navíc „ano“ u {false_yes} z {len(absent)}"
    )
