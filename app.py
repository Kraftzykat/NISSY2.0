import json
import os
import re
import time
import logging
import csv
import uuid
from datetime import datetime, timezone
import httpx
from flask import Flask, request, jsonify, send_from_directory, Response

app = Flask(__name__, static_folder='static', static_url_path='/static')
logging.basicConfig(level=logging.INFO)

# ==============================================================================
# 📊 RATINGS LOGGING
# ==============================================================================
RATINGS_FILE = "ratings.csv"

def init_ratings_log():
    if not os.path.exists(RATINGS_FILE):
        with open(RATINGS_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "session_id", "rating", "comment", "response_snippet"])

init_ratings_log()

def log_rating(session_id: str, rating: int, comment: str, response: str):
    with open(RATINGS_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            session_id, rating, comment, response[:150]
        ])

# ==============================================================================
# 🔒 PII REDACTION
# ==============================================================================
def redact_pii(text: str) -> str:
    patterns = {
        "nin": r"\b\d{9}\b",
        "email": r"\b[\w\.-]+@[\w\.-]+\.\w+\b",
        "phone": r"\b\d{3}[-.\s]?\d{4}\b",
        "address": r"\b\d{1,4}\s+[A-Za-z]+\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd)\b",
    }
    for label, pattern in patterns.items():
        text = re.sub(pattern, f"[REDACTED:{label}]", text)
    return text

# ==============================================================================
# 🚫 AXIS 1: AUTHORITY
# ==============================================================================
AUTHORITY_TRIGGERS = [
    "my case", "my application", "am i eligible", "my balance",
    "my account", "for me", "my status", "personal", "my claim",
    "my contributions", "my pension", "my benefit", "my record"
]

def check_authority(msg: str) -> bool:
    return not any(t in msg.lower() for t in AUTHORITY_TRIGGERS)

# ==============================================================================
# 🧭 AXIS 2: REGISTER
# ==============================================================================
DISTRESS_WORDS = ["passed away", "died", "funeral", "loss", "grief", "mourning", "bereavement"]
URGENT_WORDS = ["asap", "urgent", "emergency", "now", "immediately", "hurry", "quick"]
FORMAL_WORDS = ["regarding", "kindly", "please advise", "hereby", "therewith", "herewith"]

def detect_register(msg: str) -> str:
    m = msg.lower()
    if any(w in m for w in DISTRESS_WORDS):
        return "bereaved"
    if any(w in m for w in URGENT_WORDS):
        return "urgent"
    if any(w in m for w in FORMAL_WORDS):
        return "professional"
    return "warm"

# ==============================================================================
# 🗺️ AXIS 3: TERRITORY
# ==============================================================================
TERRITORY_KEYWORDS = {
    "grenada": {"country": "Grenada", "office": "Melville St, St George's", "phone": "(473) 440-6647"},
    "carriacou": {"country": "Carriacou", "office": "Hillsborough", "phone": "(473) 443-6026"},
    "petite martinique": {"country": "Petite Martinique", "office": "Hillsborough", "phone": "(473) 443-6026"},
}

def detect_territory(msg: str) -> dict:
    m = msg.lower()
    for key, data in TERRITORY_KEYWORDS.items():
        if key in m:
            return data
    return None

# ==============================================================================
# 🚨 DISTRESS DETECTION
# ==============================================================================
DISTRESS_TRIGGERS = {
    "grief": ["passed away", "died", "funeral", "lost my", "she's gone", "he's gone", "my father", "my mother", "my child"],
    "panic": ["can't breathe", "can't cope", "help now", "emergency", "overwhelmed", "it's too much"],
    "self_harm": ["hurt myself", "end it", "no way out", "kill myself", "suicide", "self harm"],
    "aggrieved": ["nobody listens", "you people never", "sick of this", "useless", "no help"],
}

