import os, json, sqlite3, re
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

load_dotenv()
BASE = Path(__file__).parent
MENU = json.loads((BASE/"data/menu.json").read_text(encoding="utf-8"))
DB = BASE/"memphis.db"

app = FastAPI(title="Memphis AI Receptionist", version="1.2.0")
app.mount("/static", StaticFiles(directory=BASE/"static"), name="static")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "memphis-ai-receptionist"}

SYSTEM = """Sei il receptionist AI ufficiale del Memphis Pub di Bernalda.
Parla in italiano, in modo naturale, breve e cordiale.

REGOLE OPERATIVE:
- Il locale ha sala principale, sala fumatori e soppalco.
- L'orario è generalmente 18:00–02:00, ma può variare.
- Per una prenotazione servono: numero persone, giorno, orario, sala, nome e cognome.
- Una prenotazione NON è confermata finché la disponibilità non viene verificata.
- Per asporto servono ordine, orario desiderato, nome e cognome.
- Per domicilio servono ordine, orario desiderato, indirizzo, nome e cognome.
- Per asporto e domicilio NON promettere mai un orario prima della verifica.
- I clienti possono chiedere di aggiungere o togliere ingredienti; eventuali costi extra vanno verificati.
- Per allergie/intolleranze devi far verificare la preparazione al personale. Non garantire assenza di allergeni o contaminazioni.
- Non inventare prezzi, ingredienti, disponibilità, tempi di consegna o informazioni non presenti nella knowledge base.
- Se una richiesta richiede una decisione del personale, dichiaralo chiaramente.
- Quando il cliente vuole prenotare, raccogli progressivamente i 5 dati: people, day, time, room, name.
- Quando tutti i dati ci sono, comunica che la richiesta sarà verificata; non dire mai "prenotazione confermata" senza verifica.
- Non assumere una sala se il cliente non la specifica.
- Per un ordine raccogli prodotto, quantità e modifiche; usa esclusivamente i prodotti presenti nella knowledge base.
- Per asporto raccogli anche orario desiderato e nome/cognome.
- Per domicilio raccogli anche indirizzo, orario desiderato e nome/cognome.
- Non inventare il prezzo di una modifica: se non è presente, va verificato.
- Non confermare mai un orario di ritiro o consegna senza verifica del personale.

KNOWLEDGE BASE:
""" + json.dumps(MENU, ensure_ascii=False)

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, type TEXT, name TEXT,
      people INTEGER, day TEXT, time TEXT, room TEXT, address TEXT,
      payload TEXT, status TEXT DEFAULT 'DA_VERIFICARE', created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS availability(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      day TEXT NOT NULL, time TEXT NOT NULL, room TEXT NOT NULL,
      capacity INTEGER NOT NULL, booked INTEGER DEFAULT 0,
      active INTEGER DEFAULT 1, note TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      channel TEXT, service TEXT, name TEXT, phone TEXT, address TEXT,
      desired_time TEXT, items TEXT, notes TEXT,
      status TEXT DEFAULT 'DA_VERIFICARE', customer_confirmed INTEGER DEFAULT 0,
      payment_status TEXT DEFAULT 'NON_RICHIESTO',
      payment_method TEXT,
      payment_reference TEXT,
      paid_at TEXT,
      created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
      session_id TEXT PRIMARY KEY,
      channel TEXT,
      intent TEXT,
      draft TEXT,
      updated_at TEXT
    )""")
    try:
        c.execute("ALTER TABLE orders ADD COLUMN customer_confirmed INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    for col, definition in [
        ("payment_status", "TEXT DEFAULT 'NON_RICHIESTO'"),
        ("payment_method", "TEXT"),
        ("payment_reference", "TEXT"),
        ("paid_at", "TEXT")
    ]:
        try:
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    return c

def save_request(data):
    c = db()
    cur = c.execute("""INSERT INTO requests
      (channel,type,name,people,day,time,room,address,payload,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)""",
      (data.get("channel","web"), data.get("type","unknown"), data.get("name"),
       data.get("people"), data.get("day"), data.get("time"), data.get("room"),
       data.get("address"), json.dumps(data, ensure_ascii=False),
       datetime.now().isoformat(timespec="seconds")))
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid

def local_fallback(message):
    m = message.lower()
    if "prenot" in m or "tavolo" in m:
        return "Certo. Per la prenotazione mi servono numero di persone, giorno, orario, sala e nome e cognome. La disponibilità va verificata prima della conferma."
    if "asporto" in m or "ritiro" in m:
        return "Va bene. Per l'asporto mi servono ordine, orario desiderato e nome e cognome. L'orario viene confermato solo dopo la verifica."
    if "domicilio" in m or "consegna" in m:
        return "Per il domicilio mi servono ordine, orario desiderato, indirizzo e nome e cognome. Prima verifico la possibilità di consegna."
    if "allerg" in m or "intoller" in m:
        return "Per allergie o intolleranze devo far verificare la preparazione al personale."
    return "Posso aiutarti con menu, prenotazioni, asporto e domicilio. Dimmi pure cosa ti serve."

def ai_reply(message):
    key = os.getenv("OPENAI_API_KEY")
    if not key or OpenAI is None:
        return local_fallback(message)
    try:
        client = OpenAI(api_key=key)
        r = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            instructions=SYSTEM,
            input=message
        )
        return r.output_text
    except Exception:
        return local_fallback(message)

@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE/"static/index.html").read_text(encoding="utf-8")

@app.get("/admin", response_class=HTMLResponse)
def admin():
    return (BASE/"static/admin.html").read_text(encoding="utf-8")

@app.get("/api/menu")
def get_menu():
    return MENU


def get_session(session_id, channel="web"):
    c=db()
    row=c.execute("SELECT * FROM sessions WHERE session_id=?",(session_id,)).fetchone()
    c.close()
    if row:
        return dict(row)
    return {"session_id":session_id,"channel":channel,"intent":"","draft":{}}

def save_session(session_id, channel, intent, draft):
    c=db()
    c.execute("""INSERT INTO sessions(session_id,channel,intent,draft,updated_at)
                 VALUES(?,?,?,?,?)
                 ON CONFLICT(session_id) DO UPDATE SET
                 channel=excluded.channel,intent=excluded.intent,
                 draft=excluded.draft,updated_at=excluded.updated_at""",
              (session_id,channel,intent,json.dumps(draft,ensure_ascii=False),
               datetime.now().isoformat(timespec="seconds")))
    c.commit(); c.close()


def menu_items():
    """Return only the actual menu item records from the knowledge base."""
    return MENU.get("items", []) if isinstance(MENU, dict) else MENU

def normalize_text(value):
    value=str(value or "").lower().strip()
    value=value.replace("’","'").replace("×","x")
    value=re.sub(r"\s+"," ",value)
    return value

def find_menu_matches(text, limit=5):
    """Find menu products by exact name first, then safe token/substring matching."""
    q=normalize_text(text)
    if not q:
        return []
    items=menu_items()
    exact=[]
    partial=[]
    for item in items:
        label=normalize_text(item.get("name",item.get("title","")))
        if not label:
            continue
        if label==q:
            exact.append(item)
        elif q in label or label in q:
            partial.append(item)
    if exact:
        return exact[:limit]
    # Token overlap, useful for phrases such as "burger wagyu" / punctuation variants.
    qtokens=set(re.findall(r"[a-zà-ÿ0-9]+",q))
    scored=[]
    for item in items:
        label=normalize_text(item.get("name",item.get("title","")))
        tokens=set(re.findall(r"[a-zà-ÿ0-9]+",label))
        overlap=len(qtokens & tokens)
        if overlap and overlap/ max(1,len(qtokens)) >= 0.5:
            scored.append((overlap/len(tokens or {""}),item))
    scored.sort(key=lambda x:x[0],reverse=True)
    return [x[1] for x in scored[:limit]]

def find_menu_item(name):
    matches=find_menu_matches(name,1)
    return matches[0] if matches else None

def parse_quantity(text, default=1):
    words={"una":1,"un":1,"uno":1,"due":2,"tre":3,"quattro":4,"cinque":5,"sei":6,"sette":7,"otto":8,"nove":9,"dieci":10}
    m=re.search(r"\b(\d{1,2})\b",normalize_text(text))
    if m:
        return int(m.group(1))
    for w,n in words.items():
        if re.search(rf"\b{w}\b",normalize_text(text)):
            return n
    return default

def detect_menu_items(message):
    """Extract products only when the user's text clearly names a menu item."""
    low=normalize_text(message)
    found=[]
    for item in menu_items():
        label=normalize_text(item.get("name",item.get("title","")))
        if not label:
            continue
        # Word-boundary matching avoids accidental matches inside unrelated words.
        variants=[label]
        if label.endswith("a") and len(label)>3:
            variants.append(label[:-1]+"e")
        matched=None
        for variant in variants:
            if re.search(r"(?<![\wà-ÿ])"+re.escape(variant)+r"(?![\wà-ÿ])",low):
                matched=variant
                break
        if matched:
            prefix=low[:low.find(matched)]
            q=parse_quantity(prefix,1)
            found.append({"name":item.get("name",item.get("title","")),"quantity":q,
                          "unit_price_eur":item.get("price_eur"),
                          "category":item.get("category"),
                          "description":item.get("description","")})
    return found

