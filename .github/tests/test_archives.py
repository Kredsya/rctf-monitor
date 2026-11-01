import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from rctf_monitor.archives import extract


class ArchiveTests(unittest.TestCase):
    def test_zip_limits_traversal_and_valid_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, data, limit, valid in [
                ("hello", b"world", 10, True),
                ("../escape", b"x", 10, False),
                ("large", b"xxxx", 2, False),
            ]:
                archive = root / "test.zip"
                target = root / name.replace("/", "_")
                target.mkdir()
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr(name, data)
                if valid:
                    extract(archive, target, limit)
                    self.assertEqual((target / name).read_bytes(), data)
                else:
                    with self.assertRaises(ValueError):
                        extract(archive, target, limit)
            self.assertFalse((root / "escape").exists())

    def test_tar_links_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "test.tar"
            with tarfile.open(archive, "w") as t:
                link = tarfile.TarInfo("link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/tmp"
                t.addfile(link)
            with self.assertRaises(ValueError):
                extract(archive, root / "out", 100)
