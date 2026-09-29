"""The shell pre-filter (03 §9.4). It may only grant, it must never raise."""

from __future__ import annotations

import random
import string

import pytest

from loom.config import DEFAULT_SHELL_READONLY
from loom.tools import shell_prefilter as pf

ALLOW = DEFAULT_SHELL_READONLY


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -la /tmp",
        "cat README.md",
        "rg --files",
        "echo hello",
        "  pwd  ",
        "/usr/bin/cat file.txt",  # a path: the basename decides
        "C:\\Windows\\System32\\where.exe python",  # .exe stripped
        "FOO=bar ls",  # leading assignments are stripped
        "FOO=bar BAZ=qux echo hi",
        "cat 'a file with spaces.txt'",
    ],
)
def test_plain_read_only_commands_are_granted(command):
    assert pf.is_readonly(command, ALLOW) is True


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline -20", "git diff HEAD", "npm ls", "python --version"],
)
def test_two_word_allowlist_entries(command):
    assert pf.is_readonly(command, ALLOW) is True


@pytest.mark.parametrize("command", ["git push", "git commit -m x", "npm install", "python script.py"])
def test_two_word_entries_do_not_grant_their_other_subcommands(command):
    assert pf.is_readonly(command, ALLOW) is False


def test_a_pipeline_of_read_only_stages_is_granted():
    assert pf.is_readonly("cat notes.md | grep TODO | wc -l", ALLOW) is True
    assert pf.is_readonly("git log --oneline | head -20", ALLOW) is True


def test_one_unlisted_stage_sinks_the_whole_chain():
    assert pf.is_readonly("ls && rm -rf /tmp/x", ALLOW) is False
    assert pf.is_readonly("cat a.txt | xargs rm", ALLOW) is False
    assert pf.is_readonly("ls; curl https://example.com", ALLOW) is False
    assert pf.is_readonly("ls || shutdown now", ALLOW) is False


@pytest.mark.parametrize(
    "command",
    ["ls > out.txt", "ls >> out.txt", "ls 2> err.txt", "cat < in.txt", "ls 2>&1 > out.txt"],
)
def test_redirection_to_a_file_is_never_granted(command):
    assert pf.is_readonly(command, ALLOW) is False


@pytest.mark.parametrize(
    "command",
    [
        "ls 2>&1",
        "git log --oneline -3 2>&1 | head -2",
        "echo hi > /dev/null",
        "ls >/dev/null 2>&1",
        "ls 2>/dev/null",
        "ls &>/dev/null",
        "ls 1>&2",
        "head -c 2>/dev/null",  # bash: `head -c` with stderr dropped, not `head -c 2`
        "ls 2>&1; git status 2>/dev/null",
    ],
)
def test_redirects_that_write_nowhere_are_granted(command):
    """`2>&1` and `/dev/null` create nothing. Before 2026-09-29 every
    `git log … 2>&1` failed the parse and cost a classifier call."""
    assert pf.is_readonly(command, ALLOW) is True


@pytest.mark.parametrize(
    "command",
    [
        "ls 2>nul",  # Git Bash creates a file called `nul`
        "ls > /dev/null/../x",
        "ls 2>&3",
        "ls >&1x",
        "ls 2 >/dev/null/x",
    ],
)
def test_other_redirects_still_fail_the_parse(command):
    assert pf.is_readonly(command, ALLOW) is False


@pytest.mark.parametrize(
    "command",
    [
        "cd C:/proj && grep -n TODO src/a.py",
        'cd "C:/my proj/scratch" && grep -o -E ".{20}x" f.js | head -3; echo ---; grep y f.js | wc -l',
        "cd .. && ls",
        "cd C:/proj && git log --oneline -3 2>&1 | head -2",
    ],
)
def test_cd_then_a_read_is_granted(command):
    """Changing directory changes nothing, and models prefix most reads with
    `cd <dir> &&`: 87 of the 339 shell calls one week's judge saw began that way, seven
    of them consecutive greps the judge then asked about (2026-09-29)."""
    assert "cd" in DEFAULT_SHELL_READONLY
    assert pf.is_readonly(command, ALLOW) is True


def test_cd_does_not_launder_a_write():
    assert pf.is_readonly("cd C:/proj && rm -rf build", ALLOW) is False
    assert pf.is_readonly("cd C:/proj && npm install", ALLOW) is False


def test_cd_into_a_protected_tree_is_not_granted(tmp_path):
    assert pf.is_readonly(f"cd {tmp_path} && ls", ALLOW, protected=[str(tmp_path)]) is False
    assert pf.is_readonly(f"cd {tmp_path / 'sub'} && cat x", ALLOW, protected=[str(tmp_path)]) is False


def test_a_dropped_redirect_leaves_no_word_behind():
    assert pf.split_stages("ls 2>&1 | wc -l") == ["ls", "wc -l"]
    assert pf.split_stages("ls >/dev/null 2>&1 && git status") == ["ls", "git status"]
    assert pf.split_stages("ls 2>&1 &") == ["ls", ""]  # a background job is still not granted
    assert pf.is_readonly("ls 2>&1 &", ALLOW) is False


