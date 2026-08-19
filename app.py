"""
==============================================
NISSY - NIS GRENADA CHATBOT
==============================================
This is the BACKEND (brain) of the Nissy chatbot.

WHAT THIS FILE DOES:
1. Receives messages from the chat window (index.html)
2. Checks if the user is in distress or asking personal questions
3. Sends the message to Google's AI (Gemini) for a response
4. Sends the AI's response back to the chat window

HOW TO RUN:
- Save this file as app.py in your project folder
- Run: python app.py
- The server starts at http://localhost:5000

WHAT YOU NEED:
- A GEMINI_API_KEY from Google AI Studio (free!)
- Put it in a .env file or Render environment variables
==============================================
"""

# =============================================
# 📦 IMPORTS - "Borrowing" code other people wrote
# =============================================
# Think of imports like getting tools from a toolbox
# instead of building everything from scratch!

import json                     # For reading/writing JSON data
import os                       # For reading environment variables (API keys!)
import re                       # For finding patterns in text (like phone numbers)
import time                     # For adding delays if needed
import logging                  # For printing useful info when debugging
import csv                      # For saving ratings to a spreadsheet
import uuid                     # For creating unique session IDs
from datetime import datetime, timezone  # For timestamps
from typing import Optional, Dict, List, Any  # For type hints (helps code editors)

import httpx                    # For making HTTP requests (calling NVIDIA API)
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS     # Allows the frontend to talk to the backend

# 📌 IMPORTANT: Google's NEW AI SDK (google-genai)
# The old one (google-generativeai) is DEPRECATED!
from google import genai
from google.genai import types as genai_types

# =============================================
# 🚀 CREATE THE FLASK APP
# =============================================
# Flask is a tool that turns Python into a web server.
# "app" is our web server object - it handles all incoming requests.

app = Flask(__name__, static_folder='static', static_url_path='/static')

# CORS = Cross-Origin Resource Sharing
# This tells the browser "it's OK for the chat page to talk to this server"
CORS(app)

# Set up logging so we can see what's happening
logging.basicConfig(level=logging.INFO)

# =============================================
# 📊 RATINGS LOGGING - Save user feedback
# =============================================
# This creates a CSV file (like a spreadsheet) to save ratings

RATINGS_FILE = "ratings.csv"

def init_ratings_log():
    """
    Creates the ratings file if it doesn't exist.
    This runs ONCE when the server starts.
    """
    if not os.path.exists(RATINGS_FILE):
        with open(RATINGS_FILE, "w", newline="") as f:
            # Write the column headers
            csv.writer(f).writerow(["timestamp", "session_id", "persona", "rating", "comment", "response_snippet"])

# Run it now!
init_ratings_log()

def log_rating(session_id: str, persona: str, rating: int, comment: str, response: str):
    """
    Saves one rating to the CSV file.
    Called when a user clicks a star rating.
    """
    with open(RATINGS_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),  # Current time
            session_id,          # Which user is this?
            persona,             # What type of user (individual, employer, etc.)
            rating,              # 1-5 stars
            comment,             # Optional text feedback
            response[:150]       # Only save first 150 chars (to save space)
        ])

# =============================================
# 🔒 PII REDACTION - Remove personal information
# =============================================
# PII = Personally Identifiable Information (like phone numbers, emails)
# This protects user privacy BEFORE sending to the AI

def redact_pii(text: str) -> str:
    """
    Finds and replaces personal info with [REDACTED] tags.
    
    Example:
    "My phone is 555-1234" → "My phone is [REDACTED:phone]"
    
    This keeps user data safe from the AI!
    """
    patterns = {
        "nin": r"\b\d{9}\b",                     # 9-digit National ID
        "email": r"\b[\w\.-]+@[\w\.-]+\.\w+\b",  # email@domain.com
        "phone": r"\b\d{3}[-.\s]?\d{4}\b",       # 555-1234 or 555.1234
        "address": r"\b\d{1,4}\s+[A-Za-z]+\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd)\b",
    }
    for label, pattern in patterns.items():
        # re.sub = "replace all matches"
        text = re.sub(pattern, f"[REDACTED:{label}]", text)
    return text

