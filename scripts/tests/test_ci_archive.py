"""Real archive-boundary checks for the CI package reader."""

import importlib.util
import io
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "clipboard_ci", Path(__file__).parents[1] / "ci.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class ArchiveBoundary(unittest.TestCase):
    def test_tar_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo("../outside")
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises(ValueError):
                ci.archive_files(path)

    def test_tar_rejects_links(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo("link")
                    member.type = kind
                    member.linkname = "outside"
                    archive.addfile(member)
                with self.assertRaises(ValueError):
                    ci.archive_files(path)

    def test_zip_rejects_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("entry", b"first")
                with self.assertWarns(UserWarning):
                    archive.writestr("entry", b"second")
            with self.assertRaises(ValueError):
                ci.archive_files(path)

    def test_zip_rejects_special_entries_with_directory_names(self):
        for kind in [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.zip"
                entry = zipfile.ZipInfo("entry/")
                entry.create_system = 3
                entry.external_attr = (kind | 0o755) << 16
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(entry, b"")
                with self.assertRaises(ValueError):
                    ci.archive_files(path)

    def test_zip_accepts_normal_directory_metadata(self):
        for kind in [0, stat.S_IFDIR]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "normal.zip"
                entry = zipfile.ZipInfo("folder/")
                entry.create_system = 3
                entry.external_attr = (kind | 0o755) << 16
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(entry, b"")
                    archive.writestr("folder/content", b"safe")
                self.assertEqual(set(ci.archive_files(path)), {"folder/content"})

    def test_rejects_windows_and_empty_file_paths(self):
        for name in ["C:/outside", "../outside", "folder\\outside"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                ci.member_name(name)

    def test_package_readback_keeps_bytes_and_execution_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "stage"
            stage.mkdir()
            executable = stage / "program"
            executable.write_bytes(b"synthetic executable bytes")
            executable.chmod(0o755)
            for name in ["checked.tar.gz", "checked.zip"]:
                with self.subTest(name=name):
                    receipt = ci.package_archive(stage, root / name)
                    self.assertEqual(len(receipt["members"]), 1)
                    member = receipt["members"][0]
                    self.assertEqual(member["name"], "program")
                    self.assertEqual(member["sha256"], ci.digest(executable))
                    self.assertEqual(
                        member["mode"], oct(executable.stat().st_mode & 0o777)
                    )


if __name__ == "__main__":
    unittest.main()