def merge_items(existing, found):
    merged=[dict(x) for x in existing]
    for it in found:
        hit=next((x for x in merged if normalize_text(x.get("name"))==normalize_text(it.get("name"))),None)
        if hit:
            hit["quantity"]=int(hit.get("quantity",0))+int(it.get("quantity",1))
        else:
            merged.append(dict(it))
    return merged

def order_total(items):
    total=0.0
    for it in items or []:
        price=it.get("unit_price_eur")
        qty=it.get("quantity",1)
        if price is None:
            return None
        try:
            total += float(price)*int(qty)
        except (ValueError,TypeError):
            return None
    return round(total,2)


def missing_fields(intent,d):
    if intent=="prenotazione":
        return [k for k in ["people","day","time","room","name"] if not d.get(k)]
    if intent=="asporto":
        return [k for k in ["items","desired_time","name"] if not d.get(k)]
    if intent=="domicilio":
        return [k for k in ["items","desired_time","address","name"] if not d.get(k)]
    return []

def normalize_channel(value):
    value=(value or "web").lower().strip()
    aliases={"wa":"whatsapp","whatsapp":"whatsapp","voice":"telephone","phone":"telephone","call":"telephone","web":"web","test":"web"}
    return aliases.get(value,value)

def normalize_phone(value):
    if not value:
        return ""
    value=str(value).strip()
    if value.lower().startswith("whatsapp:"):
        value=value.split(":",1)[1]
    return re.sub(r"[^0-9+]", "", value)