# =============================================
# 🧠 SENTIMENT ANALYSIS - How is the user feeling?
# =============================================
# This detects if the user sounds happy, angry, or neutral.
# The bot can then respond in a matching tone.

def analyze_sentiment(text: str) -> str:
    """
    Looks at the user's message and detects their mood.
    
    Returns:
    - "positive" if they seem happy
    - "negative" if they seem angry/frustrated
    - "neutral" if neither
    """
    positive = ["thank", "great", "helpful", "good", "excellent", "awesome", "love", "amazing", "perfect"]
    negative = ["bad", "useless", "terrible", "worst", "annoying", "hate", "angry", "frustrated", "awful"]
    
    lower = text.lower()
    if any(w in lower for w in positive):
        return "positive"
    if any(w in lower for w in negative):
        return "negative"
    return "neutral"

# =============================================
# 🚨 CRISIS SUPPORT - Help for users in distress
# =============================================
# If the user mentions something concerning,
# the bot provides help and local helpline numbers.

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
    """
    Checks if the user's message contains any distress triggers.
    Returns the category name if found, or None if safe.
    """
    m = msg.lower()
    for category, words in DISTRESS_TRIGGERS.items():
        if any(w in m for w in words):
            return category
    return None

def distress_reply(category: str) -> str:
    """
    Returns a caring, supportive response for users in crisis.
    Includes local helpline numbers.
    """
    helpline = CRISIS_HELPLINES.get(category, "Mental Health Helpline: 456-1353")
    replies = {
        "grief": f"I'm so sorry for your loss. You don't have to go through this alone. Please reach out to {helpline}. You are not alone.",
        "panic": f"Take a deep breath. Help is available. Please call {helpline}. You matter.",
        "self_harm": f"You matter. Please reach out right away. {helpline}. You are not alone.",
        "aggrieved": f"I hear your frustration. Please call {helpline} to speak with someone who can help.",
    }
    return replies.get(category, f"Please reach out for support: {helpline}")

# =============================================
# 🚫 AUTHORITY CHECKS - What the bot CAN'T answer
# =============================================
# The bot cannot handle personal cases (like "am I eligible?").
# These get redirected to a human.

AUTHORITY_TRIGGERS = [
    "my case", "my application", "am i eligible", "my balance",
    "my account", "for me", "my status", "personal", "my claim",
    "my contributions", "my pension", "my benefit", "my record",
    "my nin", "my social security"
]

def check_authority(msg: str) -> bool:
    """
    Returns True if the message is SAFE for the bot.
    Returns False if it contains personal info that should go to a human.
    """
    return not any(t in msg.lower() for t in AUTHORITY_TRIGGERS)

# =============================================
# 🌐 MULTI-LANGUAGE SUPPORT
# =============================================
# The bot can respond in English, Spanish, French, or Kwéyòl.
# It detects which language the user typed and responds in the same one.

LANGUAGE_DETECTION = {
    "es": ["hola", "gracias", "por favor", "cómo", "qué", "beneficio", "pension"],
    "fr": ["bonjour", "merci", "comment", "quoi", "prestation", "pension"],
    "kw": ["sa", "mwen", "ou", "li", "nou", "yo", "ki", "pansyon"],
}

LANGUAGE_HINTS = {
    "en": "Reply in clear English. Keep it CONCISE (short and to the point).",
    "es": "Reply in Spanish. Use formal 'usted' form. Keep it CONCISE.",
    "fr": "Reply in French. Use polite 'vous' form. Keep it CONCISE.",
    "kw": "Reply in Kwéyòl (Caribbean French Creole). Keep it CONCISE.",
}

# Store each user's language preference
session_languages: Dict[str, str] = {}

def detect_language(text: str) -> str:
    """
    Detects which language the user wrote in.
    Returns 'en' (English) if no other language is detected.
    """
    t = text.lower()
    for lang, words in LANGUAGE_DETECTION.items():
        if any(w in t for w in words):
            return lang
    return "en"

