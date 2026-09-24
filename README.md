# Memphis AI Receptionist

Receptionist virtuale per il Memphis Pub (Bernalda). Risponde in chat sul sito, su WhatsApp e al telefono.
Prende **prenotazioni**, **ordini da asporto e a domicilio** e risponde alle domande sul **menu**.
Il locale gestisce tutto da una dashboard protetta da password.

Il receptionist non conferma mai da solo. Raccoglie i dati, legge il riepilogo al cliente e chiede
un "sì" esplicito. La richiesta arriva poi al locale come **DA_VERIFICARE**. Quando il locale conferma
o rifiuta dalla dashboard, il cliente riceve un WhatsApp o un SMS (se Twilio è configurato).

## Pagine

| Indirizzo | A cosa serve | Accesso |
|---|---|---|
| `/` | Chat per i clienti (si può collegare dal sito del locale) | pubblica |
| `/admin` | Dashboard: prenotazioni, ordini, disponibilità, pagamenti | password |
| `/test` | Laboratorio di prova: simula web, WhatsApp e telefono, lancia i test automatici | password |
| `/api/health` | Stato del sistema (IA attiva? ultimo errore? Twilio?) | pubblica |
| `/voice`, `/whatsapp` | Webhook da inserire in Twilio | solo Twilio |

Utente dashboard: `admin`. La password è nella variabile `ADMIN_PASSWORD`.

## Come funziona

- **Con `OPENAI_API_KEY`** risponde l'IA (modello in `OPENAI_MODEL`). L'IA usa degli "strumenti" per salvare i dati.
  Il server li controlla: orari di apertura, date passate, prodotti che esistono davvero nel menu, posti liberi.
- **Senza chiave, o se OpenAI dà errore o è lento**, risponde il motore a regole. Capisce frasi come
  "2 margherite e una diavola senza cipolla", "sabato alle 9", "siamo in 4", "a nome di Mario Rossi".
  Il servizio quindi non resta mai muto.
- Gli errori dell'IA compaiono in `/api/health`, in cima alla dashboard e nel test lab.
- Il menu è in `data/menu.json`. Se lo modifichi, il receptionist si aggiorna al riavvio.
  I prezzi "n/d" e le pizze speciali senza ingredienti vengono presentati come "da confermare".

## Messa online su Render (circa 15 minuti)

1. Carica questa cartella su un repository GitHub (privato va benissimo).
2. Su Render: **New → Blueprint** e scegli il repository. Render legge `render.yaml` e crea il servizio.
   - Il piano è **Starter** con un disco da 1 GB, così i dati non si perdono e il servizio non va in pausa.
     Costa qualche dollaro al mese; controlla il prezzo aggiornato su render.com.
   - Il piano Free va bene solo per una demo. Si addormenta dopo un po' di inattività, quindi la prima
     chiamata Twilio può fallire, e **a ogni riavvio cancella il database**.
3. Nella sezione **Environment** del servizio:
   - inserisci `OPENAI_API_KEY`;
   - controlla che `OPENAI_MODEL` sia un modello disponibile sul tuo account OpenAI;
   - copia il valore di `ADMIN_PASSWORD`, generato da Render: è la password della dashboard.
4. Apri `https://<tuo-servizio>.onrender.com/test` e premi **Esegui tutti i test**. Tutto deve essere verde,
   compresa la riga "Connessione OpenAI". Se quella è rossa, il messaggio dice il motivo
   (chiave sbagliata, modello inesistente, credito finito...).

## Collegare telefono e WhatsApp (Twilio)

1. Crea un account Twilio e copia **Account SID** e **Auth Token** nelle variabili
   `TWILIO_ACCOUNT_SID` e `TWILIO_AUTH_TOKEN`.
2. **Telefono.** Compra un numero con funzione Voice. Per i numeri italiani Twilio chiede dei documenti
   (indirizzo e identità). Nella configurazione del numero, alla voce *A call comes in*, imposta
   Webhook `https://<tuo-servizio>.onrender.com/voice`, metodo **POST**.
   Poi il locale imposta sul proprio telefono l'inoltro verso quel numero: se non risponde, se è occupato, o sempre.
3. **WhatsApp.** Per le prove usa il *WhatsApp Sandbox* di Twilio. In *When a message comes in* imposta
   `https://<tuo-servizio>.onrender.com/whatsapp`, metodo **POST**. Poi metti in
   `TWILIO_WHATSAPP_FROM` il numero del sandbox, ad esempio `whatsapp:+14155238886`.
   Per l'uso vero serve un numero WhatsApp Business approvato tramite Twilio.
4. Variabili opzionali:
   - `TWILIO_SMS_FROM`: numero Twilio per mandare SMS a chi ha chiamato al telefono.
   - `STAFF_PHONE`: se il cliente al telefono chiede di parlare con una persona, la chiamata viene passata qui.
     Se non lo imposti, il receptionist registra una richiesta "da richiamare".
   - `STAFF_NOTIFY_PHONE`: riceve un WhatsApp o SMS per ogni nuova prenotazione o ordine.
   - `GROUP_MAX_AUTO`: numero massimo di persone per una prenotazione automatica (predefinito 10).
     Sopra questa soglia la richiesta passa al personale, sia con l'IA sia con il motore a regole.
   - `TWILIO_VOICE`: voce della telefonata, ad esempio `Polly.Bianca-Neural`. Se la lasci vuota,
     usa la voce italiana predefinita di Twilio.
5. Sicurezza: con `TWILIO_AUTH_TOKEN` impostato, il server accetta solo richieste firmate da Twilio.
   Se le chiamate falliscono con errore 403, imposta `PUBLIC_BASE_URL=https://<tuo-servizio>.onrender.com`
   oppure, come ultima risorsa, `TWILIO_VALIDATE=0`.

## Uso quotidiano per il locale

- In **Disponibilità tavoli** si inseriscono i posti per giorno, ora e sala. Se un giorno ha degli slot,
  il receptionist propone solo gli orari con posti liberi. Se un giorno non ne ha, le richieste arrivano da verificare a mano.
- **Conferma (usa slot)** occupa i posti nello slot. Se poi la prenotazione viene rifiutata, i posti tornano liberi.
  **Conferma senza slot** conferma senza toccare la disponibilità.
- Per gli ordini ci sono i pulsanti Conferma, In preparazione, Pronto, Ritirato/Consegnato, Segna pagato e Modifica.
  A ogni passaggio importante il cliente riceve un messaggio.
- La dashboard si aggiorna da sola ogni 8 secondi.

## Prova in locale

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # imposta almeno ADMIN_PASSWORD
uvicorn app:app --reload
```
Poi apri http://localhost:8000/test (utente `admin`).

## Note legali

- Il receptionist si presenta come "assistente virtuale", perché il cliente deve sapere che parla con un'IA.
- Il sistema salva nomi, telefoni, indirizzi e conversazioni. Serve un'informativa privacy (GDPR) del locale
  e un accordo sul trattamento dei dati tra te e il locale. OpenAI e Twilio sono fornitori che trattano quei dati.

## File

- `app.py`: server, pagine, webhook Twilio, API della dashboard, test automatici
- `conversation.py`: punto d'ingresso dei messaggi (IA → motore a regole)
- `ai_agent.py`: OpenAI con function calling
- `rules.py`: motore a regole
- `booking.py`: dati della richiesta, controlli, riepiloghi, invio, messaggi al cliente
- `knowledge.py`: menu, date, orari, nomi, indirizzi
- `storage.py`: database SQLite
- `notify.py`: invio WhatsApp/SMS e verifica firma Twilio
