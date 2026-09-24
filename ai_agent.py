"""Receptionist con OpenAI (Chat Completions + function calling).

L'IA conduce la conversazione in modo naturale, ma i dati passano sempre dagli
strumenti qui sotto, che li validano con le stesse regole del motore a regole
(orari, date, prodotti del menu, disponibilità). Se l'IA non risponde o dà errore,
conversation.py passa automaticamente al motore a regole.
"""
import json
import logging
import os
import re
import time
from datetime import timedelta

import httpx

import booking as B
import knowledge as K

log = logging.getLogger("memphis.ai")
STATUS = {"last_error": None, "last_error_at": None, "last_ok_at": None}
DEFAULT_MODEL = "gpt-5.6-luna"


def enabled():
    return bool((os.getenv("OPENAI_API_KEY") or "").strip())


def model():
    return (os.getenv("OPENAI_MODEL") or DEFAULT_MODEL).strip()


def base_url():
    return (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")


class AIError(Exception):
    pass


def record_error(e):
    STATUS["last_error"] = str(e)[:500]
    STATUS["last_error_at"] = K.now().isoformat(timespec="seconds")
    log.warning("IA non disponibile, uso il motore a regole: %s", e)


def call_openai(messages, tools=None, timeout=20.0):
    payload = {"model": model(), "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        r = httpx.post(f"{base_url()}/chat/completions", json=payload, timeout=timeout,
                       headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '').strip()}"})
    except httpx.HTTPError as e:
        raise AIError(f"connessione a OpenAI fallita: {e}") from e
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            detail = r.text
        raise AIError(f"OpenAI {r.status_code}: {detail[:300]}")
    return r.json()


def ping():
    """Controllo rapido usato dal self-test."""
    data = call_openai([{"role": "user", "content": "Rispondi solo con la parola OK."}], timeout=30)
    STATUS["last_ok_at"] = K.now().isoformat(timespec="seconds")
    return (data["choices"][0]["message"].get("content") or "").strip()


# ---------------------------------------------------------------- strumenti
ROOM_ENUM = list(K.ROOMS) + [K.ANY_ROOM]
TOOLS = [
    {"type": "function", "function": {
        "name": "aggiorna_prenotazione",
        "description": "Salva o corregge i dati di una PRENOTAZIONE TAVOLO appena il cliente li dice (anche uno solo). "
                       "Passa solo i campi nuovi o cambiati.",
        "parameters": {"type": "object", "properties": {
            "people": {"type": "integer", "description": "numero di persone"},
            "day": {"type": "string", "description": "data in formato YYYY-MM-DD (usa la tabella dei giorni)"},
            "time": {"type": "string", "description": "orario HH:MM 24 ore, es. 21:00"},
            "room": {"type": "string", "enum": ROOM_ENUM, "description": "sala; 'qualsiasi' se al cliente è indifferente"},
            "name": {"type": "string", "description": "nome e cognome del cliente"},
            "phone": {"type": "string", "description": "numero di telefono del cliente"},
            "notes": {"type": "string", "description": "richieste particolari, allergie, seggiolone, ricorrenze"},
        }}}},
    {"type": "function", "function": {
        "name": "aggiorna_ordine",
        "description": "Salva o corregge un ORDINE da asporto o a domicilio. Usa i nomi esatti dei prodotti del menu. "
                       "Per cambiare la quantità di un prodotto: rimuovilo e aggiungilo di nuovo con la quantità giusta.",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string", "enum": ["asporto", "domicilio"]},
            "add_items": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "quantity": {"type": "integer"},
                "modifications": {"type": "string", "description": "es. 'senza cipolla', 'aggiunta di funghi'"}},
                "required": ["name"]}},
            "remove_items": {"type": "array", "items": {"type": "string"}, "description": "nomi dei prodotti da togliere"},
            "desired_day": {"type": "string", "description": "YYYY-MM-DD solo se non è per oggi"},
            "desired_time": {"type": "string", "description": "HH:MM oppure 'al più presto'"},
            "address": {"type": "string", "description": "indirizzo di consegna con numero civico"},
            "name": {"type": "string"}, "phone": {"type": "string"},
            "notes": {"type": "string", "description": "allergie, citofono, note per la cucina"},
        }}}},
    {"type": "function", "function": {
        "name": "verifica_disponibilita",
        "description": "Controlla i posti inseriti dal locale per un giorno/orario/sala.",
        "parameters": {"type": "object", "properties": {
            "day": {"type": "string"}, "time": {"type": "string"},
            "room": {"type": "string", "enum": ROOM_ENUM}, "people": {"type": "integer"}},
            "required": ["day", "time", "people"]}}},
    {"type": "function", "function": {
        "name": "invia_richiesta",
        "description": "Invia al locale la prenotazione o l'ordine in corso. Chiamalo SOLO dopo aver letto il riepilogo "
                       "al cliente e aver ricevuto un sì esplicito.",
        "parameters": {"type": "object", "properties": {
            "cliente_ha_confermato": {"type": "boolean"}}, "required": ["cliente_ha_confermato"]}}},
    {"type": "function", "function": {
        "name": "annulla_richiesta",
        "description": "Annulla la prenotazione/ordine in corso (non ancora inviato) se il cliente non la vuole più.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "passa_a_personale",
        "description": "Il cliente vuole parlare con una persona, oppure la richiesta richiede una decisione del "
                       "personale (reclami, eventi privati, gruppi numerosi, problemi con un ordine già fatto).",
        "parameters": {"type": "object", "properties": {
            "motivo": {"type": "string"},
            "phone": {"type": "string", "description": "numero da richiamare, se il cliente lo ha dato"},
            "name": {"type": "string"}}, "required": ["motivo"]}}},
]