def get_language(session_id: str, message: str) -> str:
    """
    Gets the user's language preference.
    If they haven't set one yet, detects it from their message.
    """
    if session_id in session_languages:
        return session_languages[session_id]
    detected = detect_language(message)
    session_languages[session_id] = detected
    return detected

# =============================================
# 👤 PERSONAS - Different user types
# =============================================
# The bot adapts its responses based on who the user is.
# Example: An employer gets different info than a retiree.

PERSONAS = {
    "individual": {
        "label": "Individual Member",
        "focus": "You are an individual asking about your own benefits. Focus on personal benefit information.",
        "keywords": ["my pension", "my benefit", "my contributions", "i need", "for me", "my claim"],
    },
    "employer": {
        "label": "Employer / HR Contact",
        "focus": "You are an employer asking about staff contributions. Focus on employer registration and payroll.",
        "keywords": ["register as an employer", "employer registration", "remit contributions", "my employees", "payroll"],
    },
    "job_seeker": {
        "label": "Job Seeker / New Employee",
        "focus": "You are a new employee or job seeker. Focus on getting an NIS number and how contributions work.",
        "keywords": ["new job", "just started", "get my nin", "social security number", "new employee", "register for nis"],
    },
    "retiree": {
        "label": "Retiree / Near Retirement",
        "focus": "You are planning for retirement. Focus on age pension, pensionable age, and how to claim.",
        "keywords": ["retire", "pension", "retirement", "age benefit", "pensionable age", "retiring"],
    },
    "bereaved": {
        "label": "Grieving Family Member",
        "focus": "You have lost a loved one. Be extra gentle. Focus on Survivors Benefit and Funeral Grant.",
        "keywords": ["passed away", "died", "death", "funeral", "survivor", "widow", "widower", "bereaved"],
    },
}

def detect_persona(msg: str) -> Optional[str]:
    """
    Figures out what type of user is asking.
    Returns the persona key (like "employer") or None.
    """
    m = msg.lower()
    for key, data in PERSONAS.items():
        if any(kw in m for kw in data.get("keywords", [])):
            return key
    return None

# Store each user's persona
session_personas: Dict[str, str] = {}

# =============================================
# 🗺️ TERRITORY - Where is the user?
# =============================================
# Different locations have different office info.

TERRITORY_KEYWORDS = {
    "grenada": {"country": "Grenada", "office": "Melville St, St George's", "phone": "(473) 440-6647"},
    "carriacou": {"country": "Carriacou", "office": "Hillsborough", "phone": "(473) 443-6026"},
    "petite martinique": {"country": "Petite Martinique", "office": "Hillsborough", "phone": "(473) 443-6026"},
}

def detect_territory(msg: str) -> Optional[dict]:
    """
    Checks if the user mentioned a specific location.
    Returns office info if found, otherwise None.
    """
    m = msg.lower()
    for key, data in TERRITORY_KEYWORDS.items():
        if key in m:
            return data
    return None

# =============================================
# 📋 OFFICIAL FORM LINKS
# =============================================
# Hardcoded links to official NIS Grenada forms.
# These are NEVER generated by the AI - they're always correct!

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
    "employer_registration": {
        "label": "Employer Registration Form",
        "url": "https://nisgrenada.org/download/employer-registration-form/",
        "keywords": ["register as an employer", "employer registration", "new business registration"],
    },
}

def find_relevant_form(msg: str) -> Optional[dict]:
    """
    Looks at the user's message and finds a matching form link.
    Returns the form info if found, otherwise None.
    """
    m = msg.lower()
    for entry in FORM_LINKS.values():
        if any(kw in m for kw in entry["keywords"]):
            return entry
    return None

# =============================================
# 💬 CONVERSATION MEMORY
# =============================================
# Remembers the last few messages so the bot can understand
# follow-up questions like "tell me more about that."

MAX_HISTORY_TURNS = 6  # Remember 6 exchanges
conversation_histories: Dict[str, list] = {}

def add_to_history(session_id: str, role: str, text: str):
    """
    Adds one message to the conversation history.
    role = "user" or "bot"
    """
    if session_id not in conversation_histories:
        conversation_histories[session_id] = []
    conversation_histories[session_id].append({"role": role, "text": text})
    
    # Keep only the most recent messages (so we don't run out of memory!)
    max_messages = MAX_HISTORY_TURNS * 2
    if len(conversation_histories[session_id]) > max_messages:
        conversation_histories[session_id] = conversation_histories[session_id][-max_messages:]

