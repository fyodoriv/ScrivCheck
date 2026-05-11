"""
macOS-side helper functions.

Everything that shells out to AppleScript, `screencapture`, `pgrep`, or
`brctl` is tested here with `subprocess.run` mocked. The point is to
nail the contract between the Python wrapper and the shell tool — what
command is invoked, how its stdout/stderr is interpreted, what the
function returns or raises — so that a future refactor of the wrapper
can't silently change behaviour relied on by `validate_book`.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import make_fake_scriv, SAMPLE_BOOK
import scrivcheck as vsb


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["mock"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class RunHelperTests(unittest.TestCase):
    def test_run_passes_args_through(self):
        with mock.patch("scrivcheck.subprocess.run") as mrun:
            mrun.return_value = _completed(stdout="hi")
            result = vsb.run(["echo", "hi"])
            self.assertEqual(result.stdout, "hi")
            args, kwargs = mrun.call_args
            self.assertEqual(args[0], ["echo", "hi"])
            self.assertTrue(kwargs["check"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])

    def test_run_with_check_false(self):
        with mock.patch("scrivcheck.subprocess.run") as mrun:
            mrun.return_value = _completed(returncode=2)
            vsb.run(["false"], check=False)
            self.assertFalse(mrun.call_args.kwargs["check"])


class OsascriptTests(unittest.TestCase):
    def test_returns_stripped_stdout(self):
        with mock.patch("scrivcheck.subprocess.run") as mrun:
            mrun.return_value = _completed(stdout="  result  \n")
            self.assertEqual(vsb.osascript('tell application "X"'), "result")

    def test_failure_raises_with_stderr_attached(self):
        """The exception MUST carry stderr — that's where AppleScript puts
        actionable error messages (e.g. 'Not authorized to send Apple events')."""
        with mock.patch("scrivcheck.subprocess.run") as mrun:
            mrun.return_value = _completed(
                returncode=1, stderr="permission denied", stdout="",
            )
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                vsb.osascript('bad script')
            self.assertEqual(ctx.exception.stderr, "permission denied")
            self.assertEqual(ctx.exception.returncode, 1)


class ScrivenerRunningTests(unittest.TestCase):
    def test_running_when_pgrep_finds_process(self):
        with mock.patch("scrivcheck.subprocess.run") as mrun:
            mrun.return_value = _completed(returncode=0)
            self.assertTrue(vsb.scrivener_running())

    def test_not_running_when_pgrep_returns_nonzero(self):
        with mock.patch("scrivcheck.subprocess.run") as mrun:
            mrun.return_value = _completed(returncode=1)
            self.assertFalse(vsb.scrivener_running())


class RemovedScrivenerHelpersTests(unittest.TestCase):
    """Sanity checks that the broken Scrivener AppleScript helpers were
    removed when we dropped the auto-save path.

    Scrivener 3's AppleScript dictionary rejects every ``save`` form
    (``save front document``, ``save every document``, per-document
    iteration — all -1708 errAEEventNotHandled). Worse, even
    ``quit saving yes`` does not fire SCRBackUpOnManualSave: the hook
    is gated on user-initiated Cmd+S only, so AppleScript can never
    trigger a fresh backup. Validating the latest *existing* backup is
    the honest, focus-respecting contract; manual save in Scrivener is
    the prerequisite the user controls.

    These tests fail loudly if a future refactor reintroduces any of
    these without first verifying it works against live Scrivener 3.
    """

    def test_scrivener_save_active_does_not_exist(self):
        self.assertFalse(hasattr(vsb, "scrivener_save_active"))

    def test_scrivener_open_does_not_exist(self):
        self.assertFalse(hasattr(vsb, "scrivener_open"))

    def test_scrivener_quit_does_not_exist(self):
        self.assertFalse(hasattr(vsb, "scrivener_quit"))


class ScreencaptureTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("shot-test")
        self.log.addHandler(logging.NullHandler())

    def test_disabled_returns_none_without_calling_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.run") as mrun:
            out = vsb.screencapture(Path(tmp) / "shot.png", self.log, enabled=False)
            self.assertIsNone(out)
            mrun.assert_not_called()

    def test_success_returns_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.run") as mrun:
            mrun.return_value = _completed()
            target = Path(tmp) / "deep" / "shot.png"
            out = vsb.screencapture(target, self.log, enabled=True)
            self.assertEqual(out, str(target))
            # screencapture -x must be invoked
            cmd = mrun.call_args.args[0]
            self.assertIn("screencapture", cmd[0])
            self.assertIn("-x", cmd)
            # parent dir must have been created
            self.assertTrue(target.parent.exists())

    def test_failure_logs_warning_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.run",
                        side_effect=subprocess.CalledProcessError(1, "screencapture")):
            out = vsb.screencapture(Path(tmp) / "shot.png", self.log, enabled=True)
            self.assertIsNone(out)

    def test_timeout_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.run",
                        side_effect=subprocess.TimeoutExpired("screencapture", 10)):
            out = vsb.screencapture(Path(tmp) / "shot.png", self.log, enabled=True)
            self.assertIsNone(out)


class ScrivenerShotFilterTests(unittest.TestCase):
    """_scrivener_shot skips books not in the screenshot_books list."""

    def _make_validator(self, tmp, screenshot_books):
        run_dir = Path(tmp) / "run"
        (run_dir / "logs").mkdir(parents=True)
        log = logging.getLogger(f"ssf-{id(self)}")
        log.addHandler(logging.NullHandler())
        return vsb.Validator(
            local_dir=Path(tmp),
            backup_dir=Path(tmp),
            run_dir=run_dir,
            log=log,
            screenshots=True,
            screenshot_books=screenshot_books,
        )

    def test_screenshots_disabled_returns_immediately(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.run") as mrun:
            run_dir = Path(tmp) / "run"
            (run_dir / "logs").mkdir(parents=True)
            log = logging.getLogger(f"ssf-dis-{id(self)}")
            log.addHandler(logging.NullHandler())
            v = vsb.Validator(
                local_dir=Path(tmp), backup_dir=Path(tmp),
                run_dir=run_dir, log=log, screenshots=False,
            )
            book = vsb.BookResult(name="X", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            mrun.assert_not_called()
            self.assertEqual(book.screenshots, [])

    def test_unlisted_book_skips_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.run") as mrun:
            v = self._make_validator(tmp, ("IncludedBook",))
            book = vsb.BookResult(name="OtherBook", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            mrun.assert_not_called()
            self.assertEqual(book.screenshots, [])

    def test_listed_book_proceeds_to_open_scrivener(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run") as mrun, \
             mock.patch("scrivcheck.time") as mtime:
            mrun.return_value = _completed()
            v = self._make_validator(tmp, ("MyBook",))
            book = vsb.BookResult(name="MyBook", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            open_call = mrun.call_args_list[0].args[0]
            self.assertIn("Scrivener", open_call)

    def test_none_screenshot_books_captures_any_book(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run") as mrun, \
             mock.patch("scrivcheck.time"):
            mrun.return_value = _completed()
            v = self._make_validator(tmp, None)
            book = vsb.BookResult(name="AnyBook", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            open_call = mrun.call_args_list[0].args[0]
            self.assertIn("Scrivener", open_call)

    def test_case_insensitive_match(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run") as mrun, \
             mock.patch("scrivcheck.time"):
            mrun.return_value = _completed()
            v = self._make_validator(tmp, ("mybook",))
            book = vsb.BookResult(name="MyBook", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            self.assertTrue(mrun.called)

    def test_quit_scrivener_when_not_previously_running(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run") as mrun, \
             mock.patch("scrivcheck.time"):
            mrun.return_value = _completed()
            v = self._make_validator(tmp, None)
            book = vsb.BookResult(name="X", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            osascript_calls = [
                c.args[0] for c in mrun.call_args_list
                if c.args and "osascript" in c.args[0]
            ]
            self.assertTrue(any("quit" in " ".join(c) for c in osascript_calls))

    def test_close_document_when_scrivener_was_running(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=True), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run") as mrun, \
             mock.patch("scrivcheck.time"):
            mrun.return_value = _completed()
            v = self._make_validator(tmp, None)
            book = vsb.BookResult(name="X", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            osascript_calls = [
                c.args[0] for c in mrun.call_args_list
                if c.args and "osascript" in c.args[0]
            ]
            self.assertTrue(any("close document" in " ".join(c) for c in osascript_calls))
            self.assertFalse(any("quit" in " ".join(c) for c in osascript_calls))

    def test_screenshot_appended_when_screencapture_returns_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value="/tmp/shot.png"), \
             mock.patch("scrivcheck.run") as mrun, \
             mock.patch("scrivcheck.time"):
            mrun.return_value = _completed()
            v = self._make_validator(tmp, None)
            book = vsb.BookResult(name="X", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))
            self.assertEqual(book.screenshots, ["/tmp/shot.png"])

    def test_exception_in_open_is_logged_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run", side_effect=OSError("no such app")), \
             mock.patch("scrivcheck.time"):
            v = self._make_validator(tmp, None)
            book = vsb.BookResult(name="X", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))  # must not raise
            self.assertEqual(book.screenshots, [])

    def test_exception_in_cleanup_is_suppressed(self):
        call_count = [0]
        def run_side_effect(cmd, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return _completed()  # open -a Scrivener succeeds
            raise OSError("cleanup failed")  # quit/close raises
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("scrivcheck.scrivener_running", return_value=False), \
             mock.patch("scrivcheck.screencapture", return_value=None), \
             mock.patch("scrivcheck.run", side_effect=run_side_effect), \
             mock.patch("scrivcheck.time"):
            v = self._make_validator(tmp, None)
            book = vsb.BookResult(name="X", project_path="/x.scriv")
            v._scrivener_shot(book, Path("/x.scriv"))  # must not raise


class StripRtfTests(unittest.TestCase):
    def _rtf(self, body: str) -> bytes:
        return (
            r"{\rtf1\ansi\cocoartf2639"
            r"{\fonttbl\f0\fswiss Helvetica;}"
            r"{\colortbl;}"
            r"\pard\pardeftab720 "
            + body
            + "}"
        ).encode()

    def test_extracts_plain_ascii_text(self):
        data = self._rtf(r"\f0\fs24 \cf0 Hello, world.")
        self.assertEqual(vsb.strip_rtf(data), "Hello, world.")

    def test_unicode_escapes_with_uc0_prefix_decoded(self):
        # \uc0\uN form: first char in each paragraph
        data = self._rtf("\\uc0\\u1048 \\u1078 ")
        result = vsb.strip_rtf(data)
        self.assertIn("И", result)  # U+1048
        self.assertIn("ж", result)  # U+1078, bare \uN

    def test_unicode_escapes_bare_form_decoded(self):
        # Scrivener emits bare \uN for subsequent chars after the first \uc0
        data = self._rtf("\\u1055 \\u1088 \\u1080 \\u1074 \\u1077 \\u1090 ")
        result = vsb.strip_rtf(data)
        self.assertIn("Привет", result)

    def test_hex_escapes_decoded(self):
        data = self._rtf(r"caf\'e9")
        self.assertIn("é", vsb.strip_rtf(data))

    def test_par_becomes_newline(self):
        data = self._rtf(r"first\par second")
        result = vsb.strip_rtf(data)
        self.assertIn("first", result)
        self.assertIn("second", result)
        self.assertIn("\n", result)

    def test_empty_rtf_returns_empty(self):
        self.assertEqual(vsb.strip_rtf(b""), "")

    def test_utf8_text_passes_through(self):
        # Modern Scrivener files store UTF-8 Cyrillic directly
        data = self._rtf("Привет")
        self.assertIn("Привет", vsb.strip_rtf(data))


class EnsureLocallyAvailableTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("ela-test")
        self.log.addHandler(logging.NullHandler())

    def test_readable_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f"
            p.write_bytes(b"hello")
            with mock.patch("scrivcheck.subprocess.run") as mrun:
                vsb.ensure_locally_available(p, self.log)
                # brctl must NOT have been called for a readable file
                mrun.assert_not_called()

    def test_unreadable_file_falls_back_to_brctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "online-only"
            # File whose open() raises OSError (does not exist)
            with mock.patch("scrivcheck.subprocess.run") as mrun:
                vsb.ensure_locally_available(p, self.log)
                mrun.assert_called_once()
                self.assertEqual(mrun.call_args.args[0][:2], ["brctl", "download"])


class ManifestStatErrorTests(unittest.TestCase):
    """If a file disappears between os.walk listing it and our stat() call,
    we must skip it cleanly rather than aborting the manifest."""

    def test_oserror_during_stat_skips_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            scriv = make_fake_scriv(Path(tmp), "Book", SAMPLE_BOOK)

            real_stat = Path.stat
            problem = scriv / "Files/Data/UUID-2/content.rtf"

            def flaky_stat(self, *a, **kw):
                if self == problem:
                    raise OSError("vanished")
                return real_stat(self, *a, **kw)

            with mock.patch.object(Path, "stat", flaky_stat):
                m = vsb.compute_manifest(scriv)
            paths = {f.relpath for f in m.files}
            self.assertNotIn("Files/Data/UUID-2/content.rtf", paths)


class FileSha256Tests(unittest.TestCase):
    def test_known_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x"
            p.write_bytes(b"abc")
            # echo -n abc | shasum -a 256 -> ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
            self.assertEqual(
                vsb.file_sha256(p),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_chunk_boundary(self):
        """Chunk size mustn't truncate or duplicate data."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x"
            p.write_bytes(b"a" * (1 << 17))  # 128KB → spans chunk boundary
            d1 = vsb.file_sha256(p, chunk=1 << 16)
            d2 = vsb.file_sha256(p, chunk=37)  # weird small chunk
            self.assertEqual(d1, d2)


