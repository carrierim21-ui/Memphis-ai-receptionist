"""Knowledge base del locale (menu, orari, sale) e riconoscimento del testo in italiano.

Tutto quello che serve per capire date, orari, nomi, indirizzi e prodotti del menu
senza dipendere dall'IA: viene usato sia dal motore di riserva sia per validare
i dati che l'IA estrae.
"""
import difflib
import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
MENU = json.loads((BASE / "data" / "menu.json").read_text(encoding="utf-8"))
LOCALE = MENU.get("locale", {})
RULES = MENU.get("rules", {})
ITEMS = MENU.get("items", [])
BUSINESS_NAME = LOCALE.get("name", "Memphis Pub")
ROOMS = LOCALE.get("rooms", ["sala principale", "sala fumatori", "soppalco"])
ANY_ROOM = "qualsiasi"
OPENING = LOCALE.get("opening", {"open": "18:00", "close": "02:00"})
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Rome"))

CATEGORY_LABELS = {
    "pizze": "pizze",
    "pizze_speciali": "pizze speciali",
    "panini_gourmet": "panini gourmet",
    "piatti": "piatti",
    "stuzzicheria": "stuzzicheria",
    "bevande": "bevande",
    "cocktail": "cocktail",
}
WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
WEEKDAYS_PLAIN = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre"]
NUM_WORDS = {
    "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
    "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12,
    "tredici": 13, "quattordici": 14, "quindici": 15, "sedici": 16, "diciassette": 17,
    "diciotto": 18, "diciannove": 19, "venti": 20, "ventuno": 21, "ventidue": 22,
    "ventitre": 23, "venticinque": 25, "trenta": 30,
}
_NUM_ALT = "|".join(sorted(NUM_WORDS, key=len, reverse=True))


# ---------------------------------------------------------------- testo
def now():
    return datetime.now(TZ)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s):
    s = str(s or "").lower().replace("’", "'").replace("‘", "'").replace("&", " e ")
    return strip_accents(s)


def tokens(s):
    return re.findall(r"[a-z0-9]+", norm(s))


def flat(s):
    """Testo normalizzato con parole separate da un solo spazio (per regex semplici)."""
    return " ".join(tokens(s))


def to_int(tok):
    if tok is None:
        return None
    tok = str(tok)
    if tok.isdigit():
        return int(tok)
    return NUM_WORDS.get(tok)


def fmt_eur(v):
    return ("€%.2f" % v).replace(".", ",")


# ---------------------------------------------------------------- menu
def price_value(item):
    p = item.get("price_eur")
    return float(p) if isinstance(p, (int, float)) else None


def item_has_details(item):
    d = item.get("description") or ""
    return bool(d) and "verificare" not in d.lower()


def describe_item(item):
    parts = [item["name"]]
    if item_has_details(item):
        parts.append(f"({item['description']})")
    p = price_value(item)
    parts.append(f"costa {fmt_eur(p)}" if p is not None else "prezzo da confermare con il personale")
    return " ".join(parts)


SIZE_RE = re.compile(r"^(piccola|piccolo|media|medio|grande|\d+(cl|g|ml)?)$")


def _plural(tok):
    if len(tok) <= 3:
        return None
    if tok.endswith("a"):
        return tok[:-1] + "e"
    if tok.endswith("o") or tok.endswith("e"):
        return tok[:-1] + "i"
    return None


def _variants(toks):
    out = {toks}
    p = _plural(toks[-1])
    if p:
        out.add(toks[:-1] + (p,))
    return out


def _build_phrases():
    phrases = {}
    for i, it in enumerate(ITEMS):
        t = tuple(tokens(it["name"]))
        if t:
            for v in _variants(t):
                phrases.setdefault(v, ("item", i))
    bases = {}
    for i, it in enumerate(ITEMS):
        full = tuple(tokens(it["name"]))
        t = list(full)
        while len(t) > 1 and SIZE_RE.match(t[-1]):
            t.pop()
        t = tuple(t)
        if t != full:
            bases.setdefault(t, []).append(i)
    for b, idxs in bases.items():
        target = ("item", idxs[0]) if len(idxs) == 1 else ("family", tuple(idxs))
        for v in _variants(b):
            phrases.setdefault(v, target)
    return sorted(phrases.items(), key=lambda x: -len(x[0]))


