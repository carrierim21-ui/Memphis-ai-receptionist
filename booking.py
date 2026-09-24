"""Stato della richiesta del cliente (bozza), validazione dei dati, riepiloghi e invio al locale.

Queste funzioni sono usate sia dal motore a regole sia dall'IA: così i controlli
(orari di apertura, date passate, prodotti esistenti, disponibilità) valgono sempre.
"""
import json
import logging
import os
from datetime import date, timedelta

import knowledge as K
import notify as N
import storage as S

log = logging.getLogger("memphis.booking")
ASAP = "al più presto"

FIELD_LABELS = {
    "people": "numero di persone", "day": "giorno", "time": "orario", "room": "sala", "name": "nome",
    "phone": "numero di telefono", "items": "prodotti", "service": "asporto o domicilio",
    "desired_time": "orario", "address": "indirizzo",
}


def new_draft(keep=None):
    d = {"intent": None, "items": [], "notes": []}
    for k in ("name", "phone"):
        if keep and keep.get(k):
            d[k] = keep[k]
    return d


def missing_fields(d):
    intent = d.get("intent")
    if intent == "prenotazione":
        miss = [f for f in ("people", "day", "time", "room", "name") if not d.get(f)]
    elif intent == "ordine":
        miss = []
        if not d.get("items"):
            miss.append("items")
        if not d.get("service"):
            miss.append("service")
        if not d.get("desired_time"):
            miss.append("desired_time")
        if d.get("service") == "domicilio" and not d.get("address"):
            miss.append("address")
        if not d.get("name"):
            miss.append("name")
    elif intent == "richiamata":
        miss = []
    else:
        return []
    if not d.get("phone"):
        miss.append("phone")
    return miss


# ---------------------------------------------------------------- setter con validazione
# Ogni setter ritorna None se ok, altrimenti un messaggio d'errore da dire al cliente.
def opening_text():
    return f"dalle {K.OPENING['open']} alle {K.OPENING['close']}"


# Oltre questa soglia la disponibilità la decide il personale (variabile GROUP_MAX_AUTO, default 10).
GROUP_MAX_AUTO = int(os.getenv("GROUP_MAX_AUTO", "10"))


def is_large_group(v):
    try:
        return int(v) > GROUP_MAX_AUTO
    except (TypeError, ValueError):
        return False


def large_group_text(v):
    return (f"Per gruppi di più di {GROUP_MAX_AUTO} persone la disponibilità la verifica direttamente il personale: "
            f"giro la richiesta per {v} persone e ti ricontattiamo noi.")


