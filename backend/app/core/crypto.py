"""
Symmetric encryption for secrets at rest (WhatsApp access token, etc.).

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key lives in backend/.env as
APP_ENC_KEY. If absent, a key is generated once and persisted so decryption
survives restarts.

Stored format: 'enc:<fernet-token>'. Anything WITHOUT that prefix is treated as
plaintext (back-compat / a value typed straight into the DB) and returned as-is,
so the module is safe to introduce over existing plaintext columns.
"""
import os
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from typing import Optional

_ENC_PREFIX = "enc:"
_KEY_NAME = "APP_ENC_KEY"
# app/core/crypto.py -> core -> app -> backend/
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENV_FILE = os.path.join(_BACKEND_ROOT, ".env")

_fernet: Optional[Fernet] = None


def _load_or_create_key() -> bytes:
    """Resolve APP_ENC_KEY from the environment, then .env, else generate and
    persist a fresh key to .env. Returns the raw 44-byte urlsafe-base64 key."""
    key = os.environ.get(_KEY_NAME)
    if key:
        return key.encode() if isinstance(key, str) else key

    # The process env may not carry .env (loaded lazily) — read the file directly.
    if os.path.exists(_ENV_FILE):
        try:
            with open(_ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith(_KEY_NAME + "="):
                        val = s.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            os.environ[_KEY_NAME] = val
                            return val.encode()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[crypto] could not read {_KEY_NAME} from .env: {e}")

    new_key = Fernet.generate_key()
    try:
        with open(_ENV_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"\n# App encryption key (auto-generated) — keep secret; "
                f"rotating it makes existing encrypted secrets undecryptable.\n"
                f"{_KEY_NAME}={new_key.decode()}\n"
            )
        logger.info(f"[crypto] generated new {_KEY_NAME} and persisted to .env")
    except Exception as e:
        logger.warning(f"[crypto] generated {_KEY_NAME} but could NOT persist it "
                       f"to .env ({e}); encrypted values will not survive restart")
    os.environ[_KEY_NAME] = new_key.decode()
    return new_key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Empty → ''. Already-encrypted → returned
    unchanged (idempotent), so re-saving a mask-preserved value is a no-op."""
    if not plaintext:
        return ""
    if isinstance(plaintext, str) and plaintext.startswith(_ENC_PREFIX):
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode())
    return _ENC_PREFIX + token.decode()


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored secret. Empty → ''. Plaintext (no prefix) → returned
    as-is. A corrupt/foreign-key value → '' (logged), never raises."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored
    try:
        return _get_fernet().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("[crypto] decrypt failed — key mismatch or corrupt value")
        return ""


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(_ENC_PREFIX)