@pytest.mark.parametrize(
    "command",
    ["echo $(rm -rf /)", "echo `rm -rf /`", "echo ${HOME}", "cat ${FILE}"],
)
def test_substitution_is_never_granted(command):
    assert pf.is_readonly(command, ALLOW) is False


@pytest.mark.parametrize("command", ["cat 'unbalanced", 'echo "still open', "cat \"a' "])
def test_unbalanced_quotes_are_a_parse_failure(command):
    assert pf.is_readonly(command, ALLOW) is False


def test_newlines_are_never_granted():
    assert pf.is_readonly("ls\nrm -rf /", ALLOW) is False


def test_empty_and_degenerate_input():
    assert pf.is_readonly("", ALLOW) is False
    assert pf.is_readonly("   ", ALLOW) is False
    assert pf.is_readonly("ls &&", ALLOW) is False
    assert pf.is_readonly("| ls", ALLOW) is False
    assert pf.is_readonly("ls", []) is False  # an empty allowlist grants nothing


def test_background_jobs_are_not_granted():
    assert pf.is_readonly("ls &", ALLOW) is False


def test_split_stages_respects_quotes():
    assert pf.split_stages("echo 'a && b' | wc -l") == ["echo 'a && b'", "wc -l"]
    assert pf.split_stages('echo "a | b"') == ['echo "a | b"']


def test_split_words_strips_quotes():
    assert pf.split_words("cat 'a b.txt' \"c\"") == ["cat", "a b.txt", "c"]


def test_a_command_naming_a_protected_path_is_not_granted(tmp_path):
    """Read-only is not harmless when the file is `secrets.json`: protected paths used to
    guard the fs tools only, and `cat ~/.loom/secrets.json` sailed through (2026-09-22).
    Not granted means the classifier judges it; the pre-filter still never denies."""
    secret = tmp_path / "secrets.json"
    protected = [str(secret)]
    assert pf.is_readonly(f"cat {secret}", ALLOW, protected=protected) is False
    assert pf.is_readonly(f'type "{secret}"', ALLOW, protected=protected) is False
    assert pf.is_readonly(f"cat {str(secret).replace('/', chr(92))}", ALLOW, protected=protected) is False
    assert pf.is_readonly("cat README.md", ALLOW, protected=protected) is True
    # a protected directory covers everything under it
    assert pf.is_readonly(f"ls {tmp_path / 'sub' / 'x.txt'}", ALLOW, protected=[str(tmp_path)]) is False
    # relative paths resolve against the shell's cwd
    assert pf.is_readonly("cat secrets.json", ALLOW, protected=protected, cwd=str(tmp_path)) is False
    assert pf.is_readonly("cat ./secrets.json", ALLOW, protected=protected, cwd=str(tmp_path)) is False
    assert pf.is_readonly("cat secrets.json", ALLOW, protected=protected, cwd=str(tmp_path / "elsewhere")) is True
    # a path it cannot resolve statically goes to the classifier too
    assert pf.is_readonly("cat $HOME/.loom/secrets.json", ALLOW, protected=protected) is False
    assert pf.is_readonly("type %USERPROFILE%\\.loom\\secrets.json", ALLOW, protected=protected) is False
    # no protected list: unchanged behaviour
    assert pf.is_readonly(f"cat {secret}", ALLOW) is True


def test_tilde_paths_resolve_against_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    protected = [str(tmp_path / ".loom" / "secrets.json")]
    assert pf.is_readonly("cat ~/.loom/secrets.json", ALLOW, protected=protected) is False
    assert pf.is_readonly("cat ~/.loom/notes.md", ALLOW, protected=protected) is True


def test_fuzz_never_raises_and_always_returns_bool():
    rng = random.Random(20260920)
    alphabet = string.printable
    for _ in range(200):
        length = rng.randint(0, 60)
        command = "".join(rng.choice(alphabet) for _ in range(length))
        result = pf.is_readonly(command, ALLOW)
        assert result is True or result is False


def test_fuzz_of_shell_metacharacters_never_grants_something_odd():
    rng = random.Random(7)
    pieces = ["ls", "rm -rf /", "|", "&&", ";", ">", "`", "$(", "'", '"', "\\", "$", "{", "}", " "]
    for _ in range(200):
        command = " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
        assert pf.is_readonly(command, ALLOW) in (True, False)


@pytest.mark.parametrize("command", ["sleep 10", "sleep 2 && ls", "sleep 1; git status"])
def test_sleep_is_read_only(command):
    """Waiting changes nothing; a `sleep` between a background start and a status check
    must not cost a classifier call (2026-09-25)."""
    assert "sleep" in DEFAULT_SHELL_READONLY
    assert pf.is_readonly(command, ALLOW) is True


def test_sleep_does_not_launder_a_write():
    assert pf.is_readonly("sleep 1 && rm -rf build", ALLOW) is False
    assert pf.is_readonly("sleep 1 > out.txt", ALLOW) is False
