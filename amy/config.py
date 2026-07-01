import os
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
APP_NAME = "PersonalOS"
ASSISTANT_NAME = "Amy"
TAGLINE = "Your Multi-Agent AI Operating System"
MODE = os.getenv("PERSONALOS_MODE", "personal").strip().lower()
MODE = MODE if MODE in ("personal", "public") else "personal"
try:
    from dotenv import load_dotenv
    # mode file provides mode-specific defaults; .env (gitignored) provides secrets.
    # override=False -> first value wins; mode file has NO secrets, so .env keys survive.
    mf = HERE / f".env.{MODE}"
    if mf.exists():
        load_dotenv(mf, override=False)
    if (HERE / ".env").exists():
        load_dotenv(HERE / ".env", override=False)
except Exception:
    pass
MODE = os.getenv("PERSONALOS_MODE", MODE).strip().lower()
MODE = MODE if MODE in ("personal", "public") else "personal"
PUBLIC = MODE == "public"
def _env(n, d=""):
    v = os.getenv(n)
    if v is None: return d
    v = v.strip()
    return d if v.startswith("#") else v
VAULT = Path(_env("AMY_VAULT", str(HERE / "demo_vault") if PUBLIC else str(HERE.parent)))
GROQ_API_KEY = _env("GROQ_API_KEY"); GROQ_MODEL = _env("AMY_GROQ_MODEL", "llama-3.3-70b-versatile")
OPENAI_API_KEY = _env("OPENAI_API_KEY"); OPENAI_MODEL = _env("AMY_OPENAI_MODEL", "gpt-4o-mini")
OLLAMA_HOST = _env("OLLAMA_HOST", "http://localhost:11434"); OLLAMA_MODEL = _env("AMY_OLLAMA_MODEL", "llama3.2")
EMBED_MODEL = _env("AMY_EMBED_MODEL", "nomic-embed-text"); EMBED_BACKEND = _env("AMY_EMBED_BACKEND", "ollama")
# Knowledge embeddings provider: auto | nvidia | openai | st | hashing
# auto order: nvidia (if key) -> openai (if key) -> sentence-transformers -> hashing
EMBED_PROVIDER = _env("AMY_EMBED_PROVIDER", "auto")
NVIDIA_API_KEY = _env("AMY_NVIDIA_API_KEY") or _env("NVIDIA_API_KEY")
NVIDIA_BASE_URL = _env("AMY_NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_EMBED_MODEL = _env("AMY_NVIDIA_EMBED_MODEL", "nvidia/nv-embedqa-e5-v5")
ST_EMBED_MODEL = _env("AMY_ST_EMBED_MODEL", "all-MiniLM-L6-v2")
ABSTAIN_EMB_THRESHOLD = float(_env("AMY_ABSTAIN_EMB_THRESHOLD", "0.2"))
GENERAL_PROVIDER_ORDER = [p.strip() for p in _env("AMY_PROVIDER_ORDER", "openai,groq,ollama").split(",") if p.strip()]
AUTH_TOKEN = _env("AMY_AUTH_TOKEN")
# SaaS / multi-vault mode: build agents dynamically from the user's own top-level
# folders instead of the hardcoded personal layout. Off by default (personal vault
# keeps its tailored agents).
DYNAMIC_AGENTS = _env("AMY_DYNAMIC_AGENTS", "").lower() in ("1", "true", "yes", "on")
FEATURES = {"write": not PUBLIC, "scheduler": not PUBLIC, "sensitive": not PUBLIC, "vault_edit": not PUBLIC, "voice": True}
ALL_AGENTS = ["home", "profile", "projects", "family", "finances", "career", "resources", "jobsearch", "knowledge", "captures"]
BLOCKED_AGENTS = ["family", "finances"] if PUBLIC else []
ALLOWED_AGENTS = [a for a in ALL_AGENTS if a not in BLOCKED_AGENTS]
SENSITIVE_PATH_MARKERS = ["02_Family/Sathish Appa/SBI Account"]
SENSITIVE_OWNERS = ["Sathish Appa"]
SENSITIVE_TAGS = ["sensitive"]
AGENT_SCOPES = {"home": ["00_Home"], "profile": ["01_Profile"], "projects": ["01_Profile/Projects"], "family": ["02_Family"], "finances": ["03_Finances"], "career": ["04_Career"], "resources": ["05_Resources"], "jobsearch": ["06_Job_Search"], "knowledge": ["07_Knowledge"], "captures": ["08_Captures"]}
BLOCKED_ACTION_VERBS = ["pay ", "send money", "transfer", "withdraw", "delete", "remove"]
INDEX_DIR = HERE / (".index_public" if PUBLIC else ".amy_index")
VOICES_FILE = HERE / "voices.json"
PREFS_FILE = HERE / ".personalos_prefs.json"
VOICES_MODELS_DIR = HERE / "voices_models"
