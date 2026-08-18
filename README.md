# 🤖 Nissy — NIS Grenada AI Digital Assistant

> **Built for the ECCU / ECCB Generative AI & Python Summer Camp 2026**  
> *Client: National Insurance Board (NIS), Grenada*

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000.svg)](https://flask.palletsprojects.com/)
[![Render](https://img.shields.io/badge/Deployed%20on-Render-45E075.svg)](https://render.com/)
[![Gemini](https://img.shields.io/badge/AI-Gemini-4285F4.svg)](https://ai.google.dev/)
[![ElevenLabs](https://img.shields.io/badge/TTS-ElevenLabs-FF6B00.svg)](https://elevenlabs.io/)

---

## 🌟 Overview

**Nissy** is an intelligent, accessibility-focused AI chatbot designed to help citizens of Grenada navigate the National Insurance Scheme (NIS). Instead of waiting on hold or visiting the office, users can instantly learn about pensions, survivors benefits, sickness and unemployment benefits, funeral grants, and more — all through a natural conversation.

### 🧭 The Golden Rule
> *"The Bot is the GPS. The Human is the Driver."*

Nissy is designed with a strict **Autonomy Ceiling**. It guides, informs, and qualifies users, but it **never** handles personal case information, quotes specific benefits eligibility, or stores personal data. Those matters are safely escalated to human experts at the NIS office.

---

## 🚀 Key Features

| Feature | Description |
|---------|-------------|
| 🛡️ **A.R.T. Guardrails** | Authority, Register, and Territory classification ensures safe, appropriate responses |
| 🔒 **PII Redaction** | Automatically detects and redacts personal information (NIN, phone, email) |
| 🧠 **Multi-AI Fallback** | Primary: Google Gemini 2.0 Flash Lite \| Fallback: NVIDIA Llama 3.1 8B + Nemotron Mini 4B |
| 💬 **Conversation Memory** | Remembers the last 6 exchanges for contextual follow-up questions |
| 🧭 **Agentic Journey** | 5-step conversation flow: Greeting → Identify Need → Collect Facts → Offer Next Step → Confirm Close |
| 🎨 **5 Themes** | Dark, Light, Purple, Ocean, and Amber — user-selectable |
| 🎙️ **Voice I/O** | Text-to-Speech (ElevenLabs) and Speech-to-Text (Web Speech API) |
| ⭐ **Rating System** | 5-star feedback system with optional comments |
| 📋 **FAQ & Contact Pages** | Static pages with common questions and contact form |
| 🌐 **Multi-Language** | Detects and responds in English, Spanish, French, or Kwéyòl |
| 🚨 **Crisis Support** | Detects distress and provides local Grenada helplines |

---

## 🛠️ Tech Stack

| Category | Technologies Used |
| :--- | :--- |
| **Backend** | Python 3.10+, Flask, Gunicorn |
| **Primary AI** | Google Gemini 2.0 Flash Lite (`google-generativeai`) |
| **Fallback AI** | NVIDIA NIM (Llama 3.1 8B + Nemotron Mini 4B) |
| **Text-to-Speech** | ElevenLabs API |
| **Speech-to-Text** | Web Speech API (browser-based) |
| **Deployment** | Render (Web Service) |
| **Frontend** | HTML5, CSS3 (Custom Variables), Vanilla JavaScript |

---

## 📁 Project Structure