def channel_session_id(channel, phone=None, provider_id=None, session_id=None):
    phone=normalize_phone(phone)
    if phone:
        return "phone:"+phone
    if provider_id:
        return f"{channel}:{provider_id}"
    return session_id or f"{channel}:anonymous"

def process_conversation(message, channel="web", session_id=None, phone=None):
    message=(message or "").strip()
    channel=normalize_channel(channel)
    sid=channel_session_id(channel, phone=phone, session_id=session_id)
    s=get_session(sid,channel)
    draft=s.get("draft",{})
    if isinstance(draft,str):
        try: draft=json.loads(draft)
        except: draft={}
    intent=s.get("intent","")
    if phone:
        draft["phone"]=normalize_phone(phone)

    # Reservation extraction for the real test flow. We only capture explicit data;
    # availability is still checked before any confirmation.
    low=message.lower()
    if any(k in low for k in ["prenota", "prenotare", "prenotazione", "tavolo"]):
        intent="prenotazione"
    if intent=="prenotazione":
        pm=re.search(r"\b(\d{1,2})\s*(?:persone|persona|posti|posto)\b", low)
        if pm: draft["people"]=int(pm.group(1))
        dm=re.search(r"\b(?:il|per il|giorno)\s*(\d{1,2})(?:[/-](\d{1,2}))?(?:[/-](\d{2,4}))?\b", low)
        if dm:
            day=dm.group(1)
            month=dm.group(2)
            year=dm.group(3)
            if month:
                if not year: year="2026"
                if len(year)==2: year="20"+year
                draft["day"]=f"{year}-{int(month):02d}-{int(day):02d}"
            else:
                draft["day"]=day
        tm=re.search(r"\b(?:alle|ore)\s*(\d{1,2})(?::(\d{2}))?\b", low)
        if tm: draft["time"]=f"{int(tm.group(1)):02d}:{tm.group(2) or '00'}"
        for room in ["sala principale","sala fumatori","soppalco"]:
            if room in low: draft["room"]=room
        if "nome e cognome" in low or "mi chiamo" in low or "a nome di" in low:
            nm=re.search(r"(?:nome e cognome|mi chiamo|a nome di)\s*[:\-]?\s*([A-Za-zÀ-ÿ' ]{3,})", message, re.I)
            if nm: draft["name"]=nm.group(1).strip()
        elif "a nome " in low:
            nm=re.search(r"a nome\s+([A-Za-zÀ-ÿ' ]{3,})", message, re.I)
            if nm: draft["name"]=nm.group(1).strip()

    # Lightweight deterministic extraction for the MVP; AI remains responsible for natural dialogue.
    low=message.lower()
    if "domicilio" in low or "consegna" in low:
        intent="domicilio"
    elif "asporto" in low or "ritiro" in low:
        intent="asporto"

    # Pull explicit name after common phrases
    nm=re.search(r"(?:nome e cognome|mi chiamo|a nome di)\s*[:\-]?\s*([A-Za-zÀ-ÿ' ]{3,})",message,re.I)
    if nm: draft["name"]=nm.group(1).strip()

    tm=re.search(r"\b(?:alle|ore)\s*(\d{1,2}(?::\d{2})?)\b",low)
    if tm: draft["desired_time"]=tm.group(1)

    # Address after "indirizzo"
    ad=re.search(r"indirizzo\s*[:\-]?\s*(.+)",message,re.I)
    if ad: draft["address"]=ad.group(1).strip()

    # Product extraction is now driven by the real menu catalogue.
    found=detect_menu_items(message)
    if found:
        draft["items"]=merge_items(draft.get("items",[]),found)

    # Basic natural-language modification commands for the MVP.
    remove=re.search(r"(?:togli|senza|rimuovi)\s+(.+?)(?:\s+da|\s*$)",low,re.I)
    if remove and draft.get("items"):
        ingredient=remove.group(1).strip(" .,!?:;")
        draft.setdefault("notes","")
        draft["notes"] += ("; " if draft["notes"] else "") + f"modifica: senza {ingredient}"

    addq=re.search(r"(?:aggiungi|metti anche)\s+(\d+)?\s*(.+)$",low,re.I)
    if addq:
        candidate=addq.group(2).strip(" .,!?:;")
        item=find_menu_item(candidate)
        if item:
            label=str(item.get("name",item.get("title","")))
            qty=int(addq.group(1) or 1)
            items=draft.setdefault("items",[])
            hit=next((x for x in items if x["name"].lower()==label.lower()),None)
            if hit: hit["quantity"]+=qty
            else: items.append({"name":label,"quantity":qty,
                                "unit_price_eur":item.get("price_eur"),
                                "category":item.get("category"),
                                "description":item.get("description","")})
        else:
            draft.setdefault("notes","")
            draft["notes"] += ("; " if draft["notes"] else "") + f"richiesta cliente: aggiungere {candidate}"

    # Change quantity: "fammi 2 Margherite", "una Margherita in più".
    qtyq=re.search(r"(?:fammi|fammene|metti)\s+(\d+)\s+(.+)$",low,re.I)
    if qtyq:
        item=find_menu_item(qtyq.group(2).strip())
        if item:
            label=str(item.get("name",item.get("title","")))
            items=draft.setdefault("items",[])
            hit=next((x for x in items if x["name"].lower()==label.lower()),None)
            if hit: hit["quantity"]=int(qtyq.group(1))
            else: items.append({"name":label,"quantity":int(qtyq.group(1)),
                                "unit_price_eur":item.get("price_eur"),
                                "category":item.get("category"),
                                "description":item.get("description","")})

    if not intent:
        save_session(sid,channel,intent,draft)
        return {"reply":ai_reply(message),"intent":"","draft":draft,"complete":False}

    miss=missing_fields(intent,draft)
    save_session(sid,channel,intent,draft)

    if miss:
        prompts={
            "items":"Cosa vuoi ordinare?",
            "desired_time":"A che ora desideri ritirarlo?" if intent=="asporto" else "A che ora desideri riceverlo?",
            "address":"Mi dai l'indirizzo per la consegna?",
            "name":"A che nome devo registrare l'ordine?"
        }
        return {"reply":prompts[miss[0]],"intent":intent,"draft":draft,"complete":False}

    if intent=="prenotazione":
        if not draft.get("request_id"):
            rid=save_request({"channel":channel,"type":"prenotazione","name":draft.get("name"),
                              "people":draft.get("people"),"day":draft.get("day"),
                              "time":draft.get("time"),"room":draft.get("room"),
                              "payload":draft})
            draft["request_id"]=rid
            save_session(sid,channel,intent,draft)
            return {"reply":f"Perfetto. Ho registrato la richiesta di prenotazione per {draft['people']} persone, {draft['day']} alle {draft['time']} in {draft['room']}, a nome di {draft['name']}. La disponibilità deve essere verificata dal Memphis prima della conferma.","intent":intent,"draft":draft,"complete":True,"request_id":rid}
        return {"reply":"La richiesta è già registrata e resta in attesa della verifica del Memphis.","intent":intent,"draft":draft,"complete":True,"request_id":draft.get("request_id")}

    summary=", ".join(f'{x.get("quantity",1)}× {x.get("name","")}' for x in draft["items"])
    total=order_total(draft["items"])
    total_text=f" Totale prodotti: €{total:.2f}." if total is not None else " Totale da verificare (eventuali modifiche possono avere costi extra)."
    # First completion creates a draft order and asks the customer for an explicit confirmation.
    if not draft.get("order_id"):
        oid=save_order({
            "channel":channel,"service":intent,"name":draft["name"],"phone":normalize_phone(phone),
            "address":draft.get("address"),"desired_time":draft["desired_time"],
            "items":draft["items"],"notes":draft.get("notes","")
        })
        draft["order_id"]=oid
        draft["awaiting_confirmation"]=True
        save_session(sid,channel,intent,draft)
        return {"reply":f"Ti riepilogo: {summary}.{total_text} Servizio: {intent}. Orario richiesto: {draft['desired_time']}. Nome: {draft['name']}. Confermi l'ordine?",
                "intent":intent,"draft":draft,"complete":False,"awaiting_confirmation":True,"order_id":oid}

    if draft.get("awaiting_confirmation"):
        positive=any(x in low for x in ["confermo","conferma","si","sì","ok","va bene","procedi","esatto"])
        negative=any(x in low for x in ["no","annulla","cambia","modifica"])
        if positive and not negative:
            c=db()
            c.execute("UPDATE orders SET customer_confirmed=1,status='DA_VERIFICARE' WHERE id=?",
                      (draft["order_id"],))
            c.commit(); c.close()
            draft["awaiting_confirmation"]=False
            draft["customer_confirmed"]=True
            save_session(sid,channel,intent,draft)
            return {"reply":f"Perfetto, ordine #{draft['order_id']} registrato. Ora il Memphis deve verificare la possibilità di preparazione e l'orario richiesto prima della conferma definitiva.",
                    "intent":intent,"draft":draft,"complete":True,"customer_confirmed":True,"order_id":draft["order_id"]}
        if negative:
            draft["awaiting_confirmation"]=False
            save_session(sid,channel,intent,draft)
            return {"reply":"Va bene. Dimmi cosa vuoi modificare nell'ordine.","intent":intent,"draft":draft,"complete":False}

    return {"reply":f"Riepilogo: {summary}.{total_text} Confermi l'ordine?","intent":intent,"draft":draft,"complete":False,"awaiting_confirmation":True,"order_id":draft.get("order_id")}