def set_people(d, v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "Non ho capito il numero di persone."
    if not 1 <= v <= 60:
        return "Per gruppi così numerosi è meglio parlare direttamente con il locale."
    if v > GROUP_MAX_AUTO:
        return (f"GRUPPO NUMEROSO: {v} persone supera il limite di {GROUP_MAX_AUTO} per le prenotazioni automatiche. "
                "Non proseguire con la prenotazione: usa passa_a_personale indicando persone, giorno e orario richiesti.")
    d["people"] = v
    return None


def _check_datetime(day_iso, hhmm):
    if not day_iso or not hhmm or hhmm == ASAP:
        return None
    if K.service_datetime(day_iso, hhmm) < K.now() - timedelta(minutes=5):
        return "Quell'orario è già passato: che ora preferisci?"
    return None


def set_day(d, day_iso, field="day"):
    try:
        dt = date.fromisoformat(str(day_iso))
    except (TypeError, ValueError):
        return "Non ho capito il giorno."
    today = K.service_today()
    if dt < today:
        return "Quella data è già passata: per che giorno?"
    if dt > today + timedelta(days=180):
        return "Accettiamo richieste fino a sei mesi in anticipo: per che giorno?"
    d[field] = dt.isoformat()
    time_field = "time" if field == "day" else "desired_time"
    err = _check_datetime(d[field], d.get(time_field))
    if err:
        d[time_field] = None
    return err


def set_time(d, hhmm, field="time"):
    if field == "desired_time" and str(hhmm).strip().lower() in {ASAP, "al piu presto", "prima possibile", "asap", "subito"}:
        d[field] = ASAP
        return None
    t = K.valid_hhmm(hhmm)
    if not t:
        return "Non ho capito l'orario."
    if not K.within_opening(t):
        return f"Di solito il locale è aperto {opening_text()}: che orario preferisci in quella fascia?"
    day_field = "day" if field == "time" else "desired_day"
    day = d.get(day_field) or (K.service_today().isoformat() if field == "desired_time" else None)
    err = _check_datetime(day, t)
    if err:
        return err
    d[field] = t
    return None


def set_room(d, room):
    r = str(room or "").strip().lower()
    if r in K.ROOMS or r == K.ANY_ROOM:
        d["room"] = r
        return None
    r2 = K.parse_room(r)
    if r2:
        d["room"] = r2
        return None
    return "Le sale disponibili sono: " + ", ".join(K.ROOMS) + "."


def set_name(d, name):
    name = " ".join(str(name or "").split())
    if not 2 <= len(name) <= 60:
        return "Non ho capito il nome."
    d["name"] = name
    return None


def set_phone(d, phone):
    p = K.parse_phone(phone)
    if not p:
        return "Il numero di telefono non sembra valido: me lo ripeti?"
    d["phone"] = p
    return None


def set_address(d, addr):
    addr = " ".join(str(addr or "").split())
    if len(addr) < 4:
        return "Mi serve l'indirizzo completo (via e numero civico)."
    d["address"] = addr
    return None


def set_service(d, service):
    s = str(service or "").lower()
    if s not in ("asporto", "domicilio"):
        return "Il servizio può essere asporto o domicilio."
    d["service"] = s
    return None


def add_note(d, note):
    note = " ".join(str(note or "").split())
    if note and note not in d.setdefault("notes", []):
        d["notes"].append(note)


def add_item(d, item, quantity=1, modifications=""):
    try:
        quantity = max(1, min(int(quantity or 1), 50))
    except (TypeError, ValueError):
        quantity = 1
    modifications = " ".join(str(modifications or "").split())
    items = d.setdefault("items", [])
    for x in items:
        if x["name"] == item["name"] and (x.get("modifications") or "") == modifications:
            x["quantity"] += quantity
            return x
    x = {"name": item["name"], "quantity": quantity, "unit_price_eur": K.price_value(item),
         "modifications": modifications}
    items.append(x)
    return x


def remove_item(d, name):
    items = d.get("items", [])
    item, _ = K.resolve_item(name)
    target = K.flat(item["name"]) if item else K.flat(name)
    kept = [x for x in items if K.flat(x["name"]) != target]
    removed = len(kept) != len(items)
    d["items"] = kept
    return removed


# ---------------------------------------------------------------- riepiloghi
def items_text(items):
    out = []
    for x in items or []:
        s = f"{x['quantity']}× {x['name']}"
        if x.get("modifications"):
            s += f" ({x['modifications']})"
        out.append(s)
    return ", ".join(out)


def order_total(items):
    total, complete = 0.0, True
    for x in items or []:
        p = x.get("unit_price_eur")
        if p is None:
            complete = False
            continue
        total += float(p) * int(x.get("quantity", 1))
    return round(total, 2), complete


def room_text(room):
    return "sala indifferente" if room == K.ANY_ROOM else room


def summary(d, channel="web"):
    intent = d.get("intent")
    phone = f", telefono {d['phone']}" if channel == "web" and d.get("phone") else ""
    notes = f" Note: {'; '.join(d['notes'])}." if d.get("notes") else ""
    if intent == "prenotazione":
        p = d["people"]
        return (f"tavolo per {p} {'persona' if p == 1 else 'persone'}, {K.fmt_day(d['day'])} alle {d['time']}, "
                f"{room_text(d['room'])}, a nome di {d['name']}{phone}.{notes}")
    if intent == "ordine":
        total, complete = order_total(d["items"])
        tot = f"Totale prodotti {K.fmt_eur(total)}"
        if not complete:
            tot += " più i prodotti con prezzo da confermare"
        if any(x.get("modifications") for x in d["items"]):
            tot += " (le modifiche possono avere un costo extra)"
        when_day = ""
        if d.get("desired_day") and d["desired_day"] != K.service_today().isoformat():
            when_day = f" {K.fmt_day(d['desired_day'])}"
        when = ASAP if d["desired_time"] == ASAP else f"alle {d['desired_time']}"
        if d["service"] == "domicilio":
            where = f"Consegna in {d['address']}{when_day} {when}"
        else:
            where = f"Ritiro al locale{when_day} {when}"
        return (f"{'da asporto' if d['service'] == 'asporto' else 'a domicilio'}: {items_text(d['items'])}. {tot}. "
                f"{where}, a nome di {d['name']}{phone}.{notes}")
    return ""


def state_for_ai(d):
    keys = ["intent", "people", "day", "time", "room", "service", "items", "desired_day", "desired_time",
            "address", "name", "phone", "notes"]
    return {k: d.get(k) for k in keys if d.get(k) not in (None, [], "")}


# ---------------------------------------------------------------- disponibilità
def availability_problem(d):
    """Se il locale ha configurato gli slot per quel giorno e non c'è posto, ritorna un messaggio.
    Se il giorno non ha slot configurati la richiesta passa comunque (verifica manuale)."""
    if getattr(N.DISABLED, "on", False):  # self-test: non dipende dagli slot reali del locale
        return None
    day, t, room, people = d.get("day"), d.get("time"), d.get("room"), int(d.get("people") or 0)
    slots = S.rows("SELECT * FROM availability WHERE active=1 AND day=?", (day,))
    if not slots:
        return None

    def fits(s, check_time=True):
        return ((not check_time or s["time"] == t) and (room == K.ANY_ROOM or s["room"] == room)
                and s["capacity"] - s["booked"] >= people)

    if any(fits(s) for s in slots):
        return None
    alternatives = sorted({s["time"] for s in slots if fits(s, check_time=False)})
    where = "" if room == K.ANY_ROOM else f" in {room}"
    if alternatives:
        return (f"Per {K.fmt_day(day)} alle {t}{where} non risultano posti liberi. "
                f"Ci sono ancora posti alle {', '.join(alternatives[:5])}: quale preferisci?")
    return f"Per {K.fmt_day(day)}{where} non risultano più posti liberi. Vuoi provare un altro giorno o un'altra sala?"


def check_availability(day, t, room, people):
    d = {"day": day, "time": t, "room": room or K.ANY_ROOM, "people": people}
    slots = S.rows("SELECT * FROM availability WHERE active=1 AND day=?", (day,))
    if not slots:
        return {"configurata": False,
                "messaggio": "Il locale non ha inserito la disponibilità per quel giorno: la richiesta sarà verificata dal personale."}
    problem = availability_problem(d)
    return {"configurata": True, "disponibile": problem is None, "messaggio": problem or "Risultano posti liberi (conferma finale del locale)."}


# ---------------------------------------------------------------- invio
def customer_channel(channel):
    """Canale per rispondere al cliente: WhatsApp se ci ha scritto lì, altrimenti SMS se possibile."""
    return "whatsapp" if channel == "whatsapp" else "sms"


def confirmation_promise(d):
    if N.configured() and d.get("phone"):
        return "Ti arriverà un messaggio appena il locale conferma."
    return "Il locale ti ricontatterà per la conferma."


def finalize(d, channel):
    """Salva la richiesta confermata dal cliente. Ritorna (testo per il cliente, riferimento)."""
    intent = d.get("intent")
    notes = "; ".join(d.get("notes") or [])
    if intent == "prenotazione":
        rid = S.insert("requests", {
            "channel": channel, "type": "prenotazione", "name": d["name"], "phone": d.get("phone"),
            "people": d["people"], "day": d["day"], "time": d["time"], "room": d["room"], "notes": notes,
            "payload": json.dumps(state_for_ai(d), ensure_ascii=False), "status": "DA_VERIFICARE",
            "customer_confirmed": 1, "created_at": S.stamp()})
        N.notify_staff(f"Nuova prenotazione #{rid}: {d['people']} pers., {K.fmt_day(d['day'])} {d['time']}, "
                       f"{room_text(d['room'])}, {d['name']} {d.get('phone') or ''}. {notes}".strip())
        text = (f"Perfetto, richiesta di prenotazione n. {rid} inviata. Il locale verifica la disponibilità: "
                f"la prenotazione non è ancora confermata. {confirmation_promise(d)}")
        return text, {"type": "prenotazione", "id": rid}
    if intent == "ordine":
        total, complete = order_total(d["items"])
        oid = S.insert("orders", {
            "channel": channel, "service": d["service"], "name": d["name"], "phone": d.get("phone"),
            "address": d.get("address"), "desired_day": d.get("desired_day") or K.service_today().isoformat(),
            "desired_time": d["desired_time"], "items": json.dumps(d["items"], ensure_ascii=False),
            "notes": notes, "total_eur": total if complete else None, "status": "DA_VERIFICARE",
            "customer_confirmed": 1, "created_at": S.stamp()})
        N.notify_staff(f"Nuovo ordine #{oid} ({d['service']}): {items_text(d['items'])}. Ore {d['desired_time']}, "
                       f"{d['name']} {d.get('phone') or ''} {d.get('address') or ''}. {notes}".strip())
        text = (f"Perfetto, ordine n. {oid} inviato. Il locale verifica l'orario e la preparazione "
                f"prima della conferma definitiva. {confirmation_promise(d)}")
        return text, {"type": "ordine", "id": oid}
    if intent == "richiamata":
        rid = S.insert("requests", {
            "channel": channel, "type": "richiamata", "name": d.get("name"), "phone": d.get("phone"),
            "notes": notes, "payload": json.dumps(state_for_ai(d), ensure_ascii=False),
            "status": "DA_VERIFICARE", "customer_confirmed": 1, "created_at": S.stamp()})
        N.notify_staff(f"Richiesta di essere richiamato #{rid}: {d.get('name') or ''} {d.get('phone')}. {notes}".strip())
        return f"Ho avvisato il personale: ti richiameremo al più presto al {d.get('phone')}.", {"type": "richiamata", "id": rid}
    return "", None


# ---------------------------------------------------------------- messaggi al cliente dopo la verifica
def status_message(kind, row):
    name = K.BUSINESS_NAME
    if kind == "prenotazione":
        when = f"{K.fmt_day(row['day'])} alle {row['time']}"
        if row["status"] == "CONFERMATA":
            return f"{name}: la tua prenotazione per {row['people']} persone {when} è confermata. A presto!"
        if row["status"] == "RIFIUTATA":
            return (f"{name}: ci dispiace, per {when} non abbiamo disponibilità. "
                    f"Scrivici o chiamaci per trovare un altro orario.")
        return None
    if kind == "ordine":
        svc = row["service"]
        if row["status"] == "CONFERMATO":
            when = "appena pronto" if row["desired_time"] == ASAP else f"alle {row['desired_time']}"
            tot = f" Totale {K.fmt_eur(row['total_eur'])}." if row.get("total_eur") else ""
            what = "ritiro" if svc == "asporto" else "consegna prevista"
            return f"{name}: il tuo ordine n. {row['id']} è confermato, {what} {when}.{tot}"
        if row["status"] == "RIFIUTATO":
            return f"{name}: ci dispiace, non riusciamo a gestire l'ordine n. {row['id']}. Contattaci per un'alternativa."
        if row["status"] == "PRONTO" and svc == "asporto":
            return f"{name}: il tuo ordine n. {row['id']} è pronto, puoi passare a ritirarlo!"
        if row["status"] == "IN_CONSEGNA" or (row["status"] == "PRONTO" and svc == "domicilio"):
            return f"{name}: il tuo ordine n. {row['id']} è in consegna."
        return None
    return None


def notify_customer(kind, row):
    """Avvisa il cliente del cambio di stato (in background) e registra l'esito."""
    body = status_message(kind, row)
    if not body:
        return None
    if not row.get("phone"):
        return "nessun telefono del cliente"
    if not N.configured():
        return "Twilio non configurato: avvisa il cliente a mano"
    table = "requests" if kind == "prenotazione" else "orders"

    def done(ok, info):
        S.update(table, row["id"], {"notified": f"{row['status']}: {info}"})

    N.send_async(row["phone"], body, prefer=customer_channel(row.get("channel")), on_done=done)
    return "invio in corso"
