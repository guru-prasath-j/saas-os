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
        self._c = Groq(api_key=config.GROQ_API_KEY, timeout=45.0, max_retries=0)
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
        self._c = OpenAI(api_key=key, timeout=45.0, max_retries=0)
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
        # No timeout here previously — a wedged local daemon/runner (e.g.
        # after heavy concurrent load) hung the calling HTTP request
        # forever with no error, since nothing ever raised. 120s is
        # generous for CPU-only local inference on a long prompt but still
        # bounded; callers already wrap generate() in try/except and
        # degrade gracefully, same as the NvidiaLLM timeout below.
        self._c = ollama.Client(host=config.OLLAMA_HOST, timeout=120.0)
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
        # Thinking mode + a 4096-token reasoning budget can legitimately take a
        # while, but must still have a ceiling — otherwise a slow/overloaded
        # endpoint hangs the whole request (openai SDK default is 10 minutes).
        # max_retries=0: the SDK default of 2 silent retries turned one 75s
        # timeout into ~290s observed; callers (orchestrator/_gen, router
        # fallback) already do their own retry/degrade.
        self._c = OpenAI(base_url=config.NVIDIA_BASE_URL, api_key=key,
                         timeout=75.0, max_retries=0)

    def generate(self, system: str, prompt: str, context: str = "",
                 fast: bool = False) -> str:
        # fast=True: thinking OFF + small output cap. Agent step-loops
        # (orchestrator/assistant) make 6-13 sequential calls per run and each
        # only needs a one-line JSON decision — measured median 46s/call with
        # thinking on vs seconds without. Quality-sensitive batch jobs (gmail
        # enrich, budget suggestions) keep the default deep-reasoning mode.
        user_msg = f"{prompt}\n\n# Context\n{context}" if context.strip() else prompt
        stream = self._c.chat.completions.create(
            model="nvidia/nemotron-3-ultra-550b-a55b",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            top_p=0.95,
            max_tokens=700 if fast else 4096,
            extra_body={
                "chat_template_kwargs": {"enable_thinking": not fast},
                **({} if fast else {"reasoning_budget": 4096}),
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

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        llm = self.pick(sensitive)
        if fast:
            try:
                return llm.generate(system, prompt, context, fast=True), llm.name
            except TypeError:
                pass   # provider without a fast path — normal call below
        return llm.generate(system, prompt, context), llm.name
