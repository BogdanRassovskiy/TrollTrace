"""Европарламент, правительство, партийные политики → политики-акторы с функциями
(README §2 «Акторы»).

python manage.py sync_politicians                # все списки
python manage.py sync_politicians --only meps    # только евродепутаты
python manage.py sync_politicians --only parties # ручной список data/party_politicians.json
"""

from django.core.management.base import BaseCommand, CommandError

from apps.actors import registry
from apps.actors.models import ActorPosition
from apps.ingestion.http import PoliteClient

LISTS = {
    "meps": ("Europoslanci", registry.mep_records, ActorPosition.Kind.MEP),
    "government": ("Členové vlády", registry.government_records, ActorPosition.Kind.GOVERNMENT),
}
MANUAL = "parties"  # ruční seznam: více druhů funkcí a lidé bez funkce


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
    # Ручной список — последним: евродепутаты и министры к этому моменту уже заведены.
    if only in (None, MANUAL):
        results[MANUAL] = registry.apply_manual()
    registry.refresh_roles()  # u bývalých politiků „dříve …; bez veřejné funkce“
    return results


class Command(BaseCommand):
    help = (
        "Synchronizuje české europoslance, členy vlády a předsedy stran s aktéry "
        "(funkce, období zařazení)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=[*LISTS, MANUAL])

    def handle(self, *args, only, **options):
        try:
            results = run(only)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        for key, result in results.items():
            label = LISTS[key][0] if key in LISTS else "Stranické funkce a politici bez funkce"
            self.stdout.write(
                f"{label}: nových aktérů {result.created}, nalezeno existujících "
                f"{result.matched}, ukončených funkcí {result.closed}"
            )
