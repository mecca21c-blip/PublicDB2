"""Portable project-local API credential storage with no read-back projection."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path


class CredentialStoreError(ValueError):
    pass


class ApiCredentialStore:
    def __init__(self, config_root: Path) -> None:
        self.config_root = config_root.resolve()
        self.path = self.config_root / "api_credentials.json"

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CredentialStoreError("API 자격증명 저장소를 읽을 수 없습니다.") from error
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
            raise CredentialStoreError("API 자격증명 저장소 형식이 올바르지 않습니다.")
        return value

    def save(self, secret_value: str, credential_ref: str | None = None) -> dict[str, object]:
        secret_value = secret_value.strip()
        if not secret_value:
            raise CredentialStoreError("비밀 값은 비어 있을 수 없습니다.")
        reference = (credential_ref or f"cred_{secrets.token_urlsafe(18)}").strip()
        if not reference or len(reference) > 200:
            raise CredentialStoreError("자격증명 참조가 올바르지 않습니다.")
        values = self._read()
        replaced = reference in values
        values[reference] = secret_value
        self._write(values)
        return {"credential_ref": reference, "configured": True, "masked": "••••••••", "replaced": replaced}

    def remove(self, credential_ref: str) -> bool:
        values = self._read()
        existed = credential_ref in values
        values.pop(credential_ref, None)
        self._write(values)
        return existed

    def resolve(self, credential_ref: str | None) -> str:
        if not credential_ref:
            raise CredentialStoreError("API 자격증명이 설정되지 않았습니다.")
        value = self._read().get(credential_ref)
        if value is None:
            raise CredentialStoreError("API 자격증명을 찾을 수 없습니다.")
        return value

    def status(self, credential_ref: str | None) -> dict[str, object]:
        configured = bool(credential_ref and credential_ref in self._read())
        return {"credential_ref": credential_ref, "configured": configured, "masked": "••••••••" if configured else None}

    def _write(self, values: dict[str, str]) -> None:
        self.config_root.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.config_root, prefix=".api-credentials-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(values, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            temporary = None
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