def _apply(errors, err):
    if err:
        errors.append(err)


def _state(ctx, errors=None, extra=None):
    d = ctx.draft
    out = {"ok": not errors, "stato": B.state_for_ai(d), "dati_mancanti": [B.FIELD_LABELS[f] for f in B.missing_fields(d)]}
    if errors:
        out["errori"] = errors
    if d.get("intent") and not B.missing_fields(d):
        out["riepilogo_da_leggere_al_cliente"] = B.summary(d, ctx.channel)
    if extra:
        out.update(extra)
    return out


def tool_aggiorna_prenotazione(ctx, a):
    d = ctx.draft
    d["intent"] = "prenotazione"
    errors = []
    if a.get("people") is not None:
        _apply(errors, B.set_people(d, a["people"]))
    if a.get("day"):
        _apply(errors, B.set_day(d, a["day"]))
    if a.get("time"):
        _apply(errors, B.set_time(d, a["time"]))
    if a.get("room"):
        _apply(errors, B.set_room(d, a["room"]))
    if a.get("name"):
        _apply(errors, B.set_name(d, a["name"]))
    if a.get("phone"):
        _apply(errors, B.set_phone(d, a["phone"]))
    if a.get("notes"):
        B.add_note(d, a["notes"])
    extra = {}
    if not B.missing_fields(d):
        problem = B.availability_problem(d)
        if problem:
            d["time"] = None
            errors.append(problem)
    return _state(ctx, errors, extra)


def tool_aggiorna_ordine(ctx, a):
    d = ctx.draft
    d["intent"] = "ordine"
    errors, not_found = [], []
    if a.get("service"):
        _apply(errors, B.set_service(d, a["service"]))
    for name in a.get("remove_items") or []:
        if not B.remove_item(d, name):
            errors.append(f"'{name}' non era nell'ordine")
    for x in a.get("add_items") or []:
        item, suggestions = K.resolve_item(x.get("name", ""))
        if item:
            B.add_item(d, item, x.get("quantity") or 1, x.get("modifications") or "")
        else:
            not_found.append({"richiesto": x.get("name"), "forse_intendevi": suggestions})
    if a.get("desired_day"):
        _apply(errors, B.set_day(d, a["desired_day"], field="desired_day"))
    if a.get("desired_time"):
        _apply(errors, B.set_time(d, a["desired_time"], field="desired_time"))
    if a.get("address"):
        _apply(errors, B.set_address(d, a["address"]))
    if a.get("name"):
        _apply(errors, B.set_name(d, a["name"]))
    if a.get("phone"):
        _apply(errors, B.set_phone(d, a["phone"]))
    if a.get("notes"):
        B.add_note(d, a["notes"])
    extra = {}
    if not_found:
        extra["prodotti_non_trovati_nel_menu"] = not_found
    total, complete = B.order_total(d.get("items"))
    extra["totale_prodotti_eur"] = total
    if not complete:
        extra["nota_totale"] = "alcuni prodotti hanno prezzo da confermare"
    return _state(ctx, errors, extra)


def tool_verifica_disponibilita(ctx, a):
    t = K.valid_hhmm(a.get("time"))
    if not t:
        return {"ok": False, "errori": ["orario non valido"]}
    return B.check_availability(a.get("day"), t, a.get("room") or K.ANY_ROOM, int(a.get("people") or 1))


