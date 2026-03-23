from __future__ import annotations

import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from justapk.models import AppInfo, DownloadResult
from justapk.sources.base import APKSource
from justapk.utils import HTTP_TIMEOUT, create_cf_session, download_file, sha256_file


class APKComboSource(APKSource):
    name = "apkcombo"
    BASE = "https://apkcombo.com"

    def __init__(self):
        self.session = create_cf_session()

    def search(self, query: str) -> list[AppInfo]:
        resp = self.session.get(f"{self.BASE}/search/{query}", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results = []
        seen = set()
        # Links inside .content-apps: /{slug}/{package}/
        for a in soup.select(".content-apps a[href]"):
            href = a.get("href", "").strip("/")
            parts = href.split("/")
            # Pattern: slug/com.example.app
            if len(parts) >= 2 and "." in parts[-1]:
                pkg = parts[-1]
                if pkg in seen:
                    continue
                seen.add(pkg)
                name_el = a.select_one("span.name")
                name = name_el.get_text(strip=True) if name_el else a.get_text(strip=True)
                results.append(AppInfo(
                    package=pkg,
                    name=name,
                    version="",
                    source=self.name,
                ))
        return results

    def _find_slug(self, package: str) -> str | None:
        """Find the APKCombo slug for a package name."""
        resp = self.session.get(f"{self.BASE}/search/{package}", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if package in href:
                parts = [p for p in href.split("/") if p]
                # URL pattern: /slug/package
                for i, part in enumerate(parts):
                    if part == package and i > 0:
                        return parts[i - 1]
        return None

    def get_info(self, package: str) -> AppInfo | None:
        slug = self._find_slug(package)
        if not slug:
            return None

        resp = self.session.get(f"{self.BASE}/{slug}/{package}", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        name_el = soup.select_one("h1")
        ver_el = soup.select_one(".version")

        return AppInfo(
            package=package,
            name=name_el.get_text(strip=True) if name_el else package,
            version=ver_el.get_text(strip=True) if ver_el else "",
            source=self.name,
        )

    def download(self, package: str, output_dir: Path, version: str | None = None) -> DownloadResult:
        slug = self._find_slug(package)
        if not slug:
            raise RuntimeError(f"[apkcombo] Package not found: {package}")

        # Download page URL
        page_url = f"{self.BASE}/{slug}/{package}/download/apk"
        resp = self.session.get(page_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Get version from page
        page_version = ""
        ver_el = soup.select_one(".version")
        if ver_el:
            page_version = ver_el.get_text(strip=True)

        # Validate requested version matches what the page provides
        if version and page_version and version != page_version:
            raise RuntimeError(
                f"[apkcombo] Version {version} not available. "
                f"Latest available: {page_version}"
            )

        if not version:
            version = page_version

        # Find R2 download links: /r2?u={url_encoded_signed_url}
        dl_url = None
        file_type = "apk"
        for a in soup.select("a[href*='/r2?']"):
            href = a.get("href", "")
            if "/r2?" in href:
                variant_text = a.get_text(strip=True).lower()
                dl_url = href if href.startswith("http") else f"{self.BASE}{href}"

                if not version:
                    m = re.search(r'([\d.]+)\s*\(', a.get_text(strip=True))
                    if m:
                        version = m.group(1)

                file_type = "xapk" if "xapk" in variant_text else "apk"

                # Prefer APK over XAPK
                if "apk" in variant_text and "xapk" not in variant_text:
                    break

        if not dl_url:
            raise RuntimeError(f"[apkcombo] No download link on: {page_url}")

        ver = version or "latest"
        filename = f"{package}-{ver}.{file_type}"
        out_path = output_dir / filename

        sys.stderr.write(f"[apkcombo] Downloading {package} v{ver}\n")
        # APKCombo R2: HEAD=403, only GET works; follow redirects
        size = download_file(dl_url, out_path, self.session)

        return DownloadResult(
            path=out_path,
            package=package,
            version=ver,
            source=self.name,
            size=size,
            sha256=sha256_file(out_path),
        )
