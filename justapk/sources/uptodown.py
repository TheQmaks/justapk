from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

from justapk.models import AppInfo, DownloadResult
from justapk.sources.base import APKSource
from justapk.utils import download_file, sha256_file

# Reverse-engineered from Uptodown Android app v7.07
_API_BASE = "https://www.uptodown.app/eapi"
_APIKEY_SECRET = "$(=a%\u00b7!45J&S"  # $(=a%·!45J&S
_DALVIK_UA = (
    "Dalvik/2.1.0 (Linux; U; Android 14; SM-G955F "
    "Build/AP2A.240805.005)"
)


def _generate_apikey() -> str:
    """Generate hourly-rotating API key: sha256(secret + hourEpoch)."""
    now = datetime.now(UTC)
    epoch_ms = int(now.timestamp() * 1000)
    offset_ms = now.minute * 60000 + now.second * 1000 + now.microsecond // 1000
    hour_epoch = (epoch_ms - offset_ms) // 1000
    raw = _APIKEY_SECRET + str(hour_epoch)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _api_headers() -> dict[str, str]:
    return {
        "User-Agent": _DALVIK_UA,
        "Identificador": "Uptodown_Android",
        "Identificador-Version": "707",
        "APIKEY": _generate_apikey(),
    }


class UptodownSource(APKSource):
    name = "uptodown"

    def __init__(self):
        self.session = requests.Session()

    def _api_get(self, path: str) -> requests.Response:
        """Make authenticated API request."""
        return self.session.get(
            f"{_API_BASE}{path}",
            headers=_api_headers(),
            timeout=30,
        )

    def search(self, query: str) -> list[AppInfo]:
        resp = self._api_get(f"/v2/apps/search/{query}?page[limit]=30&page[offset]=0")
        resp.raise_for_status()
        data = resp.json()

        items = data.get("data", {}).get("results", [])
        if not items and isinstance(data, list):
            items = data

        results = []
        for item in items:
            pkg = item.get("packageName") or item.get("packagename", "")
            name = item.get("name", pkg)
            app_id = item.get("appID") or item.get("id", "")
            results.append(AppInfo(
                package=pkg or str(app_id),
                name=name,
                version="",
                source=self.name,
            ))
        return results

    def get_info(self, package: str) -> AppInfo | None:
        app_id = self._resolve_app_id(package)
        if not app_id:
            return None
        detail = self._get_detail(app_id)
        if not detail:
            return None

        # Prefer human-readable version from versions endpoint
        ver = detail.get("lastVersion", "")
        if not ver:
            versions = self._get_versions(app_id, limit=1)
            if versions:
                ver = versions[0].get("version", "")
        if not ver:
            ver = str(detail.get("lastVersionCode", ""))

        return AppInfo(
            package=detail.get("packagename") or package,
            name=detail.get("name", package),
            version=ver,
            source=self.name,
            description=detail.get("shortDescription", ""),
        )

    def _resolve_app_id(self, package: str) -> str | None:
        """Resolve package name to Uptodown app ID."""
        resp = self._api_get(f"/apps/byPackagename/{package}")
        if resp.status_code == 200:
            data = resp.json()
            inner = data.get("data", data)
            app_id = inner.get("appID") or inner.get("id")
            if app_id:
                return str(app_id)

        # Search as fallback — only return exact package match
        resp = self._api_get(f"/v2/apps/search/{package}?page[limit]=5&page[offset]=0")
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data", {}).get("results", [])
            for item in items:
                pkg = item.get("packageName") or item.get("packagename", "")
                if pkg == package:
                    return str(item.get("appID") or item.get("id"))
        return None

    def _get_detail(self, app_id: str) -> dict | None:
        """Get app details via API."""
        resp = self._api_get(f"/v3/apps/{app_id}/device/0?countryIsoCode=US")
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("data", data)

    def _get_versions(self, app_id: str, limit: int = 20) -> list[dict]:
        """Get available versions via eAPI."""
        resp = self._api_get(
            f"/v3/app/{app_id}/device/1/compatible/versions"
            f"?page[limit]={limit}&page[offset]=0"
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("data", [])

    def _get_download_url_api(self, app_id: str, file_id: str) -> str:
        """Get CDN download URL via eAPI."""
        resp = self._api_get(f"/apps/{app_id}/file/{file_id}/downloadUrl?update=0")
        resp.raise_for_status()
        return resp.json()["data"]["downloadURL"]

    def download(self, package: str, output_dir: Path, version: str | None = None) -> DownloadResult:
        app_id = self._resolve_app_id(package)
        if not app_id:
            raise RuntimeError(f"[uptodown] App not found: {package}")

        detail = self._get_detail(app_id)
        if not detail:
            raise RuntimeError(f"[uptodown] Cannot get details for: {package}")

        real_pkg = detail.get("packagename") or package

        # Get versions to find fileID, fileType, and version string
        versions = self._get_versions(app_id)
        if not versions:
            raise RuntimeError(f"[uptodown] No versions found for: {package}")

        # Find matching version or use latest
        target = None
        if version:
            for v in versions:
                if v.get("version") == version:
                    target = v
                    break
            if not target:
                available = [v.get("version", "?") for v in versions[:5]]
                raise RuntimeError(
                    f"[uptodown] Version {version} not found for {package}. "
                    f"Available: {', '.join(available)}"
                )
        else:
            target = versions[0]

        file_id = str(target.get("fileID") or target.get("fileid", ""))
        file_type = (target.get("fileType") or target.get("filetype") or "apk").lower()
        ver = target.get("version", "") or str(target.get("versionCode", ""))

        if not file_id:
            raise RuntimeError(f"[uptodown] No file ID for {package} v{ver}")

        dl_url = self._get_download_url_api(app_id, file_id)

        filename = f"{real_pkg}-{ver}.{file_type}" if ver else f"{real_pkg}.{file_type}"
        out_path = output_dir / filename

        # CDN requires Dalvik User-Agent
        dl_headers = {"User-Agent": _DALVIK_UA}

        sys.stderr.write(f"[uptodown] Downloading {real_pkg} v{ver} ({file_type})\n")
        size = download_file(dl_url, out_path, self.session, headers=dl_headers)

        # SHA256 verification if available
        expected_sha = target.get("sha256") or target.get("SHA256")
        actual_sha = sha256_file(out_path)
        if expected_sha and actual_sha != expected_sha:
            sys.stderr.write(
                f"[uptodown] WARNING: SHA256 mismatch! "
                f"Expected {expected_sha}, got {actual_sha}\n"
            )

        return DownloadResult(
            path=out_path,
            package=real_pkg,
            version=ver,
            source=self.name,
            size=size,
            sha256=actual_sha,
        )