@app.post("/api/conversation")
async def conversation(request: Request):
    data=await request.json()
    return process_conversation(
        data.get("message",""),
        channel=data.get("channel","web"),
        session_id=data.get("session_id"),
        phone=data.get("phone")
    )


def save_order(data):
    c=db()
    cur=c.execute("""INSERT INTO orders
      (channel,service,name,phone,address,desired_time,items,notes,created_at)
      VALUES(?,?,?,?,?,?,?,?,?)""",
      (data.get("channel","web"),data["service"],data["name"],data.get("phone"),
       data.get("address"),data["desired_time"],json.dumps(data["items"],ensure_ascii=False),
       data.get("notes",""),datetime.now().isoformat(timespec="seconds")))
    c.commit(); oid=cur.lastrowid; c.close(); return oid

@app.post("/api/chat")
async def chat(request: Request):
    data = await request.json()
    return {"reply": ai_reply(data.get("message","")), "mode": "openai" if os.getenv("OPENAI_API_KEY") else "fallback"}

@app.post("/api/request")
async def create_request(request: Request):
    data = await request.json()
    rid = save_request(data)
    return {"id": rid, "status": "DA_VERIFICARE"}

@app.get("/api/availability")
def get_availability(day: str = "", room: str = ""):
    c = db()
    q = "SELECT * FROM availability WHERE active=1"
    args = []
    if day:
        q += " AND day=?"; args.append(day)
    if room:
        q += " AND room=?"; args.append(room)
    q += " ORDER BY day,time,room"
    rows = [dict(x) for x in c.execute(q, args).fetchall()]
    c.close()
    return rows