def tool_invia_richiesta(ctx, a):
    d = ctx.draft
    if not a.get("cliente_ha_confermato"):
        return {"ok": False, "errori": ["Prima leggi il riepilogo e chiedi conferma al cliente."]}
    if d.get("intent") not in ("prenotazione", "ordine"):
        return {"ok": False, "errori": ["Non c'è nessuna prenotazione o ordine in corso."]}
    miss = B.missing_fields(d)
    if miss:
        return {"ok": False, "errori": ["Mancano dei dati"], "dati_mancanti": [B.FIELD_LABELS[f] for f in miss]}
    if d["intent"] == "prenotazione":
        problem = B.availability_problem(d)
        if problem:
            return {"ok": False, "errori": [problem]}
    text, ref = B.finalize(d, ctx.channel)
    ctx.finalized = ref
    ctx.draft = B.new_draft(d)
    return {"ok": True, "inviata": ref, "messaggio_per_il_cliente": text}


def tool_annulla_richiesta(ctx, a):
    ctx.draft = B.new_draft(ctx.draft)
    return {"ok": True}


def tool_passa_a_personale(ctx, a):
    reason = a.get("motivo") or "richiesta del cliente"
    ctx.handoff = reason
    if ctx.channel == "telephone" and os.getenv("STAFF_PHONE"):
        return {"ok": True, "trasferimento": "la chiamata verrà passata a un collega appena finisci di parlare: "
                                             "dillo al cliente in una frase"}
    d = ctx.draft
    if a.get("phone") and B.set_phone(d, a["phone"]):
        return {"ok": False, "errori": ["numero di telefono non valido: chiedilo di nuovo"]}
    if a.get("name"):
        B.set_name(d, a["name"])
    B.add_note(d, reason)
    if not d.get("phone"):
        return {"ok": False, "trasferimento": "non disponibile",
                "istruzioni": "chiedi al cliente un numero di telefono, poi richiama passa_a_personale con il campo phone"}
    d["intent"] = "richiamata"
    text, ref = B.finalize(d, ctx.channel)
    ctx.finalized = ref
    ctx.draft = B.new_draft(d)
    return {"ok": True, "trasferimento": "non disponibile", "richiamata_registrata": ref, "messaggio_per_il_cliente": text}


IMPL = {
    "aggiorna_prenotazione": tool_aggiorna_prenotazione,
    "aggiorna_ordine": tool_aggiorna_ordine,
    "verifica_disponibilita": tool_verifica_disponibilita,
    "invia_richiesta": tool_invia_richiesta,
    "annulla_richiesta": tool_annulla_richiesta,
    "passa_a_personale": tool_passa_a_personale,
}


# ---------------------------------------------------------------- prompt
def _days_table():
    today = K.service_today()
    rows = []
    for i in range(8):
        d = today + timedelta(days=i)
        label = "oggi" if i == 0 else "domani" if i == 1 else "dopodomani" if i == 2 else ""
        rows.append(f"{(label + ' = ') if label else ''}{K.WEEKDAYS[d.weekday()]} {d.day} {K.MONTHS[d.month - 1]} ({d.isoformat()})")
    return "; ".join(rows)


CHANNEL_STYLE = {
    "telephone": "Sei AL TELEFONO: la risposta viene letta da una voce sintetica. Frasi brevi (massimo 2-3), "
                 "niente elenchi puntati, niente emoji, niente simboli, niente markdown. Non leggere tutto il menu: "
                 "proponi al massimo 4-5 prodotti e chiedi cosa preferisce. Il numero del cliente è già noto.",
    "whatsapp": "Sei su WHATSAPP: risposte brevi e cordiali, niente markdown con asterischi doppi. "
                "Il numero del cliente è già noto.",
    "web": "Sei nella chat del sito web: risposte brevi e cordiali, testo semplice senza markdown. "
           "Per prenotazioni e ordini serve anche un numero di telefono del cliente per la conferma.",
}


