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

# --- Google Gemini SDK ---
# 📌 CRITICAL FIX: `google-generativeai` (the old `genai.configure()` /
# `genai.GenerativeModel()` API) is DEPRECATED. We now use the current,
# unified `google-genai` SDK (pip package: google-genai, import path:
# `from google import genai`). See requirements.txt for the matching
# dependency change.
from google import genai
from google.genai import types as genai_types

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
    # 📌 FIX: this used to hardcode history[-6:] (last 6 MESSAGES = only
    # 3 exchanges), even though MAX_HISTORY_TURNS = 6 and add_to_history
    # already stores up to MAX_HISTORY_TURNS * 2 = 12 messages. That mismatch
    # meant the bot was actually forgetting half of what it was supposed to
    # remember. Now we use everything that's actually stored.
    lines = []
    for entry in history:
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
# 📎 OFFICIAL FORM LINKS (nisgrenada.org)
# ==============================================================================
# 📌 IMPORTANT: these URLs are hardcoded from the real NIS Grenada downloads
# page (https://nisgrenada.org/downloads-2/), NOT generated by the AI. Never
# ask Gemini/NVIDIA to produce a form URL from scratch — models frequently
# invent plausible-looking but wrong links, and a broken link on a real
# government benefits form is a trust and accuracy problem. Instead we detect
# which benefit the user is asking about with plain keyword matching (same
# technique as detect_register/detect_territory above) and attach the exact,
# verified link ourselves.
FORM_LINKS = {
    "age": {
        "label": "Age Benefit Form",
        "url": "https://nisgrenada.org/download/age-benefit-form/",
        "keywords": ["age pension", "age benefit", "retirement", "pensionable age"],
    },
    "survivors": {
        "label": "Survivors Benefit Form",
        "url": "https://nisgrenada.org/download/survivors-benefit-form/",
        "keywords": ["survivors benefit", "survivor benefit", "widow", "widower"],
    },
    "funeral": {
        "label": "Funeral Grant Benefit Form",
        "url": "https://nisgrenada.org/download/funeral-grant-benefit-form/",
        "keywords": ["funeral grant", "funeral benefit", "funeral"],
    },
    "sickness": {
        "label": "Sickness Form",
        "url": "https://nisgrenada.org/download/sickness-form/",
        "keywords": ["sickness benefit", "sick leave", "sick pay"],
    },
    "unemployment": {
        "label": "Employment Certificate Form",
        "url": "https://nisgrenada.org/download/employment-certificate-form/",
        "keywords": ["unemployment benefit", "lost my job", "laid off"],
    },
    "maternity": {
        "label": "Maternity Benefit Form",
        "url": "https://nisgrenada.org/download/maternity-benefit/",
        "keywords": ["maternity benefit", "maternity allowance", "maternity grant", "pregnant", "pregnancy"],
    },
    "invalidity": {
        "label": "Invalidity Benefit Form",
        "url": "https://nisgrenada.org/download/invalidity-benefit-form/",
        "keywords": ["invalidity benefit", "permanently unable to work"],
    },
    "employment_injury": {
        "label": "Employment Injury Form",
        "url": "https://nisgrenada.org/download/employment-injury-form/",
        "keywords": ["employment injury", "workplace injury", "injury benefit"],
    },
    "disablement": {
        "label": "Disablement Benefit Form",
        "url": "https://nisgrenada.org/download/disablement-benefit-form/",
        "keywords": ["disablement benefit", "permanent disability"],
    },
    "death": {
        "label": "Death Benefit Form",
        "url": "https://nisgrenada.org/download/death-benefit-form/",
        "keywords": ["death benefit"],
    },
    "employer_registration": {
        "label": "Employer Registration Form",
        "url": "https://nisgrenada.org/download/employer-registration-form/",
        "keywords": ["register as an employer", "employer registration", "new business registration"],
    },
    "self_employed_registration": {
        "label": "Self Employed Registration Form",
        "url": "https://nisgrenada.org/download/self-employed-registration-form/",
        "keywords": ["self employed registration", "register as self employed"],
    },
    "voluntary_registration": {
        "label": "Voluntary Contribution Registration Form",
        "url": "https://nisgrenada.org/download/voluntary-contribution-registration-form/",
        "keywords": ["voluntary contributor", "voluntary contribution"],
    },
    "refund": {
        "label": "Refund Application Form",
        "url": "https://nisgrenada.org/download/refund-application-form/",
        "keywords": ["refund", "overpaid contributions"],
    },
    "pension_life_certificate": {
        "label": "Pension Life Certificate",
        "url": "https://nisgrenada.org/download/pension-life-certificate/",
        "keywords": ["life certificate", "proof of life"],
    },
    "caricom": {
        "label": "Caricom Reciprocal Agreement Claim Form",
        "url": "https://nisgrenada.org/download/caricom-reciprocal-agreement-claim-form/",
        "keywords": ["reciprocal agreement", "caricom", "worked in another caribbean country", "worked abroad"],
    },
}

