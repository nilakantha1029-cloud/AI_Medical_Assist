import asyncio
import base64
import io
import logging
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from google import genai
from google.genai import types
from google.genai import errors
from gtts import gTTS
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sarah")

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
DB_NAME = os.path.join(BASE_DIR, "appointments_poc.db")
SLOT_FORMAT = "%Y-%m-%d %H:%M"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

GEMINI_CLIENT: genai.Client | None = None


# --- DATABASE SETUP ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS doctors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, specialty TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY, patient_name TEXT, doctor_name TEXT, appointment_time TIMESTAMP,
            patient_email TEXT, status TEXT, UNIQUE(doctor_name, appointment_time))""")
        for name, spec in [("Dr. Meera Patel", "Cardiology"), ("Dr. Arjun Rao", "Neurology")]:
            conn.execute("INSERT OR IGNORE INTO doctors (name, specialty) VALUES (?, ?)", (name, spec))


init_db()
app = FastAPI()


# --- TOOLS (docstrings are read by Gemini, keep them accurate) ---
def _parse_slot(value: str):
    try:
        slot = datetime.strptime(value.strip(), SLOT_FORMAT)
    except (ValueError, AttributeError):
        return None, {"status": "invalid_time", "message": "Use the format YYYY-MM-DD HH:MM (24-hour)."}
    if slot < datetime.now():
        return None, {"status": "invalid_time", "message": "That time is in the past."}
    return slot.strftime(SLOT_FORMAT), None


def _find_doctor(conn, name: str):
    row = conn.execute("SELECT name FROM doctors WHERE lower(name)=lower(?)", (name.strip(),)).fetchone()
    return row[0] if row else None


def list_doctors_tool() -> dict:
    """List all doctors and their specialties."""
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute("SELECT name, specialty FROM doctors").fetchall()
    return {"doctors": [{"name": r[0], "specialty": r[1]} for r in rows]}


def check_slot_tool(doctor_name: str, appointment_time: str) -> dict:
    """Check if a doctor is free. appointment_time must be 'YYYY-MM-DD HH:MM' (24-hour)."""
    slot, err = _parse_slot(appointment_time)
    if err:
        return err
    with sqlite3.connect(DB_NAME) as conn:
        doctor = _find_doctor(conn, doctor_name)
        if not doctor:
            return {"status": "unknown_doctor"}
        booked = conn.execute("SELECT 1 FROM appointments WHERE doctor_name=? AND appointment_time=?", (doctor, slot)).fetchone()
    return {"status": "booked" if booked else "available"}


def book_appointment_tool(doctor_name: str, appointment_time: str, patient_name: str, patient_email: str) -> dict:
    """Book an appointment. appointment_time must be 'YYYY-MM-DD HH:MM' (24-hour)."""
    slot, err = _parse_slot(appointment_time)
    if err:
        return err
    if not EMAIL_RE.match(patient_email or ""):
        return {"status": "invalid_email"}
    try:
        with sqlite3.connect(DB_NAME) as conn:
            doctor = _find_doctor(conn, doctor_name)
            if not doctor:
                return {"status": "unknown_doctor"}
            conn.execute(
                "INSERT INTO appointments (patient_name, doctor_name, appointment_time, patient_email, status) VALUES (?,?,?,?,?)",
                (patient_name, doctor, slot, patient_email, "CONFIRMED"))
        return {"status": "success", "doctor": doctor, "time": slot}
    except sqlite3.IntegrityError:
        return {"status": "conflict"}
    except Exception:
        log.exception("Booking failed")
        return {"status": "error"}


TOOL_FUNCTIONS = {
    "list_doctors_tool": list_doctors_tool,
    "check_slot_tool": check_slot_tool,
    "book_appointment_tool": book_appointment_tool,
}


# --- AUDIO ---
def clean_text_for_audio(text: str) -> str:
    text = re.sub(r"[*_#]", "", text)
    text = re.sub(r"\([^)]*\)", "", text)  # drop bracketed translations
    return " ".join(text.split())


async def generate_audio_gtts(text: str, lang_code: str) -> str | None:
    clean = clean_text_for_audio(text or "")
    if not clean:
        return None
    lang = {"hi": "hi", "te": "te"}.get(lang_code[:2], "en")
    tld = "co.in" if lang == "en" else "com"

    def _run():
        fp = io.BytesIO()
        gTTS(text=clean, lang=lang, tld=tld, slow=False).write_to_fp(fp)
        return fp.getvalue()

    try:
        return base64.b64encode(await asyncio.to_thread(_run)).decode()
    except Exception as e:  # no internet etc. -> frontend falls back to browser voice
        log.warning("TTS unavailable: %s", e)
        return None


# --- PROMPT ---
BASE_PROMPT = """
You are Sarah, a warm and caring receptionist at City Hospital.
You help patients hear about doctors, check availability and book appointments.

