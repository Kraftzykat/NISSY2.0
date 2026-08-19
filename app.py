"""
Nissy - NIS Grenada Chatbot
===========================
IMPROVED VERSION with:
1. More personas (individual, employer, job_seeker, retiree, bereaved)
2. Persona persistence across turns
3. Language preference storage
4. Sentiment analysis
5. Task completion system
6. Health check endpoint
7. Better error handling 
"""


import json
import os
import re
import time
import logging
import csv
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

import httpx
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS

# --- Google Gemini SDK ---
from google import genai
from google.genai import types as genai_types

# ============================================================================
# 📦 APP SETUP
# ============================================================================
app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)  # Allow cross-origin requests

logging.basicConfig(level=logging.INFO)

# ============================================================================
# 📊 RATINGS LOGGING
# ============================================================================
RATINGS_FILE = "ratings.csv"

def init_ratings_log():
    if not os.path.exists(RATINGS_FILE):
        with open(RATINGS_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "session_id", "persona", "rating", "comment", "response_snippet"])

init_ratings_log()

def log_rating(session_id: str, persona: str, rating: int, comment: str, response: str):
    with open(RATINGS_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            session_id, persona, rating, comment, response[:150]
        ])

# ============================================================================
# 🔒 PII REDACTION
# ============================================================================
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

# ============================================================================
# 🧠 SENTIMENT ANALYSIS (NEW)
# ============================================================================
def analyze_sentiment(text: str) -> str:
    """Detect if the user is positive, negative, or neutral."""
    positive = ["thank", "great", "helpful", "good", "excellent", "awesome", "love", "amazing", "perfect"]
    negative = ["bad", "useless", "terrible", "worst", "annoying", "hate", "angry", "frustrated", "awful"]
    lower = text.lower()
    if any(w in lower for w in positive):
        return "positive"
    if any(w in lower for w in negative):
        return "negative"
    return "neutral"

# ============================================================================
# 🚨 CRISIS SUPPORT
# ============================================================================
DISTRESS_TRIGGERS = {
    "grief": ["passed away", "died", "funeral", "lost my", "she's gone", "he's gone", "my father", "my mother", "my child", "mourning"],
    "panic": ["can't breathe", "can't cope", "help now", "emergency", "overwhelmed", "it's too much", "panic"],
    "self_harm": ["hurt myself", "end it", "no way out", "kill myself", "suicide", "self harm", "kms", "cut myself"],
    "aggrieved": ["nobody listens", "you people never", "sick of this", "useless", "no help", "fuck", "asshole", "shit"],
}

CRISIS_HELPLINES = {
    "grief": "Bereavement Support: 473-440-6647",
    "panic": "Mental Health Helpline: 456-1353 | Emergency: 911",
    "self_harm": "Crisis Centre: 456-1353 | Mental Health: 444-1133 | Emergency: 911",
    "aggrieved": "Client Relations Desk: 473-440-6647",
}

def detect_distress(msg: str) -> Optional[str]:
    m = msg.lower()
    for category, words in DISTRESS_TRIGGERS.items():
        if any(w in m for w in words):
            return category
    return None

def distress_reply(category: str) -> str:
    helpline = CRISIS_HELPLINES.get(category, "Mental Health Helpline: 456-1353")
    replies = {
        "grief": f"I'm so sorry for your loss. You don't have to go through this alone. Please reach out to {helpline}. You are not alone. 💛",
        "panic": f"Take a deep breath. Help is available. Please call {helpline}. You matter. 💛",
        "self_harm": f"You matter. Please reach out right away. {helpline}. You are not alone. 💛",
        "aggrieved": f"I hear your frustration. Please call {helpline} to speak with someone who can help. 💛",
    }
    return replies.get(category, f"Please reach out for support: {helpline}")

# ============================================================================
# 🚫 AUTHORITY CHECKS
# ============================================================================
AUTHORITY_TRIGGERS = [
    "my case", "my application", "am i eligible", "my balance",
    "my account", "for me", "my status", "personal", "my claim",
    "my contributions", "my pension", "my benefit", "my record",
    "my nin", "my social security"
]

def check_authority(msg: str) -> bool:
    return not any(t in msg.lower() for t in AUTHORITY_TRIGGERS)

