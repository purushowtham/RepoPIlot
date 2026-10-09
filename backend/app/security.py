import base64
import hashlib
import hmac
import os
import re
import time
from .config import DATA, LLM_KEY, GITHUB_TOKEN
from .connections import settings

# A persistent local signing key; deployments provide AUTH_SECRET.
key_path = DATA / "auth.key"
if not os.getenv("AUTH_SECRET") and not key_path.exists():
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(os.urandom(32).hex())
    except FileExistsError:
        pass
SECRET = os.getenv("AUTH_SECRET") or key_path.read_text()


def password_hash(password):
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()
    return f"{salt}:{digest}"


def password_ok(password, encoded):
    salt, digest = encoded.split(":")
    return hmac.compare_digest(
        digest, hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()
    )


def token(user_id):
    body = f"{user_id}:{int(time.time()) + 86400}"
    sig = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}:{sig}".encode()).decode()


def verify(value):
    try:
        user, expiry, sig = base64.urlsafe_b64decode(value).decode().split(":")
        expected = hmac.new(SECRET.encode(), f"{user}:{expiry}".encode(), hashlib.sha256).hexdigest()
        if int(expiry) > time.time() and hmac.compare_digest(sig, expected):
            return user
    except (ValueError, UnicodeError):
        pass
    return None


def redact(value):
    text = str(value)
    configured = settings()
    for secret in (LLM_KEY, GITHUB_TOKEN, SECRET, configured["llm_api_key"], configured["github_token"]):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})", "[REDACTED]", text
    )
    return re.sub(r"(?im)((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
