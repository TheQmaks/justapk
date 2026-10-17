import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from justapk.xapk import _merge_splits


class SplitMergeTests(unittest.TestCase):
    def test_merger_failure_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, jar, output = (root / n for n in ('base.apk', 'merger.jar', 'out.apk'))
            base.touch()
            jar.touch()
            result = subprocess.CompletedProcess([], 1, '', 'merge error')
            with patch.dict(os.environ, {'APKEDITOR_JAR': str(jar)}):
                with patch('justapk.xapk.subprocess.run', return_value=result):
                    with self.assertRaisesRegex(RuntimeError, 'merge error'):
                        _merge_splits(base, [], output)
            self.assertFalse(output.exists())

    def test_merger_output_is_used_and_versions_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, split, jar, output = (root / n for n in ('base.apk', 'split.apk', 'merger.jar', 'out.apk'))
            jar.touch()
            for apk in (base, split):
                with zipfile.ZipFile(apk, 'w') as archive:
                    archive.writestr('AndroidManifest.xml', b'original manifest')
                    archive.writestr('resources.arsc', b'original table')

            def merge(command, **kwargs):
                self.assertIn('-validate-modules', command)
                destination = Path(command[command.index('-o') + 1])
                with zipfile.ZipFile(destination, 'w') as archive:
                    archive.writestr('AndroidManifest.xml', b'merged manifest')
                    archive.writestr('resources.arsc', b'merged table')
                return subprocess.CompletedProcess(command, 0, '', '')

            with patch.dict(os.environ, {'APKEDITOR_JAR': str(jar)}):
                with patch('justapk.xapk.subprocess.run', side_effect=merge):
                    _merge_splits(base, [split], output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read('resources.arsc'), b'merged table')
                self.assertEqual(archive.read('AndroidManifest.xml'), b'merged manifest')

    def test_missing_resource_merger_does_not_publish_incomplete_apk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, split, output = (root / n for n in ('base.apk', 'config.xxhdpi.apk', 'merged.apk'))
            for apk, table in ((base, b'base table'), (split, b'density table')):
                with zipfile.ZipFile(apk, 'w') as archive:
                    archive.writestr('AndroidManifest.xml', b'manifest')
                    archive.writestr('resources.arsc', table)
            with patch.dict(os.environ, {'APKEDITOR_JAR': str(root / 'missing.jar')}):
                with self.assertRaisesRegex(RuntimeError, 'APKEDITOR_JAR'):
                    _merge_splits(base, [split], output)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