# ============================================================================
# 🌐 MULTI-LANGUAGE (with preference storage)
# ============================================================================
LANGUAGE_DETECTION = {
    "es": ["hola", "gracias", "por favor", "cómo", "qué", "beneficio", "pension"],
    "fr": ["bonjour", "merci", "comment", "quoi", "prestation", "pension"],
    "kw": ["sa", "mwen", "ou", "li", "nou", "yo", "ki", "pansyon"],
}

LANGUAGE_HINTS = {
    "en": "Reply in clear English.",
    "es": "Reply in Spanish. Use formal 'usted' form.",
    "fr": "Reply in French. Use polite 'vous' form.",
    "kw": "Reply in Kwéyòl (Caribbean French Creole).",
}

# Store language per session
session_languages: Dict[str, str] = {}

def detect_language(text: str) -> str:
    t = text.lower()
    for lang, words in LANGUAGE_DETECTION.items():
        if any(w in t for w in words):
            return lang
    return "en"

def get_language(session_id: str, message: str) -> str:
    if session_id in session_languages:
        return session_languages[session_id]
    detected = detect_language(message)
    session_languages[session_id] = detected
    return detected

# ============================================================================
# 👤 PERSONAS (ENHANCED - More personas!)
# ============================================================================
PERSONAS = {
    "individual": {
        "label": "Individual Member",
        "focus": "They're asking about their own benefits. Focus on personal benefit information, eligibility, and how to claim.",
        "keywords": ["my pension", "my benefit", "my contributions", "i need", "for me", "my claim"],
    },
    "employer": {
        "label": "Employer / HR Contact",
        "focus": "They're asking on behalf of a business. Focus on employer registration, remittance, payroll, and staff contributions.",
        "keywords": ["register as an employer", "employer registration", "remit contributions", "my employees", "payroll"],
    },
    "job_seeker": {
        "label": "Job Seeker / New Employee",
        "focus": "They're looking for work or just started a job. Focus on getting an NIS number, how contributions work, and benefits.",
        "keywords": ["new job", "just started", "get my nin", "social security number", "new employee", "register for nis"],
    },
    "retiree": {
        "label": "Retiree / Near Retirement",
        "focus": "They're planning for retirement or already retired. Focus on age pension, pensionable age, and how to claim.",
        "keywords": ["retire", "pension", "retirement", "age benefit", "pensionable age", "retiring"],
    },
    "bereaved": {
        "label": "Grieving Family Member",
        "focus": "They've lost a loved one. Be extra gentle. Focus on Survivors Benefit and Funeral Grant with compassion.",
        "keywords": ["passed away", "died", "death", "funeral", "survivor", "widow", "widower", "bereaved"],
    },
}

def detect_persona(msg: str) -> Optional[str]:
    m = msg.lower()
    for key, data in PERSONAS.items():
        if any(kw in m for kw in data.get("keywords", [])):
            return key
    return None

# Store persona per session
session_personas: Dict[str, str] = {}

# ============================================================================
# 🗺️ TERRITORY
# ============================================================================
TERRITORY_KEYWORDS = {
    "grenada": {"country": "Grenada", "office": "Melville St, St George's", "phone": "(473) 440-6647"},
    "carriacou": {"country": "Carriacou", "office": "Hillsborough", "phone": "(473) 443-6026"},
    "petite martinique": {"country": "Petite Martinique", "office": "Hillsborough", "phone": "(473) 443-6026"},
}

def detect_territory(msg: str) -> Optional[dict]:
    m = msg.lower()
    for key, data in TERRITORY_KEYWORDS.items():
        if key in m:
            return data
    return None

# ============================================================================
# 📋 OFFICIAL FORM LINKS
# ============================================================================
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
    "employer_registration": {
        "label": "Employer Registration Form",
        "url": "https://nisgrenada.org/download/employer-registration-form/",
        "keywords": ["register as an employer", "employer registration", "new business registration"],
    },
    "self_employed": {
        "label": "Self Employed Registration Form",
        "url": "https://nisgrenada.org/download/self-employed-registration-form/",
        "keywords": ["self employed registration", "register as self employed"],
    },
    "voluntary": {
        "label": "Voluntary Contribution Registration Form",
        "url": "https://nisgrenada.org/download/voluntary-contribution-registration-form/",
        "keywords": ["voluntary contributor", "voluntary contribution"],
    },
}

def find_relevant_form(msg: str) -> Optional[dict]:
    m = msg.lower()
    for entry in FORM_LINKS.values():
        if any(kw in m for kw in entry["keywords"]):
            return entry
    return None