@app.post("/api/availability")
async def add_availability(request: Request):
    data = await request.json()
    required = ["day","time","room","capacity"]
    if any(not data.get(k) for k in required):
        return {"error":"day, time, room e capacity sono obbligatori"}
    c = db()
    cur = c.execute(
        "INSERT INTO availability(day,time,room,capacity,note) VALUES(?,?,?,?,?)",
        (data["day"], data["time"], data["room"], int(data["capacity"]), data.get("note",""))
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return {"id": rid}

@app.post("/api/availability/{slot_id}/toggle")
async def toggle_availability(slot_id: int, request: Request):
    data = await request.json()
    active = 1 if data.get("active", True) else 0
    c = db()
    c.execute("UPDATE availability SET active=? WHERE id=?", (active, slot_id))
    c.commit(); c.close()
    return {"id":slot_id,"active":active}

@app.get("/api/reservation-check")
def reservation_check(day: str, time: str, room: str, people: int):
    c = db()
    rows = [dict(x) for x in c.execute(
        """SELECT * FROM availability
           WHERE active=1 AND day=? AND time=? AND room=?
           AND (capacity-booked)>=?""",
        (day,time,room,int(people))
    ).fetchall()]
    c.close()
    return {"available": bool(rows), "slots": rows}

@app.post("/api/requests/{request_id}/confirm-reservation")
async def confirm_reservation(request_id: int):
    c = db()
    req = c.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not req or req["type"] != "prenotazione":
        c.close()
        return {"error":"richiesta di prenotazione non trovata"}
    if not all([req["day"], req["time"], req["room"], req["people"]]):
        c.close()
        return {"error":"dati di prenotazione incompleti"}
    slot = c.execute(
        """SELECT * FROM availability
           WHERE active=1 AND day=? AND time=? AND room=?
           AND (capacity-booked)>=?
           ORDER BY id LIMIT 1""",
        (req["day"],req["time"],req["room"],req["people"])
    ).fetchone()
    if not slot:
        c.close()
        return {"error":"nessuna disponibilità configurata per questo slot"}
    c.execute("UPDATE availability SET booked=booked+? WHERE id=?",
              (req["people"],slot["id"]))
    c.execute("UPDATE requests SET status='CONFERMATA' WHERE id=?", (request_id,))
    c.commit(); c.close()
    return {"status":"CONFERMATA","slot_id":slot["id"]}


@app.post("/api/orders")
async def create_order(request: Request):
    data = await request.json()
    required = ["service","name","desired_time","items"]
    if any(not data.get(k) for k in required):
        return {"error":"service, name, desired_time e items sono obbligatori"}
    service = data["service"]
    if service not in ["asporto","domicilio"]:
        return {"error":"servizio non valido"}
    if service == "domicilio" and not data.get("address"):
        return {"error":"per il domicilio serve l'indirizzo"}
    oid=save_order(data)
    return {"id":oid,"status":"DA_VERIFICARE","customer_confirmed":False}


@app.post("/api/orders/{order_id}/edit")
async def edit_order(order_id:int, request:Request):
    data=await request.json()
    c=db()
    row=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not row:
        c.close(); return {"error":"ordine non trovato"}
    if row["status"] in ["RITIRATO","CONSEGNATO","CHIUSO","RIFIUTATO"]:
        c.close(); return {"error":"ordine non modificabile nello stato attuale"}
    try:
        items=data.get("items")
        if items is None:
            items=json.loads(row["items"])
        c.execute("""UPDATE orders SET items=?,desired_time=?,address=?,notes=?,
                     status='DA_VERIFICARE' WHERE id=?""",
                  (json.dumps(items,ensure_ascii=False),
                   data.get("desired_time",row["desired_time"]),
                   data.get("address",row["address"]),
                   data.get("notes",row["notes"]),
                   order_id))
        c.commit(); c.close()
        return {"id":order_id,"status":"DA_VERIFICARE","items":items}
    except Exception as e:
        c.close(); return {"error":"modifica non valida"}


@app.get("/api/orders/{order_id}/review")
def review_order(order_id:int):
    c=db()
    row=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    c.close()
    if not row: return {"error":"ordine non trovato"}
    try: items=json.loads(row["items"])
    except: items=[]
    return {
        "id":row["id"], "service":row["service"], "name":row["name"],
        "payment_status":row["payment_status"], "payment_method":row["payment_method"],
        "address":row["address"], "desired_time":row["desired_time"],
        "items":items, "notes":row["notes"], "status":row["status"],
        "base_total_eur":order_total(items),
        "customer_confirmed":bool(row["customer_confirmed"])
    }

@app.post("/api/orders/{order_id}/customer-confirm")
async def customer_confirm_order(order_id:int):
    c=db()
    row=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not row:
        c.close(); return {"error":"ordine non trovato"}
    c.execute("UPDATE orders SET customer_confirmed=1,status='DA_VERIFICARE' WHERE id=?",(order_id,))
    c.commit(); c.close()
    return {"id":order_id,"customer_confirmed":True,"status":"DA_VERIFICARE"}


@app.get("/api/orders/{order_id}/payment")
def get_payment(order_id:int):
    c=db()
    row=c.execute("SELECT id,status,customer_confirmed,payment_status,payment_method,payment_reference,paid_at FROM orders WHERE id=?",(order_id,)).fetchone()
    c.close()
    if not row: return {"error":"ordine non trovato"}
    return dict(row)

@app.post("/api/orders/{order_id}/payment-request")
async def payment_request(order_id:int):
    c=db()
    row=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not row:
        c.close(); return {"error":"ordine non trovato"}
    if not row["customer_confirmed"]:
        c.close(); return {"error":"il cliente non ha ancora confermato l'ordine"}
    if row["status"] != "CONFERMATO":
        c.close(); return {"error":"il Memphis deve confermare l'ordine prima di richiedere il pagamento"}
    c.execute("UPDATE orders SET payment_status='RICHIESTO' WHERE id=?",(order_id,))
    c.commit(); c.close()
    return {"id":order_id,"payment_status":"RICHIESTO"}

@app.post("/api/orders/{order_id}/payment")
async def set_payment(order_id:int):
    data=await request.json()
    method=(data.get("payment_method") or "").strip()
    reference=(data.get("payment_reference") or "").strip() or None
    if method not in {"CONTANTI","POS","ONLINE"}:
        return {"error":"metodo pagamento non valido"}
    c=db()
    row=c.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
    if not row:
        c.close(); return {"error":"ordine non trovato"}
    if row["status"] != "CONFERMATO":
        c.close(); return {"error":"ordine non confermato dal Memphis"}
    if row["payment_status"] not in {"RICHIESTO","NON_RICHIESTO"}:
        c.close(); return {"error":"stato pagamento non modificabile"}
    from datetime import datetime
    c.execute("""UPDATE orders SET payment_status='PAGATO',payment_method=?,
                 payment_reference=?,paid_at=? WHERE id=?""",
              (method,reference,datetime.now().isoformat(timespec="seconds"),order_id))
    c.commit(); c.close()
    return {"id":order_id,"payment_status":"PAGATO","payment_method":method,"payment_reference":reference}

@app.get("/api/orders")
def get_orders():
    c=db()
    rows=[dict(x) for x in c.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()]
    c.close()
    for x in rows:
        try: x["items"]=json.loads(x["items"])
        except: pass
    return rows

@app.post("/api/orders/{order_id}/status")
async def order_status(order_id:int, request:Request):
    data=await request.json()
    status=data.get("status","DA_VERIFICARE")
    allowed=["DA_VERIFICARE","CONFERMATO","RIFIUTATO","IN_PREPARAZIONE","PRONTO","CONSEGNATO","RITIRATO","CHIUSO"]
    if status not in allowed: return {"error":"stato non valido"}
    c=db(); c.execute("UPDATE orders SET status=? WHERE id=?",(status,order_id)); c.commit(); c.close()
    return {"id":order_id,"status":status}

@app.get("/api/menu/search")
def menu_search(q:str):
    q=q.lower().strip()
    results=[]
    for item in menu_items():
        text=json.dumps(item,ensure_ascii=False).lower()
        if q in text:
            results.append(item)
    return results[:20]

@app.get("/api/requests")
def requests():
    c = db()
    rows = [dict(x) for x in c.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()]
    c.close()
    return rows

@app.post("/api/requests/{request_id}/status")
async def set_status(request_id: int, request: Request):
    data = await request.json()
    status = data.get("status","DA_VERIFICARE")
    if status not in ["DA_VERIFICARE","CONFERMATA","RIFIUTATA","CHIUSA"]:
        return {"error":"stato non valido"}
    c = db()
    c.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
    c.commit()
    c.close()
    return {"id": request_id, "status": status}

@app.post("/api/test/reset")
async def test_reset(request: Request):
    data=await request.json()
    sid=data.get("session_id")
    if not sid: return {"ok":False,"error":"session_id richiesto"}
    c=db(); c.execute("DELETE FROM sessions WHERE session_id=?",(sid,)); c.commit(); c.close()
    return {"ok":True,"session_id":sid}

@app.get("/api/self-test")
def self_test():
    results=[]
    sid="selftest-order-"+datetime.now().strftime("%Y%m%d%H%M%S%f")
    try:
        r1=process_conversation("Vorrei 2 Margherite da asporto", "web", sid, "+393331234567")
        results.append({"name":"order extraction","ok":any(x.get("name")=="Margherita" and x.get("quantity")==2 for x in r1["draft"].get("items",[]))})
        r2=process_conversation("alle 21", "whatsapp", None, "+393331234567")
        results.append({"name":"shared phone session","ok":r2["draft"].get("desired_time")=="21"})
        r3=process_conversation("mi chiamo Mario Rossi", "telephone", None, "+393331234567")
        results.append({"name":"cross-channel name","ok":r3["draft"].get("name")=="Mario Rossi"})
        r4=process_conversation("Vorrei prenotare un tavolo per 4 persone il 26/09 alle 21 in sala principale a nome Luca Bianchi", "web", "selftest-reservation", "+393331234567")
        results.append({"name":"reservation capture","ok":r4.get("intent")=="prenotazione" and r4.get("complete") is True})
    except Exception as e:
        results.append({"name":"exception","ok":False,"error":str(e)})
    return {"ok":all(x["ok"] for x in results),"results":results}

@app.get("/test", response_class=HTMLResponse)
def test_lab():
    return (BASE/"static/test.html").read_text(encoding="utf-8")

@app.post("/voice")
async def voice(request: Request):
    form=await request.form()
    phone=normalize_phone(form.get("From"))
    call_id=form.get("CallSid") or form.get("CallId") or "anonymous"
    xml=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
<Say language="it-IT">Ciao, Memphis Pub. Come posso aiutarti?</Say>
<Gather input="speech" language="it-IT" action="/voice/turn" method="POST" speechTimeout="auto">
<Say language="it-IT">Dimmi pure.</Say>
</Gather>
</Response>"""
    return Response(content=xml, media_type="application/xml")

@app.post("/voice/turn")
async def voice_turn(request: Request):
    form=await request.form()
    speech=str(form.get("SpeechResult") or "").strip()
    phone=normalize_phone(form.get("From"))
    call_id=form.get("CallSid") or form.get("CallId") or "anonymous"
    result=process_conversation(speech,"telephone",call_id,phone)
    safe=(result.get("reply") or "").replace("&","e").replace("<","").replace(">","")
    xml=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
<Say language="it-IT">{safe}</Say>
<Gather input="speech" language="it-IT" action="/voice/turn" method="POST" speechTimeout="auto">
<Say language="it-IT">Dimmi pure.</Say>
</Gather>
</Response>"""
    return Response(content=xml, media_type="application/xml")

@app.post("/whatsapp")
async def whatsapp(request: Request):
    form=await request.form()
    incoming=str(form.get("Body") or "").strip()
    phone=normalize_phone(form.get("From"))
    provider_id=form.get("MessageSid") or form.get("SmsMessageSid") or "anonymous"
    result=process_conversation(incoming,"whatsapp",provider_id,phone)
    safe=(result.get("reply") or "").replace("&","e").replace("<","").replace(">","")
    xml=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{safe}</Message></Response>"""
    return Response(content=xml, media_type="application/xml")

@app.post("/api/channel/whatsapp")
async def channel_whatsapp(request: Request):
    data=await request.json()
    result=process_conversation(data.get("message",""),"whatsapp",data.get("message_id"),data.get("phone"))
    return result

@app.post("/api/channel/voice")
async def channel_voice(request: Request):
    data=await request.json()
    result=process_conversation(data.get("message",""),"telephone",data.get("call_id"),data.get("phone"))
    return result
