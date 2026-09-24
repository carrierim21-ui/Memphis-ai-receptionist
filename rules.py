"""Motore a regole: funziona anche senza IA (o se l'IA non risponde).

Gestisce prenotazioni, ordini, richieste di richiamata e domande frequenti sul menu,
chiedendo un dato alla volta e sempre con riepilogo + conferma esplicita prima dell'invio.
"""
import difflib
import os
import re

import booking as B
import knowledge as K

RES_RE = r"\b(prenot\w*|tavolo|tavoli|riservare|posto per|posti per|spazio per|avete (?:posto|spazio)|c e (?:posto|spazio)|compleanno|festa di|cena di|siamo in \d+|siamo in (?:due|tre|quattro|cinque|sei|sette|otto|nove|dieci))\b"
# prenotazione/ordine GIÀ inviati che il cliente vuole cambiare o disdire: decide il personale
EXISTING_RE = r"\b(ho prenotato|avevo prenotato|abbiamo prenotato|avevamo prenotato|la mia prenotazione|nostra prenotazione|la prenotazione (?:di|a nome|fatta)|ho ordinato|avevo ordinato|il mio ordine|l ordine di prima)\b"
CHANGE_EXISTING_RE = r"\b(spost\w*|cambi\w*|modific\w*|anticip\w*|posticip\w*|disd\w*|annull\w*|cancell\w*|aggiung\w*|togli\w*|siamo in piu|siamo in meno|non veniamo|non vengo|ritardo)\b"
TAKE_RE = r"\b(asporto|ritiro|ritirare|ritiro io|passo a prendere|passo io|vengo a prendere|vengo io|porto via|portar via)\b"
DELIV_RE = r"\b(domicilio|consegna|consegnare|consegnate|consegnarmi|portate|portarmi|portarlo|a casa)\b"
ORDER_RE = r"\b(ordin\w*|vorrei|vorremmo|voglio|prendo|prendiamo|mi fai|mi fate|mi prepari|aggiungi|metti)\b"
QUESTION_RE = r"\b(avete|c e|ce l avete|quanto costa|quanto costano|quanto viene|prezzo|prezzi|cosa c e|che cosa|com e|come e fatta|ingredienti|cosa contiene|cosa mette|consigli|consigliate|che differenza|menu)\b"
REMOVE_WORDS = {"togli", "togliere", "toglimi", "rimuovi", "elimina", "cancella", "leva", "levami"}
HANDOFF_RE = r"\b(operatore|operatrice|parlare con (?:una persona|qualcuno|il personale|un cameriere|il titolare|un umano|il proprietario|una persona vera)|persona vera|essere richiamat\w*|richiamatemi|mi richiamate)\b"
CANCEL_RE = r"\b(annulla|annullare|lascia stare|lascia perdere|cancella tutto|non fa niente|ricominciamo|ricomincia|non mi serve piu)\b"
ALLERGY_RE = r"\b(allergi\w*|intolleran\w*|celiac\w*|glutine|lattosio|senza lattosio|vegan\w*)\b"
GREET_RE = r"^(ciao|salve|buonasera|buongiorno|pronto|ehi|hey|hello)\b"

QUESTIONS = {
    "people": "Per quante persone?",
    "day": "Per che giorno?",
    "time": "A che ora?",
    "room": "Preferisci sala principale, sala fumatori o soppalco? Se ti è indifferente dimmelo pure.",
    "name": "A che nome registro la richiesta?",
    "phone": "Mi lasci un numero di telefono per la conferma?",
    "items": "Cosa vuoi ordinare?",
    "service": "È da asporto (passi tu a ritirare) o a domicilio?",
    "desired_time": "A che ora lo vorresti?",
    "address": "Qual è l'indirizzo di consegna, con numero civico?",
}