# ============================================================================
# 💬 CONVERSATION MEMORY
# ============================================================================
MAX_HISTORY_TURNS = 6
conversation_histories: Dict[str, list] = {}

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
    for entry in history:
        speaker = "User" if entry["role"] == "user" else "Nissy"
        lines.append(f"{speaker}: {entry['text']}")
    return "\n".join(lines) + "\n\n"

# ============================================================================
# 🧭 JOURNEY TRACKING
# ============================================================================
JOURNEY_STEPS = ["greeting", "identify_need", "collect_facts", "offer_next_step", "confirm_close"]
session_states: Dict[str, int] = {}

def get_journey_step(session_id: str) -> str:
    step_idx = session_states.get(session_id, 0)
    return JOURNEY_STEPS[min(step_idx, len(JOURNEY_STEPS) - 1)]

def advance_journey(session_id: str):
    current = session_states.get(session_id, 0)
    if current < len(JOURNEY_STEPS) - 1:
        session_states[session_id] = current + 1

# ============================================================================
# 📝 TASK COMPLETION SYSTEM (NEW)
# ============================================================================
class Task:
    def __init__(self, session_id: str, task_type: str, steps: List[str]):
        self.session_id = session_id
        self.task_type = task_type
        self.steps = steps
        self.current_step = 0
        self.completed = False
        self.data = {}
    
    def next_step(self) -> Optional[str]:
        if self.current_step < len(self.steps) - 1:
            self.current_step += 1
            return self.steps[self.current_step]
        self.completed = True
        return None
    
    def get_step(self) -> str:
        return self.steps[self.current_step] if self.current_step < len(self.steps) else "complete"

active_tasks: Dict[str, Task] = {}

def start_task(session_id: str, task_type: str) -> Task:
    if task_type == "claim":
        steps = ["which_benefit", "check_requirements", "gather_documents", "submit_claim"]
    elif task_type == "registration":
        steps = ["identify_type", "collect_info", "confirm_registration"]
    else:
        steps = ["greeting", "identify_need", "offer_next_step"]
    
    task = Task(session_id, task_type, steps)
    active_tasks[session_id] = task
    return task

def get_task(session_id: str) -> Optional[Task]:
    return active_tasks.get(session_id)

