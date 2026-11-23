from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def generate_fernet_key() -> str:
    """Generate a new URL-safe base64-encoded Fernet key."""
    return Fernet.generate_key().decode("utf-8")


def _get_fernet(key: str | None = None) -> Fernet:
    raw_key = key if key is not None else get_settings().token_encryption_key
    if not raw_key or not raw_key.strip():
        raise ValueError("TOKEN_ENCRYPTION_KEY is not configured")
    return Fernet(raw_key.strip().encode("utf-8"))


def encrypt_token(plaintext: str, key: str | None = None) -> str:
    """Encrypt a secret token string using Fernet."""
    fernet = _get_fernet(key)
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str, key: str | None = None) -> str:
    """Decrypt a Fernet-encrypted token string."""
    fernet = _get_fernet(key)
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Invalid token ciphertext or encryption key") from exc
