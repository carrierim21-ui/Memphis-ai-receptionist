# Memphis AI Receptionist — test deployment

FastAPI service for the Memphis Pub receptionist MVP.

## Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health: `/api/health`
- Test interface: `/test`
- Admin: `/admin`

## Environment variables
Set `OPENAI_API_KEY` in Render to enable the OpenAI-backed receptionist.
`OPENAI_MODEL` defaults to `gpt-5.6-luna`.

The current test build also includes the Memphis menu knowledge base and the conversation/order/reservation flows prepared in the project.