def get_history_text(session_id: str) -> str:
    """
    Builds a text block of the conversation history.
    Example:
        User: What is the age pension?
        Nissy: The age pension is...
        User: How do I apply?
    """
    history = conversation_histories.get(session_id, [])
    if not history:
        return ""
    lines = []
    for entry in history:
        speaker = "User" if entry["role"] == "user" else "Nissy"
        lines.append(f"{speaker}: {entry['text']}")
    return "\n".join(lines) + "\n\n"

# =============================================
# 🧭 JOURNEY TRACKING
# =============================================
# Tracks where the user is in their conversation journey.
# Steps: greeting -> identify_need -> collect_facts -> offer_next_step -> confirm_close

JOURNEY_STEPS = ["greeting", "identify_need", "collect_facts", "offer_next_step", "confirm_close"]
session_states: Dict[str, int] = {}

def get_journey_step(session_id: str) -> str:
    """Returns the current journey step name."""
    step_idx = session_states.get(session_id, 0)
    return JOURNEY_STEPS[min(step_idx, len(JOURNEY_STEPS) - 1)]

def advance_journey(session_id: str):
    """Moves the user to the next journey step."""
    current = session_states.get(session_id, 0)
    if current < len(JOURNEY_STEPS) - 1:
        session_states[session_id] = current + 1

# =============================================
# 📝 TASK COMPLETION SYSTEM
# =============================================
# Helps users complete multi-step tasks (like applying for a benefit).

class Task:
    """A task that the user is working on (like applying for a benefit)."""
    def __init__(self, session_id: str, task_type: str, steps: List[str]):
        self.session_id = session_id
        self.task_type = task_type  # "claim" or "registration"
        self.steps = steps          # List of step names
        self.current_step = 0       # Which step are we on?
        self.completed = False      # Is the task done?
        self.data = {}              # Store info collected along the way
    
    def next_step(self) -> Optional[str]:
        """Moves to the next step. Returns the step name or None if complete."""
        if self.current_step < len(self.steps) - 1:
            self.current_step += 1
            return self.steps[self.current_step]
        self.completed = True
        return None
    
    def get_step(self) -> str:
        """Returns the current step name."""
        return self.steps[self.current_step] if self.current_step < len(self.steps) else "complete"

active_tasks: Dict[str, Task] = {}

def start_task(session_id: str, task_type: str) -> Task:
    """
    Creates a new task for the user.
    task_type: "claim" or "registration"
    """
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
    """Gets the user's current task, if they have one."""
    return active_tasks.get(session_id)

# =============================================
# 🛡️ safe_call - Error protection
# =============================================
# This function catches errors so the whole server doesn't crash.
# It's like a safety net for the code!

