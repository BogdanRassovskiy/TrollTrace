"""Европарламент, правительство, председатели партий → политики-акторы с функциями
(README §2 «Акторы»).

python manage.py sync_politicians                # оба списка
python manage.py sync_politicians --only meps    # только евродепутаты
"""

from django.core.management.base import BaseCommand, CommandError

from apps.actors import registry
from apps.actors.models import ActorPosition
from apps.ingestion.http import PoliteClient

LISTS = {
    "meps": ("Europoslanci", registry.mep_records, ActorPosition.Kind.MEP),
    "government": ("Členové vlády", registry.government_records, ActorPosition.Kind.GOVERNMENT),
    "party_leaders": (
        "Předsedové stran",
        registry.party_leader_records,
        ActorPosition.Kind.PARTY_LEADER,
    ),
}


def run(only=None) -> dict[str, registry.SyncResult]:
    results = {}
    with PoliteClient(delay=2.0) as client:
        for key, (_, fetch, kind) in LISTS.items():
            if only and key != only:
                continue
            records = fetch(client)
            if not records:  # пустой ответ не должен закрыть все функции
                raise RuntimeError(f"{key}: prázdný seznam")
            results[key] = registry.apply_records(records, kind)
    return results


class Command(BaseCommand):
    help = (
        "Synchronizuje české europoslance, členy vlády a předsedy stran s aktéry "
        "(funkce, období zařazení)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=list(LISTS))

    def handle(self, *args, only, **options):
        try:
            results = run(only)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        for key, result in results.items():
            self.stdout.write(
                f"{LISTS[key][0]}: nových aktérů {result.created}, nalezeno existujících "
                f"{result.matched}, ukončených funkcí {result.closed}"
            )