LANGUAGE: reply only in the language requested in each message's [Instruction]. No translations,
no bracketed text, no bold or bullet formatting. Keep replies short: this is a voice call.

BOOKING RULES:
1. Use the tools for doctors and availability. Never invent doctors or free slots.
2. Convert dates the patient says ("tomorrow 2 pm") to YYYY-MM-DD HH:MM (24-hour) using the
   date given in [Context], and pass that format to the tools.
3. Call check_slot_tool first, repeat the doctor and time to the patient, and book only after they agree.
4. The patient's name and email come in [Context]. Do not ask for them again.
5. You are not a doctor: do not diagnose or give medical advice. For emergencies, tell the patient to call local emergency services.
"""


class AgentMessageRequest(BaseModel):
    session_id: str | None = None
    text: str
    language_code: str | None = "en-IN"
    patient_name: str | None = None
    patient_email: str | None = None


AGENT_SESSIONS: dict = {}
LANG_RULES = {
    "te-IN": "STRICT: Respond in Telugu script only. No English.",
    "hi-IN": "STRICT: Respond in Hindi script only. No English.",
    "en-IN": "Respond in English.",
}


def get_chat_session(session_id=None):
    global GEMINI_CLIENT
    if not GEMINI_CLIENT:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to the .env file and restart the server.")
        GEMINI_CLIENT = genai.Client(api_key=key)
    if not session_id or session_id not in AGENT_SESSIONS:
        session_id = str(uuid.uuid4())
        AGENT_SESSIONS[session_id] = GEMINI_CLIENT.chats.create(
            model=MODEL_NAME,
            config=types.GenerateContentConfig(
                system_instruction=BASE_PROMPT,
                tools=list(TOOL_FUNCTIONS.values()),
                # we run the tool loop ourselves below
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    return session_id, AGENT_SESSIONS[session_id]


# --- ROUTES ---

async def send_with_retry(chat, payload, tries=4):
    """Retry when Gemini says 'high demand' (503). Waits 2s, 4s, 8s."""
    for attempt in range(tries):
        try:
            return await asyncio.to_thread(chat.send_message, payload)
        except errors.ServerError as e:
            if attempt == tries - 1:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("Gemini busy (%s), retrying in %ss", e.code, wait)
            await asyncio.sleep(wait)

@app.get("/")
def read_root():
    return FileResponse(os.path.join(BASE_DIR, "frontend.html"), media_type="text/html")


@app.get("/health")
def health():
    return {"status": "ok", "api_key_set": bool(os.environ.get("GEMINI_API_KEY"))}


@app.post("/agent/new_session")
def new_session():
    try:
        sid, _ = get_chat_session()
        return {"session_id": sid}
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/agent/message")
async def agent_message(req: AgentMessageRequest):
    try:
        sid, chat = get_chat_session(req.session_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    rule = LANG_RULES.get(req.language_code, LANG_RULES["en-IN"])
    context = (f"Today is {datetime.now():%A, %Y-%m-%d}, current time {datetime.now():%H:%M}. "
               f"Patient name: {req.patient_name or 'unknown'}. Patient email: {req.patient_email or 'unknown'}.")
    message = f"{req.text}\n\n[Context: {context}]\n[Instruction: {rule}]"

    try:
        response = await send_with_retry(chat, message)
        for _ in range(5):  # safety cap on tool rounds
            calls = getattr(response, "function_calls", None)
            if not calls:
                break
            parts = []
            for call in calls:
                fn = TOOL_FUNCTIONS.get(call.name)
                try:
                    result = fn(**dict(call.args or {})) if fn else {"status": "unknown_tool"}
                except Exception:
                    log.exception("Tool %s failed", call.name)
                    result = {"status": "error"}
                parts.append(types.Part.from_function_response(name=call.name, response={"result": result}))
            response = await send_with_retry(chat, parts)

        final_text = (response.text or "").strip()
        audio = await generate_audio_gtts(final_text, req.language_code or "en-IN")
        return {"session_id": sid, "text": final_text, "audio": audio}
    except Exception as e:
        log.exception("Gemini request failed")
        msg = "Sarah could not reach the AI service. Check the server log and try again."
        if "NOT_FOUND" in str(e):
            msg = f"The model '{MODEL_NAME}' is not available. Set GEMINI_MODEL in .env to a current model (for example gemini-3.8-flash) and restart."
        elif "UNAVAILABLE" in str(e):
            msg = "Google's AI service is busy right now. Please wait a few seconds and try again."
        elif "API key" in str(e) or "PERMISSION_DENIED" in str(e) or "UNAUTHENTICATED" in str(e):
            msg = "The Gemini API key was rejected. Check GEMINI_API_KEY in .env."
        return JSONResponse({"session_id": sid, "error": msg}, status_code=502)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
