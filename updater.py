# -*- coding: utf-8 -*-
"""Secure-ish GitHub Releases updater for Madrasati Pro (standard library only)."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

API_VERSION = "2022-11-28"
MAX_INSTALLER_BYTES = 350 * 1024 * 1024
_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    """A clear, user-displayable error while checking or downloading an update."""


def version_key(value: str) -> tuple[int, ...]:
    """Parse numeric versions such as v1.2.3 into a comparable tuple."""
    value = str(value or "").strip()
    if value[:1].lower() == "v":
        value = value[1:]
    match = re.match(r"^(\d+(?:\.\d+){1,3})", value)
    if not match:
        raise UpdateError(f"صيغة رقم الإصدار غير معروفة: {value or '(فارغ)'}")
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def validate_repository(repository: str) -> str:
    value = (repository or "").strip().strip("/")
    if value.lower() in {"owner/repository", "your-username/your-repository", ""}:
        raise UpdateError(
            "لم يُضبط مصدر التحديث بعد. يلزم نشر المشروع في مستودع GitHub عام، "
            "ثم إنشاء الإصدار الأول من مسار النشر المرفق بالمشروع."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise UpdateError("رابط مستودع التحديث غير صحيح. المطلوب بالشكل: owner/repository")
    return value


def _safe_download_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
        raise UpdateError("رابط تنزيل التحديث ليس من نطاق GitHub المسموح.")
    return url


class UpdateService:
    def __init__(self, repository: str, current_version: str):
        self.repository = validate_repository(repository)
        self.current_version = current_version
        self.api_url = f"https://api.github.com/repos/{self.repository}/releases/latest"

    @staticmethod
    def _request(url: str, *, timeout: int = 15, accept: str = "application/vnd.github+json"):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "MadrasatiPro-Updater/1.0",
                "Accept": accept,
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("لم يتم العثور على إصدار منشور للتحديث. تحقق من نشر إصدار GitHub.") from exc
            if exc.code == 403:
                raise UpdateError("GitHub رفض الطلب مؤقتاً بسبب حدّ الطلبات أو صلاحية المستودع.") from exc
            raise UpdateError(f"تعذّر الاتصال بخدمة التحديث (HTTP {exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError("تعذّر الوصول إلى التحديث. تحقق من اتصال الإنترنت ثم حاول مجدداً.") from exc

    def latest_release(self) -> dict:
        with self._request(self.api_url, timeout=12) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateError("بيانات الإصدار المستلمة أكبر من الحد المسموح.")
        try:
            release = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("تعذّر قراءة بيانات الإصدار من GitHub.") from exc
        tag = str(release.get("tag_name") or "").strip()
        latest_version = tag[1:] if tag[:1].lower() == "v" else tag
        if not latest_version:
            raise UpdateError("الإصدار المنشور لا يحتوي على رقم إصدار واضح.")
        assets = release.get("assets") or []
        installer = None
        checksum = None
        for asset in assets:
            name = str(asset.get("name") or "")
            if re.fullmatch(r"SchoolManagerPro_Setup_\d+(?:\.\d+){1,2}\.exe", name, re.I):
                installer = asset
            elif re.fullmatch(r"SchoolManagerPro_Setup_\d+(?:\.\d+){1,2}\.exe\.sha256", name, re.I):
                checksum = asset
        if not installer:
            raise UpdateError("الإصدار موجود، لكن ملف تثبيت Windows المطلوب غير مرفق به.")
        version_from_asset = re.search(r"_(\d+(?:\.\d+){1,2})\.exe$", str(installer.get("name") or ""), re.I)
        asset_version = version_from_asset.group(1) if version_from_asset else latest_version
        # Prefer the version encoded in the installer filename to prevent a tag/asset mismatch.
        if version_key(asset_version) != version_key(latest_version):
            raise UpdateError("رقم إصدار ملف التثبيت لا يطابق رقم الإصدار المنشور.")
        url = _safe_download_url(str(installer.get("browser_download_url") or ""))
        if int(installer.get("size") or 0) > MAX_INSTALLER_BYTES:
            raise UpdateError("حجم ملف التحديث يتجاوز الحد المسموح.")
        checksum_url = None
        if checksum:
            checksum_url = _safe_download_url(str(checksum.get("browser_download_url") or ""))
        return {
            "version": latest_version,
            "tag": tag,
            "name": str(release.get("name") or tag),
            "notes": str(release.get("body") or "").strip(),
            "url": url,
            "checksum_url": checksum_url,
            "size": int(installer.get("size") or 0),
            "html_url": str(release.get("html_url") or ""),
        }

    def download_and_verify(self, release: dict) -> Path:
        """Download a Windows installer and require its SHA-256 sidecar."""
        url = _safe_download_url(str(release.get("url") or ""))
        checksum_url = release.get("checksum_url")
        if not checksum_url:
            raise UpdateError("ملف التحديث لا يحتوي على بصمة SHA-256؛ أوقفنا التثبيت للحماية.")
        checksum_url = _safe_download_url(str(checksum_url))
        with self._request(checksum_url, timeout=20, accept="application/octet-stream") as response:
            checksum_final = urlsplit(response.geturl())
            if checksum_final.scheme != "https" or checksum_final.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                raise UpdateError("تم رفض تحويل رابط بصمة التحقق إلى نطاق غير موثوق.")
            checksum_text = response.read(4096).decode("utf-8", errors="replace").strip()
        match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
        if not match:
            raise UpdateError("ملف التحقق من التحديث غير صالح.")
        expected_hash = match.group(1).lower()

        tmpdir = Path(tempfile.gettempdir()) / "MadrasatiProUpdates"
        tmpdir.mkdir(parents=True, exist_ok=True)
        filename = f"SchoolManagerPro_Setup_{release['version']}.exe"
        destination = tmpdir / filename
        digest = hashlib.sha256()
        total = 0
        try:
            with self._request(url, timeout=60, accept="application/octet-stream") as response:
                final_host = urlsplit(response.geturl()).hostname
                if urlsplit(response.geturl()).scheme != "https" or final_host not in _ALLOWED_DOWNLOAD_HOSTS:
                    raise UpdateError("تم رفض تحويل رابط التنزيل إلى نطاق غير موثوق.")
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_INSTALLER_BYTES:
                            raise UpdateError("حجم ملف التحديث تجاوز الحد المسموح.")
                        digest.update(chunk)
                        output.write(chunk)
            if total < 100_000:
                raise UpdateError("ملف التحديث الذي نُزّل أصغر من المتوقع.")
            with destination.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise UpdateError("الملف الذي نُزّل ليس ملف تثبيت Windows صالحاً.")
            if digest.hexdigest().lower() != expected_hash:
                raise UpdateError("فشل التحقق من بصمة التحديث. لم يتم تشغيل الملف.")
            return destination
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
