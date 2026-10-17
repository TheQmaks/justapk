from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def _safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract ZIP contents with path traversal protection."""
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        try:
            target.relative_to(dest)
        except ValueError:
            raise RuntimeError(f"Zip path traversal detected: {member}") from None
    zf.extractall(dest)


def convert_xapk_to_apk(xapk_path: Path, output_dir: Path | None = None) -> Path:
    """Convert an XAPK (split APK bundle) to a single installable APK.

    XAPK is a ZIP archive containing manifest.json + split APKs.
    Merges base APK with config splits, including resource tables, and re-signs.
    """
    output_dir = output_dir or xapk_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        with zipfile.ZipFile(xapk_path, "r") as zf:
            _safe_extractall(zf, tmp_path)

        # Read manifest
        manifest_path = tmp_path / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)
            package = manifest.get("package_name", "")
            version = manifest.get("version_name", "")
        else:
            package = xapk_path.stem
            version = ""

        # Find APK files
        apks = list(tmp_path.glob("*.apk"))
        if not apks:
            apks = list(tmp_path.rglob("*.apk"))
        if not apks:
            raise RuntimeError(f"No APK files found in XAPK: {xapk_path}")

        out_name = f"{package}-{version}.apk" if version else f"{package}.apk"
        out_path = output_dir / out_name

        # Single APK — just copy
        if len(apks) == 1:
            shutil.copy2(apks[0], out_path)
            return out_path

        # Classify: base vs config splits
        base_apk, config_apks = _classify_splits(apks, package)

        if not config_apks:
            shutil.copy2(base_apk, out_path)
            return out_path

        # Merge splits into single APK
        split_names = [a.stem for a in config_apks]
        sys.stderr.write(f"[xapk] Merging {len(config_apks)} splits: {', '.join(split_names)}\n")

        merged_path = tmp_path / "merged.apk"
        stats = _merge_splits(base_apk, config_apks, merged_path)
        sys.stderr.write(f"[xapk] Merged: {stats['added']} files from splits\n")

        # Sign the merged APK
        signed = _try_sign(merged_path, tmp_path)
        if signed:
            sys.stderr.write("[xapk] Signed with debug key\n")
        else:
            sys.stderr.write("[xapk] Warning: unsigned (no keytool/jarsigner found)\n")

        shutil.copy2(merged_path, out_path)
        return out_path


def _classify_splits(
    apks: list[Path], package: str
) -> tuple[Path, list[Path]]:
    """Separate base APK from config splits."""
    base_apk = None
    config_apks = []

    for apk in apks:
        stem = apk.stem.lower()
        if stem == "base" or stem == package:
            base_apk = apk
        elif stem.startswith("config.") or stem.startswith("split_config."):
            config_apks.append(apk)
        elif "base" in stem and base_apk is None:
            base_apk = apk
        else:
            config_apks.append(apk)

    if not base_apk:
        base_apk = max(apks, key=lambda p: p.stat().st_size)
        config_apks = [a for a in apks if a != base_apk]

    return base_apk, config_apks


def _merge_splits(
    base_apk: Path, config_apks: list[Path], output: Path
) -> dict[str, int]:
    """Merge resource tables and remove split requirements with APKEditor.

    ZIP overlays cannot merge resources.arsc and must never be used as fallback.
    APKEDITOR_JAR must point to a trusted, independently verified APKEditor jar.
    """
    jar = Path(os.environ.get("APKEDITOR_JAR", ""))
    if not jar.is_file():
        raise RuntimeError("Set APKEDITOR_JAR to a verified APKEditor jar to merge split APKs")
    if output.exists():
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        inputs = staging / "splits"
        inputs.mkdir()
        shutil.copy2(base_apk, inputs / "base.apk")
        for index, apk in enumerate(config_apks):
            shutil.copy2(apk, inputs / f"split-{index}.apk")
        merged = staging / "merged.apk"
        completed = subprocess.run(
            ["java", "-jar", str(jar.resolve()), "m", "-i", str(inputs),
             "-o", str(merged), "-validate-modules"],
            capture_output=True, text=True, timeout=300,
        )
        if completed.returncode or not merged.is_file():
            raise RuntimeError(f"APKEditor merge failed: {completed.stderr or completed.stdout}")
        with zipfile.ZipFile(merged) as archive:
            names = set(archive.namelist())
            if not {"AndroidManifest.xml", "resources.arsc"} <= names or archive.testzip():
                raise RuntimeError("APKEditor produced an invalid merged APK")
        with zipfile.ZipFile(base_apk) as archive:
            added = len(names - set(archive.namelist()))
        shutil.copy2(merged, output)
    return {"added": added}


def _try_sign(apk_path: Path, tmp_dir: Path) -> bool:
    """Try to sign the APK. Returns True if signed successfully."""
    # Prefer apksigner (v2 signing, more compatible)
    apksigner = _find_apksigner()
    if apksigner:
        ks = _ensure_debug_keystore(tmp_dir)
        if ks:
            return _sign_with_apksigner(apksigner, apk_path, ks)

    # Fallback to jarsigner (v1 signing)
    jarsigner = _find_jdk_tool("jarsigner")
    if jarsigner:
        ks = _ensure_debug_keystore(tmp_dir)
        if ks:
            return _sign_with_jarsigner(jarsigner, apk_path, ks)

    return False


def _find_apksigner() -> str | None:
    """Find apksigner in Android SDK build-tools."""
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if not sdk:
        # Common default locations
        for candidate in _sdk_candidates():
            if candidate.exists():
                sdk = str(candidate)
                break
    if not sdk:
        return None
    bt = Path(sdk) / "build-tools"
    if not bt.exists():
        return None
    for v in sorted(bt.iterdir(), reverse=True):
        for name in ("apksigner.bat", "apksigner"):
            p = v / name
            if p.exists():
                return str(p)
    return None


def _sdk_candidates() -> list[Path]:
    """Common Android SDK install locations."""
    home = Path.home()
    return [
        home / "AppData" / "Local" / "Android" / "Sdk",  # Windows
        home / "Android" / "Sdk",  # Linux
        home / "Library" / "Android" / "sdk",  # macOS
    ]


def _find_jdk_tool(name: str) -> str | None:
    """Find a JDK tool (keytool, jarsigner) even if not in PATH."""
    found = shutil.which(name)
    if found:
        return found
    # Search common JDK locations on Windows
    for jdk_root in (Path("C:/Program Files/Java"), Path("C:/Program Files (x86)/Java")):
        if not jdk_root.exists():
            continue
        for jdk_dir in sorted(jdk_root.iterdir(), reverse=True):
            tool = jdk_dir / "bin" / f"{name}.exe"
            if tool.exists():
                return str(tool)
    # JAVA_HOME
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        tool = Path(java_home) / "bin" / (f"{name}.exe" if sys.platform == "win32" else name)
        if tool.exists():
            return str(tool)
    return None


def _ensure_debug_keystore(tmp_dir: Path) -> Path | None:
    """Generate a temporary debug keystore."""
    ks = tmp_dir / "debug.keystore"
    if ks.exists():
        return ks
    keytool = _find_jdk_tool("keytool")
    if not keytool:
        return None
    try:
        subprocess.run(
            [keytool, "-genkeypair",
             "-keystore", str(ks),
             "-alias", "androiddebugkey",
             "-storepass", "android",
             "-keypass", "android",
             "-keyalg", "RSA", "-keysize", "2048",
             "-validity", "10000",
             "-dname", "CN=Android Debug,O=Android,C=US"],
            check=True, capture_output=True, timeout=60,
        )
        return ks
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _sign_with_apksigner(apksigner: str, apk_path: Path, keystore: Path) -> bool:
    try:
        subprocess.run(
            [apksigner, "sign",
             "--ks", str(keystore),
             "--ks-pass", "pass:android",
             "--ks-key-alias", "androiddebugkey",
             "--key-pass", "pass:android",
             str(apk_path)],
            check=True, capture_output=True, timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _sign_with_jarsigner(jarsigner: str, apk_path: Path, keystore: Path) -> bool:
    try:
        subprocess.run(
            [jarsigner,
             "-keystore", str(keystore),
             "-storepass", "android",
             "-keypass", "android",
             "-sigalg", "SHA256withRSA",
             "-digestalg", "SHA-256",
             str(apk_path),
             "androiddebugkey"],
            check=True, capture_output=True, timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
