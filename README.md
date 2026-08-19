<div align="center">

<img src="https://em-content.zobj.net/source/apple/391/package_1f4e6.png" width="80" />

# justapk

**Download any APK by package name. 7 sources, automatic fallback, zero config.**

[![PyPI](https://img.shields.io/pypi/v/justapk?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/justapk/)
[![Python](https://img.shields.io/pypi/pyversions/justapk?logo=python&logoColor=white)](https://pypi.org/project/justapk/)
[![License](https://img.shields.io/github/license/TheQmaks/justapk)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/TheQmaks/justapk/ci.yml?label=CI&logo=github)](https://github.com/TheQmaks/justapk/actions/workflows/ci.yml)

<br>

```
justapk download org.telegram.messenger
```

*One command. Seven sources. Always gets the APK.*

</div>

---

## Why?

Existing APK downloaders break constantly — sites add Cloudflare, change APIs, go offline. **justapk** doesn't care. It cycles through 7 sources automatically until one works. Under the hood it uses reverse-engineered mobile APIs and Cloudflare bypass via TLS fingerprint impersonation.

## Install

```bash
pip install justapk
```

> Python 3.11+

## Usage

### `download` — grab an APK

```bash
justapk download <package>              # auto-select best source
justapk download <package> -s apkpure   # from a specific source
justapk download <package> -v 11.6.2    # specific version
justapk download <package> -o ./apks/   # custom output directory
justapk download <package> --no-convert # keep XAPK as-is (no merge)
```

### `search` — find apps

```bash
justapk search telegram
justapk search telegram -s fdroid
```

### `info` — app metadata

```bash
justapk info org.telegram.messenger
justapk info org.telegram.messenger -s apkpure
```

### `convert` — XAPK/split APK to single APK

```bash
justapk convert app.xapk
justapk convert app.xapk -o output/
```

Merges split APKs (base + native libs + assets) and signs with a debug key.

### `sources` — list available sources

```bash
justapk sources
```

> All commands output JSON to stdout. Progress goes to stderr — pipe-friendly by design.

## Sources

Tried in this order. If one fails, the next one picks up automatically.

| | Source | How it works | Notes |
|:---:|--------|-------------|-------|
| 1 | **APK20** | REST API + HTML parsing | No Cloudflare |
| 2 | **F-Droid** | JSON API | FOSS apps only (~4K packages) |
| 3 | **APKPure** | Reverse-engineered mobile API | Largest catalog |
| 4 | **APKMirror** | HTML scraping + `curl_cffi` | Cloudflare bypass |
| 5 | **Uptodown** | Reverse-engineered mobile API | No Cloudflare |
| 6 | **APKCombo** | HTML scraping + `curl_cffi` | Cloudflare bypass |
| 7 | **MI9** | Google Play SSE API | Original Play files, no extra deps |

## Python API

```python
from pathlib import Path
from justapk import APKDownloader

dl = APKDownloader()

# Download with auto-fallback
result = dl.download("org.telegram.messenger", output_dir=Path("./apks/"))
print(result.path, result.size, result.sha256)

# Search across all sources
apps = dl.search("telegram")
for app in apps:
    print(app.package, app.name, app.source)

# Get app info
info = dl.info("org.telegram.messenger")
if info:
    print(info.name, info.version)

# Pin a specific source
result = dl.download("org.telegram.messenger", source="apkpure")
```

## License

[MIT](LICENSE)
