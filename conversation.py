"""Punto di ingresso unico per tutti i canali (web, WhatsApp, telefono).

Carica la sessione del cliente, prova prima l'IA e, se non è configurata o non risponde,
usa il motore a regole. Salva sempre bozza e cronologia.
"""
import logging
from datetime import datetime, timedelta

import ai_agent
import booking as B
import knowledge as K
import rules
import storage as S

log = logging.getLogger("memphis.conversation")
SESSION_TTL = timedelta(hours=3)


class Ctx:
    def __init__(self, channel, draft):
        self.channel = channel
        self.draft = draft
        self.handoff = None
        self.finalized = None


def session_key(channel, session_id=None, phone=None):
    if channel in ("whatsapp", "telephone") and phone:
        return f"phone:{phone}"
    return f"{channel}:{session_id or 'anonimo'}"


def _expired(sess):
    try:
        ts = datetime.fromisoformat(sess["updated_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=K.TZ)
        return K.now() - ts > SESSION_TTL
    except (TypeError, ValueError, KeyError):
        return True


def reset_session(channel, session_id=None, phone=None, keep_identity=True):
    """Nuova conversazione (es. inizio di una telefonata): azzera bozza e cronologia."""
    key = session_key(channel, session_id, K.normalize_phone(phone))
    sess = S.load_session(key)
    draft = B.new_draft(sess["draft"] if sess and keep_identity else None)
    S.save_session(key, channel, draft, [])
    return key


def handle_message(message, channel="web", session_id=None, phone=None, force_rules=False):
    channel = channel if channel in ("web", "whatsapp", "telephone") else "web"
    message = " ".join(str(message or "").split())[:1000]
    phone = K.normalize_phone(phone)
    key = session_key(channel, session_id, phone)
    sess = S.load_session(key)
    if not sess or _expired(sess):
        draft, history = B.new_draft(sess["draft"] if sess else None), []
    else:
        draft, history = sess["draft"], sess["history"]
    if phone:
        if channel in ("whatsapp", "telephone"):
            draft["phone"] = phone
        elif not draft.get("phone"):
            draft["phone"] = K.parse_phone(phone) or None

    ctx = Ctx(channel, draft)
    engine = "regole"
    if not message:
        reply = "Scusa, non ho sentito bene. Puoi ripetere?"
    else:
        reply = None
        if not force_rules and ai_agent.enabled():
            try:
                reply = ai_agent.run(ctx, message, history)
                engine = "ia"
            except Exception as e:
                ai_agent.record_error(e)
                reply = None
        if not reply:
            try:
                reply = rules.turn(ctx, message)
            except Exception:
                log.exception("errore nel motore a regole")
                reply = "Scusa, ho avuto un problema tecnico. Puoi ripetere la richiesta?"
        history = history + [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    S.save_session(key, channel, ctx.draft, history)
    return {
        "reply": reply,
        "engine": engine,
        "intent": ctx.draft.get("intent"),
        "draft": B.state_for_ai(ctx.draft),
        "missing": B.missing_fields(ctx.draft),
        "complete": bool(ctx.finalized),
        "ref": ctx.finalized,
        "handoff": ctx.handoff,
        "session": key,
    }
