# Contributing to ScrivCheck

Issues and pull requests are welcome.

## The one non-negotiable rule

**The data-safety invariant must hold at every observable point in the code.**

At every moment during a run, each book's original `.scriv` must exist in
at least one of: the local folder, the quarantine/originals directory, or
the quarantine/safety-copies directory. No code path may leave a book
unrecoverable, even if an exception is raised partway through.

This invariant is asserted by `tests/test_validation_flow.py`. If your
change introduces a new failure-handling path, add a test there that
asserts the invariant holds for that path before opening a PR.

## Pull requests

- Keep the test suite at 100% coverage (`python3 -m coverage report -m --fail-under=100`).
- For non-trivial changes, include a before/after description of the
  observable behaviour that changes. Bug fixes should name the failure
  mode they close.
- The macOS-specific code paths (screencapture, AppleScript, Scrivener
  interaction) are mocked at the `subprocess` boundary — new macOS calls
  should follow the same pattern so CI can run on Linux.

## Running the tests

```bash
python3 -m pip install coverage
python3 -m coverage run --source=scrivcheck -m unittest discover -s tests -v
python3 -m coverage report -m --fail-under=100
```
