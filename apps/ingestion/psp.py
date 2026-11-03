"""Poslanecká sněmovna (слой E): реестр депутатов из открытых данных и разбор стенограмм.

Открытые данные: https://www.psp.cz/sqw/hp.sqw?k=1300 — архив poslanci.zip (файлы .unl,
разделитель «|», кодировка windows-1250). id_osoba из реестра совпадает с id в ссылках
стенограмм (/sqw/detail.sqw?id=…), поэтому выступления привязываются к акторам точно.
"""

import datetime
import io
import re
import zipfile
from dataclasses import dataclass, field

import lxml.html

PSP = "https://www.psp.cz"
OPEN_DATA_URL = f"{PSP}/eknih/cdrom/opendata/poslanci.zip"
SOURCE_URL = f"{PSP}/"
DETAIL_URL = PSP + "/sqw/detail.sqw?id={id}"

# Тип органа «Parlament» (созыв PSP) и «Klub» в typ_organu.unl.
ORGAN_TYPE_TERM = "11"
ORGAN_TYPE_CLUB = "1"

# Сокращения клубов → обычные названия партий.
CLUB_LABELS = {"ANO2011": "ANO", "TOP09": "TOP 09", "MS": "Motoristé"}


def term_slug(term_start: datetime.date) -> str:
    """Каталог созыва на psp.cz: 2025ps для созыва, начавшегося в 2025 году."""
    return f"{term_start.year}ps"


def photo_url(term_start: datetime.date, id_osoba: int) -> str:
    slug = term_slug(term_start)
    return f"{PSP}/eknih/cdrom/{slug}/eknih/{slug}/poslanci/i{id_osoba}.jpg"


# --- Реестр депутатов ---------------------------------------------------------------------


def _rows(archive: zipfile.ZipFile, name: str) -> list[list[str]]:
    text = archive.read(f"{name}.unl").decode("cp1250")
    return [line.split("|") for line in text.splitlines() if line.strip()]


def _date(value: str) -> datetime.date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class Mandate:
    start: datetime.date
    end: datetime.date | None


@dataclass
class ChamberFunction:
    """Funkce nebo členství v orgánu Sněmovny (výbor, komise, klub, delegace, vedení)."""

    title: str
    organ: str
    leadership: bool  # předseda / místopředseda celé Sněmovny
    start: datetime.date | None
    end: datetime.date | None


# Ženské tvary názvů funkcí (registr je uvádí jen v mužském rodě).
FEMININE = {
    "předseda": "předsedkyně", "místopředseda": "místopředsedkyně", "člen": "členka",
    "ověřovatel": "ověřovatelka", "náhradník": "náhradnice", "ověřovatel výboru":
    "ověřovatelka výboru", "ověřovatel komise": "ověřovatelka komise",
}  # fmt: skip
CHAMBER_GENITIVE = "Poslanecké sněmovny"
LEADERSHIP_FUNCTIONS = {"předseda", "místopředseda"}
# Orgány, u kterých se pro úplnost ukládá i prosté členství (cl_funkce=0).
MEMBERSHIP_ORGAN_TYPES = {"2", "3", "4", "7", "13"}  # komise, výbor, podvýbor, delegace, MPS


@dataclass
class Deputy:
    id_osoba: int
    first_name: str
    last_name: str
    title_before: str
    title_after: str
    gender: str  # M / Z (или Ž)
    club: str
    mandates: list[Mandate] = field(default_factory=list)
    functions: list[ChamberFunction] = field(default_factory=list)
    # Начала созывов, где человек был депутатом, от нового к старому: фото ищутся по ним
    # (у сложивших мандат в начале созыва фото есть только в каталоге прошлого созыва).
    term_starts: list[datetime.date] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def role(self) -> str:
        return "Poslankyně PSP" if self.is_female else "Poslanec PSP"

    @property
    def is_female(self) -> bool:
        return self.gender in {"Z", "Ž"}  # в реестре встречаются оба варианта

    def function_title(self, name: str) -> str:
        name = name.strip().lower()
        return FEMININE.get(name, name) if self.is_female else name

    @property
    def is_active(self) -> bool:
        return any(m.end is None for m in self.mandates)


