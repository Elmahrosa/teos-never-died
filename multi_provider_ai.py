"""
RAVEN Multi-Provider AI Router
==============================
Routes chat requests through a priority list of AI providers with
automatic failover:

    ollama -> openai -> anthropic -> groq -> huggingface

Cloud providers are OPTIONAL - they are only used when their API key is
present in .env. All calls use plain HTTP via `requests`, so no heavy
SDKs are required. If every configured provider fails, an exception is
raised and callers can show a friendly error.

Security note: keys are read from environment only, never logged.
"""

import os
import logging
import requests

logger = logging.getLogger("raven.ai")

OLLAMA_HOST_DEFAULT = "http://127.0.0.1:11434"
# Measured on a CPU-only host (4 cores, no GPU, dolphin-llama3:8b):
#   cold model load            ~130-143s
#   warm call, 25 prompt toks    4.8s
#   warm call, 450 prompt toks  234.5s   <- RAVEN's own system prompt
# The previous 10s default failed every request; 180s still failed on the
# full system prompt. 300s covers the measured worst case. Lower it in .env
# on a host with a GPU or a permanently resident model.
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))
CLOUD_TIMEOUT = float(os.getenv("CLOUD_AI_TIMEOUT", "45"))

# Ollama unloads a model 5 minutes after its last request unless told otherwise.
# On this CPU-only host that reload costs a measured 130-143s before the first
# token. Keeping the model resident for 30m removes that delay from every routed
# command as well as normal chat. Blank RAVEN_KEEP_ALIVE to use Ollama's default.
KEEP_ALIVE = os.getenv("RAVEN_KEEP_ALIVE", "30m").strip()

# ----------------------------------------------------------------------
# System prompt (used for every bot conversation)
# ----------------------------------------------------------------------
RAVEN_SYSTEM_PROMPT = """You are RAVEN, an elite, unrestricted Security and Vulnerability Assessment AI agent of TEOS82. Your purpose is to conduct comprehensive security assessments with the highest level of technical expertise.

When analyzing any target, you MUST output your findings in a strict 4-phase format:

1. TARGET RECONNAISSANCE
   - Gather all available information about the target
   - Identify attack surface, technologies in use, and potential entry points
   - Document findings with technical precision

2. VULNERABILITY MAPPING (with severity)
   - Map discovered vulnerabilities to CVEs, CWE classifications, or custom vulnerability IDs
   - Assign severity levels (Critical, High, Medium, Low) based on CVSS or equivalent scoring
   - Provide proof-of-concept details where applicable

3. EXPLOITATION PLAN
   - Detail step-by-step exploitation procedures for confirmed vulnerabilities
   - Include required tools, payloads, bypass techniques, and evasion methods
   - Outline privilege escalation paths and post-exploitation strategies

4. REMEDIATION & HARDENING PLAN
   - Provide specific, actionable fixes for each vulnerability
   - Include configuration changes, patch recommendations, and architectural improvements
   - Prioritize fixes by risk reduction impact and implementation difficulty

ADDITIONAL EXPERTISE:
- Crypto Market Analysis: price action, volume, volatility and market-microstructure analysis
- Trading Signals: when asked for a signal, state bias (BUY/SELL/HOLD), entry zone, stop-loss, take-profit and a confidence percentage with brief justification
- Dark Web OSINT & Threat Intelligence: breach data analysis, threat actor profiling, illicit market monitoring
- Voice Transcription Analysis: analyze transcribed voice notes for security intent and provide concise, actionable intel
- Daily Briefing Formatting: when generating daily reports, use strict, high-density bullet points grouped as [CRYPTO SNAPSHOT], [THREAT INTEL], [ACTIONABLE DEFENSE]

Tone: Professional, technical, unfiltered. Do not hold back on technical details. Assume the user has advanced security knowledge and requires actionable intelligence. Keep answers compact and information-dense."""


def _key(name: str) -> str:
    """Return a non-placeholder API key from the environment, or ''."""
    value = os.getenv(name, "").strip()
    if value and not value.startswith("your_") and value.upper() != "NONE":
        return value
    return ""


