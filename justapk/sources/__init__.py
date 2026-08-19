from __future__ import annotations

from justapk.sources.apk20 import APK20Source
from justapk.sources.apkcombo import APKComboSource
from justapk.sources.apkmirror import APKMirrorSource
from justapk.sources.apkpure import APKPureSource
from justapk.sources.base import APKSource
from justapk.sources.fdroid import FDroidSource
from justapk.sources.mi9 import MI9Source
from justapk.sources.uptodown import UptodownSource

SOURCE_REGISTRY: dict[str, type[APKSource]] = {
    "apk20": APK20Source,
    "fdroid": FDroidSource,
    "apkpure": APKPureSource,
    "apkmirror": APKMirrorSource,
    "uptodown": UptodownSource,
    "apkcombo": APKComboSource,
    "mi9": MI9Source,
}

SOURCE_PRIORITY = ["apk20", "fdroid", "apkpure", "apkmirror", "uptodown", "apkcombo", "mi9"]

__all__ = ["SOURCE_PRIORITY", "SOURCE_REGISTRY"]
