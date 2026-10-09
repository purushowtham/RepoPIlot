import os
from pathlib import Path

DATA = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA}/repopilot.db")
MODE = os.getenv("REPOPILOT_MODE", "live")
if MODE not in {"demo", "live"}:
    raise ValueError("REPOPILOT_MODE must be demo or live")
MAX_REPAIRS = 2
MAX_PATCH_BYTES = 100_000
MAX_FILES = 10
SANDBOX_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT_SECONDS", "180"))
RUN_TIMEOUT = int(os.getenv("RUN_TIMEOUT_SECONDS", "600"))
MODEL = os.getenv("LLM_MODEL", "")
LLM_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", os.getenv("GH_TOKEN", ""))
LLM_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "repopilot-sandbox:local")