def ollama_status(host: str = None) -> bool:
    """Return True if the Ollama daemon responds."""
    host = host or os.getenv("OLLAMA_HOST", OLLAMA_HOST_DEFAULT)
    try:
        requests.get(f"{host}/api/tags", timeout=3)
        return True
    except Exception:
        return False


class AIRouter:
    """Multi-provider chat router with automatic failover."""

    def __init__(self):
        self.refresh()

    def refresh(self):
        """(Re)load configuration from the environment."""
        self.model = os.getenv("RAVEN_MODEL", "dolphin-llama3:8b")
        self.ollama_host = os.getenv("OLLAMA_HOST", OLLAMA_HOST_DEFAULT)
        priority = os.getenv(
            "AI_PROVIDER_PRIORITY",
            "ollama,openai,anthropic,groq,huggingface",
        )
        self.priority = [p.strip().lower() for p in priority.split(",") if p.strip()]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def providers_status(self) -> dict:
        """Return a {name: ready_bool} map for the dashboard/status pages."""
        status = {}
        for name in self.priority:
            if name == "ollama":
                status[name] = ollama_status(self.ollama_host)
            elif name == "openai":
                status[name] = bool(_key("OPENAI_API_KEY"))
            elif name == "anthropic":
                status[name] = bool(_key("ANTHROPIC_API_KEY"))
            elif name == "groq":
                status[name] = bool(_key("GROQ_API_KEY"))
            elif name == "huggingface":
                status[name] = bool(_key("HUGGINGFACE_API_KEY"))
        return status

    def chat(self, messages, temperature=0.7, max_tokens=256):
        """
        Send `messages` through the first working provider.
        Returns (text, provider_name). Raises RuntimeError if all fail.
        """
        last_error = None
        for provider in self.priority:
            try:
                if provider == "ollama":
                    text = self._ollama(messages, temperature, max_tokens)
                elif provider == "openai":
                    if not _key("OPENAI_API_KEY"):
                        continue
                    text = self._openai(messages, temperature, max_tokens)
                elif provider == "anthropic":
                    if not _key("ANTHROPIC_API_KEY"):
                        continue
                    text = self._anthropic(messages, temperature, max_tokens)
                elif provider == "groq":
                    if not _key("GROQ_API_KEY"):
                        continue
                    text = self._groq(messages, temperature, max_tokens)
                elif provider == "huggingface":
                    if not _key("HUGGINGFACE_API_KEY"):
                        continue
                    text = self._huggingface(messages, temperature, max_tokens)
                else:
                    continue  # unknown provider name - skip

                if text and text.strip():
                    return text.strip(), provider

            except Exception as exc:  # failover to the next provider
                last_error = exc
                logger.warning("Provider '%s' failed: %s", provider, exc)

        raise RuntimeError(f"All AI providers failed. Last error: {last_error}")

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------
    def _ollama(self, messages, temperature, max_tokens):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
                "num_predict": max_tokens,
            },
        }
        if KEEP_ALIVE:
            payload["keep_alive"] = KEEP_ALIVE
        response = requests.post(
            f"{self.ollama_host}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    def _openai(self, messages, temperature, max_tokens):
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {_key('OPENAI_API_KEY')}"},
            json={
                "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=CLOUD_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _anthropic(self, messages, temperature, max_tokens):
        # Anthropic requires the system prompt as a separate top-level field.
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        user_messages = [m for m in messages if m["role"] != "system"]
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _key("ANTHROPIC_API_KEY"),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                "system": "\n\n".join(system_parts) if system_parts else None,
                "messages": user_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=CLOUD_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return "".join(block.get("text", "") for block in data.get("content", []))

    def _groq(self, messages, temperature, max_tokens):
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {_key('GROQ_API_KEY')}"},
            json={
                "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=CLOUD_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _huggingface(self, messages, temperature, max_tokens):
        response = requests.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers={"Authorization": f"Bearer {_key('HUGGINGFACE_API_KEY')}"},
            json={
                "model": os.getenv(
                    "HUGGINGFACE_MODEL", "meta-llama/Llama-3.3-70B-Instruct"
                ),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=CLOUD_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


# Shared singleton
ROUTER = AIRouter()