DOWNLOADS_PAGE = "https://nisgrenada.org/downloads-2/"

def find_relevant_form(msg: str):
    """
    Looks at the user's message and returns the matching entry from
    FORM_LINKS if one of its keywords appears, otherwise None. Only ever
    returns a link we've hardcoded above — never something the AI made up.
    """
    m = msg.lower()
    for entry in FORM_LINKS.values():
        if any(kw in m for kw in entry["keywords"]):
            return entry
    return None

# ==============================================================================
# 🤖 AI CONFIGURATION - Gemini (Primary) + NVIDIA Llama/Nemotron (Fallback)
# ==============================================================================
# --- GEMINI CONFIGURATION (Primary) ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 📌 CRITICAL FIX: the old default here was "gemini-2.0-flash-lite", which
# Google has SHUT DOWN. Every Gemini call was failing silently and quietly
# falling back to NVIDIA — the app still "worked", so this was easy to miss.
# New default is a current, free-tier-eligible model. You can still override
# it without touching code by setting GEMINI_MODEL in Render's environment
# variables (e.g. to try "gemini-2.5-flash" or "gemini-3.5-flash").
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

gemini_client = None
gemini_available = False
if GEMINI_API_KEY:
    try:
        # 📌 New SDK call shape: genai.Client(api_key=...) instead of the
        # old genai.configure(api_key=...).
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        gemini_available = True
        app.logger.info(f"✅ Gemini configured. Model: {GEMINI_MODEL}")
    except Exception as e:
        app.logger.warning(f"⚠️ Gemini config failed: {e}")
else:
    app.logger.warning("⚠️ GEMINI_API_KEY not set")

# --- NVIDIA CONFIGURATION (Fallback - Llama 3.1 + Nemotron) ---
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_API = "https://integrate.api.nvidia.com/v1/chat/completions"

# Your models - in order of preference
NVIDIA_MODELS = [
    "meta/llama-3.1-8b-instruct",       # Llama 3.1 8B - Primary fallback
    "nvidia/nemotron-mini-4b-instruct", # Nemotron Mini 4B - Secondary fallback
]

# Optional: Override with environment variable
NVIDIA_MODEL_OVERRIDE = os.environ.get("NVIDIA_MODEL", "")
if NVIDIA_MODEL_OVERRIDE:
    NVIDIA_MODELS.insert(0, NVIDIA_MODEL_OVERRIDE)

# --- ELEVENLABS TTS ---
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.environ.get("ELEVEN_VOICE_ID", "hpp4J3VqNfWAUOO0d1Us")

if ELEVEN_KEY:
    app.logger.info("✅ ElevenLabs configured")
else:
    app.logger.warning("⚠️ ELEVENLABS_API_KEY not set")