@dataclass
class Term:
    organ_id: str
    code: str  # PSP10
    start: datetime.date
    deputies: list[Deputy]


def parse_registry(zip_bytes: bytes, term_code: str | None = None) -> Term:
    """Депутаты созыва (по умолчанию — последнего) с мандатами и клубом."""
    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    organs = _rows(archive, "organy")
    terms = [o for o in organs if o[2] == ORGAN_TYPE_TERM and o[3].startswith("PSP")]
    if term_code:
        term = next(o for o in terms if o[3] == term_code)
    else:
        term = max(terms, key=lambda o: _date(o[6]) or datetime.date.min)
    term_id, term_start = term[0], _date(term[6])

    clubs = {
        o[0]: CLUB_LABELS.get(o[3], o[3])
        for o in organs
        if o[2] == ORGAN_TYPE_CLUB and o[1] == term_id
    }
    people = {r[0]: r for r in _rows(archive, "osoby")}
    term_start_of = {o[0]: _date(o[6]) for o in terms}
    deputy_rows = _rows(archive, "poslanec")
    deputy_ids = {r[1] for r in deputy_rows if r[4] == term_id}
    starts_of: dict[str, set[datetime.date]] = {}
    for row in deputy_rows:
        if row[1] in deputy_ids and term_start_of.get(row[4]):
            starts_of.setdefault(row[1], set()).add(term_start_of[row[4]])

    # Органы созыва: сама Sněmovna, её клубы, комитеты, комиссии, делегации и их подорганы.
    term_organs = {o[0]: o for o in organs if o[0] == term_id or o[1] == term_id}
    term_organs |= {o[0]: o for o in organs if o[1] in term_organs and o[0] not in term_organs}
    functions_of = {r[0]: r for r in _rows(archive, "funkce")}
    chamber: dict[str, list[ChamberFunction]] = {}

    mandates: dict[str, list[Mandate]] = {}
    memberships: dict[str, list[tuple[bool, datetime.date, str]]] = {}
    for id_osoba, id_of, cl_funkce, od, do, *_ in _rows(archive, "zarazeni"):
        if id_osoba not in deputy_ids:
            continue
        if cl_funkce == "1":  # функция: id_of — строка таблицы funkce
            function = functions_of.get(id_of)
            organ = term_organs.get(function[1]) if function else None
            if organ is not None:
                chamber.setdefault(id_osoba, []).append((function[3], organ, _date(od), _date(do)))
            continue
        if cl_funkce != "0":
            continue  # cl_funkce=0 — членство в органе
        if id_of in term_organs and term_organs[id_of][2] in MEMBERSHIP_ORGAN_TYPES:
            chamber.setdefault(id_osoba, []).append(
                ("člen", term_organs[id_of], _date(od), _date(do))
            )
        if id_of == term_id:
            mandates.setdefault(id_osoba, []).append(Mandate(_date(od), _date(do)))
        elif id_of in clubs:
            is_current = not do.strip()
            memberships.setdefault(id_osoba, []).append(
                (is_current, _date(od) or datetime.date.min, clubs[id_of])
            )
    # Действующий клуб, иначе последний; депутаты вне клубов — пустая строка.
    club_of = {pid: max(items)[2] for pid, items in memberships.items()}

    deputies = []
    for id_osoba in sorted(deputy_ids, key=int):
        person = people[id_osoba]
        deputy = Deputy(
            id_osoba=int(id_osoba),
            title_before=person[1].strip(),
            last_name=person[2].strip(),
            first_name=person[3].strip(),
            title_after=person[4].strip(),
            gender=person[6].strip(),
            club=club_of.get(id_osoba, ""),
            mandates=sorted(mandates.get(id_osoba, []), key=lambda m: m.start),
            term_starts=sorted(starts_of.get(id_osoba, {term_start}), reverse=True),
        )
        for name, organ, start, end in chamber.get(id_osoba, []):
            title = deputy.function_title(name)
            # Vedení Sněmovny — jen předseda a místopředsedové; ověřovatelé ne.
            leadership = organ[0] == term_id and name.strip().lower() in LEADERSHIP_FUNCTIONS
            deputy.functions.append(
                ChamberFunction(
                    title=f"{title} {CHAMBER_GENITIVE}" if leadership else f"{title} ({organ[4]})",
                    organ=organ[4],
                    leadership=leadership,
                    start=start,
                    end=end,
                )
            )
        deputies.append(deputy)
    return Term(organ_id=term_id, code=term[3], start=term_start, deputies=deputies)