CHANGE_KEYS = [
    (r"\b(orario|ora|ore)\b", ["time", "desired_time"]),
    (r"\b(giorno|data)\b", ["day"]),
    (r"\b(persone|numero di persone|posti)\b", ["people"]),
    (r"\b(sala)\b", ["room"]),
    (r"\b(nome)\b", ["name"]),
    (r"\b(indirizzo)\b", ["address"]),
    (r"\b(telefono|numero di telefono|cellulare)\b", ["phone"]),
    (r"\b(ordine|pizza|pizze|prodotti|piatti)\b", ["items"]),
]

CATEGORY_WORDS = [
    (r"\bpizz\w*\b", ["pizze", "pizze_speciali"]),
    (r"\b(panin\w*|burger\w*|hamburger\w*)\b", ["panini_gourmet"]),
    (r"\bbirr\w*\b", ["bevande:beer"]),
    (r"\bcocktail\w*\b", ["cocktail"]),
    (r"\b(stuzzich\w*|fritti|antipast\w*|sfizi)\b", ["stuzzicheria"]),
    (r"\b(piatti|secondi|carne|grigliat\w*)\b", ["piatti"]),
    (r"\b(bibit\w*|analcolic\w*|bevande|bere)\b", ["bevande:soft"]),
]


def ask(ctx, field):
    ctx.draft["asked"] = field
    q = QUESTIONS[field]
    if field == "desired_time":
        q = "A che ora vorresti ritirarlo?" if ctx.draft.get("service") == "asporto" else "A che ora vorresti riceverlo?"
    if field == "name" and ctx.draft.get("intent") == "ordine":
        q = "A che nome registro l'ordine?"
    if field == "name" and ctx.draft.get("intent") == "prenotazione":
        q = "A che nome prenoto?"
    return q


def is_question(message, t):
    return "?" in message or re.search(QUESTION_RE, t) is not None


# ---------------------------------------------------------------- domande frequenti
def _limit(names, channel, n_voice=6, n_text=15):
    n = n_voice if channel == "telephone" else n_text
    if len(names) > n:
        return ", ".join(names[:n]) + f" e altri {len(names) - n}"
    return ", ".join(names)


def category_items(key):
    if key == "bevande:beer":
        return [it for it in K.ITEMS if it["category"] == "bevande"
                and (it.get("description") in ("alla spina", "bottiglia"))]
    if key == "bevande:soft":
        return [it for it in K.ITEMS if it["category"] == "bevande"
                and it.get("description") not in ("alla spina", "bottiglia")]
    return [it for it in K.ITEMS if it["category"] == key]


