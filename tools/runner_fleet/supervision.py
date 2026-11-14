"""Foreground phase supervision, independent of cooperative operation timeouts."""

from __future__ import annotations

import os
import ctypes
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Iterator

_owned_deadline: float | None = None
LOG_LIMIT = 64 * 1024**2


class Interrupted(BaseException):
    """Cancellation bypasses operational exception handlers."""


class Deadline(Interrupted):
    pass


class UncertainProcess(RuntimeError):
    """A host phase process group could not be proved terminated."""


@contextmanager
def watch_descendants(deadline: float) -> Iterator[None]:
    """Detect orphaned setsid descendants too; never mistake them for cleanup.

    This process-local Linux setting requires no host-helper/system change.
    systemd-created guests are not subprocess descendants and remain separately
    tracked by the durable reservation and positive owned-service observations.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    previous = ctypes.c_int()
    if prctl(37, ctypes.addressof(previous), 0, 0, 0) != 0:
        raise UncertainProcess("Cannot inspect process-local descendant observation")
    try:
        os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        pass
    else:
        raise UncertainProcess("Supervisor already has child processes")
    if prctl(36, 1, 0, 0, 0) != 0:
        raise UncertainProcess("Cannot establish process-local descendant observation")
    try:
        yield
    finally:
        try:
            # Reap only our newly adopted exited children. A live detached child
            # is not broadly killed or assumed to be part of a safe process group.
            with alarm(deadline, cancellation=False):
                while True:
                    try:
                        child = os.waitid(
                            os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT
                        )
                    except ChildProcessError:
                        break
                    if child is None:
                        raise UncertainProcess(
                            "Detached phase descendant remains alive"
                        )
                    os.waitpid(child.si_pid, os.WNOHANG)
        except UncertainProcess:
            raise
        except BaseException as error:
            raise UncertainProcess(
                "Phase descendant observation is uncertain"
            ) from error
        finally:
            if prctl(36, previous.value, 0, 0, 0) != 0:
                raise UncertainProcess(
                    "Process-local descendant observation restoration failed"
                )


@contextmanager
def alarm(deadline: float, *, cancellation: bool = True) -> Iterator[None]:
    """Bound supervisor-side I/O too; never replace another active timer."""
    global _owned_deadline
    outer = _owned_deadline
    if outer is None and signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("Existing timer cannot be replaced")
    deadline = min(deadline, outer) if outer is not None else deadline
    signals = (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.getsignal(sig) for sig in signals}

    def interrupt(signum: int, frame: FrameType | None) -> None:
        if signum == signal.SIGALRM:
            raise Deadline("Supervisor deadline reached")
        raise Interrupted("Supervisor interrupted")

    try:
        signal.signal(signal.SIGALRM, interrupt)
        for sig in signals[1:]:
            signal.signal(sig, interrupt if cancellation else signal.SIG_IGN)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Deadline("No phase budget remains")
        _owned_deadline = deadline
        signal.setitimer(signal.ITIMER_REAL, remaining)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        _owned_deadline = outer
        if outer is not None:
            signal.setitimer(
                signal.ITIMER_REAL, max(0.000001, outer - time.monotonic())
            )


def group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def terminate(process: subprocess.Popen[bytes], deadline: float) -> None:
    """No unbounded wait, broad pkill, or assumption that sudo children died."""
    for sig, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise UncertainProcess("Cannot terminate owned phase group") from error
        until = min(deadline, time.monotonic() + grace)
        while time.monotonic() < until:
            process.poll()  # Reap our direct child, never wait indefinitely.
            if not group_exists(process.pid):
                return
            time.sleep(min(0.02, max(0.0, until - time.monotonic())))
    process.poll()
    if group_exists(process.pid):
        raise UncertainProcess("Owned phase group termination is uncertain")


def supervise(argv: list[str], payload: str, log: Path, deadline: float) -> None:
    with watch_descendants(deadline):
        supervise_group(argv, payload, log, deadline)


def supervise_group(argv: list[str], payload: str, log: Path, deadline: float) -> None:
    """A separately scheduled parent enforces the phase deadline and kills its group.

    Reserve three seconds inside the phase for termination/reaping. Guest services
    are outside this process group and require separate owner-checked cleanup.
    """
    if deadline - time.monotonic() <= 3:
        raise Deadline("Insufficient phase and termination budget")
    encoded = payload.encode()
    if len(encoded) > 65536:
        raise ValueError("Phase input exceeds the bounded protocol")
    process: subprocess.Popen[bytes] | None = None
    spawning = False
    try:
        with log.open("xb") as output, tempfile.TemporaryFile() as request:
            request.write(encoded)
            request.seek(0)
            spawning = True
            process = subprocess.Popen(
                argv,
                stdin=request,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            if process.stdout is None:
                raise UncertainProcess("Phase log pipe unavailable")
            count = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic() - 3
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, 0)
                    for key, _ in selector.select(min(remaining, 0.1)):
                        block = os.read(key.fd, 65536)
                        if not block:
                            selector.unregister(key.fd)
                            continue
                        count += len(block)
                        if count > LOG_LIMIT:
                            raise RuntimeError("Phase diagnostic log limit exceeded")
                        output.write(block)
                # EOF is not proof of exit (a worker may close stdout then hang).
                while process.poll() is None:
                    if time.monotonic() >= deadline - 3:
                        raise subprocess.TimeoutExpired(argv, 0)
                    time.sleep(0.01)
            if group_exists(process.pid):
                raise UncertainProcess("Phase left descendant processes")
            if process.returncode != 0:
                raise RuntimeError("Phase failed; inspect the private operation log")
    except BaseException as error:
        if spawning and process is None:
            raise UncertainProcess(
                "Phase process creation outcome is unknown"
            ) from error
        raise
    finally:
        if process is not None:
            # Interruptions also terminate this group before owned guest cleanup.
            # SIGKILL/host loss cannot run finally; durable capacity stays occupied.
            try:
                with alarm(deadline, cancellation=False):
                    terminate(process, deadline)
            except BaseException as error:
                raise UncertainProcess(
                    "Phase group termination is not verified"
                ) from error
            finally:
                if process.stdout is not None:
                    process.stdout.close()
