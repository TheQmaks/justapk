from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import requests

from justapk.models import AppInfo, DownloadResult
from justapk.sources.base import APKSource
from justapk.utils import HTTP_TIMEOUT, download_file, sha256_file

# Reverse-engineered from APKPure Android app v3.20.6309
_API_BASE = "https://tapi.pureapk.com/v3"
_AUTH_KEY = "qNKrYmW8SSUqJ73k3P2yfMxRTo3sJTR"
_SIGN_SECRET = "d33cb23fd17fda8ea38be504929b77ef"


def _make_headers() -> dict[str, str]:
    """Build headers matching the APKPure mobile app."""
    device_uuid = str(uuid.uuid4())
    project_a = json.dumps({
        "device_info": {
            "abis": ["arm64-v8a", "armeabi-v7a"],
            "android_id": hashlib.md5(device_uuid.encode()).hexdigest()[:16],
            "brand": "samsung", "country": "United States",
            "country_code": "US", "imei": "", "language": "en-US",
            "manufacturer": "samsung", "mode": "SM-G955F",
            "os_ver": "34", "os_ver_name": "14", "platform": 1,
            "product": "dream2lte", "screen_height": 2888, "screen_width": 1440,
        },
        "host_app_info": {
            "build_no": "873", "channel": "", "md5": "",
            "pkg_name": "com.apkpure.aegon", "sdk_ver": "3.20.6309",
            "version_code": 3206397, "version_name": "3.20.6309",
        },
        "net_info": {
            "carrier_code": 0, "ipv4": "", "ipv6": "", "mac_address": "",
            "net_type": 1, "use_vpn": False, "wifi_bssid": "", "wifi_ssid": "",
        },
        "user_info": {
            "auth_key": _AUTH_KEY, "country": "United States",
            "country_code": "US", "guid": "", "language": "en-US",
            "qimei": "", "qimei_token": "", "user_id": "",
            "uuid": device_uuid,
        },
    }, separators=(",", ":"))

    ext_info = json.dumps({
        "ext_info": '{"gaid":"","oaid":""}',
        "lbs_info": {
            "accuracy": 0.0, "city": "", "city_code": 0,
            "country": "", "country_code": "", "district": "",
            "latitude": 0.0, "longitude": 0.0, "province": "", "street": "",
        },
    }, separators=(",", ":"))

    return {
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 14; SM-G955F "
                       "Build/AP2A.240805.005); APKPure/3.20.6309 (Aegon)",
        "Ual-Access-Businessid": "projecta",
        "Ual-Access-ProjectA": project_a,
        "Ual-Access-ExtInfo": ext_info,
        "Ual-Access-Sequence": str(uuid.uuid4()),
        "Ual-Access-Signature": "",
        "Ual-Access-Nonce": "0",
        "Ual-Access-Timestamp": "0",
        "Accept-Encoding": "gzip",
    }


def _sign_body(headers: dict[str, str], body: str) -> None:
    """Compute MD5 signature for POST requests."""
    import random
    import time
    ts = str(int(time.time() * 1000))
    nonce = str(random.randint(10000000, 99999999))
    sig = hashlib.md5((body + ts + _SIGN_SECRET + nonce).encode()).hexdigest()
    headers["Ual-Access-Signature"] = sig
    headers["Ual-Access-Nonce"] = nonce
    headers["Ual-Access-Timestamp"] = ts
    headers["Content-Type"] = "application/json; charset=utf-8"


class APKPureSource(APKSource):
    name = "apkpure"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_make_headers())

    def search(self, query: str) -> list[AppInfo]:
        resp = self.session.get(
            f"{_API_BASE}/search_query_new",
            params={"hl": "en-US", "key": query, "page": "1",
                    "search_type": "active_search"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        seen = set()
        for section in data.get("data", {}).get("data", []):
            for item in section.get("data", []):
                ai = item.get("app_info", {})
                pkg = ai.get("package_name", "")
                if not pkg or pkg in seen:
                    continue
                seen.add(pkg)
                results.append(AppInfo(
                    package=pkg,
                    name=ai.get("title", pkg),
                    version=ai.get("version_name", ""),
                    source=self.name,
                    description=ai.get("description_short", ""),
                ))
        return results

    def get_info(self, package: str) -> AppInfo | None:
        detail = self._get_detail(package)
        if not detail:
            return None
        return AppInfo(
            package=package,
            name=detail.get("title", package),
            version=detail.get("version_name", ""),
            source=self.name,
            description=detail.get("description_short", ""),
        )

    def _get_detail(self, package: str) -> dict | None:
        """Call get_app_detail API."""
        body = json.dumps({"package_name": package, "hl": "en-US"})
        headers = dict(self.session.headers)
        _sign_body(headers, body)
        resp = self.session.post(
            f"{_API_BASE}/get_app_detail",
            data=body, headers=headers, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("app_detail")

    def download(self, package: str, output_dir: Path, version: str | None = None) -> DownloadResult:
        detail = self._get_detail(package)
        if not detail:
            raise RuntimeError(f"[apkpure] Package not found: {package}")

        asset = detail.get("asset", {})
        dl_url = asset.get("url", "")
        if not dl_url:
            raise RuntimeError(f"[apkpure] No download URL for: {package}")

        detail_version = detail.get("version_name", "")

        # Validate requested version matches what the API provides
        if version and detail_version and version != detail_version:
            raise RuntimeError(
                f"[apkpure] Version {version} not available. "
                f"Latest available: {detail_version}"
            )

        ver = detail_version or "latest"
        file_type = asset.get("type", "APK").lower()
        filename = f"{package}-{ver}.{file_type}"
        out_path = output_dir / filename

        sys.stderr.write(f"[apkpure] Downloading {package} v{ver} ({file_type})\n")
        size = download_file(dl_url, out_path, self.session)

        return DownloadResult(
            path=out_path,
            package=package,
            version=ver,
            source=self.name,
            size=size,
            sha256=sha256_file(out_path),
        )