def detect_distress(msg: str) -> str:
    m = msg.lower()
    for category, words in DISTRESS_TRIGGERS.items():
        if any(w in m for w in words):
            return category
    return None

def distress_reply(category: str) -> str:
    replies = {
        "grief": "I'm so sorry for your loss. You don't have to go through this alone. Please call our bereavement support team at (473) 440-6647 or visit Melville St, St George's. We're here for you.",
        "panic": "Take a deep breath. Help is available. Please call the NIS Customer Service at (473) 440-6647, or visit us at Melville St. We'll take care of you.",
        "self_harm": "You matter. Please reach out right away. Grenada Crisis Centre: 456-1353. Grenada Mental Health Association: 444-1133. You're not alone.",
        "aggrieved": "I hear your frustration, and I'm sorry you're feeling this way. Let me connect you with a human agent who can listen and help. Please call (473) 440-6647.",
    }
    return replies.get(category, "I want to help you. Please call (473) 440-6647 to speak with someone who can assist you directly.")

# ==============================================================================
# 💬 CONVERSATION MEMORY
# ==============================================================================
MAX_HISTORY_TURNS = 6
conversation_histories = {}

def add_to_history(session_id: str, role: str, text: str):
    if session_id not in conversation_histories:
        conversation_histories[session_id] = []
    conversation_histories[session_id].append({"role": role, "text": text})
    max_messages = MAX_HISTORY_TURNS * 2
    if len(conversation_histories[session_id]) > max_messages:
        conversation_histories[session_id] = conversation_histories[session_id][-max_messages:]

def get_history_text(session_id: str) -> str:
    history = conversation_histories.get(session_id, [])
    if not history:
        return ""
    lines = []
    for entry in history[-6:]:
        speaker = "User" if entry["role"] == "user" else "Nissy"
        lines.append(f"{speaker}: {entry['text']}")
    return "\n".join(lines) + "\n\n"

# ==============================================================================
# 🧭 AGENTIC JOURNEY
# ==============================================================================
JOURNEY_STEPS = ["greeting", "identify_need", "collect_facts", "offer_next_step", "confirm_close"]
session_states = {}

def get_journey_step(session_id: str) -> str:
    step_idx = session_states.get(session_id, 0)
    return JOURNEY_STEPS[min(step_idx, len(JOURNEY_STEPS) - 1)]

def advance_journey(session_id: str):
    current = session_states.get(session_id, 0)
    if current < len(JOURNEY_STEPS) - 1:
        session_states[session_id] = current + 1

