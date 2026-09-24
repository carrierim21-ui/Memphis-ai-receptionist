"""Memphis AI Receptionist — server web (FastAPI).

Pagine:  /  chat pubblica · /admin dashboard del locale · /test laboratorio di prova
Canali:  /api/conversation (sito) · /whatsapp e /voice (webhook Twilio)
"""
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import date
from xml.sax.saxutils import escape

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402

import ai_agent  # noqa: E402
import booking as B  # noqa: E402
import conversation as C  # noqa: E402
import knowledge as K  # noqa: E402
import notify as N  # noqa: E402
import storage as S  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("memphis")

S.init_db()
app = FastAPI(title="Memphis AI Receptionist", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
STATIC = S.BASE / "static"


# ---------------------------------------------------------------- sicurezza
security = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    password = (os.getenv("ADMIN_PASSWORD") or "").strip()
    if not password:
        raise HTTPException(503, "Dashboard bloccata: imposta la variabile ADMIN_PASSWORD sul server.")
    user = (os.getenv("ADMIN_USER") or "admin").strip()
    ok = credentials is not None and secrets.compare_digest(credentials.username.encode(), user.encode()) \
        and secrets.compare_digest(credentials.password.encode(), password.encode())
    if not ok:
        raise HTTPException(401, "Credenziali non valide", headers={"WWW-Authenticate": 'Basic realm="Memphis"'})
    return True


ADMIN = [Depends(require_admin)]
_hits = defaultdict(deque)


def rate_limited(key, limit=40, window=600):
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


async def twilio_form(request: Request):
    form = await request.form()
    params = {k: v for k, v in form.items()}
    if N.validation_enabled():
        url = N.public_url(request)
        if not N.valid_signature(url, params, request.headers.get("X-Twilio-Signature")):
            log.warning("Firma Twilio non valida per %s", url)
            raise HTTPException(403, "Firma Twilio non valida")
    return params


async def json_body(request: Request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def page(name):
    return HTMLResponse((STATIC / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- pagine
@app.get("/", response_class=HTMLResponse)
def home():
    return page("index.html")


@app.get("/admin", response_class=HTMLResponse, dependencies=ADMIN)
def admin():
    return page("admin.html")


@app.get("/test", response_class=HTMLResponse, dependencies=ADMIN)
def test_lab():
    return page("test.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "memphis-ai-receptionist",
        "ai": {"enabled": ai_agent.enabled(), "model": ai_agent.model(), **ai_agent.STATUS},
        "twilio": {"messages": N.configured(), "webhook_signature_check": N.validation_enabled(),
                   "staff_transfer": bool(os.getenv("STAFF_PHONE")), "staff_alerts": bool(os.getenv("STAFF_NOTIFY_PHONE"))},
        "admin_protected": bool(os.getenv("ADMIN_PASSWORD")),
        "database": str(S.DB_PATH),
        "time": K.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- conversazione (sito)
@app.post("/api/conversation")
async def conversation(request: Request):
    data = await json_body(request)
    sid = str(data.get("session_id") or "")[:80] or "anonimo"
    ip = request.client.host if request.client else "?"
    if rate_limited(f"web:{sid}") or rate_limited(f"ip:{ip}", limit=120):
        return JSONResponse({"reply": "Troppi messaggi in poco tempo: riprova tra qualche minuto."}, status_code=429)
    return await run_in_threadpool(C.handle_message, data.get("message", ""), "web", sid, data.get("phone"))


@app.post("/api/chat")
async def chat_alias(request: Request):
    return await conversation(request)


# simulazione canali per il laboratorio di test (protetta)
@app.post("/api/channel/whatsapp", dependencies=ADMIN)
async def channel_whatsapp(request: Request):
    data = await json_body(request)
    return await run_in_threadpool(C.handle_message, data.get("message", ""), "whatsapp", None, data.get("phone"),
                                   bool(data.get("force_rules")))


@app.post("/api/channel/voice", dependencies=ADMIN)
async def channel_voice(request: Request):
    data = await json_body(request)
    return await run_in_threadpool(C.handle_message, data.get("message", ""), "telephone", data.get("call_id"),
                                   data.get("phone"), bool(data.get("force_rules")))


@app.post("/api/test/reset", dependencies=ADMIN)
async def test_reset(request: Request):
    data = await json_body(request)
    channel = data.get("channel") or "web"
    key = C.reset_session(channel, data.get("session_id"), data.get("phone"), keep_identity=False)
    return {"ok": True, "session": key}


# ---------------------------------------------------------------- Twilio: WhatsApp
@app.post("/whatsapp")
async def whatsapp(request: Request):
    form = await twilio_form(request)
    phone = K.normalize_phone(form.get("From"))
    body = str(form.get("Body") or "").strip()
    if not body and form.get("NumMedia", "0") != "0":
        body = "(il cliente ha inviato un allegato)"
    if rate_limited(f"wa:{phone}", limit=60):
        reply = "Stai inviando troppi messaggi: riprova tra qualche minuto."
    else:
        result = await run_in_threadpool(C.handle_message, body, "whatsapp", None, phone)
        reply = result["reply"]
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reply)}</Message></Response>'
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------- Twilio: telefono
def say(text):
    voice = (os.getenv("TWILIO_VOICE") or "").strip()
    v = f' voice="{escape(voice)}"' if voice else ""
    return f'<Say language="it-IT"{v}>{escape(text)}</Say>'


def gather(text):
    hints = "prenotazione, asporto, domicilio, sala principale, sala fumatori, soppalco, Memphis"
    return (f'<Gather input="speech" language="it-IT" action="/voice/turn" method="POST" speechTimeout="auto" '
            f'hints="{escape(hints)}">{say(text)}</Gather>'
            + say("Non ho sentito nessuna risposta. Grazie per aver chiamato, a presto!") + "<Hangup/>")


def twiml(inner):
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
                    media_type="application/xml")


@app.post("/voice")
async def voice(request: Request):
    form = await twilio_form(request)
    phone = K.normalize_phone(form.get("From"))
    call_id = form.get("CallSid") or "anonimo"
    await run_in_threadpool(C.reset_session, "telephone", call_id, phone)
    greeting = (f"Ciao, sei al {K.BUSINESS_NAME}. Sono l'assistente virtuale: posso aiutarti con prenotazioni, "
                "ordini da asporto e a domicilio, o informazioni sul menu. Dimmi pure.")
    return twiml(gather(greeting))


@app.post("/voice/turn")
async def voice_turn(request: Request):
    form = await twilio_form(request)
    phone = K.normalize_phone(form.get("From"))
    call_id = form.get("CallSid") or "anonimo"
    speech = str(form.get("SpeechResult") or "").strip()
    result = await run_in_threadpool(C.handle_message, speech, "telephone", call_id, phone)
    staff = (os.getenv("STAFF_PHONE") or "").strip()
    if result.get("handoff") and staff:
        return twiml(say(result["reply"]) + f'<Dial timeout="25">{escape(staff)}</Dial>'
                     + say("Al momento non riusciamo a rispondere. Ti richiameremo al più presto.") + "<Hangup/>")
    if result.get("complete"):
        return twiml(gather(result["reply"] + " Posso aiutarti con altro?"))
    return twiml(gather(result["reply"]))


# ---------------------------------------------------------------- menu (pubblico)
@app.get("/api/menu")
def get_menu():
    return K.MENU


@app.get("/api/menu/search")
def menu_search(q: str):
    return K.search_menu(q, 20)


# ---------------------------------------------------------------- prenotazioni / richieste (admin)
@app.get("/api/requests", dependencies=ADMIN)
def list_requests(status: str = "", limit: int = 200):
    if status:
        return S.rows("SELECT * FROM requests WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    return S.rows("SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/requests", dependencies=ADMIN)
@app.post("/api/request", dependencies=ADMIN)
async def create_request(request: Request):
    data = await json_body(request)
    data.setdefault("channel", "manuale")
    data.setdefault("type", "prenotazione")
    data["status"] = "DA_VERIFICARE"
    data["created_at"] = S.stamp()
    rid = S.insert("requests", data)
    return {"id": rid, "status": "DA_VERIFICARE"}


REQUEST_STATUSES = ["DA_VERIFICARE", "CONFERMATA", "RIFIUTATA", "CHIUSA"]


def _set_request_status(request_id, status):
    row = S.one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not row:
        return None, {"error": "richiesta non trovata"}
    if status != "CONFERMATA" and row["status"] == "CONFERMATA" and row.get("slot_id"):
        S.execute("UPDATE availability SET booked=MAX(0, booked-?) WHERE id=?", (row["people"] or 0, row["slot_id"]))
        S.update("requests", request_id, {"slot_id": None})
    S.update("requests", request_id, {"status": status})
    row = S.one("SELECT * FROM requests WHERE id=?", (request_id,))
    notice = B.notify_customer("prenotazione", row) if row["type"] == "prenotazione" else None
    return row, {"id": request_id, "status": status, "notifica": notice}


@app.post("/api/requests/{request_id}/status", dependencies=ADMIN)
async def set_request_status(request_id: int, request: Request):
    data = await json_body(request)
    status = data.get("status", "DA_VERIFICARE")
    if status not in REQUEST_STATUSES:
        return JSONResponse({"error": "stato non valido"}, 400)
    _, out = _set_request_status(request_id, status)
    return out


@app.post("/api/requests/{request_id}/confirm-reservation", dependencies=ADMIN)
def confirm_reservation(request_id: int):
    req = S.one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not req or req["type"] != "prenotazione":
        return JSONResponse({"error": "richiesta di prenotazione non trovata"}, 404)
    if req["status"] == "CONFERMATA":
        return {"status": "CONFERMATA", "info": "già confermata"}
    if not all([req["day"], req["time"], req["people"]]):
        return JSONResponse({"error": "dati di prenotazione incompleti"}, 400)
    rooms_sql, args = ("", ()) if req["room"] in (None, "", K.ANY_ROOM) else (" AND room=?", (req["room"],))
    slot = S.one("SELECT * FROM availability WHERE active=1 AND day=? AND time=? AND (capacity-booked)>=?"
                 + rooms_sql + " ORDER BY (capacity-booked) ASC LIMIT 1",
                 (req["day"], req["time"], req["people"]) + args)
    if not slot:
        return JSONResponse({"error": "Nessuno slot libero configurato per questo giorno/ora/sala. "
                                      "Aggiungi disponibilità o usa 'Conferma senza slot'."}, 409)
    S.execute("UPDATE availability SET booked=booked+? WHERE id=?", (req["people"], slot["id"]))
    S.update("requests", request_id, {"slot_id": slot["id"], "room": slot["room"]})
    _, out = _set_request_status(request_id, "CONFERMATA")
    out["slot_id"] = slot["id"]
    return out


# ---------------------------------------------------------------- disponibilità (admin)
@app.get("/api/availability", dependencies=ADMIN)
def get_availability(day: str = "", room: str = "", include_inactive: bool = True):
    q, args = "SELECT * FROM availability WHERE 1=1", []
    if not include_inactive:
        q += " AND active=1"
    if day:
        q += " AND day=?"
        args.append(day)
    if room:
        q += " AND room=?"
        args.append(room)
    return S.rows(q + " ORDER BY day, time, room", args)


@app.post("/api/availability", dependencies=ADMIN)
async def add_availability(request: Request):
    data = await json_body(request)
    t = K.valid_hhmm(data.get("time"))
    try:
        day = str(data.get("day") or "")
        date.fromisoformat(day)
        cap = int(data.get("capacity") or 0)
    except (ValueError, TypeError):
        return JSONResponse({"error": "giorno (AAAA-MM-GG) e posti devono essere validi"}, 400)
    if not t or cap < 1 or data.get("room") not in K.ROOMS:
        return JSONResponse({"error": "servono giorno, orario HH:MM, sala e posti"}, 400)
    rid = S.insert("availability", {"day": day, "time": t, "room": data["room"], "capacity": cap,
                                    "note": data.get("note", "")})
    return {"id": rid}


@app.post("/api/availability/{slot_id}/toggle", dependencies=ADMIN)
async def toggle_availability(slot_id: int, request: Request):
    data = await json_body(request)
    active = 1 if data.get("active", True) else 0
    S.execute("UPDATE availability SET active=? WHERE id=?", (active, slot_id))
    return {"id": slot_id, "active": active}


@app.post("/api/availability/{slot_id}/delete", dependencies=ADMIN)
def delete_availability(slot_id: int):
    S.execute("DELETE FROM availability WHERE id=?", (slot_id,))
    return {"id": slot_id, "deleted": True}


@app.get("/api/reservation-check", dependencies=ADMIN)
def reservation_check(day: str, time: str, people: int, room: str = K.ANY_ROOM):
    t = K.valid_hhmm(time)
    if not t:
        return JSONResponse({"error": "orario non valido"}, 400)
    return B.check_availability(day, t, room, people)


# ---------------------------------------------------------------- ordini (admin)
ORDER_STATUSES = ["DA_VERIFICARE", "CONFERMATO", "RIFIUTATO", "IN_PREPARAZIONE", "PRONTO", "IN_CONSEGNA",
                  "CONSEGNATO", "RITIRATO", "CHIUSO"]


def _order(order_id):
    return S.decode_order(S.one("SELECT * FROM orders WHERE id=?", (order_id,)))


@app.get("/api/orders", dependencies=ADMIN)
def get_orders(status: str = "", limit: int = 200):
    if status:
        rows = S.rows("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        rows = S.rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
    return [S.decode_order(r) for r in rows]


@app.post("/api/orders", dependencies=ADMIN)
async def create_order(request: Request):
    data = await json_body(request)
    if any(not data.get(k) for k in ["service", "name", "desired_time", "items"]):
        return JSONResponse({"error": "service, name, desired_time e items sono obbligatori"}, 400)
    if data["service"] not in ["asporto", "domicilio"]:
        return JSONResponse({"error": "servizio non valido"}, 400)
    if data["service"] == "domicilio" and not data.get("address"):
        return JSONResponse({"error": "per il domicilio serve l'indirizzo"}, 400)
    import json as _json
    total, complete = B.order_total(data["items"])
    oid = S.insert("orders", {**data, "channel": data.get("channel", "manuale"),
                              "items": _json.dumps(data["items"], ensure_ascii=False),
                              "total_eur": total if complete else None, "created_at": S.stamp()})
    return {"id": oid, "status": "DA_VERIFICARE"}


@app.get("/api/orders/{order_id}/review", dependencies=ADMIN)
def review_order(order_id: int):
    row = _order(order_id)
    if not row:
        return JSONResponse({"error": "ordine non trovato"}, 404)
    total, complete = B.order_total(row["items"] if isinstance(row["items"], list) else [])
    row["base_total_eur"] = total if complete else None
    return row


@app.post("/api/orders/{order_id}/edit", dependencies=ADMIN)
async def edit_order(order_id: int, request: Request):
    import json as _json
    data = await json_body(request)
    row = _order(order_id)
    if not row:
        return JSONResponse({"error": "ordine non trovato"}, 404)
    if row["status"] in ["RITIRATO", "CONSEGNATO", "CHIUSO", "RIFIUTATO"]:
        return JSONResponse({"error": "ordine non modificabile nello stato attuale"}, 409)
    items = data.get("items", row["items"])
    if not isinstance(items, list):
        return JSONResponse({"error": "items deve essere una lista"}, 400)
    clean = []
    for x in items:
        if not isinstance(x, dict) or not x.get("name"):
            return JSONResponse({"error": "ogni prodotto deve avere un nome"}, 400)
        item, _ = K.resolve_item(x["name"])
        price = x.get("unit_price_eur")
        if price is None and item:
            price = K.price_value(item)
        clean.append({"name": item["name"] if item else x["name"], "quantity": int(x.get("quantity") or 1),
                      "unit_price_eur": price, "modifications": x.get("modifications", "")})
    total, complete = B.order_total(clean)
    S.update("orders", order_id, {
        "items": _json.dumps(clean, ensure_ascii=False), "total_eur": total if complete else None,
        "desired_time": data.get("desired_time", row["desired_time"]), "address": data.get("address", row["address"]),
        "notes": data.get("notes", row["notes"]), "status": "DA_VERIFICARE"})
    return {"id": order_id, "status": "DA_VERIFICARE", "items": clean, "total_eur": total if complete else None}


@app.post("/api/orders/{order_id}/status", dependencies=ADMIN)
async def order_status(order_id: int, request: Request):
    data = await json_body(request)
    status = data.get("status", "DA_VERIFICARE")
    if status not in ORDER_STATUSES:
        return JSONResponse({"error": "stato non valido"}, 400)
    if not _order(order_id):
        return JSONResponse({"error": "ordine non trovato"}, 404)
    S.update("orders", order_id, {"status": status})
    notice = B.notify_customer("ordine", _order(order_id))
    return {"id": order_id, "status": status, "notifica": notice}


@app.post("/api/orders/{order_id}/customer-confirm", dependencies=ADMIN)
def customer_confirm_order(order_id: int):
    if not _order(order_id):
        return JSONResponse({"error": "ordine non trovato"}, 404)
    S.update("orders", order_id, {"customer_confirmed": 1})
    return {"id": order_id, "customer_confirmed": True}


@app.get("/api/orders/{order_id}/payment", dependencies=ADMIN)
def get_payment(order_id: int):
    row = _order(order_id)
    if not row:
        return JSONResponse({"error": "ordine non trovato"}, 404)
    return {k: row[k] for k in ("id", "status", "customer_confirmed", "payment_status", "payment_method",
                                "payment_reference", "paid_at", "total_eur")}


@app.post("/api/orders/{order_id}/payment-request", dependencies=ADMIN)
def payment_request(order_id: int):
    row = _order(order_id)
    if not row:
        return JSONResponse({"error": "ordine non trovato"}, 404)
    if row["status"] in ("DA_VERIFICARE", "RIFIUTATO"):
        return JSONResponse({"error": "conferma l'ordine prima di richiedere il pagamento"}, 409)
    S.update("orders", order_id, {"payment_status": "RICHIESTO"})
    return {"id": order_id, "payment_status": "RICHIESTO"}


@app.post("/api/orders/{order_id}/payment", dependencies=ADMIN)
async def set_payment(order_id: int, request: Request):
    data = await json_body(request)
    method = (data.get("payment_method") or "").strip().upper()
    reference = (data.get("payment_reference") or "").strip() or None
    if method not in {"CONTANTI", "POS", "ONLINE"}:
        return JSONResponse({"error": "metodo di pagamento non valido (CONTANTI, POS, ONLINE)"}, 400)
    row = _order(order_id)
    if not row:
        return JSONResponse({"error": "ordine non trovato"}, 404)
    if row["status"] in ("DA_VERIFICARE", "RIFIUTATO"):
        return JSONResponse({"error": "conferma l'ordine prima di registrare il pagamento"}, 409)
    if row["payment_status"] == "PAGATO":
        return JSONResponse({"error": "ordine già pagato"}, 409)
    S.update("orders", order_id, {"payment_status": "PAGATO", "payment_method": method,
                                  "payment_reference": reference, "paid_at": S.stamp()})
    return {"id": order_id, "payment_status": "PAGATO", "payment_method": method, "payment_reference": reference}


# ---------------------------------------------------------------- self-test (admin)
SELF_TESTS = [
    ("Prenotazione passo per passo", "web", [
        ("Vorrei prenotare un tavolo per domani", None), ("siamo in 4", None), ("alle 9", None),
        ("indifferente", None), ("Mario Rossi", None), ("333 1234567", "Confermi"), ("sì", "inviata")],
     lambda d: d.get("ref", {}) and d["ref"]["type"] == "prenotazione"),
    ("Prenotazione in un solo messaggio", "whatsapp", [
        ("Prenotazione per 2 persone domani alle 22 in sala fumatori a nome di Paolo Rossi", "Confermi"),
        ("confermo", "inviata")], lambda d: d.get("ref", {}) and d["ref"]["type"] == "prenotazione"),
    ("Ordine asporto + secondo ordine stesso cliente", "whatsapp", [
        ("Vorrei 2 Margherite e una Diavola senza cipolla da asporto", "ritirarlo"), ("al più presto", None),
        ("mi chiamo Anna Verdi", "Confermi"), ("sì confermo", "inviato"),
        ("Vorrei anche 1 Capricciosa da asporto, al più presto", "Confermi"), ("sì", "inviato")],
     lambda d: d.get("ref", {}) and d["ref"]["type"] == "ordine"),
    ("Ordine a domicilio con formato bevanda", "whatsapp", [
        ("vorrei una bufala e una guinness a domicilio", "formato"), ("grande", None), ("via Roma 12", None),
        ("appena possibile", None), ("Luca Neri", "Confermi"), ("sono d'accordo, confermo", "inviato")],
     lambda d: d.get("ref", {}) and d["ref"]["type"] == "ordine"),
    ("Nessuna pizza inventata ('Ciao Memphis' / 'mozzarella di bufala')", "whatsapp", [
        ("Ciao Memphis, vorrei una pizza con mozzarella di bufala da asporto", "Quale preferisci")],
     lambda d: not d["draft"].get("items")),
    ("Orario fuori apertura rifiutato", "whatsapp", [
        ("prenoto per 4 alle 15 domani", "aperto")], lambda d: not d["draft"].get("time")),
    ("Domanda sul menu", "web", [("avete la diavola?", "6,50")], lambda d: True),
    ("Gruppo oltre la soglia passa al personale", "whatsapp", [
        ("siamo in 25 per un compleanno venerdì, avete spazio?", "personale")], lambda d: True),
    ("Modifica di una prenotazione già inviata passa al personale", "whatsapp", [
        ("ho prenotato per domani alle 21 ma devo spostare alle 22", "personale")], lambda d: True),
    ("Errori di battitura capiti", "whatsapp", [
        ("vorei prenotare x 5 persone sabto verso le 21 e 30", "sala")],
     lambda d: d["draft"].get("time") == "21:30" and d["draft"].get("people") == 5),
]


@app.get("/api/self-test", dependencies=ADMIN)
def self_test(ai: bool = True):
    results = []
    created_req, created_ord = [], []
    N.DISABLED.on = True
    try:
        for i, (name, channel, steps, final_check) in enumerate(SELF_TESTS):
            phone = f"+3900000000{i:02d}"
            sid = f"selftest-{i}-{int(time.time())}"
            C.reset_session(channel, sid, phone if channel != "web" else None, keep_identity=False)
            ok, error, last = True, "", None
            for msg, expect in steps:
                last = C.handle_message(msg, channel, sid, phone if channel != "web" else None, force_rules=True)
                if last.get("ref"):
                    (created_req if last["ref"]["type"] != "ordine" else created_ord).append(last["ref"]["id"])
                if expect and expect.lower() not in last["reply"].lower():
                    ok, error = False, f"dopo «{msg}» risposta inattesa: {last['reply']}"
                    break
            if ok and not final_check(last):
                ok, error = False, f"controllo finale fallito: {last['reply']}"
            results.append({"name": name, "ok": ok, "error": error})
            S.delete_session(C.session_key(channel, sid, phone if channel != "web" else None))
    except Exception as e:
        log.exception("self-test")
        results.append({"name": "errore interno", "ok": False, "error": str(e)})
    finally:
        N.DISABLED.on = False
        for rid in created_req:
            S.execute("DELETE FROM requests WHERE id=?", (rid,))
        for oid in created_ord:
            S.execute("DELETE FROM orders WHERE id=?", (oid,))
    if ai:
        if ai_agent.enabled():
            try:
                ai_agent.ping()
                results.append({"name": f"Connessione OpenAI ({ai_agent.model()})", "ok": True, "error": ""})
            except Exception as e:
                ai_agent.record_error(e)
                results.append({"name": f"Connessione OpenAI ({ai_agent.model()})", "ok": False, "error": str(e)})
        else:
            results.append({"name": "OpenAI non configurato (manca OPENAI_API_KEY): uso solo il motore a regole",
                            "ok": True, "error": ""})
    return {"ok": all(r["ok"] for r in results), "results": results}