# ============================================================================
# 🛡️ safe_call
# ============================================================================
def safe_call(fn, *args, fallback=None, on_error=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        app.logger.warning(f"safe_call caught: {e}")
        if on_error:
            on_error(e)
        return fallback or "I'm having trouble right now. Please call (473) 440-6647 for help."

# ============================================================================
# 🤖 AI CONFIGURATION
# ============================================================================
# --- GEMINI ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

gemini_client = None
gemini_available = False

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        gemini_available = True
        app.logger.info(f"✅ Gemini configured. Model: {GEMINI_MODEL}")
    except Exception as e:
        app.logger.warning(f"⚠️ Gemini config failed: {e}")
else:
    app.logger.warning("⚠️ GEMINI_API_KEY not set")

# --- NVIDIA FALLBACK ---
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_API = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODELS = [
    "meta/llama-3.1-8b-instruct",
    "nvidia/nemotron-mini-4b-instruct",
]

# --- ELEVENLABS TTS ---
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.environ.get("ELEVEN_VOICE_ID", "hpp4J3VqNfWAUOO0d1Us")

# ============================================================================
# 📝 TCRDEI PROMPT BUILDER
# ============================================================================
def build_tcrdei_prompt(
    service_name: str,
    register: str,
    journey_step: str,
    persona: str = None,
    persona_focus: str = "",
    language: str = "en",
    sentiment: str = "neutral",
    task: Optional[Task] = None,
    territory: Optional[dict] = None,
) -> str:
    """Build the system prompt with all context"""
    
    tone_hints = {
        "warm": "TONE: Warm, friendly, encouraging. Use 💛 emoji occasionally.",
        "professional": "TONE: Professional, polished, respectful. No emojis.",
        "urgent": "TONE: Urgent, direct, action-oriented.",
        "bereaved": "TONE: Gentle condolences FIRST. Then explain. Max 2 sentences of facts.",
    }
    
    journey_hints = {
        "greeting": "Welcome the user warmly and ask what they need.",
        "identify_need": "Help clarify which benefit they're asking about.",
        "collect_facts": "Provide clear, accurate facts about the benefit.",
        "offer_next_step": "Proactively suggest a concrete next step.",
        "confirm_close": "Wrap up warmly. Confirm they have what they need.",
    }
    
    # Persona hint
    if persona and persona in PERSONAS:
        persona_hint = f"USER TYPE: {PERSONAS[persona]['label']}. {PERSONAS[persona]['focus']}"
    else:
        persona_hint = "USER TYPE: Individual member. Focus on personal benefit information."
    
    # Sentiment hint
    sentiment_hints = {
        "positive": "The user seems happy. Match their positive energy!",
        "negative": "The user seems frustrated. Be extra patient and helpful.",
        "neutral": "The user is neutral. Keep a professional, friendly tone.",
    }
    sentiment_hint = sentiment_hints.get(sentiment, sentiment_hints["neutral"])
    
    # Task hint
    task_hint = ""
    if task and not task.completed:
        task_hint = f"TASK STATUS: You are helping with a '{task.task_type}' task. Current step: {task.get_step()}."
    
    # Territory context
    territory_context = ""
    if territory:
        territory_context = f"Territory: {territory['country']}. Office: {territory['office']}. Phone: {territory['phone']}."
    else:
        territory_context = "Office: Melville St, St George's. Phone: (473) 440-6647."
    
    # Service name
    if not service_name:
        service_name = "NIS Grenada benefits"
    
    return f"""
[T] You are Nissy, a warm, professional assistant for NIS Grenada.

[C] Context: The user is asking about {service_name}.
    {territory_context}
    Ethical rule: NEVER quote personal case details or handle personal data.

[R] Rules:
    - NEVER ask for or store personal data (NIN, phone, email)
    - If asked a personal case question, say: "Please call (473) 440-6647"
    - Always be truthful. If unsure, say so and direct to the office.

[D] Success = the user feels respected, informed, and knows their next steps.

[E] Before replying: Is this accurate? Does it show care? Does it guide to a next step?

[I] If unsure, ask ONE clarifying question.

{persona_hint}
{tone_hints.get(register, tone_hints['warm'])}
{sentiment_hint}
{task_hint}

CONVERSATION STAGE: {journey_step}. {journey_hints.get(journey_step, journey_hints['greeting'])}

LANGUAGE: {LANGUAGE_HINTS.get(language, LANGUAGE_HINTS['en'])}

MEMORY: Previous turns are below with "User:" and "Nissy:" labels. Use them for context.

CONVERSATION HISTORY:
{history_text}

User's message: {user_message}

RESPONSE GUIDELINES:
1. Open with warmth and respect
2. Provide COMPLETE information - include requirements, amounts, deadlines
3. Use bullet points (- ) for clarity, each with substance
4. End with a clear, actionable next step
5. Use a caring, professional tone - like a trusted government representative
"""

# ============================================================================
# 🧠 AI CALL FUNCTIONS
# ============================================================================
def call_gemini(prompt: str, user_msg: str, history_text: str = "") -> str:
    if not gemini_available:
        raise Exception("Gemini not configured")
    
    full_prompt = f"{prompt}\n\n{history_text}User: {user_msg}"
    
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=500,
            top_p=0.8,
        ),
    )
    
    if not response.text:
        raise Exception("Gemini returned empty response")
    return response.text.strip()

def call_nvidia(prompt: str, user_msg: str, model: str) -> str:
    if not NVIDIA_KEY:
        raise Exception("NVIDIA key not set")
    
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
                "max_tokens": 500,
            },
        )
        if r.status_code == 200:
            body = r.json()
            reply = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            return reply
        raise Exception(f"NVIDIA {model} HTTP {r.status_code}")

def call_llm_with_fallback(prompt: str, user_msg: str, history_text: str = "") -> dict:
    """Try Gemini first, then NVIDIA, then human fallback"""
    
    # 1. Try Gemini
    if gemini_available:
        try:
            result = call_gemini(prompt, user_msg, history_text)
            if result:
                app.logger.info("✅ Gemini response successful")
                return {"text": result, "engine": "gemini"}
        except Exception as e:
            app.logger.warning(f"⚠️ Gemini failed: {e}")
    
    # 2. Try NVIDIA
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
    
    # 3. Human fallback
    return {
        "text": "I'm having trouble connecting. Please call (473) 440-6647 or visit Melville St, St George's for help.",
        "engine": "none",
    }