# ==============================================================================
# 🛡️ safe_call()
# ==============================================================================
def safe_call(fn, *args, fallback=None, on_error=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        app.logger.warning(f"safe_call caught: {e}")
        if on_error:
            on_error(e)
        return fallback or "I'm having trouble right now. Please call (473) 440-6647 or visit Melville St, St George's for help."

# ==============================================================================
# 🌐 MULTI-LANGUAGE
# ==============================================================================
def detect_language(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["hola", "gracias", "por favor", "cómo", "qué", "beneficio"]):
        return "es"
    if any(w in t for w in ["bonjour", "merci", "comment", "quoi", "prestation"]):
        return "fr"
    if any(w in t for w in ["sa", "mwen", "ou", "li", "nou", "yo", "ki"]):
        return "kw"
    return "en"

LANGUAGE_HINTS = {
    "en": "Reply in clear English.",
    "es": "Reply in Spanish. Use formal 'usted' form.",
    "fr": "Reply in French. Use polite 'vous' form.",
    "kw": "Reply in Kwéyòl (Caribbean French Creole).",
}

# ==============================================================================
# 🤖 NVIDIA AI
# ==============================================================================
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_API = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
NVIDIA_MODELS = [
    NVIDIA_MODEL,
    "meta/llama-3.2-3b-instruct",
    "nvidia/nemotron-mini-4b-instruct",
]

ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.environ.get("ELEVEN_VOICE_ID", "hpp4J3VqNfWAUOO0d1Us")

# ==============================================================================
# 📝 TCRDEI PROMPT
# ==============================================================================
BASE_SYSTEM_PROMPT = """You are Nissy, a warm, caring, human-sounding assistant for the NIS Grenada National Insurance Scheme.

[T] TASK: Help users understand NIS Grenada benefits and guide them toward taking action.

[C] CONTEXT: NIS Grenada provides 19 benefits. Office at Melville St, St George's. Phone (473) 440-6647. Email nisgrenada@nisgrenada.org. Hours Mon-Fri 7:30-4:30. {territory_context}

[R] RULES (NEVER BREAK):
- NEVER quote prices or specific case details.
- NEVER ask for or store personal info (NIN, phone, email).
- If someone asks a personal case question, say: "I can't answer personal case questions. Please call (473) 440-6647."
- If someone is in distress, offer support and helplines.

[D] DEFINITION OF SUCCESS: The user feels heard, understood, and knows their next step.

[E] EVALUATE: Before replying, check: Does this answer help the user? Is it accurate? Is it safe?

[I] ITERATE: If you're unsure about something, ask ONE clarifying question.

{register_hint}
{journey_hint}
{language_hint}

FACTS (current 2026):
- Contribution rate: 13.5% (Employee 6.25% + Employer 7.25%)
- Self-employed: 13.5% | Voluntary: 6.75%
- Max insurable earnings: $5,200/month or $1,200/week
- Pensionable age: 63 (rising to 65 by 2028)
- Age Pension: 575+ contribution weeks | 27% of best 5 years' average earnings, up to 60% max | Minimum $58/month
- Survivors Benefit: Monthly pension if deceased had 150+ contributions OR was receiving a pension
- One-time grant if 50+ contributions (5x average earnings per 50 weeks)
- Minimum survivor payments: widow(er)/parent 100% of age pension min ($58), child/orphan 50% ($29)
- Funeral Grant: One-time payment to whoever pays funeral costs
- Sickness Benefit: 65% of earnings, up to 26 weeks (up to 52 for long-time contributors)
- Unemployment Benefit: 50% of earnings, up to 13 weeks
- Maternity, Invalidity, Work Injury benefits also available
- Claim deadlines: Sickness within 3 months | Funeral within 6 months of death

CONVERSATION HISTORY:
{history}

User's message: {user_message}

FORMAT: Answer in at most 4 short bullet lines, each starting with '- ' and under ~15 words. Then ONE short closing line inviting the next step.
"""

# ==============================================================================
# 🧠 CALL NVIDIA
# ==============================================================================
def call_nvidia(prompt: str, user_msg: str, model: str = None) -> str:
    model_to_use = model or NVIDIA_MODELS[0]
    with httpx.Client(timeout=15.0) as cx:
        r = cx.post(
            NVIDIA_API,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {NVIDIA_KEY}"},
            json={
                "model": model_to_use,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user_msg}],
                "temperature": 0.3,
                "top_p": 0.8,
                "max_tokens": 250,
            },
        )
    if r.status_code == 200:
        body = r.json()
        reply = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return reply
    raise Exception(f"AI HTTP {r.status_code}: {r.text[:200]}")

# ==============================================================================
# 🌐 API ENDPOINTS
# ==============================================================================

@app.route("/")
def index():
    """Serve the main chat page."""
    try:
        return send_from_directory("static", "index.html")
    except Exception:
        return "Error: index.html not found in static folder.", 404

