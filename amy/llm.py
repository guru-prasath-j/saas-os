"""Hybrid LLM router.

General queries  -> Groq, then OpenAI, then local Ollama, then template fallback.
Sensitive queries -> LOCAL Ollama only (never a cloud API). If Ollama is down,
                     uses the deterministic template (still local) — cloud is never used.

Every provider degrades gracefully if its SDK/key is missing.
"""
from __future__ import annotations
from . import config


class TemplateLLM:
    name = "template"
    def generate(self, system: str, prompt: str, context: str = "") -> str:
        ctx = context.strip()
        if not ctx:
            return "I don't have anything in the vault for that yet."
        return "Based on the vault:\n" + "\n".join(ctx.splitlines()[:12])


class GroqLLM:
    name = "groq"
    def __init__(self):
        from groq import Groq
        if not config.GROQ_API_KEY:
            raise RuntimeError("no GROQ_API_KEY")
        self._c = Groq(api_key=config.GROQ_API_KEY)
        self._model = config.GROQ_MODEL

    def generate(self, system, prompt, context=""):
        r = self._c.chat.completions.create(model=self._model, max_tokens=450, messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\n# Context\n{context}"},
        ])
        return r.choices[0].message.content


class OpenAILLM:
    name = "openai"
    def __init__(self, api_key=None):
        from openai import OpenAI
        key = api_key or config.OPENAI_API_KEY
        if not key:
            raise RuntimeError("no OPENAI_API_KEY")
        self._c = OpenAI(api_key=key)
        self._model = config.OPENAI_MODEL

    def generate(self, system, prompt, context=""):
        r = self._c.chat.completions.create(model=self._model, max_tokens=450, messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\n# Context\n{context}"},
        ])
        return r.choices[0].message.content


class OllamaLLM:
    name = "ollama"
    def __init__(self):
        import ollama
        self._c = ollama.Client(host=config.OLLAMA_HOST)
        self._c.list()  # raises if daemon down
        self._model = config.OLLAMA_MODEL

    def generate(self, system, prompt, context=""):
        r = self._c.chat(model=self._model, messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\n# Context\n{context}"},
        ])
        return r["message"]["content"]


class NvidiaLLM:
    """NVIDIA NIM API — Nemotron thinking model via OpenAI-compatible endpoint."""
    name = "nvidia"

    def __init__(self, api_key: str | None = None):
        from openai import OpenAI
        key = api_key or config.NVIDIA_API_KEY
        if not key:
            raise RuntimeError("no NVIDIA_API_KEY")
        self._c = OpenAI(base_url=config.NVIDIA_BASE_URL, api_key=key)

    def generate(self, system: str, prompt: str, context: str = "") -> str:
        user_msg = f"{prompt}\n\n# Context\n{context}" if context.strip() else prompt
        stream = self._c.chat.completions.create(
            model="nvidia/nemotron-3-ultra-550b-a55b",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            top_p=0.95,
            max_tokens=4096,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": True},
                "reasoning_budget": 4096,
            },
            stream=True,
        )
        parts: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                parts.append(delta.content)
        return "".join(parts)


_PROVIDERS = {"groq": GroqLLM, "openai": OpenAILLM, "ollama": OllamaLLM, "nvidia": NvidiaLLM}


class LLMRouter:
    """Routes generation across providers.

    Personal app: use_global_keys=True -> uses keys from config/.env as today.
    SaaS (BYO-key): use_global_keys=False + openai_api_key=<user's key> -> uses ONLY
    the user's OpenAI key (never a shared cloud key); groq is disabled (no per-user
    groq key yet); local Ollama is still allowed (it's the server's local model, not
    a shared secret), and the template fallback always works.
    """
    def __init__(self, openai_api_key=None, use_global_keys=True):
        self._openai_key = openai_api_key
        self._use_global = use_global_keys
        self._fallback = TemplateLLM()
        self._cache: dict[str, object] = {}
        for name in set(config.GENERAL_PROVIDER_ORDER) | {"ollama"}:
            self._cache[name] = self._build(name)

    def _build(self, name):
        try:
            if name == "openai":
                key = self._openai_key or (config.OPENAI_API_KEY if self._use_global else None)
                return OpenAILLM(key) if key else None
            if name == "groq":
                return GroqLLM() if self._use_global else None
            if name == "ollama":
                return OllamaLLM()
            if name == "nvidia":
                return NvidiaLLM() if config.NVIDIA_API_KEY else None
        except Exception:
            return None
        return None

    def _first_live(self, order):
        for name in order:
            inst = self._cache.get(name)
            if inst is not None:
                return inst
        return self._fallback

    def pick(self, sensitive: bool):
        if sensitive:
            # privacy: sensitive data stays local. Ollama only; never cloud.
            return self._cache.get("ollama") or self._fallback
        return self._first_live(config.GENERAL_PROVIDER_ORDER)

    def status(self) -> dict:
        s = {name: (self._cache.get(name) is not None) for name in _PROVIDERS}
        s["fallback"] = True
        s["general_order"] = config.GENERAL_PROVIDER_ORDER
        return s

    def generate(self, system, prompt, context="", sensitive=False):
        llm = self.pick(sensitive)
        return llm.generate(system, prompt, context), llm.name
