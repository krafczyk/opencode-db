"""Bounded local Git evidence collection for sibling move validation.

This internal module runs no-shell, no-stdin Git probes against operator-selected
directories. It does not contact remotes or mutate checkout content.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit


Evidence = TypeVar("Evidence")


def collect_evidence(
    path: str,
    side: str,
    *,
    evidence_factory: Callable[[str, str, str | None, str, str, str], Evidence],
    move_error: type[Exception],
    operational_error: type[Exception],
    display: Callable[[str], str],
    maximum_output_bytes: int,
    timeout_seconds: float,
    cleanup_timeout_seconds: float,
) -> Evidence:
    """Collect bounded identity, checkout, and absolute Git-admin evidence."""
    root = _single_line(
        git_output(
            path,
            side,
            ("rev-parse", "--show-toplevel"),
            "repository root",
            move_error=move_error,
            operational_error=operational_error,
            display=display,
            maximum_output_bytes=maximum_output_bytes,
            timeout_seconds=timeout_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        ),
        "repository root",
        move_error,
    )
    if root != os.path.normpath(path):
        raise move_error(f"git {side} path is not a worktree root: path={display(path)}")
    origin_result = git_output(
        path,
        side,
        ("config", "--get", "remote.origin.url"),
        "origin",
        allow_missing=True,
        move_error=move_error,
        operational_error=operational_error,
        display=display,
        maximum_output_bytes=maximum_output_bytes,
        timeout_seconds=timeout_seconds,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )
    branch_result = git_output(
        path,
        side,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        "checkout state",
        allow_missing=True,
        move_error=move_error,
        operational_error=operational_error,
        display=display,
        maximum_output_bytes=maximum_output_bytes,
        timeout_seconds=timeout_seconds,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )
    head = _commit(
        _single_line(
            git_output(
                path,
                side,
                ("rev-parse", "HEAD"),
                "HEAD",
                move_error=move_error,
                operational_error=operational_error,
                display=display,
                maximum_output_bytes=maximum_output_bytes,
                timeout_seconds=timeout_seconds,
                cleanup_timeout_seconds=cleanup_timeout_seconds,
            ),
            "HEAD",
            move_error,
        ),
        "HEAD",
        move_error,
    )
    git_dir = _absolute_git_path(
        git_output(
            path,
            side,
            ("rev-parse", "--absolute-git-dir"),
            "Git directory",
            move_error=move_error,
            operational_error=operational_error,
            display=display,
            maximum_output_bytes=maximum_output_bytes,
            timeout_seconds=timeout_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        ),
        "Git directory",
        move_error,
    )
    common_dir = _absolute_git_path(
        git_output(
            path,
            side,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            "Git common directory",
            move_error=move_error,
            operational_error=operational_error,
            display=display,
            maximum_output_bytes=maximum_output_bytes,
            timeout_seconds=timeout_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        ),
        "Git common directory",
        move_error,
    )
    if origin_result is None:
        identity = f"root:{_root_commit(path, side, move_error, operational_error, display, maximum_output_bytes, timeout_seconds, cleanup_timeout_seconds)}"
    else:
        normalized = _normalize_origin(_single_line(origin_result, "origin", move_error), move_error)
        identity = (
            f"root:{_root_commit(path, side, move_error, operational_error, display, maximum_output_bytes, timeout_seconds, cleanup_timeout_seconds)}"
            if normalized is None
            else "origin:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        )
    if branch_result is None:
        return evidence_factory(identity, "detached", None, head, git_dir, common_dir)
    branch = _single_line(branch_result, "branch", move_error)
    if not branch or any(character.isspace() or ord(character) < 32 for character in branch):
        raise move_error(f"git {side} branch output is malformed: path={display(path)}")
    return evidence_factory(identity, "attached", branch, head, git_dir, common_dir)


def git_output(
    path: str,
    side: str,
    arguments: tuple[str, ...],
    label: str,
    *,
    allow_missing: bool = False,
    move_error: type[Exception],
    operational_error: type[Exception],
    display: Callable[[str], str],
    maximum_output_bytes: int,
    timeout_seconds: float,
    cleanup_timeout_seconds: float,
) -> str | None:
    """Run one bounded local Git probe and confirm child cleanup on failure."""
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    output = bytearray()
    try:
        process = subprocess.Popen(
            ["git", "-C", path, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise operational_error("git probe could not capture stdout")
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise operational_error("git probe timed out")
            events = selector.select(remaining)
            if not events:
                if process.poll() is not None:
                    break
                continue
            for key, _event in events:
                chunk = os.read(key.fd, min(4096, maximum_output_bytes + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > maximum_output_bytes:
                    raise operational_error("git probe stdout limit exceeded")
        status = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        if status != 0:
            if allow_missing and status == 1:
                return None
            raise move_error(f"git {side} {label} probe failed: path={display(path)}")
        try:
            return output.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise move_error(f"git {side} {label} output is malformed: path={display(path)}") from error
    except KeyboardInterrupt as error:
        raise operational_error("git probe was interrupted") from error
    except FileNotFoundError as error:
        raise operational_error("git is unavailable") from error
    except subprocess.TimeoutExpired as error:
        raise operational_error("git probe timed out") from error
    except OSError as error:
        raise operational_error("git probe could not run") from error
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            _reap_process(process, operational_error, cleanup_timeout_seconds)
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _reap_process(
    process: subprocess.Popen[bytes], operational_error: type[Exception], cleanup_timeout_seconds: float
) -> None:
    """Kill a still-running probe and confirm reaping within a finite budget."""
    if process.poll() is not None:
        return
    try:
        process.kill()
        process.wait(timeout=cleanup_timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise operational_error("git probe cleanup could not reap process") from error


def _root_commit(
    path: str,
    side: str,
    move_error: type[Exception],
    operational_error: type[Exception],
    display: Callable[[str], str],
    maximum_output_bytes: int,
    timeout_seconds: float,
    cleanup_timeout_seconds: float,
) -> str:
    """Return exactly one root commit for local-origin fallback identity evidence."""
    output = git_output(
        path,
        side,
        ("rev-list", "--max-parents=0", "HEAD"),
        "root commit",
        move_error=move_error,
        operational_error=operational_error,
        display=display,
        maximum_output_bytes=maximum_output_bytes,
        timeout_seconds=timeout_seconds,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )
    assert output is not None
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        raise move_error(f"git {side} root commit is ambiguous: path={display(path)}")
    return _commit(lines[0], "root commit", move_error)


def _absolute_git_path(value: str | None, label: str, move_error: type[Exception]) -> str:
    """Require one bounded absolute Git-admin path emitted by Git itself."""
    if value is None:
        raise move_error(f"git {label} output is malformed")
    result = _single_line(value, label, move_error)
    if not os.path.isabs(result):
        raise move_error(f"git {label} output is malformed")
    return os.path.normpath(result)


def _single_line(value: str | None, label: str, move_error: type[Exception]) -> str:
    """Return one bounded single-line Git value while rejecting malformed output."""
    if value is None or not value.endswith("\n") or value.count("\n") != 1 or "\r" in value:
        raise move_error(f"git {label} output is malformed")
    result = value[:-1]
    if not result or len(result.encode("utf-8", "surrogatepass")) > 16 * 1024:
        raise move_error(f"git {label} output is malformed")
    return result


def _commit(value: str, label: str, move_error: type[Exception]) -> str:
    """Require a bounded SHA-1 or SHA-256 hexadecimal Git commit identifier."""
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is None:
        raise move_error(f"git {label} output is malformed")
    return value


def _normalize_origin(value: str, move_error: type[Exception]) -> str | None:
    """Normalize a non-file Git origin without retaining credentials for diagnostics."""
    if len(value.encode("utf-8", "surrogatepass")) > 16 * 1024 or any(
        ord(character) < 32 for character in value
    ):
        raise move_error("git origin output is malformed")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise move_error("git origin output is malformed") from error
    if parsed.scheme:
        if parsed.scheme.lower() == "file" or not parsed.hostname:
            return None
        try:
            port = parsed.port
        except ValueError as error:
            raise move_error("git origin output is malformed") from error
        host = parsed.hostname.lower()
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))
    if ":" in value and not any(character.isspace() for character in value):
        host_path = value.rsplit("@", 1)[-1]
        if host_path.startswith("/") or host_path.startswith("."):
            return None
        return host_path.rstrip("/")
    return None