# --- Стенограммы --------------------------------------------------------------------------

MONTHS = {
    "ledna": 1, "února": 2, "března": 3, "dubna": 4, "května": 5, "června": 6,
    "července": 7, "srpna": 8, "září": 9, "října": 10, "listopadu": 11, "prosince": 12,
}  # fmt: skip

DAY_LINK = re.compile(r'href="(\d{3}schuz)/(\d+-\d+)\.html"')
PAGE_LINK = re.compile(r'href="(s\d{6}\.htm)')
SPEAKER_ID = re.compile(r"detail\.sqw\?id=(\d+)")
DATE_TEXT = re.compile(r"(\d{1,2})\.\s*([^\W\d_]+)\s+(\d{4})")
TIME_TEXT = re.compile(r"\((\d{1,2})\.(\d{2}) hodin\)")
CONTINUES = re.compile(r"\(pokračuje\s+([^)]+)\)")


def day_pages(term_index_html: str) -> list[tuple[str, str]]:
    """[(«001schuz», «1-1»), …] в порядке от новых к старым."""
    seen = dict.fromkeys(DAY_LINK.findall(term_index_html))
    return sorted(seen, key=lambda d: (d[0], [int(x) for x in d[1].split("-")]), reverse=True)


def speech_pages(day_html: str) -> list[str]:
    return list(dict.fromkeys(PAGE_LINK.findall(day_html)))


@dataclass
class Speech:
    anchor: str  # r1, r2… или «pokracovani» для продолжения с прошлой страницы
    speaker_id: int | None
    speaker_label: str
    text: str


@dataclass
class StenoPage:
    date: datetime.date | None
    time: datetime.time | None
    speeches: list[Speech]


def _clean(text: str) -> str:
    return re.sub(r"[ \t ]+", " ", text).strip()


def parse_steno_page(html: str) -> StenoPage:
    root = lxml.html.fromstring(html)
    main = root.get_element_by_id("main-content", None)
    if main is None:
        return StenoPage(None, None, [])
    full_text = main.text_content()

    date = None
    if m := DATE_TEXT.search(" ".join(root.xpath('//p[@class="date"]//text()'))):
        day, month, year = m.groups()
        if month.lower() in MONTHS:
            date = datetime.date(int(year), MONTHS[month.lower()], int(day))
    time = None
    if m := TIME_TEXT.search(full_text):
        time = datetime.time(int(m.group(1)) % 24, int(m.group(2)))

    speeches: list[Speech] = []
    current: Speech | None = None
    if m := CONTINUES.search(full_text[:500]):
        current = Speech("pokracovani", None, _clean(m.group(1)), "")

    for p in main.iter("p"):
        if p.get("class") in {"date", "links no-print"}:
            continue
        link = p.xpath("./b/a[@id]") or p.xpath("./a[@id][contains(@href, 'detail.sqw')]")
        if link:
            a = link[0]
            if current is not None and current.text:
                speeches.append(current)
            href_id = SPEAKER_ID.search(a.get("href", ""))
            label = _clean(a.text_content())
            body = _clean(p.text_content())
            body = body[len(label) :].lstrip(" :") if body.startswith(label) else body
            current = Speech(a.get("id"), int(href_id.group(1)) if href_id else None, label, body)
        elif current is not None:
            paragraph = _clean(p.text_content())
            if paragraph:
                current.text = f"{current.text}\n{paragraph}".strip()
    if current is not None and current.text:
        speeches.append(current)
    return StenoPage(date, time, speeches)


def name_from_label(label: str) -> str:
    """«Předseda vlády ČR Andrej Babiš» → «Andrej Babiš» (две последние части)."""
    return " ".join(label.split()[-2:])