PHRASES = _build_phrases()
# parole che, se compaiono subito prima, indicano che NON si sta ordinando quel prodotto
# ("mozzarella di bufala", "ciao Memphis", "sono vegetariana"...)
BLOCK_PREV = {"di", "del", "della", "dello", "dei", "delle", "degli", "con", "senza", "ciao",
              "salve", "buonasera", "buongiorno", "al", "alla", "allo", "sul", "sulla",
              "chiamo", "sono", "siamo", "sei", "locale"}
BLOCK_NEXT = {"pub"}
QTY_SKIP = {"pizza", "pizze", "porzione", "porzioni", "piatto", "piatti", "panino", "panini",
            "birra", "birre", "bottiglia", "bottiglie", "di", "ne", "anche", "altra", "altre", "altro", "altri"}
MOD_KEYS = {"senza", "con", "aggiunta", "aggiungi", "extra", "doppia", "doppio", "poco", "poca",
            "tanta", "ben", "niente", "no", "tagliata", "tagliate", "cotta", "cottura"}
MOD_STOP = {"per", "alle", "all", "ore", "da", "asporto", "domicilio", "consegna", "ritiro", "nome",
            "mi", "chiamo", "indirizzo", "via", "entro", "verso", "stasera", "domani", "oggi", "grazie",
            "al", "appena", "prima", "subito", "quando"}
MOD_TRAIL = {"e", "ed", "poi", "anche", "una", "un", "uno", "la", "il", "le", "i", "pure"}


def find_items(text, use_blockers=True):
    """Trova i prodotti del menu citati nel testo, con quantità e modifiche.

    Ritorna una lista di dict: {"item": dict|None, "family": [dict]|None, "quantity": int,
    "modifications": str}.
    """
    toks = tokens(text)
    taken = [False] * len(toks)
    matches = []
    for phrase, target in PHRASES:
        L = len(phrase)
        for s in range(0, len(toks) - L + 1):
            if tuple(toks[s:s + L]) != phrase or any(taken[s:s + L]):
                continue
            if use_blockers:
                prev = toks[s - 1] if s > 0 else ""
                nxt = toks[s + L] if s + L < len(toks) else ""
                if prev in BLOCK_PREV or nxt in BLOCK_NEXT:
                    continue
            for k in range(s, s + L):
                taken[k] = True
            matches.append((s, s + L, target))
    matches.sort()
    out = []
    for n, (s, e, target) in enumerate(matches):
        qty = 1
        k = s - 1
        while k >= 0 and toks[k] in QTY_SKIP:
            k -= 1
        explicit = False
        if k >= 0 and to_int(toks[k]) and 0 < to_int(toks[k]) <= 30:
            qty = to_int(toks[k])
            explicit = toks[k] not in ("un", "uno", "una")
        nxt_start = matches[n + 1][0] if n + 1 < len(matches) else len(toks)
        seg = toks[e:nxt_start]
        mods = ""
        for j, t in enumerate(seg):
            if t in MOD_KEYS:
                part = []
                for t2 in seg[j:]:
                    if t2 in MOD_STOP:
                        break
                    part.append(t2)
                while part and (part[-1] in MOD_TRAIL or to_int(part[-1])):
                    part.pop()
                if len(part) >= 2:
                    mods = " ".join(part)
                break
        kind, ref = target
        entry = {"item": None, "family": None, "quantity": qty, "qty_explicit": explicit,
                 "modifications": mods, "start": s, "end": e}
        if kind == "item":
            entry["item"] = ITEMS[ref]
        else:
            entry["family"] = [ITEMS[i] for i in ref]
        out.append(entry)
    return out


def resolve_item(name):
    """Associa un nome detto dal cliente/IA a un prodotto reale. Ritorna (item, suggerimenti)."""
    n = flat(name)
    for it in ITEMS:
        if flat(it["name"]) == n:
            return it, []
    found = find_items(name, use_blockers=False)
    if len(found) == 1 and found[0]["item"]:
        return found[0]["item"], []
    if len(found) == 1 and found[0]["family"]:
        return None, [x["name"] for x in found[0]["family"]]
    names = {flat(it["name"]): it for it in ITEMS}
    close = difflib.get_close_matches(n, list(names), n=3, cutoff=0.6)
    if close and difflib.SequenceMatcher(None, n, close[0]).ratio() >= 0.85:
        return names[close[0]], []
    return None, [names[c]["name"] for c in close]