def faq_reply(ctx, message, found=None):
    t = K.flat(message)
    ch = ctx.channel
    found = found if found is not None else K.find_items(message, use_blockers=False)
    out = []
    if re.search(ALLERGY_RE, t):
        out.append("Per allergie o intolleranze il personale deve verificare la preparazione: non posso garantire "
                   "l'assenza di allergeni o contaminazioni. Se ordini o prenoti, lo segnalo nella richiesta.")
    if found:
        descr = []
        for f in found[:4]:
            if f["item"]:
                descr.append(K.describe_item(f["item"]))
            else:
                opts = [f"{it['name']} {K.fmt_eur(K.price_value(it)) if K.price_value(it) is not None else '(prezzo da confermare)'}"
                        for it in f["family"]]
                descr.append("disponibile nei formati " + ", ".join(opts))
        out.append("Sì: " + "; ".join(descr) + ".")
    if not found:
        for pat, keys in CATEGORY_WORDS:
            if re.search(pat, t) and re.search(r"\b(quali|che|cosa|elenco|lista|avete|menu|tipi|consigli)\b", t):
                names = [it["name"] for k in keys for it in category_items(k)]
                if names:
                    out.append(f"Abbiamo: {_limit(names, ch)}.")
                break
    if not out and re.search(r"\b(con|senza|vegetarian\w*|piccant\w*|funghi|salsiccia|pesce|tonno|salmone)\b", t) \
            and re.search(r"\b(avete|consigli|qualcosa|quali|cosa)\b", t):
        res = K.search_menu(message)
        if res:
            out.append(f"Ti posso proporre: {_limit([it['name'] for it in res], ch, 5, 8)}.")
    if re.search(r"\b(orari|orario|aperti|apertura|chiudete|chiusura|a che ora aprite)\b", t) and not ctx.draft.get("intent"):
        out.append(f"Di solito siamo aperti {B.opening_text()}; gli orari possono variare.")
    if re.search(r"\b(dove siete|indirizzo del locale|dove si trova|come arrivo)\b", t):
        out.append(f"Siamo a {K.LOCALE.get('city', '')}.")
    if re.search(r"\b(sale|sala fumatori|fumatori|soppalco|fumare)\b", t) and not ctx.draft.get("intent"):
        out.append("Abbiamo " + ", ".join(K.ROOMS) + ".")
    if not out and re.search(r"\bmenu\b|\bcosa avete\b|\bcosa si mangia\b", t):
        out.append(f"Nel menu trovi {K.menu_overview()}. Chiedimi pure di un prodotto.")
    if not out and re.search(r"\b(prezzo|prezzi|costa|costano|quanto viene|quanto vengono)\b", t):
        for pat, keys in CATEGORY_WORDS:
            if re.search(pat, t):
                its = [it for k in keys for it in category_items(k)]
                priced = [f"{it['name']} {K.fmt_eur(K.price_value(it))}" for it in its if K.price_value(it) is not None]
                if priced:
                    out.append(f"Ecco i prezzi: {_limit(priced, ch, 5, 12)}.")
                elif its:
                    out.append(f"I prezzi di questa sezione vanno confermati con il personale. Abbiamo: "
                               f"{_limit([it['name'] for it in its], ch, 5, 12)}.")
                break
    if not out and re.search(r"\b(prezzo|costa|costano)\b", t):
        out.append("Di quale prodotto vuoi sapere il prezzo?")
    if not out and re.search(r"\b(sei un robot|sei una persona|sei umano|sei vero|chi sei)\b", t):
        out.append(f"Sono l'assistente virtuale del {K.BUSINESS_NAME}. Se preferisci parlare con il personale, dimmelo.")
    if not out and re.search(r"\b(grazie|perfetto grazie|ok grazie)\b", t):
        out.append("Figurati! Posso fare altro per te?")
    if not out and re.search(GREET_RE, t):
        out.append(f"Ciao! Sono l'assistente virtuale del {K.BUSINESS_NAME}: posso aiutarti a prenotare un tavolo, "
                   "ordinare da asporto o a domicilio, o darti informazioni sul menu.")
    if not out:
        out.append("Posso aiutarti a prenotare un tavolo, ordinare da asporto o a domicilio, o darti informazioni "
                   "sul menu. Dimmi pure cosa ti serve.")
    return " ".join(out)


# ---------------------------------------------------------------- estrazione
def _fuzzy_booking_word(t):
    """'prentoare', 'prenotre', 'prenotazine': errori di battitura su prenotare/prenotazione."""
    return any(len(w) >= 6 and max(difflib.SequenceMatcher(None, w, x).ratio()
                                   for x in ("prenotare", "prenotazione", "prenoto", "prenotiamo")) >= 0.8
               for w in t.split())


def detect_intent(d, t, found):
    intent = d.get("intent")
    res, take, deliv = re.search(RES_RE, t) or _fuzzy_booking_word(t), re.search(TAKE_RE, t), re.search(DELIV_RE, t)
    has_res_data = any(d.get(k) for k in ("people", "day", "time", "room"))
    if intent is None or intent == "richiamata":
        if res:
            d["intent"] = "prenotazione"
        elif take or deliv or (found and re.search(ORDER_RE, t)):
            d["intent"] = "ordine"
    elif intent == "prenotazione" and (take or deliv) and not has_res_data:
        d["intent"] = "ordine"
    elif intent == "ordine" and res and not d.get("items"):
        d["intent"] = "prenotazione"