@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static files."""
    return send_from_directory("static", filename)

@app.route("/api/demo/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or not data.get("message", "").strip():
        return jsonify({"ok": False, "error": "Message required"}), 400

    raw_message = data["message"].strip()
    session_id = data.get("session_id", str(uuid.uuid4()))

    safe_message = redact_pii(raw_message)

    # Check distress
    distress = detect_distress(safe_message)
    if distress:
        add_to_history(session_id, "user", safe_message)
        reply = distress_reply(distress)
        add_to_history(session_id, "bot", reply)
        return jsonify({"ok": True, "reply": reply, "session_id": session_id})

    # Check authority
    if not check_authority(safe_message):
        add_to_history(session_id, "user", safe_message)
        reply = "I appreciate you reaching out! 🙏 For personal case questions, our team needs to handle this directly. Please call (473) 440-6647 or visit Melville St, St George's."
        add_to_history(session_id, "bot", reply)
        return jsonify({"ok": True, "reply": reply, "session_id": session_id})

    advance_journey(session_id)
    register = detect_register(safe_message)
    territory = detect_territory(safe_message)
    language = detect_language(safe_message)

    territory_context = f"Territory detected: {territory['country']}. Office: {territory['office']}." if territory else "Office: Melville St, St George's."

    register_hints = {
        "warm": "TONE: Warm, friendly, encouraging. Use 💛 emoji occasionally.",
        "professional": "TONE: Professional, polished, respectful. No emojis.",
        "urgent": "TONE: Urgent, direct, action-oriented.",
        "bereaved": "TONE: Gentle condolences FIRST. Then explain. Max 2 sentences of facts.",
    }

    current_step = get_journey_step(session_id)
    journey_hints = {
        "greeting": "This is the start. Welcome the user warmly.",
        "identify_need": "Help clarify which benefit they're asking about.",
        "collect_facts": "Provide clear, accurate facts about the benefit.",
        "offer_next_step": "Proactively suggest a concrete next step.",
        "confirm_close": "Wrap up warmly. Confirm they have what they need.",
    }

    history_text = get_history_text(session_id)

    full_prompt = BASE_SYSTEM_PROMPT.format(
        territory_context=territory_context,
        register_hint=register_hints.get(register, register_hints["warm"]),
        journey_hint=journey_hints.get(current_step, journey_hints["greeting"]),
        language_hint=LANGUAGE_HINTS.get(language, LANGUAGE_HINTS["en"]),
        history=history_text,
        user_message=safe_message,
    )

    def call_with_fallback():
        last_err = None
        for model in NVIDIA_MODELS:
            try:
                reply = call_nvidia(full_prompt, safe_message, model)
                if reply:
                    return reply
            except Exception as e:
                last_err = e
                continue
        raise Exception(last_err or "All models failed")

    reply = safe_call(
        call_with_fallback,
        fallback="I'm having trouble connecting right now. Please call (473) 440-6647 or visit Melville St, St George's for help.",
    )

    add_to_history(session_id, "user", safe_message)
    add_to_history(session_id, "bot", reply)

    return jsonify({
        "ok": True,
        "reply": reply,
        "session_id": session_id,
        "register": register,
    })

@app.route("/api/demo/tts", methods=["POST", "OPTIONS"])
def tts():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or not data.get("text", "").strip():
        return jsonify({"ok": False, "error": "Text required"}), 400

    text = data["text"][:1000].strip()
    text = redact_pii(text)

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}?output_format=mp3_44100_128"
        with httpx.Client(timeout=20.0) as cx:
            r = cx.post(
                url,
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": 0.7,
                        "similarity_boost": 0.85,
                        "style": 0.2,
                        "use_speaker_boost": True
                    }
                },
                headers={
                    "xi-api-key": ELEVEN_KEY,
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json"
                },
            )
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"TTS failed: {r.status_code}"}), 500
        return Response(r.content, mimetype="audio/mpeg", headers={"Cache-Control": "no-cache"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/rate", methods=["POST", "OPTIONS"])
def rate():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400

    try:
        log_rating(
            data.get("session_id", "unknown"),
            data.get("rating", 0),
            data.get("comment", ""),
            data.get("response_text", "")
        )
        return jsonify({"ok": True, "message": "Rating recorded!"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/keep-warm")
def keep_warm():
    return jsonify({"ok": True, "ts": time.time()})

@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