def safe_call(fn, *args, fallback=None, on_error=None, **kwargs):
    """
    Tries to run a function. If it fails, returns a fallback value.
    
    Example:
        result = safe_call(call_gemini, prompt, user_msg, fallback="Sorry, I'm having trouble")
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        app.logger.warning(f"safe_call caught: {e}")
        if on_error:
            on_error(e)
        return fallback or "I'm having trouble right now. Please call (473) 440-6647 for help."

# =============================================
# 🤖 AI CONFIGURATION
# =============================================
# This sets up the connection to Google Gemini (the AI brain).

# --- GEMINI (Primary AI) ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

gemini_client = None
gemini_available = False

if GEMINI_API_KEY:
    try:
        # 📌 NEW SDK: genai.Client() instead of the old genai.configure()
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        gemini_available = True
        app.logger.info(f"✅ Gemini configured. Model: {GEMINI_MODEL}")
    except Exception as e:
        app.logger.warning(f"⚠️ Gemini config failed: {e}")
else:
    app.logger.warning("⚠️ GEMINI_API_KEY not set - please add it to your environment")

# --- NVIDIA FALLBACK (Backup AI if Gemini fails) ---
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_API = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODELS = [
    "meta/llama-3.1-8b-instruct",
    "nvidia/nemotron-mini-4b-instruct",
]

# --- ELEVENLABS TTS (Text-to-Speech) ---
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE = os.environ.get("ELEVEN_VOICE_ID", "hpp4J3VqNfWAUOO0d1Us")

# =============================================
# 📝 TCRDEI PROMPT BUILDER
# =============================================
# This builds the instructions sent to the AI.
# TCRDEI = Task, Context, Rules, Definition, Evaluate, Iterate

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
    history_text: str = "",
    user_message: str = "",
) -> str:
    """
    Builds the system prompt for the AI.
    This tells the AI who it is, what to do, and how to respond.
    """
    
    # =============================================
    # TONE HINTS - How the bot should sound
    # =============================================
    tone_hints = {
        "warm": "TONE: Warm but CONCISE. Get to the point quickly.",
        "professional": "TONE: Professional and CONCISE. No fluff.",
        "urgent": "TONE: Direct and QUICK. Just the facts.",
        "bereaved": "TONE: Gentle but CONCISE. Don't overwhelm with text.",
    }
    
    # =============================================
    # JOURNEY HINTS - What stage of the conversation
    # =============================================
    journey_hints = {
        "greeting": "Welcome briefly and ask what they need. MAX 2 sentences.",
        "identify_need": "Ask ONE question to clarify. No long explanations.",
        "collect_facts": "Give CONCISE facts. MAX 3 bullet points.",
        "offer_next_step": "Suggest ONE concrete next step. Be direct.",
        "confirm_close": "Wrap up in 1 sentence. Short and warm.",
    }
    
    # =============================================
    # CONCISE RULES - Keep responses SHORT!
    # =============================================
    concise_rules = """
    CONCISE RESPONSE RULES (MANDATORY):
    - MAXIMUM 3-4 sentences total
    - MAXIMUM 2 bullet points (if needed)
    - NO long introductions or conclusions
    - Get to the answer in the FIRST sentence
    - If the user needs more details, they will ask follow-up questions
    """
    
    # =============================================
    # PERSONA HINT - What type of user
    # =============================================
    if persona and persona in PERSONAS:
        persona_hint = f"USER TYPE: {PERSONAS[persona]['label']}. {PERSONAS[persona]['focus']}"
    else:
        persona_hint = "USER TYPE: Individual member. Focus on personal benefit information."
    
    # =============================================
    # SENTIMENT HINT - How the user is feeling
    # =============================================
    sentiment_hints = {
        "positive": "The user seems happy. Match their positive energy!",
        "negative": "The user seems frustrated. Be extra patient and helpful.",
        "neutral": "The user is neutral. Keep a professional, friendly tone.",
    }
    sentiment_hint = sentiment_hints.get(sentiment, sentiment_hints["neutral"])
    
    # =============================================
    # TASK HINT - If they're in the middle of a task
    # =============================================
    task_hint = ""
    if task and not task.completed:
        task_hint = f"TASK STATUS: You are helping with a '{task.task_type}' task. Current step: {task.get_step()}."
    
    # =============================================
    # TERRITORY CONTEXT - Where they are
    # =============================================
    territory_context = ""
    if territory:
        territory_context = f"Territory: {territory['country']}. Office: {territory['office']}. Phone: {territory['phone']}."
    else:
        territory_context = "Office: Melville St, St George's. Phone: (473) 440-6647."
    
    # =============================================
    # BUILD THE FINAL PROMPT
    # =============================================
    return f"""
[T] You are Nissy, a CONCISE assistant for NIS Grenada.

{concise_rules}

[C] Context: The user is asking about {service_name}.
    {territory_context}
    Ethical rule: NEVER quote personal case details or handle personal data.

[R] Rules:
    - NEVER ask for or store personal data (NIN, phone, email)
    - If asked a personal case question, say: "Please call (473) 440-6647"
    - Always be truthful. If unsure, say so and direct to the office.

[D] Success = the user gets a CLEAR, QUICK answer in under 30 seconds of reading.

[E] Before replying: Is this accurate? Is it CONCISE? Does it guide to a next step?