def extract(ctx, message, found):
    """Applica alla bozza i dati presenti nel messaggio. Ritorna (conferme, errori)."""
    d, asked, t = ctx.draft, ctx.draft.get("asked"), K.flat(message)
    acks, errors = [], []

    def apply(err):
        if err:
            errors.append(err)
            return False
        return True

    if d.get("intent") == "prenotazione":
        v = K.parse_people(message, bare=asked == "people")
        if v and B.is_large_group(v):
            d["people"] = v
            d["large_group"] = True
            acks.append("people")
        elif v and apply(B.set_people(d, v)):
            d.pop("large_group", None)
            acks.append("people")
        v = K.parse_day(message, bare=asked == "day")
        if v and apply(B.set_day(d, v)):
            acks.append("day")
        v = K.parse_time(message, bare=asked == "time")
        if v and apply(B.set_time(d, v)):
            acks.append("time")
        v = K.parse_room(message)
        if v and apply(B.set_room(d, v)):
            acks.append("room")

    if d.get("intent") == "ordine":
        if re.search(TAKE_RE, t) or (asked == "service" and re.search(r"\b(io|ritiro|prendo)\b", t)):
            B.set_service(d, "asporto")
            acks.append("service")
        elif re.search(DELIV_RE, t):
            B.set_service(d, "domicilio")
            acks.append("service")
        acks += _extract_items(ctx, message, t, found, errors)
        v = K.parse_day(message)
        if v and v != K.service_today().isoformat() and apply(B.set_day(d, v, field="desired_day")):
            acks.append("desired_day")
        if re.search(r"\b(al piu presto|prima possibile|appena possibile|appena pronto|subito)\b", t):
            B.set_time(d, B.ASAP, field="desired_time")
            acks.append("desired_time")
        else:
            v = K.parse_time(message, bare=asked == "desired_time")
            if v and apply(B.set_time(d, v, field="desired_time")):
                acks.append("desired_time")
        if d.get("service") == "domicilio" or asked == "address":
            v = K.parse_address(message, bare=asked == "address")
            if v and apply(B.set_address(d, v)):
                acks.append("address")

    if d.get("intent") in ("prenotazione", "ordine", "richiamata"):
        v = K.parse_name(message, bare=asked == "name")
        if v and apply(B.set_name(d, v)):
            acks.append("name")
        if asked == "phone" or re.search(r"\b(numero|telefono|cellulare|cell)\b", t):
            v = K.parse_phone(message)
            if v:
                B.set_phone(d, v)
                acks.append("phone")
            elif asked == "phone":
                errors.append("Il numero di telefono non sembra valido: me lo ripeti?")
        if re.search(ALLERGY_RE, t):
            B.add_note(d, "Allergie/intolleranze: " + message.strip()[:200])
            acks.append("allergy")
        elif asked == "notes":
            B.add_note(d, message.strip()[:200])
            acks.append("notes")
    return acks, errors


def _choose_option(options, toks):
    """Sceglie il formato (es. 'Guinness 25cl' / 'Guinness 56cl') dalla risposta del cliente."""
    common = set.intersection(*[set(K.tokens(o)) for o in options])
    for name in options:
        distinct = set(K.tokens(name)) - common
        if distinct & set(toks):
            return name
        for d_tok in distinct:
            num = re.match(r"\d+", d_tok)
            if num and num.group(0) in toks:
                return name
    words = set(toks)
    if words & {"piccola", "piccolo", "piccole", "piccoli", "small"}:
        return options[0]
    if words & {"grande", "grandi", "media", "medio", "medie", "large", "pinta"}:
        return options[-1]
    return None