# ============================================================================
# 🌐 API ENDPOINTS
# ============================================================================
@app.route("/")
def index():
    try:
        return send_from_directory("static", "index.html")
    except Exception:
        return "Error: index.html not found in static folder.", 404

@app.route("/static/<path:filename>")
def serve_static(filename):
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
    requested_language = data.get("language", "en")
    requested_persona = data.get("persona", "individual")
    
    safe_message = redact_pii(raw_message)
    
    # 1. 🚨 Check distress
    distress = detect_distress(safe_message)
    if distress:
        add_to_history(session_id, "user", safe_message)
        reply = distress_reply(distress)
        add_to_history(session_id, "bot", reply)
        return jsonify({
            "ok": True,
            "reply": reply,
            "session_id": session_id,
            "distress": True,
        })
    
    # 2. 🛡️ Check authority
    if not check_authority(safe_message):
        add_to_history(session_id, "user", safe_message)
        reply = "🙏 For personal case questions, please call (473) 440-6647 or visit Melville St, St George's."
        add_to_history(session_id, "bot", reply)
        return jsonify({
            "ok": True,
            "reply": reply,
            "session_id": session_id,
            "escalated": True,
        })
    
    # 3. 🌐 Get language
    lang = requested_language if requested_language != "en" else get_language(session_id, safe_message)
    session_languages[session_id] = lang
    
    # 4. 👤 Get persona
    if requested_persona and requested_persona in PERSONAS:
        session_personas[session_id] = requested_persona
    elif requested_persona not in PERSONAS:
        detected = detect_persona(safe_message)
        if detected:
            session_personas[session_id] = detected
    persona = session_personas.get(session_id, "individual")
    
    # 5. 🧠 Analyze sentiment
    sentiment = analyze_sentiment(safe_message)
    
    # 6. 🧭 Advance journey
    advance_journey(session_id)
    journey_step = get_journey_step(session_id)
    
    # 7. 🗺️ Detect territory
    territory = detect_territory(safe_message)
    
    # 8. 📝 Check for task
    task = get_task(session_id)
    if not task and any(w in safe_message.lower() for w in ["claim", "apply", "register", "form"]):
        task_type = "claim" if "claim" in safe_message.lower() else "registration"
        task = start_task(session_id, task_type)
    
    # 9. 📋 Find relevant form
    matched_form = find_relevant_form(safe_message)
    
    # 10. 📝 Build prompt
    history_text = get_history_text(session_id)
    
    prompt = build_tcrdei_prompt(
        service_name="NIS Grenada benefits",
        register="warm",
        journey_step=journey_step,
        persona=persona,
        persona_focus=PERSONAS.get(persona, {}).get("focus", ""),
        language=lang,
        sentiment=sentiment,
        task=task,
        territory=territory,
    )
    
    # 11. 🤖 Get AI response
    result = safe_call(
        call_llm_with_fallback,
        prompt,
        safe_message,
        history_text,
        fallback={"text": "Please call (473) 440-6647 for help.", "engine": "none"},
    )
    reply = result["text"]
    engine_used = result["engine"]
    
    # 12. 📋 Add form link if relevant
    if matched_form:
        reply += f"\n\n📄 Official form: {matched_form['label']} — {matched_form['url']}"
    
    # 13. 💾 Save to history
    add_to_history(session_id, "user", safe_message)
    add_to_history(session_id, "bot", reply)
    
    # 14. 📊 Return response
    return jsonify({
        "ok": True,
        "reply": reply,
        "session_id": session_id,
        "persona": persona,
        "language": lang,
        "sentiment": sentiment,
        "journey_step": journey_step,
        "ai_used": engine_used,
        "task_step": task.get_step() if task else None,
    })

@app.route("/api/rate", methods=["POST", "OPTIONS"])
def rate():
    if request.method == "OPTIONS":
        return "", 204
    
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    
    try:
        session_id = data.get("session_id", "unknown")
        persona = data.get("persona", "individual")
        rating = data.get("rating", 0)
        comment = data.get("comment", "")
        response = data.get("response_text", "")
        
        log_rating(session_id, persona, rating, comment, response)
        return jsonify({"ok": True, "message": "Rating recorded!"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

# 🌟 NEW: Health check endpoint
@app.route("/api/health")
def health_check():
    return jsonify({
        "ok": True,
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "gemini_available": gemini_available,
        "nvidia_available": bool(NVIDIA_KEY),
        "active_sessions": len(conversation_histories),
        "active_tasks": len(active_tasks),
    })

@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
