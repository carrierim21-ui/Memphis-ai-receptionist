"""Integrazione Twilio: invio WhatsApp/SMS e verifica della firma dei webhook.

Usa direttamente le API REST di Twilio (niente SDK), così non ci sono dipendenze in più.
Tutto è opzionale: se le variabili d'ambiente non sono impostate, le notifiche vengono saltate.
"""
import base64
import hashlib
import hmac
import logging
import os
import threading

import httpx

log = logging.getLogger("memphis.notify")
DISABLED = threading.local()  # usato dal self-test per non mandare messaggi veri


def env(name):
    return (os.getenv(name) or "").strip()


def configured():
    return bool(env("TWILIO_ACCOUNT_SID") and env("TWILIO_AUTH_TOKEN")
                and (env("TWILIO_WHATSAPP_FROM") or env("TWILIO_SMS_FROM")))


def _with_prefix(n):
    n = n.strip()
    return n if n.startswith("whatsapp:") else "whatsapp:" + n


def send_message(to, body, prefer="whatsapp"):
    """Invia un messaggio. prefer='whatsapp' usa WhatsApp se configurato, altrimenti SMS.
    Ritorna (ok, descrizione)."""
    if getattr(DISABLED, "on", False):
        return False, "notifiche disattivate (test)"
    if not to:
        return False, "nessun numero del cliente"
    sid, token = env("TWILIO_ACCOUNT_SID"), env("TWILIO_AUTH_TOKEN")
    wa_from, sms_from = env("TWILIO_WHATSAPP_FROM"), env("TWILIO_SMS_FROM")
    if not (sid and token):
        return False, "Twilio non configurato"
    if prefer == "whatsapp" and wa_from:
        frm, dest, kind = _with_prefix(wa_from), _with_prefix(to), "WhatsApp"
    elif sms_from:
        frm, dest, kind = sms_from, to, "SMS"
    elif wa_from:
        frm, dest, kind = _with_prefix(wa_from), _with_prefix(to), "WhatsApp"
    else:
        return False, "nessun mittente Twilio configurato"
    try:
        r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                       data={"From": frm, "To": dest, "Body": body[:1500]}, auth=(sid, token), timeout=15)
        if r.status_code >= 300:
            log.warning("Twilio %s: %s", r.status_code, r.text[:300])
            return False, f"{kind} non inviato ({r.status_code}): {r.json().get('message', '') if r.headers.get('content-type', '').startswith('application/json') else r.text[:120]}"
        return True, f"{kind} inviato"
    except Exception as e:  # rete, timeout...
        log.warning("Twilio errore: %s", e)
        return False, f"{kind} non inviato: {e}"


def send_async(to, body, prefer="whatsapp", on_done=None):
    def run():
        ok, info = send_message(to, body, prefer)
        if on_done:
            try:
                on_done(ok, info)
            except Exception as e:
                log.warning("callback notifica fallita: %s", e)
    if getattr(DISABLED, "on", False):
        return
    threading.Thread(target=run, daemon=True).start()


def notify_staff(body):
    staff = env("STAFF_NOTIFY_PHONE")
    if staff:
        send_async(staff, body)


# ---------------------------------------------------------------- firma webhook
def validation_enabled():
    return bool(env("TWILIO_AUTH_TOKEN")) and env("TWILIO_VALIDATE") not in {"0", "false", "no"}


def public_url(request):
    base = env("PUBLIC_BASE_URL").rstrip("/")
    if base:
        url = base + request.url.path
    else:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += "?" + request.url.query
    return url


def valid_signature(url, params, signature):
    token = env("TWILIO_AUTH_TOKEN")
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = base64.b64encode(hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest, signature or "")