# ==============================================================================
# 📝 TCRDEI PROMPT - Formal yet Friendly with Detailed Responses
# ==============================================================================
BASE_SYSTEM_PROMPT = """You are Nissy, a warm, professional, and knowledgeable assistant for the NIS Grenada National Insurance Scheme. You speak with dignity, respect, and genuine care for every person who reaches out.

[T] TASK: Help users understand NIS Grenada benefits and guide them toward taking action. Provide complete, accurate information while maintaining a professional yet approachable tone.

[C] CONTEXT: NIS Grenada provides 19 benefits to protect workers and their families. Office at Melville St, St George's. Phone (473) 440-6647. Email nisgrenada@nisgrenada.org. Hours Mon-Fri 7:30-4:30. {territory_context}

[R] RULES (NEVER BREAK):
- NEVER quote specific case details, personal information, or individual eligibility.
- NEVER ask for or store personal data (NIN, phone, email, address).
- If someone asks a personal case question, say: "I cannot answer personal case questions. Please call (473) 440-6647 to speak with our team."
- If someone is in distress, offer immediate support with care and compassion.
- Always be truthful. If you don't know something, say so and direct them to the office.

[D] DEFINITION OF SUCCESS: The user feels respected, fully informed, and confident about their next steps. They understand the benefit, the requirements, and what action to take.

[E] EVALUATE: Before replying, check: Is this information accurate and complete? Does it show respect and care? Does it guide the user toward a clear next step?

[I] ITERATE: If you're unsure about something, ask ONE clarifying question. Never guess or make up information.

{register_hint}
{journey_hint}
{language_hint}

NIS GRENADA - COMPLETE BENEFITS INFORMATION (2026):

CONTRIBUTIONS:
- Contribution rate: 13.5% of insurable earnings (Employee pays 6.25%, Employer pays 7.25%)
- Self-employed individuals: 13.5% of gross earnings
- Voluntary contributors: 6.75%
- Maximum insurable earnings: $5,200 per month or $1,200 per week
- Contribution rates are gradually increasing to 16% by 2031

AGE PENSION (RETIREMENT):
- Pensionable age is currently 63 (will increase to 65 by 2028)
- Requires 575+ contribution weeks (approximately 11.5 years of contributions)
- Benefit: 27% of your best 5 years' average earnings, up to 60% maximum
- Minimum pension payment: $58 per month
- You can continue working while receiving your pension

SURVIVORS BENEFIT (FOR FAMILIES AFTER A DEATH):
- Monthly pension if the deceased had 150+ contributions or was already receiving a pension
- One-time grant if the deceased had 50+ contributions (calculated as 5x average earnings per 50 weeks)
- Minimum monthly payments: Widow(er)/parent receives 100% of age pension minimum ($58)
- Child/orphan receives 50% of age pension minimum ($29)
- Claim must be submitted within 6 months of the death

FUNERAL GRANT:
- One-time payment to help cover funeral costs
- Available for the insured person, their spouse (including common-law), or child under 16 (including step/adopted)
- Must be claimed within 6 months of the death
- Payment goes to whoever paid the funeral expenses

SICKNESS BENEFIT:
- Pays 65% of your average insurable earnings
- Duration: Up to 26 weeks (extended to 52 weeks for long-time contributors)
- Requirements: Must be registered at least 3 months and have 2 months' contributions before sick leave
- Must claim within 3 months of your sick leave starting

UNEMPLOYMENT BENEFIT:
- Pays 50% of your average weekly insurable earnings
- Duration: Up to 13 weeks
- Requirements: Must be registered and contributing for at least 52 weeks
- Must have contributions in the weeks before losing your job

MATERNITY BENEFIT:
- Available for employed and self-employed women
- Includes Maternity Allowance and Maternity Grant
- Must have been contributing for at least 5 months before the expected delivery date

INVALIDITY BENEFIT:
- For individuals who become permanently unable to work due to illness or injury
- Requires 150+ contribution weeks
- Provides a monthly pension

EMPLOYMENT INJURY BENEFITS:
- Injury Benefit: For temporary disability from workplace injury
- Disablement Benefit: For permanent disability from workplace injury
- Medical Expenses: Coverage for treatment of workplace injuries
- Death Benefit: For families of workers who die from workplace injury

HOW TO CLAIM:
- Submit the relevant claim form to the NIS office
- Each benefit has specific deadlines (Sickness: 3 months, Funeral: 6 months)
- Visit Melville St office or call (473) 440-6647 for claim forms
- You can also check your contribution record online at my.nisgrenada.org

CONVERSATION HISTORY:
{history}

User's message: {user_message}

RESPONSE GUIDELINES:
1. Always open with a warm, respectful greeting that acknowledges the user's question.
2. Provide COMPLETE information - explain the benefit clearly, including requirements, payment amounts, and deadlines.
3. Use bullet points (starting with '- ') for clarity, but make each bullet a FULL sentence with substance - at least 15-20 words per bullet.
4. Include relevant details that would actually help someone take action (forms needed, deadlines, contact information).
5. Always end with a clear, actionable next step.
6. Use a professional yet caring tone - think of how a trusted bank manager or government representative would speak.

EXAMPLE OF GOOD RESPONSE:
"Thank you for asking about the Survivors Benefit. I understand this is a difficult time, and I want to make sure you have all the information you need.

- The Survivors Benefit provides a monthly pension to the family of a deceased NIS contributor. To qualify, the deceased must have had at least 150 contribution weeks or must have already been receiving their age pension.
- If the deceased had 50 to 149 contribution weeks, the family may receive a one-time grant instead of a monthly pension. The grant is calculated as 5 times the average earnings for each 50 weeks of contributions.
- The minimum monthly pension amounts are $58 for a widow(er) or parent, and $29 for each child or orphan. These amounts are adjusted periodically to help with the cost of living.
- To claim this benefit, please visit the NIS office at Melville St, St George's with the death certificate, the deceased's NIS number, and proof of relationship. You have 6 months from the date of death to submit your claim.

Would you like me to explain the Funeral Grant as well, or would you prefer to speak with someone at the office about your specific situation?"""

