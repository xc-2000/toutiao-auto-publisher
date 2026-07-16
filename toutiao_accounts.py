"""Encrypted multi-account storage and Toutiao QR login over HTTP."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from db import load_or_migrate, save_store
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken
from curl_cffi import requests

from toutiao_protocol import PublisherError, parse_cookie_header, resolve_config_path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretBox:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.is_file():
            key = self.key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            self.key_path.write_bytes(key + b"\n")
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
        self.fernet = Fernet(key)

    def encrypt(self, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.fernet.encrypt(raw).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            raw = self.fernet.decrypt(token.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise PublisherError("本地凭据解密失败") from exc
        if not isinstance(payload, dict):
            raise PublisherError("本地凭据格式无效")
        return payload


def storage_paths(config: dict[str, Any], config_dir: Path) -> tuple[Path, Path, Path]:
    dashboard = config.get("dashboard", {})
    account_path = resolve_config_path(config_dir, dashboard.get("accounts_file", "./state/accounts.json"))
    model_path = resolve_config_path(config_dir, dashboard.get("models_file", "./state/models.json"))
    key_path = resolve_config_path(config_dir, dashboard.get("secret_key_file", "./state/.secret-key"))
    assert account_path is not None and model_path is not None and key_path is not None
    return account_path, model_path, key_path


def _first(data: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def normalize_profile(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    assert isinstance(data, Mapping)
    user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
    media = data.get("media") if isinstance(data.get("media"), Mapping) else {}
    user_id = _first(user, "id", "user_id", "uid", "id_str")
    media_id = _first(media, "id", "media_id", "pgc_id", "id_str")
    name = _first(media, "name", "display_name", "screen_name", "title") or _first(
        user, "name", "display_name", "screen_name", "nickname"
    )
    avatar = _first(media, "avatar_url", "avatar", "avatar_uri", "icon_url") or _first(
        user, "avatar_url", "avatar", "avatar_uri"
    )
    description = _first(media, "description", "intro", "signature")
    identifier = media_id or user_id
    return {
        "external_id": identifier,
        "user_id": user_id,
        "media_id": media_id,
        "name": name or (f"头条账号 {identifier[-6:]}" if identifier else "头条账号"),
        "avatar": avatar,
        "description": description,
        "raw": {"user": dict(user), "media": dict(media)},
    }


class AccountStore:
    def __init__(self, config: dict[str, Any], config_dir: Path) -> None:
        account_path, _, key_path = storage_paths(config, config_dir)
        self.path = account_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.box = SecretBox(key_path)
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"active_id": "", "accounts": []}
        saved = load_or_migrate(self.path, kind="accounts")
        if isinstance(saved, dict):
            self.data["active_id"] = str(saved.get("active_id", ""))
            self.data["accounts"] = saved.get("accounts", [])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            accounts = [self._public(item) for item in self.data["accounts"]]
            return {
                "active_id": self.data["active_id"],
                "accounts": accounts,
                "count": len(accounts),
            }

    def active_credentials(self) -> dict[str, Any] | None:
        with self.lock:
            return self.credentials(str(self.data.get("active_id") or ""))

    def credentials(self, account_id: str) -> dict[str, Any] | None:
        with self.lock:
            account = self._find(account_id)
            if account is None:
                return None
            secret = self.box.decrypt(str(account["secret"]))
            return {
                "account": self._public(account),
                "cookies": secret.get("cookies", {}),
                "headers": secret.get("headers", {}),
                "profile": secret.get("profile", {}),
            }

    def save_account(
        self,
        profile_payload: Mapping[str, Any],
        cookies: Mapping[str, str] | str,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        profile = normalize_profile(profile_payload)
        cookie_map = parse_cookie_header(cookies) if isinstance(cookies, str) else dict(cookies)
        if not cookie_map:
            raise PublisherError("登录成功但未获取到账号 Cookie")
        external_id = str(profile.get("external_id") or "")
        account_id = (
            "tt-" + hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16]
            if external_id
            else "tt-" + hashlib.sha256(json.dumps(cookie_map, sort_keys=True).encode()).hexdigest()[:16]
        )
        secret = self.box.encrypt(
            {"cookies": cookie_map, "headers": dict(headers or {}), "profile": profile.get("raw", {})}
        )
        with self.lock:
            existing = self._find(account_id)
            created_at = existing.get("created_at", now_iso()) if existing else now_iso()
            item = {
                "id": account_id,
                "external_id": external_id,
                "user_id": profile.get("user_id", ""),
                "media_id": profile.get("media_id", ""),
                "name": profile.get("name", "头条账号"),
                "avatar": profile.get("avatar", ""),
                "description": profile.get("description", ""),
                "cookie_count": len(cookie_map),
                "header_count": len(headers or {}),
                "secret": secret,
                "created_at": created_at,
                "updated_at": now_iso(),
            }
            if existing:
                self.data["accounts"] = [item if row["id"] == account_id else row for row in self.data["accounts"]]
            else:
                self.data["accounts"].append(item)
            self.data["active_id"] = account_id
            self._save()
            return self._public(item)

    def activate(self, account_id: str) -> dict[str, Any]:
        with self.lock:
            account = self._find(account_id)
            if account is None:
                raise KeyError(account_id)
            self.data["active_id"] = account_id
            self._save()
            return self._public(account)

    def rename(self, account_id: str, name: str) -> dict[str, Any]:
        cleaned = " ".join(str(name or "").split()).strip()
        if not cleaned:
            raise ValueError("账号名称不能为空")
        if len(cleaned) > 40:
            raise ValueError("账号名称不能超过 40 个字符")
        with self.lock:
            account = self._find(account_id)
            if account is None:
                raise KeyError(account_id)
            account["name"] = cleaned
            self._save()
            return self._public(account)

    def delete(self, account_id: str) -> bool:
        with self.lock:
            original = len(self.data["accounts"])
            self.data["accounts"] = [row for row in self.data["accounts"] if row["id"] != account_id]
            if len(self.data["accounts"]) == original:
                return False
            if self.data["active_id"] == account_id:
                self.data["active_id"] = self.data["accounts"][0]["id"] if self.data["accounts"] else ""
            self._save()
            return True

    def _find(self, account_id: str) -> dict[str, Any] | None:
        return next((item for item in self.data["accounts"] if item.get("id") == account_id), None)

    def _public(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key != "secret"}

    def _save(self) -> None:
        save_store(self.path, self.data, kind="accounts", also_json=True)


@dataclass
class LoginAttempt:
    session: Any
    token: str
    qrcode: str
    created_at: float
    expires_at: float


class QRLoginManager:
    START_URL = "https://sso.toutiao.com/get_qrcode/"
    CHECK_URL = "https://sso.toutiao.com/check_qrconnect/"
    PROFILE_URL = "https://mp.toutiao.com/mp/agw/media/user_login_status_api"

    def __init__(self, config: dict[str, Any], accounts: AccountStore) -> None:
        self.config = config
        self.accounts = accounts
        self.attempts: dict[str, LoginAttempt] = {}
        self.lock = threading.RLock()
        self.aid = int(config.get("toutiao", {}).get("aid", 1231))
        self.service = str(config.get("toutiao", {}).get("base_url", "https://mp.toutiao.com"))
        self.impersonate = str(config.get("toutiao", {}).get("impersonate", "chrome"))

    def start(self) -> dict[str, Any]:
        session = requests.Session(impersonate=self.impersonate)
        session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://mp.toutiao.com/",
                "Origin": "https://mp.toutiao.com",
            }
        )
        try:
            response = session.get(
                self.START_URL,
                params={"aid": self.aid, "service": self.service, "need_logo": 1},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not data.get("token") or not data.get("qrcode"):
                message = payload.get("description") or payload.get("message") if isinstance(payload, dict) else ""
                raise PublisherError(str(message or "二维码创建失败"))
        except PublisherError:
            session.close()
            raise
        except Exception as exc:
            session.close()
            raise PublisherError(f"二维码创建失败：{exc}") from exc
        login_id = uuid.uuid4().hex
        attempt = LoginAttempt(
            session=session,
            token=str(data["token"]),
            qrcode=self._normalize_qrcode(str(data["qrcode"])),
            created_at=time.time(),
            expires_at=time.time() + 180,
        )
        with self.lock:
            self._cleanup()
            self.attempts[login_id] = attempt
        return {
            "login_id": login_id,
            "status": "new",
            "qrcode": attempt.qrcode,
            "expires_at": datetime.fromtimestamp(attempt.expires_at, timezone.utc).isoformat(),
        }

    def poll(self, login_id: str) -> dict[str, Any]:
        with self.lock:
            attempt = self.attempts.get(login_id)
        if attempt is None:
            raise KeyError(login_id)
        if time.time() > attempt.expires_at:
            self._drop(login_id)
            return {"status": "expired"}
        try:
            response = attempt.session.get(
                self.CHECK_URL,
                params={
                    "aid": self.aid,
                    "service": self.service,
                    "token": attempt.token,
                    "need_logo": 1,
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise PublisherError(f"登录状态获取失败：{exc}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            message = payload.get("description") or payload.get("message") if isinstance(payload, dict) else ""
            raise PublisherError(str(message or "登录状态获取失败"))
        raw_status = str(data.get("status", ""))
        status_map = {"1": "new", "2": "scanned", "3": "confirmed", "5": "expired"}
        status = status_map.get(raw_status, raw_status)
        if raw_status == "4" and data.get("token") and data.get("qrcode"):
            attempt.token = str(data["token"])
            attempt.qrcode = self._normalize_qrcode(str(data["qrcode"]))
            attempt.expires_at = time.time() + 180
            return {"status": "new", "qrcode": attempt.qrcode}
        if status == "confirmed":
            try:
                account = self._complete(attempt, str(data.get("redirect_url", "")))
            except PublisherError:
                raise
            except Exception as exc:
                raise PublisherError(f"登录会话保存失败：{exc}") from exc
            self._drop(login_id)
            return {"status": "confirmed", "account": account}
        if status == "expired":
            self._drop(login_id)
        return {"status": status or "error"}

    def _complete(self, attempt: LoginAttempt, redirect_url: str) -> dict[str, Any]:
        if not redirect_url:
            raise PublisherError("扫码已确认，但登录响应缺少 redirect_url")
        redirect_response = attempt.session.get(redirect_url, allow_redirects=True, timeout=20)
        redirect_response.raise_for_status()
        response = attempt.session.get(
            self.PROFILE_URL,
            headers={"Referer": "https://mp.toutiao.com/profile_v4/", "X-Requested-With": "XMLHttpRequest"},
            timeout=20,
        )
        response.raise_for_status()
        profile = response.json()
        data = profile.get("data") if isinstance(profile, dict) else None
        if not isinstance(data, dict) or not data.get("is_login"):
            raise PublisherError(str(profile.get("message") or "扫码确认后账号会话仍未生效"))
        cookies = {
            cookie.name: cookie.value
            for cookie in attempt.session.cookies.jar
            if "toutiao.com" in str(cookie.domain)
        }
        return self.accounts.save_account(profile, cookies)

    @staticmethod
    def _normalize_qrcode(value: str) -> str:
        value = value.strip()
        if value.startswith("data:image/"):
            return value
        return f"data:image/png;base64,{value}"

    def _drop(self, login_id: str) -> None:
        with self.lock:
            attempt = self.attempts.pop(login_id, None)
        if attempt:
            attempt.session.close()

    def _cleanup(self) -> None:
        expired = [key for key, value in self.attempts.items() if value.expires_at < time.time()]
        for key in expired:
            attempt = self.attempts.pop(key)
            attempt.session.close()
