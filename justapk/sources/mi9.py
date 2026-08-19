from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from justapk.models import AppInfo, DownloadResult
from justapk.sources.base import APKSource
from justapk.utils import HTTP_TIMEOUT, create_session, download_file, sha256_file

_SSE_TIMEOUT = 90
_ICON_BASE = "https://lh3.googleusercontent.com"


class MI9Source(APKSource):
    name = "mi9"
    BASE = "https://mi9.com"
    SEARCH_API = "https://search.mi9.com/"

    def __init__(self):
        self.session = create_session()
        self.session.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{self.BASE}/apk-downloader",
        })
        self._warmed = False

    def _warm(self) -> None:
        if self._warmed:
            return
        try:
            self.session.get(f"{self.BASE}/apk-downloader", timeout=HTTP_TIMEOUT)
        except Exception:
            pass
        self._warmed = True

    def search(self, query: str) -> list[AppInfo]:
        self._warm()
        resp = self.session.get(
            self.SEARCH_API,
            params={"q": query},
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        results = []
        seen = set()
        for item in data:
            pkg = str(item.get("google_id") or "").strip()
            if not pkg or pkg in seen:
                continue
            seen.add(pkg)
            icon = str(item.get("icon") or "").strip()
            results.append(AppInfo(
                package=pkg,
                name=str(item.get("app_name") or pkg).strip(),
                version="",
                source=self.name,
                icon_url=f"{_ICON_BASE}/{icon}=s128-rw" if icon else None,
                description=str(item.get("tg_name") or "").strip() or None,
            ))
        return results

    def get_info(self, package: str) -> AppInfo | None:
        self._warm()
        resp = self.session.get(f"{self.BASE}/package/{package}/", timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._parse_app_page(package, resp.text)

    def _parse_app_page(self, package: str, html: str) -> AppInfo:
        soup = BeautifulSoup(html, "lxml")
        name = package
        version = ""
        size = None
        description = None
        icon_url = None

        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else []
            for item in graph:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "MobileApplication":
                    continue
                name = item.get("name") or name
                version = item.get("softwareVersion") or version
                size = _parse_size(str(item.get("fileSize") or ""))
                description = item.get("description") or description
                icon_url = item.get("image") or icon_url
                break

        if name == package:
            title_el = soup.select_one("h1.apk-hero__title, h1")
            if title_el:
                name = title_el.get_text(strip=True) or name
        if not version:
            ver_el = soup.select_one(".hero__version")
            if ver_el:
                version = ver_el.get_text(strip=True)
        if not description:
            desc_el = soup.select_one(".apk-hero__desc")
            if desc_el:
                description = desc_el.get_text(strip=True)

        return AppInfo(
            package=package,
            name=name,
            version=version,
            size=size,
            source=self.name,
            icon_url=icon_url,
            description=description,
        )

    def _find_version_code(self, package: str, version: str) -> str:
        resp = self.session.get(f"{self.BASE}/package/{package}/", timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:
            raise RuntimeError(f"[mi9] Package not found: {package}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        wanted = version.lstrip("v")
        available: list[str] = []
        for a in soup.select("a.vx-item[href]"):
            href = a.get("href", "")
            m = re.search(rf"/package/{re.escape(package)}/download/(\d+)/", href)
            if not m:
                continue
            vc = m.group(1)
            ver_el = a.select_one(".vx-version")
            ver_name = (ver_el.get_text(strip=True) if ver_el else "").lstrip("v")
            if not ver_name:
                title = a.get("title") or ""
                tm = re.search(r"APK\s+([\d.]+)", title)
                ver_name = tm.group(1) if tm else ""
            if ver_name:
                available.append(ver_name)
            if wanted == ver_name or wanted == vc:
                return vc

        latest = self._parse_app_page(package, resp.text).version or "unknown"
        hint = ", ".join(available[:8]) or latest
        raise RuntimeError(
            f"[mi9] Version {version} not found for {package}. "
            f"Latest available: {latest}" + (f" (also: {hint})" if hint != latest else "")
        )

    def download(self, package: str, output_dir: Path, version: str | None = None) -> DownloadResult:
        self._warm()
        vc = ""
        if version:
            vc = self._find_version_code(package, version)

        event = self._generate_links(package, vc)
        html = event.get("html") or ""
        if event.get("ok") is False or event.get("type") == "error" or not html.strip():
            msg = event.get("message") or event.get("title") or event.get("status") or "unknown error"
            raise RuntimeError(f"[mi9] Failed to generate download link: {msg}")

        dl_url, file_type, page_version = self._extract_download(html, package)
        if version and page_version and version.lstrip("v") != page_version.lstrip("v") and not version.isdigit():
            raise RuntimeError(
                f"[mi9] Version {version} not available. "
                f"Latest available: {page_version}"
            )

        ver = page_version or version or "latest"
        filename = f"{package}-{ver}.{file_type}"
        out_path = output_dir / filename

        sys.stderr.write(f"[mi9] Downloading {package} v{ver}\n")
        size = download_file(
            dl_url, out_path, self.session,
            headers={"Referer": f"{self.BASE}/apk-downloader"},
        )

        return DownloadResult(
            path=out_path,
            package=package,
            version=ver,
            source=self.name,
            size=size,
            sha256=sha256_file(out_path),
        )

    def _generate_links(self, package: str, vc: str) -> dict:
        payload = {
            "package": package,
            "vc": vc,
            "device": "phone",
            "arch": "arm64-v8a",
            "device_id": "",
            "sdk": "default",
            "hl": "en",
            "timestamp": int(time.time() * 1000),
        }
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()

        resp = self.session.get(
            f"{self.BASE}/mi9apk",
            params={"id_token": "", "data": encoded},
            headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                "Referer": f"{self.BASE}/apk-downloader",
            },
            timeout=_SSE_TIMEOUT,
        )
        resp.raise_for_status()
        events = _parse_sse(resp.text)
        if not events:
            raise RuntimeError(f"[mi9] Empty SSE response for: {package}")
        return events[-1]

    def _extract_download(self, html: str, package: str) -> tuple[str, str, str]:
        soup = BeautifulSoup(html, "lxml")
        ver_el = soup.select_one("ul.apk_ad_info span._version, span._version")
        version = ver_el.get_text(strip=True) if ver_el else ""

        files: list[tuple[str, str]] = []
        for a in soup.select("div.apk_files_list a[href]"):
            href = a.get("href", "").strip()
            if not href or href.startswith("#"):
                continue
            name_el = a.select_one("span.der_name")
            name = name_el.get_text(strip=True) if name_el else ""
            files.append((href, name))
        if not files:
            for a in soup.select("a[rel*='nofollow'][href*='filename=']"):
                href = a.get("href", "").strip()
                if href:
                    files.append((href, ""))

        xapk_btn = soup.select_one("button.zip-action-btn[data-package-type='xapk']")
        apk_btn = soup.select_one("button.zip-action-btn[data-package-type='apk']")

        unique_files = []
        seen_urls = set()
        for href, name in files:
            if href in seen_urls:
                continue
            seen_urls.add(href)
            unique_files.append((href, name))

        if len(unique_files) == 1:
            href, name = unique_files[0]
            lower = name.lower()
            if "config." not in lower:
                ext = "xapk" if lower.endswith(".xapk") else "apk"
                return href, ext, version

        # Split packages: keep XAPK so --no-convert can skip merge.
        if xapk_btn:
            return self._compress_url(xapk_btn, "xapk"), "xapk", version

        if apk_btn:
            return self._compress_apk(apk_btn), "apk", version

        for href, name in unique_files:
            if "config." not in name.lower():
                ext = "xapk" if name.lower().endswith(".xapk") else "apk"
                return href, ext, version

        if unique_files:
            return unique_files[0][0], "apk", version

        raise RuntimeError(f"[mi9] No download link for: {package}")

    def _compress_url(self, btn, ptype: str) -> str:
        params = {
            "h": btn.get("data-h") or "",
            "p": ptype,
            "token": btn.get("data-token") or "",
            "ip": btn.get("data-ip") or "",
            "google_id": btn.get("data-google-id") or "",
            "t": btn.get("data-expiration") or "",
        }
        return f"{self.BASE}/compress/?{urlencode(params)}"

    def _compress_apk(self, btn) -> str:
        url = self._compress_url(btn, "apk")
        resp = self.session.get(
            url,
            headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                "Referer": f"{self.BASE}/apk-downloader",
            },
            timeout=_SSE_TIMEOUT,
        )
        resp.raise_for_status()
        events = _parse_sse(resp.text)
        for event in reversed(events):
            dl = event.get("download_url")
            if event.get("ok") and dl:
                if dl.startswith("/"):
                    return f"{self.BASE}{dl}"
                return dl
        raise RuntimeError("[mi9] Compress APK did not return a download URL")


def _parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _parse_size(size_str: str) -> int | None:
    m = re.match(r"([\d.]+)\s*(MB|GB|KB)", size_str, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper()
    multipliers = {"GB": 1024**3, "MB": 1024**2, "KB": 1024}
    return int(val * multipliers.get(unit, 1))
