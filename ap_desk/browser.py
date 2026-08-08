"""Launch a browser and genuinely put it in front. Standard library only.

This module exists because `webbrowser.open()` is not sufficient on Windows, and
the way it fails is silent and expensive.

Two distinct problems
---------------------

**1. The URL goes to an existing instance.** `webbrowser.open` hands the URL to
an already-running Chrome over DDE. That instance opens a tab in whichever
window it happens to own -- which may be minimised, behind the editor, or on
another virtual desktop. Nothing new appears in front.

**2. Windows refuses to give up the foreground.** A process that does not
already own the foreground cannot take it. `SetForegroundWindow` returns
success and merely flashes the taskbar button. `pygetwindow`'s `activate()` and
PyAutoGUI's window helpers both go through that same call, so neither is a fix.

Why this matters more here than usual: the agent acts on a screenshot of
whatever is actually in front. If the browser is not there, it photographs an
editor and reasons about the wrong screen -- confidently, and at 5 credits a
step. Verified live: with no browser focused, the model correctly reported "this
is NOT an accounts-payable terminal" and refused to continue. Right call, wasted
run.

What this does instead
----------------------
* finds a real browser binary rather than trusting the shell association;
* launches it as a NEW process with its own throwaway profile, so there is no
  existing instance to defer to and no extensions, tabs or restore prompts;
* breaks the foreground lock with the documented AttachThreadInput dance plus a
  synthetic ALT keypress, which is what makes SetForegroundWindow actually work;
* **verifies** the window became foreground, and reports honestly if it did not.

Non-Windows platforms fall back to `webbrowser.open`, which behaves acceptably
on macOS and most Linux desktops.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Ordered by preference. Chrome first because it is what most demos run, Edge
# second because it is present on every Windows install and is the same engine.
_WINDOWS_CANDIDATES = [
    r"{ProgramFiles}\Google\Chrome\Application\chrome.exe",
    r"{ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    r"{LocalAppData}\Google\Chrome\Application\chrome.exe",
    r"{ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    r"{ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
]

_POSIX_CANDIDATES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
]


@dataclass
class Launch:
    """The outcome of trying to put a page in front of the operator."""

    ok: bool
    detail: str
    process: subprocess.Popen | None = None
    binary: str | None = None
    profile_dir: Path | None = None

    def close(self) -> None:
        """Shut the launched browser down, if we own it."""
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best effort cleanup
                try:
                    self.process.kill()
                except Exception:  # noqa: BLE001
                    pass


def find_browser() -> str | None:
    """Locate a Chromium-family browser binary."""
    if IS_WINDOWS:
        env = {
            "ProgramFiles": os.environ.get("ProgramFiles", r"C:\Program Files"),
            "ProgramFiles(x86)": os.environ.get(
                "ProgramFiles(x86)", r"C:\Program Files (x86)"
            ),
            "LocalAppData": os.environ.get("LocalAppData", ""),
        }
        for template in _WINDOWS_CANDIDATES:
            try:
                path = template.format(**env)
            except KeyError:
                continue
            if path and Path(path).is_file():
                return path
        return shutil.which("chrome") or shutil.which("msedge")

    for name in _POSIX_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
# Windows foreground handling
# --------------------------------------------------------------------------- #


def _win32():
    """ctypes handles with correct 64-bit signatures.

    Declaring argtypes/restype is not optional here: HWND is pointer-sized, and
    letting ctypes default to int truncates handles on 64-bit Windows. The
    symptom is a function that appears to work and silently addresses the wrong
    window.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [
        wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)
    ]
    return ctypes, wintypes, user32, kernel32


def _windows_matching(title_fragment: str, pid: int | None = None) -> list[int]:
    """Visible top-level window handles whose title contains `title_fragment`.

    Filtering by `pid` matters when several browsers are open: the fragment
    alone can match a stale window belonging to a different instance, and
    focusing that one puts the wrong page in front while reporting success.
    """
    ctypes, wintypes, user32, _ = _win32()

    found: list[int] = []
    needle = title_fragment.lower()

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle not in buf.value.lower():
            return True
        if pid is not None:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
        found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def _force_foreground(hwnd: int) -> bool:
    """Take the foreground for `hwnd`, defeating Windows' foreground lock.

    Windows only lets a process set the foreground window if it already owns
    the foreground, or if it has just received input. Two documented moves get
    around that, and both are needed:

    * **AttachThreadInput** -- attach our input queue to the current foreground
      window's thread, so as far as Windows is concerned we are that thread and
      are entitled to move focus.
    * **A synthetic ALT keypress** -- receiving input releases the foreground
      lock for the calling process. ALT is chosen because it has no effect on a
      browser on its own.

    Neither is a hack in the "might work" sense; together they are the standard
    way to do this. What matters is the final check: we return whether the
    handle actually IS the foreground window, never merely that the call was
    made.
    """
    ctypes, wintypes, user32, kernel32 = _win32()

    SW_RESTORE = 9
    SW_SHOW = 5
    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return True

    our_tid = kernel32.GetCurrentThreadId()
    their_tid = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    attached = False
    if their_tid and their_tid != our_tid:
        attached = bool(user32.AttachThreadInput(their_tid, our_tid, True))

    try:
        user32.keybd_event(VK_MENU, 0, 0, None)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)

        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(their_tid, our_tid, False)

    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