[I] If unsure, ask ONE clarifying question. No more than one!

{persona_hint}
{tone_hints.get(register, tone_hints['warm'])}
{sentiment_hint}
{task_hint}

CONVERSATION STAGE: {journey_step}. {journey_hints.get(journey_step, journey_hints['greeting'])}

LANGUAGE: {LANGUAGE_HINTS.get(language, LANGUAGE_HINTS['en'])}

CONVERSATION HISTORY:
{history_text}

User's message: {user_message}

RESPONSE GUIDELINES:
1. Open with warmth but get to the point
2. Provide CONCISE information - 3-4 sentences max
3. Use bullet points ONLY if listing 2+ items
4. End with a clear, actionable next step
5. Be a trusted, helpful guide - not a robot!
"""

# =============================================
# 🧠 AI CALL FUNCTIONS
# =============================================
# These actually send the message to the AI and get a response.

def call_gemini(prompt: str, user_msg: str, history_text: str = "") -> str:
    """
    Sends the prompt to Google Gemini and returns the response.
    This is the PRIMARY AI.
    """
    if not gemini_available:
        raise Exception("Gemini not configured")
    
    # Combine the system prompt + history + user message
    full_prompt = f"{prompt}\n\n{history_text}User: {user_msg}"
    
    # 📌 NEW SDK: models.generate_content() instead of the old method
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.3,      # Lower = more focused, higher = more creative
            max_output_tokens=200,  # ⬅️ LOW = CONCISE responses (150-200 words max)
            top_p=0.8,
        ),
    )
    
    if not response.text:
        raise Exception("Gemini returned empty response")
    return response.text.strip()

def call_nvidia(prompt: str, user_msg: str, model: str) -> str:
    """
    Sends the prompt to NVIDIA NIM API (backup AI).
    This runs if Gemini fails.
    """
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
                "max_tokens": 200,  # ⬅️ LOW = CONCISE responses
            },
        )
        if r.status_code == 200:
            body = r.json()
            reply = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            return reply
        raise Exception(f"NVIDIA {model} HTTP {r.status_code}")

def call_llm_with_fallback(prompt: str, user_msg: str, history_text: str = "") -> dict:
    """
    Tries Gemini first, then NVIDIA, then returns a human fallback message.
    
    Returns:
        {"text": "the response", "engine": "gemini" or "nvidia:model" or "none"}
    """
    
    # =============================================
    # 1. Try Gemini (Primary)
    # =============================================
    if gemini_available:
        try:
            result = call_gemini(prompt, user_msg, history_text)
            if result:
                app.logger.info("✅ Gemini response successful")
                return {"text": result, "engine": "gemini"}
        except Exception as e:
            app.logger.warning(f"⚠️ Gemini failed: {e}")
    
    # =============================================
    # 2. Try NVIDIA (Backup)
    # =============================================
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
    
    # =============================================
    # 3. Human Fallback (if all AIs fail)
    # =============================================
    return {
        "text": "I'm having trouble connecting. Please call (473) 440-6647 or visit Melville St, St George's for help.",
        "engine": "none",
    }

# =============================================
# 🌐 API ENDPOINTS - The web addresses the frontend calls
# =============================================
# These are the "doors" that the chat page uses to talk to the server.

@app.route("/")
def index():
    """Serves the main chat page (index.html)."""
    try:
        return send_from_directory("static", "index.html")
    except Exception:
        return "Error: index.html not found in static folder.", 404

@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serves static files like images and CSS."""
    return send_from_directory("static", filename)

