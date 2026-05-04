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
import validate_scrivener_backups as vsb


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["mock"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class RunHelperTests(unittest.TestCase):
    def test_run_passes_args_through(self):
        with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
            mrun.return_value = _completed(stdout="hi")
            result = vsb.run(["echo", "hi"])
            self.assertEqual(result.stdout, "hi")
            args, kwargs = mrun.call_args
            self.assertEqual(args[0], ["echo", "hi"])
            self.assertTrue(kwargs["check"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])

    def test_run_with_check_false(self):
        with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
            mrun.return_value = _completed(returncode=2)
            vsb.run(["false"], check=False)
            self.assertFalse(mrun.call_args.kwargs["check"])


class OsascriptTests(unittest.TestCase):
    def test_returns_stripped_stdout(self):
        with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
            mrun.return_value = _completed(stdout="  result  \n")
            self.assertEqual(vsb.osascript('tell application "X"'), "result")

    def test_failure_raises_with_stderr_attached(self):
        """The exception MUST carry stderr — that's where AppleScript puts
        actionable error messages (e.g. 'Not authorized to send Apple events')."""
        with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
            mrun.return_value = _completed(
                returncode=1, stderr="permission denied", stdout="",
            )
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                vsb.osascript('bad script')
            self.assertEqual(ctx.exception.stderr, "permission denied")
            self.assertEqual(ctx.exception.returncode, 1)


class ScrivenerRunningTests(unittest.TestCase):
    def test_running_when_pgrep_finds_process(self):
        with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
            mrun.return_value = _completed(returncode=0)
            self.assertTrue(vsb.scrivener_running())

    def test_not_running_when_pgrep_returns_nonzero(self):
        with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
            mrun.return_value = _completed(returncode=1)
            self.assertFalse(vsb.scrivener_running())


class ScrivenerQuitTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("quit-test")
        self.log.addHandler(logging.NullHandler())

    def test_no_op_when_not_running(self):
        with mock.patch("validate_scrivener_backups.scrivener_running", return_value=False), \
             mock.patch("validate_scrivener_backups.osascript") as mosa:
            vsb.scrivener_quit(self.log)
            mosa.assert_not_called()

    def test_runs_applescript_quit_saving_yes_when_running(self):
        running_states = iter([True, False])  # running once, then exited
        def fake_running():
            return next(running_states)
        with mock.patch("validate_scrivener_backups.scrivener_running", side_effect=fake_running), \
             mock.patch("validate_scrivener_backups.osascript") as mosa, \
             mock.patch("validate_scrivener_backups.time.sleep"):
            vsb.scrivener_quit(self.log)
            mosa.assert_called_once()
            script = mosa.call_args.args[0]
            self.assertIn("Scrivener", script)
            # `quit saving yes` is the Standard-Suite verb that DOES work
            # in Scrivener 3 (verified live; commit log has the receipts).
            # Pin the literal so a refactor can't regress to a `save`-loop
            # form that Scrivener doesn't understand.
            self.assertIn("quit saving yes", script)
            self.assertNotIn("save every document", script)
            self.assertNotIn("repeat with", script)

    def test_warns_but_does_not_raise_when_applescript_fails(self):
        # Even if AppleScript fails, if Scrivener has actually quit we
        # should not propagate the error.
        running = iter([True, False])
        with mock.patch("validate_scrivener_backups.scrivener_running",
                        side_effect=lambda: next(running)), \
             mock.patch("validate_scrivener_backups.osascript",
                        side_effect=subprocess.CalledProcessError(1, "osascript", "", "boom")), \
             mock.patch("validate_scrivener_backups.time.sleep"):
            vsb.scrivener_quit(self.log)  # must not raise

    def test_raises_if_process_never_exits(self):
        # Scrivener reports running forever
        with mock.patch("validate_scrivener_backups.scrivener_running", return_value=True), \
             mock.patch("validate_scrivener_backups.osascript"), \
             mock.patch("validate_scrivener_backups.time.sleep"), \
             mock.patch("validate_scrivener_backups.time.time",
                        side_effect=[0, 1, 999]):  # start, loop check, deadline exceeded
            with self.assertRaises(RuntimeError):
                vsb.scrivener_quit(self.log)


class ScrivenerOpenTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("open-test")
        self.log.addHandler(logging.NullHandler())

    def test_open_invokes_open_command_in_background(self):
        """The `-g` flag is required so opening Scrivener doesn't steal
        the user's window focus during the drill."""
        with mock.patch("validate_scrivener_backups.run") as mrun, \
             mock.patch("validate_scrivener_backups.time.sleep"):
            vsb.scrivener_open(Path("/tmp/MyBook.scriv"), self.log)
            args = mrun.call_args.args[0]
            self.assertEqual(args[:4], ["open", "-g", "-a", "Scrivener"])
            self.assertEqual(args[4], "/tmp/MyBook.scriv")

    def test_save_active_function_does_not_exist(self):
        """Sanity check: the broken save helper was removed.

        Scrivener 3's AppleScript dictionary rejects every ``save`` form
        we tried (``save front document``, ``save every document``, and
        ``save d`` inside ``repeat with d in documents`` all raise -1708
        errAEEventNotHandled). Saving is now ridden by ``quit saving yes``
        which lives inside :func:`scrivener_quit`. If a future refactor
        re-introduces a standalone save helper without first verifying it
        works against Scrivener 3, this test fails to flag the risk.
        """
        self.assertFalse(hasattr(vsb, "scrivener_save_active"))


class ScreencaptureTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("shot-test")
        self.log.addHandler(logging.NullHandler())

    def test_disabled_returns_none_without_calling_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("validate_scrivener_backups.run") as mrun:
            out = vsb.screencapture(Path(tmp) / "shot.png", self.log, enabled=False)
            self.assertIsNone(out)
            mrun.assert_not_called()

    def test_success_returns_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("validate_scrivener_backups.run") as mrun:
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
             mock.patch("validate_scrivener_backups.run",
                        side_effect=subprocess.CalledProcessError(1, "screencapture")):
            out = vsb.screencapture(Path(tmp) / "shot.png", self.log, enabled=True)
            self.assertIsNone(out)

    def test_timeout_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("validate_scrivener_backups.run",
                        side_effect=subprocess.TimeoutExpired("screencapture", 10)):
            out = vsb.screencapture(Path(tmp) / "shot.png", self.log, enabled=True)
            self.assertIsNone(out)


class EnsureLocallyAvailableTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("ela-test")
        self.log.addHandler(logging.NullHandler())

    def test_readable_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f"
            p.write_bytes(b"hello")
            with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
                vsb.ensure_locally_available(p, self.log)
                # brctl must NOT have been called for a readable file
                mrun.assert_not_called()

    def test_unreadable_file_falls_back_to_brctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "online-only"
            # File whose open() raises OSError (does not exist)
            with mock.patch("validate_scrivener_backups.subprocess.run") as mrun:
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
