"""Application users, password hashing, and signed browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db import load_or_migrate, save_store


PASSWORD_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
USERNAME_PATTERN = re.compile(r"^[\w.-]{3,32}$", re.UNICODE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _password_hash(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return _encode(digest)


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"users": []}
        saved = load_or_migrate(self.path, kind="users")
        if isinstance(saved, dict) and isinstance(saved.get("users"), list):
            self.data["users"] = saved["users"]

    def count(self) -> int:
        with self.lock:
            return len(self.data["users"])

    def register(
        self,
        username: str,
        password: str,
        display_name: str = "",
        *,
        role: str | None = None,
        enabled: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        normalized = self._normalize_username(username)
        self._validate_password(password)
        with self.lock:
            if self._find_username(normalized):
                raise ValueError("用户名已存在")
            first_user = not self.data["users"]
            assigned_role = "admin" if first_user else (role or "user")
            if assigned_role not in {"admin", "user"}:
                raise ValueError("用户角色无效")
            salt = secrets.token_bytes(16)
            timestamp = now_iso()
            item = {
                "id": f"usr-{uuid.uuid4().hex[:16]}",
                "username": normalized,
                "display_name": display_name.strip() or username.strip(),
                "role": assigned_role,
                "enabled": bool(enabled),
                "password_salt": _encode(salt),
                "password_hash": _password_hash(password, salt),
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_login_at": None,
            }
            self.data["users"].append(item)
            self._save()
            return self._public(item), first_user

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        normalized = username.strip().casefold()
        with self.lock:
            item = self._find_username(normalized)
            if item is None or not item.get("enabled"):
                raise ValueError("用户名或密码错误")
            expected = str(item.get("password_hash") or "")
            try:
                actual = _password_hash(password, _decode(str(item.get("password_salt") or "")))
            except (ValueError, TypeError):
                actual = ""
            if not hmac.compare_digest(actual, expected):
                raise ValueError("用户名或密码错误")
            item["last_login_at"] = now_iso()
            item["updated_at"] = item["last_login_at"]
            self._save()
            return self._public(item)

    def get(self, user_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self._find(user_id)
            return self._public(item) if item else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._public(item) for item in self.data["users"]]

    def update(
        self,
        user_id: str,
        *,
        actor_user_id: str,
        display_name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            item = self._find(user_id)
            if item is None:
                raise KeyError(user_id)
            if role is not None and role not in {"admin", "user"}:
                raise ValueError("用户角色无效")
            if user_id == actor_user_id:
                if role is not None and role != item.get("role"):
                    raise ValueError("当前管理员不可修改自己的角色")
                if enabled is False:
                    raise ValueError("当前管理员不可停用自己的账号")
            removing_admin = item.get("role") == "admin" and item.get("enabled") and (
                role == "user" or enabled is False
            )
            if removing_admin and self._enabled_admin_count() <= 1:
                raise ValueError("至少需要保留一个启用的管理员")
            if display_name is not None:
                cleaned = display_name.strip()
                if not cleaned:
                    raise ValueError("显示名称不能为空")
                item["display_name"] = cleaned[:40]
            if role is not None:
                item["role"] = role
            if enabled is not None:
                item["enabled"] = bool(enabled)
            if password:
                self._validate_password(password)
                salt = secrets.token_bytes(16)
                item["password_salt"] = _encode(salt)
                item["password_hash"] = _password_hash(password, salt)
            item["updated_at"] = now_iso()
            self._save()
            return self._public(item)

    def delete(self, user_id: str, *, actor_user_id: str) -> bool:
        with self.lock:
            item = self._find(user_id)
            if item is None:
                return False
            if user_id == actor_user_id:
                raise ValueError("当前管理员不可删除自己的账号")
            if item.get("role") == "admin" and item.get("enabled") and self._enabled_admin_count() <= 1:
                raise ValueError("至少需要保留一个启用的管理员")
            self.data["users"] = [row for row in self.data["users"] if row.get("id") != user_id]
            self._save()
            return True

    def _enabled_admin_count(self) -> int:
        return sum(
            1
            for item in self.data["users"]
            if item.get("role") == "admin" and item.get("enabled")
        )

    def _find(self, user_id: str) -> dict[str, Any] | None:
        return next((item for item in self.data["users"] if item.get("id") == user_id), None)

    def _find_username(self, username: str) -> dict[str, Any] | None:
        return next((item for item in self.data["users"] if item.get("username") == username), None)

    @staticmethod
    def _normalize_username(username: str) -> str:
        normalized = username.strip().casefold()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("用户名需为 3-32 位文字、字母、数字或 ._- 组合")
        return normalized

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 8 or len(password) > 128:
            raise ValueError("密码长度需要保持在 8-128 个字符")

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"password_salt", "password_hash"}
        }

    def _save(self) -> None:
        save_store(self.path, self.data, kind="users", also_json=True)


class SessionSigner:
    def __init__(self, key_path: Path, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.key_path = key_path
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.is_file():
            self.key = self.key_path.read_bytes()
        else:
            self.key = secrets.token_bytes(32)
            self.key_path.write_bytes(self.key)
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
        self.ttl_seconds = ttl_seconds

    def issue(self, user_id: str) -> str:
        payload = {
            "uid": user_id,
            "exp": int(time.time()) + self.ttl_seconds,
            "nonce": secrets.token_hex(8),
        }
        encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _encode(hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str) -> str | None:
        try:
            encoded, signature = token.split(".", 1)
            expected = _encode(hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(_decode(encoded).decode("utf-8"))
            if int(payload.get("exp", 0)) < int(time.time()):
                return None
            user_id = str(payload.get("uid") or "")
            return user_id or None
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return None
