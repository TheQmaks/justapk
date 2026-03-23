from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from justapk.models import AppInfo, DownloadResult
from justapk.sources.base import APKSource
from justapk.utils import HTTP_TIMEOUT, create_session, download_file, sha256_file


class APK20Source(APKSource):
    name = "apk20"
    BASE = "https://www.apk20.com"
    FILE_SERVER = "https://srv01.apk20.com"

    def __init__(self):
        self.session = create_session()

    def _parse_rsc_apps(self, text: str) -> list[dict]:
        """Parse Next.js RSC payload for app objects."""
        results = []
        seen = set()
        for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.+?)"\]\)', text):
            chunk = m.group(1).replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
            for arr_m in re.finditer(r'\[(\{[^]]*"packageName"[^]]*\}(?:,\{[^]]*"packageName"[^]]*\})*)\]', chunk):
                try:
                    data = json.loads(f"[{arr_m.group(1)}]")
                    for item in data:
                        pkg = item.get("packageName", "")
                        if pkg and pkg not in seen:
                            seen.add(pkg)
                            results.append(item)
                except (json.JSONDecodeError, ValueError):
                    pass
            for obj_m in re.finditer(r'\{"packageName":"[^"]+?"[^}]*\}', chunk):
                try:
                    item = json.loads(obj_m.group(0))
                    pkg = item.get("packageName", "")
                    if pkg and pkg not in seen:
                        seen.add(pkg)
                        results.append(item)
                except (json.JSONDecodeError, ValueError):
                    pass
        return results

    def search(self, query: str) -> list[AppInfo]:
        resp = self.session.get(f"{self.BASE}/search/{query}", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()

        apps = self._parse_rsc_apps(resp.text)
        return [
            AppInfo(
                package=app.get("packageName", ""),
                name=app.get("title", app.get("packageName", "")),
                version="",
                source=self.name,
            )
            for app in apps
        ]

    def get_info(self, package: str) -> AppInfo | None:
        resp = self.session.get(f"{self.BASE}/apk/{package}", timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._parse_app_page(package, resp.text)

    def _parse_app_page(self, package: str, html: str) -> AppInfo:
        """Parse app page HTML for metadata + version code."""
        soup = BeautifulSoup(html, "lxml")

        name = package
        version = ""
        size = None

        name_el = soup.select_one("[itemprop='name']")
        if name_el:
            name = name_el.get("content", "") or name_el.get_text(strip=True) or name

        ver_el = soup.select_one("[itemprop='softwareVersion']")
        if ver_el:
            version = ver_el.get("content", "") or ver_el.get_text(strip=True)

        size_el = soup.select_one("[itemprop='fileSize']")
        if size_el:
            size_text = size_el.get("content", "") or size_el.get_text(strip=True)
            size = self._parse_size(size_text)

        return AppInfo(
            package=package,
            name=name,
            version=version,
            size=size,
            source=self.name,
        )

    @staticmethod
    def _parse_size(size_str: str) -> int | None:
        m = re.match(r'([\d.]+)\s*(MB|GB|KB)', size_str, re.IGNORECASE)
        if not m:
            return None
        val = float(m.group(1))
        unit = m.group(2).upper()
        multipliers = {"GB": 1024**3, "MB": 1024**2, "KB": 1024}
        return int(val * multipliers.get(unit, 1))

    def _find_version_code(self, package: str, version: str, html: str) -> str:
        """Find version code for a specific version name from page data."""
        # Parse RSC payload for objects with versionCode + versionName
        for chunk_m in re.finditer(r'self\.__next_f\.push\(\[1,"(.+?)"\]\)', html):
            chunk = chunk_m.group(1).replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
            for obj_m in re.finditer(r'\{[^{}]*"versionCode"\s*:\s*\d+[^{}]*\}', chunk):
                try:
                    obj = json.loads(obj_m.group(0))
                    name = obj.get("versionName", "") or obj.get("version", "")
                    if name == version:
                        return str(obj["versionCode"])
                except (json.JSONDecodeError, ValueError):
                    pass

        # Check if the current page version matches
        info = self._parse_app_page(package, html)
        if info.version == version:
            m = re.search(rf'/apk/{re.escape(package)}/download/(\d+)', html)
            if m:
                return m.group(1)
            codes = re.findall(r'"versionCode":\s*(\d+)', html)
            if codes:
                return codes[0]

        raise RuntimeError(
            f"[apk20] Version {version} not found for {package}. "
            f"Latest available: {info.version or 'unknown'}"
        )

    def download(self, package: str, output_dir: Path, version: str | None = None) -> DownloadResult:
        # Fetch app page once — extract version code + version name
        resp = self.session.get(f"{self.BASE}/apk/{package}", timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:
            raise RuntimeError(f"[apk20] Package not found: {package}")
        resp.raise_for_status()

        if version:
            version_code = self._find_version_code(package, version, resp.text)
            ver = version
        else:
            m = re.search(rf'/apk/{re.escape(package)}/download/(\d+)', resp.text)
            if not m:
                codes = re.findall(r'"versionCode":\s*(\d+)', resp.text)
                if not codes:
                    raise RuntimeError(f"[apk20] No versions found for: {package}")
                version_code = codes[0]
            else:
                version_code = m.group(1)

            info = self._parse_app_page(package, resp.text)
            ver = info.version or "unknown"

        # Verify + get download URL
        verify_resp = self.session.get(
            f"{self.BASE}/api/verify/{package}/{version_code}", timeout=HTTP_TIMEOUT
        )
        verify_resp.raise_for_status()
        verify_data = verify_resp.json()

        if not verify_data.get("success"):
            raise RuntimeError(f"[apk20] Verify failed: {verify_data.get('message', 'unknown error')}")

        filename = verify_data["filename"]
        dl_url = f"{self.FILE_SERVER}/{filename}"

        out_filename = f"{package}-{ver}{_ext(filename)}"
        out_path = output_dir / out_filename

        sys.stderr.write(f"[apk20] Downloading {package} v{ver}\n")
        size = download_file(dl_url, out_path, self.session)

        return DownloadResult(
            path=out_path,
            package=package,
            version=ver,
            source=self.name,
            size=size,
            sha256=sha256_file(out_path),
        )


def _ext(filename: str) -> str:
    """Extract file extension from filename."""
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1]
    return ".apk"