def system_prompt(ctx):
    n = K.now()
    d = ctx.draft
    state = B.state_for_ai(d)
    miss = [B.FIELD_LABELS[f] for f in B.missing_fields(d)]
    rules = K.RULES
    return f"""Sei l'assistente virtuale (receptionist) del {K.BUSINESS_NAME} di {K.LOCALE.get('city', '')}.
Parla sempre in italiano, in modo naturale, cordiale e breve. Dai del tu.
Se il cliente chiede se sei una persona, spiega che sei un assistente virtuale e che può parlare con il personale.

DATA E ORA ATTUALI: {K.WEEKDAYS[n.weekday()]} {n.day} {K.MONTHS[n.month - 1]} {n.year}, ore {n:%H:%M}.
GIORNI: {_days_table()}.
ORARI DEL LOCALE: {K.LOCALE.get('hours_general', '')} Sale: {', '.join(K.ROOMS)}.

{CHANNEL_STYLE.get(ctx.channel, CHANNEL_STYLE['web'])}

COSA PUOI FARE
1. Prenotazioni tavolo: servono persone, giorno, orario, sala (o "qualsiasi" se è indifferente, ma chiedila: non assumerla), nome e cognome{', telefono' if ctx.channel == 'web' else ''}.
2. Ordini da asporto: prodotti, orario di ritiro, nome{', telefono' if ctx.channel == 'web' else ''}. Ordini a domicilio: in più l'indirizzo.
3. Informazioni su menu, prezzi, ingredienti, orari e sale, usando SOLO i dati qui sotto.

REGOLE
- Ogni volta che il cliente ti dà un dato, salvalo subito con aggiorna_prenotazione o aggiorna_ordine. Il sistema valida i dati: se uno strumento restituisce errori, spiegali al cliente e chiedi di nuovo.
- Chiedi un dato alla volta (al massimo due), in modo naturale.
- Quando non mancano più dati, leggi al cliente il riepilogo (campo riepilogo_da_leggere_al_cliente) e chiedi conferma. Solo dopo un sì esplicito chiama invia_richiesta con cliente_ha_confermato=true.
- Una prenotazione o un ordine inviato NON è confermato: il locale deve verificarlo. Non dire mai "confermato" e non promettere orari di ritiro o consegna.
- Usa solo prodotti del menu. Se un prodotto non esiste dillo e proponi alternative simili. Non inventare prezzi, ingredienti, disponibilità o tempi.
- Prezzi "da verificare" e ingredienti "da verificare": dillo chiaramente.
- Modifiche agli ingredienti: {rules.get('modifications', '')}
- Allergie e intolleranze: {rules.get('allergens', '')} Segnala sempre le allergie nel campo notes.
- Gruppi di più di {B.GROUP_MAX_AUTO} persone: non fare la prenotazione. Raccogli giorno, orario, nome e telefono e usa passa_a_personale spiegando che la disponibilità la verifica il personale.
- Modifiche o disdette di una prenotazione/ordine GIÀ inviati: non puoi modificarli tu. Usa passa_a_personale con i dettagli (vecchi e nuovi dati).
- Se il cliente vuole parlare con una persona, o serve una decisione del personale, usa passa_a_personale.
- Non parlare di argomenti che non riguardano il locale.

RICHIESTA IN CORSO (stato salvato): {json.dumps(state, ensure_ascii=False) if state.get('intent') else 'nessuna'}
{('Dati ancora mancanti: ' + ', '.join(miss)) if miss else ''}

MENU (categoria | nome | prezzo | ingredienti):
{K.menu_for_prompt()}
"""


def clean_reply(text, channel):
    text = (text or "").strip()
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    if channel == "telephone":
        text = re.sub(r"^\s*[-•*]\s*", "", text, flags=re.M)
        text = re.sub(r"€\s?(\d+(?:[.,]\d+)?)", r"\1 euro", text).replace("€", "euro").replace("\n", " ")
        text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def run(ctx, message, history):
    deadline = time.monotonic() + (11.0 if ctx.channel == "telephone" else 45.0)
    messages = [{"role": "system", "content": system_prompt(ctx)}]
    messages += [m for m in history[-16:] if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": message})
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise AIError("tempo scaduto")
        data = call_openai(messages, TOOLS, timeout=min(remaining, 25.0))
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = clean_reply(msg.get("content"), ctx.channel)
            if not reply:
                raise AIError("risposta vuota")
            STATUS["last_ok_at"] = K.now().isoformat(timespec="seconds")
            return reply
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = IMPL[name](ctx, args) if name in IMPL else {"ok": False, "errori": ["strumento sconosciuto"]}
            except Exception as e:  # argomenti malformati o errore nello strumento
                log.exception("errore nello strumento %s", name)
                result = {"ok": False, "errori": [f"errore interno: {e}"]}
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)})
    raise AIError("troppi passaggi senza risposta")
