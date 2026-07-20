import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

# On Streamlit Community Cloud there's no .env file -- secrets are configured
# in the app's "Secrets" settings instead, exposed via st.secrets. Bridge them
# into os.environ so the rest of this module (and the whole codebase) can keep
# reading plain environment variables either way.
try:
    import streamlit as st

    for key, value in st.secrets.items():
        os.environ.setdefault(key, str(value))
except Exception:
    pass

META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_AD_ACCOUNT_ID = os.environ["META_AD_ACCOUNT_ID"]
# Comma-separated extra ad account IDs (same product, different ad accounts/business portfolios)
META_AD_ACCOUNT_IDS = [META_AD_ACCOUNT_ID] + [
    a.strip() for a in os.environ.get("META_EXTRA_AD_ACCOUNT_IDS", "").split(",") if a.strip()
]

# The dashboard only needs the META_* credentials above (read-only DB browsing +
# one live "which adsets are active" call). Everything below is only used by the
# collector/notification scripts, so it's optional here to keep the dashboard
# deployable with just Meta secrets configured.
COUPANG_ACCESS_KEY = os.environ.get("COUPANG_ACCESS_KEY", "")
COUPANG_SECRET_KEY = os.environ.get("COUPANG_SECRET_KEY", "")
COUPANG_VENDOR_ID = os.environ.get("COUPANG_VENDOR_ID", "")

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL_ACCOUNTS = os.environ.get("DISCORD_WEBHOOK_URL_ACCOUNTS", "")
DISCORD_WEBHOOK_URL_ORDERS = os.environ.get("DISCORD_WEBHOOK_URL_ORDERS", "")
DISCORD_WEBHOOK_URL_TESTBED = os.environ.get("DISCORD_WEBHOOK_URL_TESTBED", "")
TESTBED_AD_ACCOUNT_ID = os.environ.get("TESTBED_AD_ACCOUNT_ID", "")

CAFE24_CLIENT_ID = os.environ.get("CAFE24_CLIENT_ID", "")
CAFE24_CLIENT_SECRET = os.environ.get("CAFE24_CLIENT_SECRET", "")
CAFE24_MALL_ID = os.environ.get("CAFE24_MALL_ID", "")
CAFE24_REDIRECT_URI = os.environ.get("CAFE24_REDIRECT_URI", "")
CAFE24_REFRESH_TOKEN = os.environ.get("CAFE24_REFRESH_TOKEN", "")

# Break-even ROAS for 알파셀 올나잇 세이프 -- at this ROAS, ad spend + COGS/fees
# exactly offset revenue (profit = 0). Used to estimate net profit from actual ROAS.
BEP_ROAS = 1.6

ENV_PATH = ROOT_DIR / ".env"

DB_PATH = ROOT_DIR / "data" / "roas.db"


def get_env_value(key: str, default: str = "") -> str:
    """Re-reads a single key's current value straight from the .env file on disk,
    bypassing the cached module attribute. Needed for CAFE24_REFRESH_TOKEN: Cafe24
    rotates the refresh token on every use, and a long-running process (the
    realtime watcher) must not refresh using a stale in-memory copy that another
    process (the daily pipeline cron) may have already rotated on disk."""
    if not ENV_PATH.exists():
        return default
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
    return default


def update_env_value(key: str, value: str):
    """Persist a single key=value into the .env file, used for rotating refresh tokens.
    Also syncs to GitHub Actions Variable if GH_PAT + GITHUB_REPOSITORY are available,
    so the cloud order watcher always has the latest token."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # GitHub Variable 동기화 — 로컬 daily pipeline이 토큰 rotate 후 클라우드와 동기화
    gh_pat = os.environ.get("GH_PAT", "")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if gh_pat and gh_repo:
        try:
            import requests as _req
            _h = {"Authorization": f"Bearer {gh_pat}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            _base = f"https://api.github.com/repos/{gh_repo}/actions/variables"
            _r = _req.patch(f"{_base}/{key}", headers=_h, json={"name": key, "value": value}, timeout=10)
            if _r.status_code == 404:
                _req.post(_base, headers=_h, json={"name": key, "value": value}, timeout=10)
        except Exception:
            pass