def _extract_items(ctx, message, t, found, errors):
    d = ctx.draft
    acks = []
    toks = K.tokens(message)
    pending = d.get("pending_family")
    if pending:
        chosen = _choose_option(pending["options"], toks)
        if not chosen and set(toks) & {"niente", "lascia", "no", "nessuna", "nessuno"}:
            d.pop("pending_family", None)
            acks.append("items")
        if chosen:
            item, _ = K.resolve_item(chosen)
            B.add_item(d, item, pending["quantity"], pending.get("modifications", ""))
            d.pop("pending_family", None)
            acks.append("items")
    remove_at = [i for i, tok in enumerate(toks) if tok in REMOVE_WORDS]
    for f in found:
        removing = any(0 < f["start"] - i <= 3 for i in remove_at)
        if f["family"]:
            if removing:
                for it in f["family"]:
                    B.remove_item(d, it["name"])
                acks.append("items")
                continue
            d["pending_family"] = {"options": [it["name"] for it in f["family"]], "quantity": f["quantity"],
                                   "modifications": f["modifications"]}
            continue
        item = f["item"]
        if removing:
            if B.remove_item(d, item["name"]):
                acks.append("items")
            continue
        existing = [x for x in d.get("items", []) if x["name"] == item["name"]]
        if existing and f["modifications"] and not f["qty_explicit"]:
            existing[-1]["modifications"] = f["modifications"]
        else:
            B.add_item(d, item, f["quantity"], f["modifications"])
        acks.append("items")
    if not found and d.get("items") and ctx.draft.get("asked") not in ("name", "address", "phone"):
        m = re.search(r"\b(senza|con aggiunta di|aggiungi|extra|doppia|doppio|ben cott\w*)\b.*", t)
        if m and not re.search(r"\b(senza glutine|senza lattosio)\b", t):
            mod = re.split(r"\b(per|alle|ore|a nome|grazie)\b", m.group(0))[0].strip()
            if len(mod.split()) >= 2:
                last = d["items"][-1]
                last["modifications"] = (last.get("modifications", "") + "; " + mod).strip("; ")
                acks.append("items")
    return acks


def _ack_text(d, acks):
    parts = []
    if "items" in acks and d.get("items"):
        parts.append(f"Segnato: {B.items_text(d['items'])}.")
    if "allergy" in acks:
        parts.append("Ho segnalato allergie/intolleranze: il personale verificherà la preparazione, "
                     "non posso garantire l'assenza di contaminazioni.")
    return " ".join(parts)


# ---------------------------------------------------------------- turno
def handoff(ctx, reason):
    ctx.handoff = reason
    d = ctx.draft
    if ctx.channel == "telephone" and os.getenv("STAFF_PHONE"):
        return "Certo, ti passo subito un collega. Resta in linea."
    d["intent"] = "richiamata"
    B.add_note(d, reason)
    if not d.get("phone"):
        return "Certo. " + ask(ctx, "phone")
    text, ref = B.finalize(d, ctx.channel)
    ctx.finalized = ref
    ctx.draft = B.new_draft(d)
    return text


def next_step(ctx, prefix=""):
    d = ctx.draft
    if d.get("pending_family"):
        opts = d["pending_family"]["options"]
        d["asked"] = "items"
        return (prefix + " " if prefix else "") + f"Quale formato preferisci: {', '.join(opts)}?"
    miss = B.missing_fields(d)
    if miss:
        return (prefix + " " if prefix else "") + ask(ctx, miss[0])
    if d["intent"] == "richiamata":
        text, ref = B.finalize(d, ctx.channel)
        ctx.finalized = ref
        ctx.draft = B.new_draft(d)
        return text
    if d["intent"] == "prenotazione":
        problem = B.availability_problem(d)
        if problem:
            d["time"] = None
            d["asked"] = "time"
            return problem
    d["awaiting_confirmation"] = True
    d["asked"] = None
    head = "Riepilogo della prenotazione: " if d["intent"] == "prenotazione" else "Riepilogo dell'ordine "
    return (prefix + " " if prefix else "") + head + B.summary(d, ctx.channel) + " Confermi?"