def search_menu(text, limit=8):
    """Ricerca libera (es. 'pizze con funghi', 'birre senza glutine')."""
    toks = [t for t in tokens(text) if len(t) > 2]
    scored = []
    for it in ITEMS:
        hay = flat(it["name"] + " " + (it.get("description") or "") + " " + it.get("category", ""))
        score = sum(1 for t in toks if re.search(r"\b" + re.escape(t), hay))
        if score:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def menu_overview():
    parts = []
    for cat, label in CATEGORY_LABELS.items():
        items = [it for it in ITEMS if it.get("category") == cat]
        if items:
            examples = ", ".join(it["name"] for it in items[:3])
            parts.append(f"{label} ({len(items)}, es. {examples})")
    return "; ".join(parts)


def menu_for_prompt():
    lines = []
    for it in ITEMS:
        p = price_value(it)
        price = fmt_eur(p) if p is not None else "prezzo da verificare"
        desc = it.get("description") if item_has_details(it) else "ingredienti da verificare col personale"
        lines.append(f"- [{CATEGORY_LABELS.get(it.get('category'), it.get('category'))}] {it['name']} | {price} | {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------- date e orari
def _to_minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def within_opening(hhmm):
    o, c, t = _to_minutes(OPENING["open"]), _to_minutes(OPENING["close"]), _to_minutes(hhmm)
    if c <= o:  # chiusura dopo mezzanotte
        return t >= o or t <= c
    return o <= t <= c


def service_datetime(day_iso, hhmm):
    """Orari tra mezzanotte e le 6 appartengono alla serata del giorno indicato."""
    d = date.fromisoformat(day_iso)
    h, m = map(int, hhmm.split(":"))
    dt = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
    if h < 6:
        dt += timedelta(days=1)
    return dt


def service_today(ref=None):
    ref = ref or now()
    return (ref - timedelta(hours=6)).date() if ref.hour < 6 else ref.date()


def fmt_day(day_iso):
    try:
        d = date.fromisoformat(day_iso)
    except (TypeError, ValueError):
        return str(day_iso)
    today = service_today()
    label = f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
    if d == today:
        return f"oggi ({label})"
    if d == today + timedelta(days=1):
        return f"domani ({label})"
    return label


def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_day(text, bare=False, ref=None):
    t = flat(text)
    today = service_today(ref)
    if re.search(r"\bdopodomani\b", t):
        return (today + timedelta(days=2)).isoformat()
    if re.search(r"\bdomani\b", t):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\b(oggi|stasera|stanotte|questa sera|in giornata)\b", t):
        return today.isoformat()
    words = t.split()
    for i, w in enumerate(WEEKDAYS_PLAIN):
        # tollera errori di battitura: "sabto", "venrdi", "domenca"
        if re.search(rf"\b{w}\b", t) or any(len(x) >= 4 and difflib.SequenceMatcher(None, x, w).ratio() >= 0.8
                                            for x in words):
            delta = (i - today.weekday()) % 7
            if delta == 0 and re.search(r"\bprossim", t):
                delta = 7
            return (today + timedelta(days=delta)).isoformat()
    raw = norm(text)
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b", raw)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        dt = _safe_date(y, mo, d)
        if dt and not m.group(3) and dt < today:
            dt = _safe_date(y + 1, mo, d)
        if dt:
            return dt.isoformat()
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", t)
    if m:
        d, mo = int(m.group(1)), MONTHS.index(m.group(2)) + 1
        y = int(m.group(3)) if m.group(3) else today.year
        dt = _safe_date(y, mo, d)
        if dt and not m.group(3) and dt < today:
            dt = _safe_date(y + 1, mo, d)
        if dt:
            return dt.isoformat()
    pat = r"\b(?:il|l|giorno|per il)\s+(\d{1,2})\b(?!\s*(?:persone|persona|posti|posto|pizz|e mezza|e un))"
    m = re.search(pat, t)
    if not m and bare:
        m = re.fullmatch(r"\s*(?:il\s+)?(\d{1,2})\s*", t)
    if m:
        d = int(m.group(1))
        dt = _safe_date(today.year, today.month, d)
        if dt and dt < today:
            nm = today.month % 12 + 1
            dt = _safe_date(today.year + (1 if nm == 1 else 0), nm, d)
        if dt:
            return dt.isoformat()
    return None


def _norm_hour(h):
    if 3 <= h <= 11:  # "alle 9" in un pub serale significa le 21
        return h + 12
    if h == 24:
        return 0
    return h


def _minutes_suffix(s):
    s = s or ""
    if re.search(r"e mezz|e trenta|e 30\b", s):
        return 30
    if re.search(r"e un quarto|e quindici|e 15\b", s):
        return 15
    if re.search(r"e tre quarti|e quarantacinque|e 45\b", s):
        return 45
    return 0


def parse_time(text, bare=False):
    t = flat(text)
    raw = norm(text)
    if re.search(r"\bmezzanotte\b", t):
        return "00:00"
    # 21:30 / 21.30 con prefisso
    m = re.search(r"\b(?:alle|all|ore|verso le|per le|entro le|dalle|le)\s+(\d{1,2})[:.](\d{2})\b", raw)
    if not m and bare:
        m = re.search(r"^\s*(?:le\s+)?(\d{1,2})[:.](\d{2})\s*$", raw)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 24 and mi < 60:
            return f"{_norm_hour(h):02d}:{mi:02d}"
    pat = r"\b(?:alle|all|ore|verso le|per le|entro le|dalle)\s+(\d{1,2}|" + _NUM_ALT + r")\b((?:\s+e\s+(?:mezza|mezzo|trenta|un quarto|quindici|tre quarti|quarantacinque|30|15|45)|\s+(?:meno un quarto)))?"
    m = re.search(pat, t)
    if not m and bare:
        m = re.fullmatch(r"\s*(?:le\s+)?(\d{1,2}|" + _NUM_ALT + r")((?:\s+e\s+(?:mezza|mezzo|trenta|un quarto|quindici|tre quarti|quarantacinque)|\s+(?:meno un quarto)))?\s*", t)
    if m:
        h = to_int(m.group(1))
        if h is None or h > 24:
            return None
        suffix = m.group(2) or ""
        if "meno un quarto" in suffix:
            return f"{_norm_hour(h) - 1 if _norm_hour(h) > 0 else 23:02d}:45"
        return f"{_norm_hour(h):02d}:{_minutes_suffix(suffix):02d}"
    return None


def valid_hhmm(s):
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


# ---------------------------------------------------------------- persone, nomi, indirizzi
def parse_people(text, bare=False):
    t = flat(text)
    n = r"(\d{1,2}|" + _NUM_ALT + r")"
    for pat in [n + r"\s+(?:persone|persona|posti|posto|adulti|pers|ospiti|coperti)\b",
                r"\bsiamo\s+(?:in\s+)?" + n + r"\b",
                r"\bin\s+" + n + r"\b(?!\s*(?:sala|via))",
                r"\btavolo\s+(?:da|per)\s+" + n + r"\b",
                r"\b(?:x|per)\s+" + n + r"\s*$",
                r"\bx\s+" + n + r"\b",
                r"\bper\s+" + n + r"\b(?!\s*(?:e mezza|e un|e trenta|pizz|margh))"]:
        m = re.search(pat, t)
        if m:
            v = to_int(m.group(1))
            if v and 0 < v <= 60:
                return v
    if bare:
        m = re.fullmatch(r"\s*(?:siamo\s+)?(?:in\s+)?" + n + r"(?:\s+persone)?\s*", t)
        if m:
            v = to_int(m.group(1))
            if v and 0 < v <= 60:
                return v
    return None


NAME_STOP = {"e", "ed", "per", "alle", "ore", "vorrei", "voglio", "volevo", "prenotare", "ordinare",
             "da", "a", "con", "il", "la", "lo", "di", "che", "ma", "poi", "grazie", "passo", "io",
             "siamo", "in", "tavolo", "domani", "stasera", "oggi", "asporto", "domicilio", "si", "no",
             "ok", "sono", "numero", "telefono", "cellulare", "il", "mio", "nome", "cognome", "buonasera",
             "ciao", "salve", "confermo", "vorremmo", "una", "un", "uno", "prenotazione", "ordine",
             "va", "bene", "perfetto", "esatto", "certo", "mille", "buona", "sera", "pure", "grazie", "annulla",
             "sala", "fumatori", "soppalco", "principale", "indifferente", "via", "corso", "piazza"}


def _clean_name_words(words):
    out = []
    for w in words:
        w = w.strip(".,;:!?\"()")
        if not w:
            break
        if not re.fullmatch(r"[A-Za-zÀ-ÿ'’\-]+", w) or norm(w) in NAME_STOP:
            break
        out.append(w)
        if len(out) == 4:
            break
    if not out:
        return None
    return " ".join(x[:1].upper() + x[1:] for x in out)


def parse_name(text, bare=False):
    s = str(text or "").strip()
    m = re.search(r"(?i)\b(?:mi chiamo|a nome di|a nome|il mio nome [eè]|nome e cognome\s*[:è]?|nome\s*:)\s+(.+)", s)
    if m:
        return _clean_name_words(m.group(1).split())
    m = re.search(r"\b(?:[Ss]ono|[Qq]ui [eè])\s+([A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+)*)", s)
    if m:
        return _clean_name_words(m.group(1).split())
    if bare:
        s2 = re.sub(r"(?i)^(sono|è|e'|a nome( di)?|nome)\s+", "", s).strip(" .!")
        words = s2.split()
        if 1 <= len(words) <= 4 and all(re.fullmatch(r"[A-Za-zÀ-ÿ'’\-]+", w) for w in words):
            if not any(norm(w) in NAME_STOP for w in words):
                return _clean_name_words(words)
    return None


STREET = r"(?:via|viale|piazza|piazzale|corso|contrada|c\.da|vico|vicolo|largo|strada|traversa|località|loc\.)"


def parse_address(text, bare=False):
    s = str(text or "").strip()
    m = re.search(r"(?i)\bindirizzo\s*(?:[:è]|e')?\s*(.+)", s)
    if not m:
        m = re.search(r"(?i)\b(" + STREET + r"\s+.+)", s)
    if m:
        a = m.group(1)
    elif bare and len(s) >= 5 and re.search(r"\d", s):
        a = s
    else:
        return None
    a = re.split(r"(?i)\s+(?:alle|all'|ore|verso le|per le|entro le|a nome|mi chiamo|il mio nome|grazie)\b", a)[0]
    a = a.strip(" .,;!")
    return a if len(a) >= 4 else None


def parse_phone(text):
    m = re.search(r"(\+?\d[\d\s\-.]{6,16}\d)", str(text or ""))
    if not m:
        return None
    p = re.sub(r"[^\d+]", "", m.group(1))
    digits = p.lstrip("+")
    if len(digits) < 8 or len(digits) > 15:
        return None
    if not p.startswith("+"):
        p = "+39" + p if not digits.startswith("39") or len(digits) <= 10 else "+" + p
    return p


def normalize_phone(value):
    if not value:
        return ""
    value = str(value).strip()
    if value.lower().startswith("whatsapp:"):
        value = value.split(":", 1)[1]
    return re.sub(r"[^0-9+]", "", value)


def parse_room(text):
    t = flat(text)
    if re.search(r"\bfumator", t):
        return "sala fumatori"
    if re.search(r"\bsoppalco\b", t):
        return "soppalco"
    if re.search(r"\b(principale|sala grande|sala interna|dentro)\b", t):
        return "sala principale"
    if re.search(r"\b(indifferente|qualsiasi|qualunque|dove capita|non importa|e uguale|fa lo stesso|dove volete|come volete|decidete voi|scegliete voi|va bene tutto|e lo stesso)\b", t):
        return ANY_ROOM
    return None


def parse_yes_no(text):
    t = flat(text)
    if re.search(r"\b(no|annulla|cambia|cambiare|modifica|modificare|aspetta|sbagliato|non va bene|non e giusto|non confermo|errato)\b", t):
        return False
    if re.search(r"\b(si|confermo|conferma|confermato|ok|okay|va bene|perfetto|esatto|procedi|certo|certamente|d accordo|giusto|corretto|va benissimo|benissimo|yes|vai|confermiamo|tutto ok)\b", t):
        return True
    return None