def active_window_title() -> str:
    """Title of whatever currently owns the foreground. Never raises."""
    if not IS_WINDOWS:
        return ""
    try:
        ctypes, _, user32, _ = _win32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:  # noqa: BLE001 - diagnostics must not become the failure
        return ""


# --------------------------------------------------------------------------- #


def _maximise(hwnd: int) -> bool:
    """Maximise a window and confirm it took.

    `--start-maximized` is a request Chrome does not always honour: it is
    ignored when the profile has a remembered window size, and on a fresh
    profile it can land before the window manager has settled. A part-screen
    browser is not cosmetic here -- the agent reasons about a screenshot, so
    every pixel outside the window is desktop it has to look past, and a
    half-width window pushes the invoice table into a scroll it then has to
    discover.

    So the flag stays (it usually works and costs nothing) and this verifies it.
    """
    ctypes, wintypes, user32, _ = _win32()

    SW_MAXIMIZE = 3
    user32.IsZoomed.argtypes = [wintypes.HWND]
    if user32.IsZoomed(hwnd):
        return True
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.3)
    return bool(user32.IsZoomed(hwnd))


def open_focused(
    url: str,
    *,
    title_fragment: str,
    profile_dir: Path | None = None,
    timeout: float = 25.0,
    kiosk: bool = False,
) -> Launch:
    """Open `url` in a new browser window and put it genuinely in front.

    A throwaway `--user-data-dir` is what makes this reliable. It forces a
    genuinely separate process rather than a DDE handoff to a running instance,
    and it means the window opens clean: no extensions, no restored tabs, no
    "Chrome didn't shut down correctly" bar eating the top of the screen and
    confusing the agent about what it is looking at.
    """
    binary = find_browser()
    if not binary:
        import webbrowser

        webbrowser.open(url)
        return Launch(False, "no Chrome/Edge binary found; fell back to the default browser")

    profile = profile_dir or Path(__file__).resolve().parents[1] / "tmp" / "browser-profile"

    # Start from a genuinely empty profile every run.
    #
    # Chrome persists a session in the user-data-dir, so a profile left over
    # from a previous run restores that run's tabs -- which point at ports that
    # are no longer listening, under titles from whatever the app was called
    # then. The window that appears is therefore NOT the portal, the title match
    # fails, and the launcher reports "no window matching the page title yet"
    # while a stale page sits in front of the operator. Observed exactly that.
    #
    # A stale profile also makes Chrome hand the URL to an already-running
    # instance holding the same directory instead of starting its own, which is
    # the behaviour the separate profile existed to avoid in the first place.
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)

    args = [
        binary,
        f"--user-data-dir={profile}",
        # NOT --new-window. A fresh user-data-dir already opens a new window,
        # and asking for another makes Chrome open a SECOND, blank one that
        # appears a moment later and steals the foreground from the page we
        # just focused. Observed directly: focus succeeded on "DoliDesk", then
        # the active window became "Untitled - Google Chrome".
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--hide-crash-restore-bubble",
        # Belt and braces against session restore: even on a fresh directory,
        # a crashed previous run can leave Chrome offering to restore.
        "--disable-features=ChromeWhatsNewUI,ProfilePicker,SigninInterceptBubble,InfiniteSessionRestore",
        "--disable-restore-session-state",
        # Keep the window predictable for the agent and for a recording.
        "--window-position=0,0",
        "--start-maximized",
    ]
    if kiosk:
        args.append("--kiosk")
    args.append(url)

    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Detach so the browser does not die with a Ctrl-C aimed at the run.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0,
        )
    except OSError as exc:
        return Launch(False, f"could not start {Path(binary).name}: {exc}", binary=binary)

    if not IS_WINDOWS:
        time.sleep(2.0)
        return Launch(True, f"launched {Path(binary).name}", process, binary, profile)

    # Converge on focus rather than firing once and hoping.
    #
    # A browser starting up is a moving target: windows appear late, titles
    # change from "Untitled" to the page title as the document loads, and a
    # second window can steal the foreground moments after the first is
    # focused. So the success condition is not "we called SetForegroundWindow"
    # but "the foreground window IS the page, and stayed that way".
    deadline = time.monotonic() + timeout
    last = "window never appeared"
    stable_since: float | None = None

    while time.monotonic() < deadline:
        if title_fragment.lower() in active_window_title().lower():
            # Require the state to hold briefly, so a window that is about to
            # be covered by a late-opening sibling is not reported as success.
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 1.2:
                for hwnd in _windows_matching(title_fragment):
                    _maximise(hwnd)
                    break
                return Launch(
                    True,
                    f"{Path(binary).name} in front, maximised (\"{title_fragment}\")",
                    process, binary, profile,
                )
        else:
            stable_since = None
            handles = _windows_matching(title_fragment)
            if not handles:
                last = "no window matching the page title yet"
            for hwnd in handles:
                _maximise(hwnd)
                if not _force_foreground(hwnd):
                    last = "found the window but Windows refused the foreground"
        time.sleep(0.4)

    return Launch(False, last, process, binary, profile)