def turn(ctx, message):
    d = ctx.draft
    t = K.flat(message)

    if re.search(HANDOFF_RE, t):
        return handoff(ctx, "Il cliente ha chiesto di parlare con il personale.")

    if re.search(EXISTING_RE, t) and re.search(CHANGE_EXISTING_RE, t):
        text = handoff(ctx, "Modifica/disdetta di una richiesta già inviata: " + message.strip()[:300])
        return "Le prenotazioni e gli ordini già inviati li modifica il personale: giro subito la tua richiesta. " + \
            (text[len("Certo. "):] if text.startswith("Certo. ") else text)

    if d.get("intent") and re.search(CANCEL_RE, t) and not (d.get("awaiting_confirmation") and re.search(r"\bno\b", t) and not re.search(r"\b(annulla|lascia)\b", t)):
        ctx.draft = B.new_draft(d)
        return "Va bene, ho annullato la richiesta. Posso aiutarti con altro?"

    found = K.find_items(message)
    detect_intent(d, t, found)

    if not d.get("intent"):
        return faq_reply(ctx, message)

    if d.get("awaiting_confirmation"):
        acks, errors = extract(ctx, message, found)
        if errors:
            d["awaiting_confirmation"] = False
            return errors[0]
        if acks:
            d["awaiting_confirmation"] = False
            return next_step(ctx, _ack_text(d, acks))
        yn = K.parse_yes_no(message)
        if yn is True:
            d["awaiting_confirmation"] = False
            text, ref = B.finalize(d, ctx.channel)
            ctx.finalized = ref
            ctx.draft = B.new_draft(d)
            return text
        if yn is False:
            d["awaiting_confirmation"] = False
            for pat, fields in CHANGE_KEYS:
                if re.search(pat, t):
                    for f in fields:
                        if f in d and f != "items":
                            d[f] = None
                    field = next((f for f in fields if f in B.missing_fields(d)), None)
                    if fields == ["items"]:
                        d["asked"] = "items"
                        return "Cosa vuoi aggiungere o togliere dall'ordine?"
                    if field:
                        return ask(ctx, field)
            d["asked"] = "change"
            return "Va bene, cosa vuoi cambiare?"
        return "Scusa, non ho capito: confermi la richiesta? Rispondi sì o no."

    if d.get("asked") == "change":
        for pat, fields in CHANGE_KEYS:
            if re.search(pat, t):
                acks, errors = extract(ctx, message, found)
                if errors:
                    return errors[0]
                if acks:
                    return next_step(ctx, _ack_text(d, acks))
                for f in fields:
                    if f in d and f != "items":
                        d[f] = None
                if fields == ["items"]:
                    d["asked"] = "items"
                    return "Cosa vuoi aggiungere o togliere dall'ordine?"
                return next_step(ctx)

    acks, errors = extract(ctx, message, found)
    if d.get("large_group") and d.get("intent") == "prenotazione":
        people = d.get("people")
        info = ", ".join(x for x in [f"{people} persone",
                                    K.fmt_day(d["day"]) if d.get("day") else "",
                                    f"ore {d['time']}" if d.get("time") else ""] if x)
        text = handoff(ctx, f"Gruppo numeroso da valutare: {info}. Messaggio: {message.strip()[:200]}")
        d = ctx.draft
        return B.large_group_text(people) + " " + (text[len("Certo. "):] if text.startswith("Certo. ") else text)
    if errors:
        field = d.get("asked")
        return errors[0] if not field or field in ("change",) else errors[0]
    if not acks:
        pending = d.get("asked")
        if is_question(message, t) or re.search(ALLERGY_RE, t):
            answer = faq_reply(ctx, message, K.find_items(message, use_blockers=False))
            return answer + (" " + QUESTIONS[pending] if pending in QUESTIONS else "")
        if pending in QUESTIONS and pending not in ("items",):
            return "Scusa, non ho capito. " + ask(ctx, pending)
        if pending == "items" and not found:
            maybe = K.search_menu(message, 3)
            hint = f" Forse intendevi: {', '.join(it['name'] for it in maybe)}?" if maybe else ""
            return "Non ho trovato questo prodotto nel menu." + hint + " Cosa vuoi ordinare?"
    if d.get("intent") == "ordine" and not d.get("items") and not found and not d.get("pending_family"):
        m = re.search(r"\b(?:con|senza)\b(.*)", t)
        maybe = K.search_menu(m.group(1), 6) if m else []
        if maybe:
            d["asked"] = "items"
            return f"Ti posso proporre: {', '.join(it['name'] for it in maybe)}. Quale preferisci?"
    return next_step(ctx, _ack_text(d, acks))
