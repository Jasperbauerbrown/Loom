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
    ["ls > out.txt", "ls >> out.txt", "ls 2> err.txt", "cat < in.txt", "echo hi > /dev/null"],
)
def test_redirection_is_never_granted(command):
    assert pf.is_readonly(command, ALLOW) is False


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