@app.route("/api/demo/chat", methods=["POST", "OPTIONS"])
def chat():
    """
    The MAIN chat endpoint.
    This is called when the user sends a message.
    
    It:
    1. Checks for distress
    2. Checks for personal questions
    3. Detects language, persona, sentiment
    4. Gets a response from the AI
    5. Returns the response to the frontend
    """
    if request.method == "OPTIONS":
        return "", 204  # Handle CORS preflight

    # Get the user's message from the request
    data = request.get_json(silent=True)
    if not data or not data.get("message", "").strip():
        return jsonify({"ok": False, "error": "Message required"}), 400

    raw_message = data["message"].strip()
    session_id = data.get("session_id", str(uuid.uuid4()))
    requested_language = data.get("language", "en")
    requested_persona = data.get("persona", "individual")
    
    # Remove any personal info before processing
    safe_message = redact_pii(raw_message)
    
    # =============================================
    # 1. 🚨 Check for distress
    # =============================================
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
    
    # =============================================
    # 2. 🛡️ Check authority (personal questions)
    # =============================================
    if not check_authority(safe_message):
        add_to_history(session_id, "user", safe_message)
        reply = "For personal case questions, please call (473) 440-6647 or visit Melville St, St George's."
        add_to_history(session_id, "bot", reply)
        return jsonify({
            "ok": True,
            "reply": reply,
            "session_id": session_id,
            "escalated": True,
        })
    
    # =============================================
    # 3. 🌐 Get language preference
    # =============================================
    lang = requested_language if requested_language != "en" else get_language(session_id, safe_message)
    session_languages[session_id] = lang
    
    # =============================================
    # 4. 👤 Get persona (user type)
    # =============================================
    if requested_persona and requested_persona in PERSONAS:
        session_personas[session_id] = requested_persona
    elif requested_persona not in PERSONAS:
        detected = detect_persona(safe_message)
        if detected:
            session_personas[session_id] = detected
    persona = session_personas.get(session_id, "individual")
    
    # =============================================
    # 5. 🧠 Analyze sentiment (how they're feeling)
    # =============================================
    sentiment = analyze_sentiment(safe_message)
    
    # =============================================
    # 6. 🧭 Advance journey step
    # =============================================
    advance_journey(session_id)
    journey_step = get_journey_step(session_id)
    
    # =============================================
    # 7. 🗺️ Detect territory
    # =============================================
    territory = detect_territory(safe_message)
    
    # =============================================
    # 8. 📝 Check if they're in a task
    # =============================================
    task = get_task(session_id)
    if not task and any(w in safe_message.lower() for w in ["claim", "apply", "register", "form"]):
        task_type = "claim" if "claim" in safe_message.lower() else "registration"
        task = start_task(session_id, task_type)
    
    # =============================================
    # 9. 📋 Find relevant form link
    # =============================================
    matched_form = find_relevant_form(safe_message)
    
    # =============================================
    # 10. Get conversation history
    # =============================================
    history_text = get_history_text(session_id)
    
    # =============================================
    # 11. 📝 Build the prompt
    # =============================================
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
        history_text=history_text,
        user_message=safe_message,
    )
    
    # =============================================
    # 12. 🤖 Get AI response (with fallback)
    # =============================================
    result = safe_call(
        call_llm_with_fallback,
        prompt,
        safe_message,
        history_text,
        fallback={"text": "Please call (473) 440-6647 for help.", "engine": "none"},
    )
    reply = result["text"]
    engine_used = result["engine"]
    
    # =============================================
    # 13. 📋 Add form link if relevant
    # =============================================
    if matched_form:
        reply += f"\n\nForm: {matched_form['label']} - {matched_form['url']}"
    
    # =============================================
    # 14. 💾 Save to history
    # =============================================
    add_to_history(session_id, "user", safe_message)
    add_to_history(session_id, "bot", reply)
    
    # =============================================
    # 15. 📊 Return response
    # =============================================
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
    """
    Saves user ratings (star ratings).
    Called when the user clicks a star.
    """
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
    """
    Text-to-Speech endpoint.
    Converts text to speech using ElevenLabs API.
    """
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

@app.route("/api/health")
def health_check():
    """
    Health check endpoint.
    Used to verify the server is running properly.
    """
    return jsonify({
        "ok": True,
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "gemini_available": gemini_available,
        "nvidia_available": bool(NVIDIA_KEY),
        "active_sessions": len(conversation_histories),
        "active_tasks": len(active_tasks),
    })

# =============================================
# 🔧 CORS - Allow frontend to talk to backend
# =============================================
@app.after_request
def cors(response):
    """Adds CORS headers to every response."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

# =============================================
# 🚀 RUN THE SERVER
# =============================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