class SetupLoggingTests(unittest.TestCase):
    def test_creates_handlers_and_writes_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "logs").mkdir()
            log = vsb.setup_logging(run_dir)
            log.info("hello-from-test")
            for h in log.handlers:
                h.flush()
            log_file = run_dir / "logs" / "run.log"
            self.assertTrue(log_file.exists())
            self.assertIn("hello-from-test", log_file.read_text())
            # Clean up handlers so they don't leak into other tests
            for h in list(log.handlers):
                h.close()
                log.removeHandler(h)

    def test_idempotent_does_not_double_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "logs").mkdir()
            log1 = vsb.setup_logging(run_dir)
            n1 = len(log1.handlers)
            log2 = vsb.setup_logging(run_dir)
            self.assertEqual(len(log2.handlers), n1)
            for h in list(log2.handlers):
                h.close()
                log2.removeHandler(h)


class DataclassToDictTests(unittest.TestCase):
    def test_manifest_to_dict_is_json_safe(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            scriv = make_fake_scriv(Path(tmp), "Book", SAMPLE_BOOK)
            m = vsb.compute_manifest(scriv)
            d = m.to_dict()
            # Must round-trip through JSON without surprises
            payload = json.dumps(d)
            again = json.loads(payload)
            self.assertEqual(again["project"], "Book")
            self.assertEqual(again["file_count"], len(SAMPLE_BOOK))
            self.assertEqual(len(again["files"]), len(SAMPLE_BOOK))

    def test_book_result_to_dict_with_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            scriv = make_fake_scriv(Path(tmp), "Book", SAMPLE_BOOK)
            m = vsb.compute_manifest(scriv)
            book = vsb.BookResult(name="Book", project_path=str(scriv))
            book.pre_manifest = m
            book.post_manifest = m
            book.status = "PASS"
            book.add_step("hello", True, "world")
            d = book.to_dict()
            self.assertEqual(d["status"], "PASS")
            self.assertEqual(d["name"], "Book")
            self.assertIsNotNone(d["pre_manifest"])
            self.assertEqual(len(d["steps"]), 1)
            self.assertEqual(d["steps"][0]["name"], "hello")

    def test_book_result_to_dict_without_manifests(self):
        book = vsb.BookResult(name="X", project_path="/nope")
        d = book.to_dict()
        self.assertIsNone(d["pre_manifest"])
        self.assertIsNone(d["post_manifest"])


if __name__ == "__main__":
    unittest.main()