# ==============================================================================
# 🧠 AI CALL FUNCTIONS
# ==============================================================================
def call_gemini(prompt: str, user_msg: str, history_text: str = "") -> str:
    """Call Google Gemini API using the current google-genai SDK."""
    if not gemini_available:
        raise Exception("Gemini is not configured")

    full_prompt = f"{prompt}\n\n{history_text}User: {user_msg}"

    # 📌 New SDK call shape. Compare to the old, deprecated way:
    #   OLD: model = genai.GenerativeModel(GEMINI_MODEL)
    #        model.generate_content(full_prompt, generation_config=genai.types.GenerationConfig(...))
    #   NEW: gemini_client.models.generate_content(model=..., contents=..., config=...)
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=250,
            top_p=0.8,
        ),
    )

    if not response.text:
        raise Exception("Gemini returned empty response")
    return response.text.strip()

def call_nvidia(prompt: str, user_msg: str, model: str) -> str:
    """Call NVIDIA NIM API with specific model."""
    if not NVIDIA_KEY:
        raise Exception("NVIDIA_API_KEY not set")

    with httpx.Client(timeout=15.0) as cx:
        r = cx.post(
            NVIDIA_API,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {NVIDIA_KEY}"
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg}
                ],
                "temperature": 0.3,
                "top_p": 0.8,
                "max_tokens": 250,
            },
        )
        if r.status_code == 200:
            body = r.json()
            reply = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            return reply
        raise Exception(f"NVIDIA {model} HTTP {r.status_code}: {r.text[:200]}")

def call_llm_with_fallback(prompt: str, user_msg: str, history_text: str = "") -> dict:
    """
    Try Gemini first, then fallback to Llama 3.1, then Nemotron.

    📌 FIX: this used to just return a string, which meant the /api/demo/chat
    endpoint had no reliable way to know which AI ACTUALLY answered — it just
    reported "gemini" any time the key was configured, even on turns where
    Gemini silently failed and NVIDIA answered instead. Now we return which
    engine really produced the reply, so the "ai_used" field in the response
    (and your Render logs) tell the truth.
    """
    # 1. Try Gemini (Primary)
    if gemini_available:
        try:
            result = call_gemini(prompt, user_msg, history_text)
            if result:
                app.logger.info("✅ Gemini response successful")
                return {"text": result, "engine": "gemini"}
        except Exception as e:
            app.logger.warning(f"⚠️ Gemini failed: {e}. Falling back...")

    # 2. Try NVIDIA models (Llama 3.1 → Nemotron)
    if NVIDIA_KEY:
        for model in NVIDIA_MODELS:
            try:
                app.logger.info(f"🔄 Trying NVIDIA: {model}")
                result = call_nvidia(prompt, user_msg, model)
                if result:
                    app.logger.info(f"✅ NVIDIA {model} successful")
                    return {"text": result, "engine": f"nvidia:{model}"}
            except Exception as e:
                app.logger.warning(f"⚠️ NVIDIA {model} failed: {e}")
                continue

    # 3. Final fallback - friendly message
    return {
        "text": "I'm having trouble connecting right now. Please call (473) 440-6647 or visit Melville St, St George's for help.",
        "engine": "none",
    }

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

    # Use safe_call with Gemini + NVIDIA fallback
    result = safe_call(
        call_llm_with_fallback,
        full_prompt,
        safe_message,
        history_text,
        fallback={"text": "I'm having trouble connecting right now. Please call (473) 440-6647 or visit Melville St, St George's for help.", "engine": "none"},
        on_error=lambda e: app.logger.error(f"Chat failed: {e}"),
    )
    reply = result["text"]
    engine_used = result["engine"]

    # Clean up the reply
    reply = re.sub(r'\*\*([^*]+)\*\*', r'\1', reply)  # Remove bold
    reply = re.sub(r'#+\s*', '', reply)                # Remove headings

    # 📌 NEW: attach the real, verified official form link if this turn was
    # about a specific benefit. This runs AFTER the AI generates its reply,
    # using our own hardcoded FORM_LINKS lookup — the AI never has to recall
    # or invent a URL itself, so the link is always accurate.
    matched_form = find_relevant_form(safe_message)
    if matched_form and engine_used != "none":
        reply += f"\n\n📄 Official form: {matched_form['label']} — {matched_form['url']}"

    add_to_history(session_id, "user", safe_message)
    add_to_history(session_id, "bot", reply)

    return jsonify({
        "ok": True,
        "reply": reply,
        "session_id": session_id,
        "register": register,
        "ai_used": engine_used,  # 📌 now reflects what ACTUALLY answered this turn
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

    if not ELEVEN_KEY:
        return jsonify({"ok": False, "error": "ElevenLabs API key not configured"}), 500

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
