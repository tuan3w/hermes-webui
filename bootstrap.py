#!/usr/bin/env python3
"""One-shot bootstrap launcher for Hermes Web UI."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path


INSTALLER_URL = "https://raw.githubusercontent.com/tuan3w/hermes-agent/main/scripts/install.sh"
def _win_norm(p: Path) -> Path:
    r"""Strip the \\?\ extended-length prefix that Path.resolve() can add on Windows.

    That prefix is valid for file I/O but breaks subprocess argument lists and
    some stdlib tools (ensurepip, pip). Safe no-op on non-Windows.
    """
    if platform.system() == "Windows":
        s = str(p)
        if s.startswith("\\\\?\\"):
            return Path(s[4:])
    return p


REPO_ROOT = _win_norm(Path(__file__).resolve().parent)


def _load_repo_dotenv() -> None:
    """Load REPO_ROOT/.env into os.environ.

    Mirrors what start.sh does via ``set -a; source .env`` so that running
    ``python3 bootstrap.py`` directly behaves identically to ``./start.sh``.
    Variables are set unconditionally (matching shell source semantics), so a
    value in .env overrides one already present in the shell environment.
    ``ctl.sh`` sets HERMES_WEBUI_PRESERVE_ENV=1 when it has already resolved
    launcher-specific values such as HERMES_HOME or HERMES_WEBUI_STATE_DIR.

    Only loads the webui repo .env — not ~/.hermes/.env, which the server
    loads independently at startup for provider credentials.

    Note: does not handle the ``export FOO=bar`` prefix — strip ``export``
    from .env values if copy-pasting from a shell rc file.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        preserve_existing = os.getenv("HERMES_WEBUI_PRESERVE_ENV", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            # Strip optional 'export' prefix (common in copy-pasted shell snippets)
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip().strip('"').strip("'")
            if k:
                if preserve_existing and k in os.environ:
                    continue
                os.environ[k] = v
    except Exception as exc:
        import sys as _sys
        print(f"[bootstrap] Warning: could not load .env — {exc}", file=_sys.stderr)


# Side effect: loads REPO_ROOT/.env into os.environ on import.
# Must run before DEFAULT_HOST / DEFAULT_PORT so os.getenv() picks up
# values from .env even when bootstrap.py is invoked directly (not via start.sh).
_load_repo_dotenv()

DEFAULT_HOST = os.getenv("HERMES_WEBUI_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("HERMES_WEBUI_PORT", "8787"))
# Set HERMES_WEBUI_SKIP_ONBOARDING=1 to bypass the first-run wizard when
# the environment is already fully configured (e.g. managed hosting).


def info(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    release = platform.release().lower()
    return (
        "microsoft" in release or "wsl" in release or bool(os.getenv("WSL_DISTRO_NAME"))
    )


def ensure_supported_platform() -> None:
    if platform.system() == "Windows" and not is_wsl():
        # Allow Windows when launched by the Tauri desktop app (it manages the
        # Python venv via uv and sets HERMES_DESKTOP=1).  Bare CLI use on
        # native Windows is still untested; WSL2 is the recommended shell path.
        if not os.getenv("HERMES_DESKTOP"):
            raise RuntimeError(
                "Native Windows is not supported for this bootstrap yet. "
                "Please run it from Linux, macOS, or inside WSL2."
            )


def _agent_dir_from_hermes_cli() -> Path | None:
    """Resolve the agent install root by inspecting the `hermes` CLI shebang.

    The Hermes Agent installer drops a `hermes` console-script in the user's
    PATH whose shebang points at the agent's bundled venv:

        #!/path/to/hermes-agent/venv/bin/python3

    Walking up the parents until we find a directory that contains
    `run_agent.py` recovers the install root regardless of where the user
    chose to clone the agent (e.g. ~/Projects/GitHub/hermes-agent), which
    the hard-coded candidate list in :func:`discover_agent_dir` cannot.

    Last-resort only: this is invoked after every explicit candidate
    (`HERMES_WEBUI_AGENT_DIR`, `$HERMES_HOME/hermes-agent`, etc.) has missed.
    A stale clone in a known location still wins over the live `hermes` CLI
    — that's intentional, since the candidate list is treated as
    authoritative when present, and matches existing behavior.
    """
    hermes_path = shutil.which("hermes")
    if not hermes_path:
        return None
    try:
        with open(hermes_path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
    except OSError:
        return None
    if not first_line.startswith("#!"):
        return None
    interp_field = first_line[2:].strip().split(None, 1)
    if not interp_field:
        return None
    interp = Path(interp_field[0])
    if not interp.is_absolute():
        return None
    for parent in interp.parents:
        if (parent / "run_agent.py").exists():
            return _win_norm(parent.resolve())
    return None


def discover_agent_dir() -> Path | None:
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    candidates = [
        os.getenv("HERMES_WEBUI_AGENT_DIR", ""),
        str(home / "hermes-agent"),
        str(REPO_ROOT.parent / "hermes-agent"),
        str(Path.home() / ".hermes" / "hermes-agent"),
        str(Path.home() / "hermes-agent"),
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = _win_norm(Path(raw).expanduser().resolve())
        if candidate.exists() and (candidate / "run_agent.py").exists():
            return candidate
    return _agent_dir_from_hermes_cli()


def discover_launcher_python(agent_dir: Path | None) -> str:
    # Agent Python takes highest priority — it has all agent + webui deps installed.
    # HERMES_WEBUI_PYTHON (Tauri minimal venv) is a fallback for when no agent is present.
    if agent_dir:
        for rel in ("venv/bin/python", "venv/Scripts/python.exe", ".venv/bin/python", ".venv/Scripts/python.exe"):
            candidate = agent_dir / rel
            if candidate.exists():
                return str(candidate)
    env_python = os.getenv("HERMES_WEBUI_PYTHON")
    if env_python:
        return env_python
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        candidate = REPO_ROOT / rel
        if candidate.exists():
            return str(candidate)
    return shutil.which("python3") or shutil.which("python") or sys.executable


def _python_can_run_webui_and_agent(
    python_exe: str,
    agent_dir: Path | None = None,
    require_agent: bool = True,
) -> bool:
    script = "import yaml\nfrom run_agent import AIAgent\n" if require_agent else "import yaml\n"
    env = os.environ.copy()
    if agent_dir:
        # PREPEND agent_dir to PYTHONPATH so an `agent_dir/run_agent.py` wins
        # over any stale `run_agent` package in system site-packages (sys.path
        # order: script-dir → PYTHONPATH entries → site-packages). The
        # "if PYTHONPATH unset" branch avoids a leading os.pathsep, which
        # CPython would interpret as "current directory" — a footgun.
        env["PYTHONPATH"] = (
            str(agent_dir)
            if not env.get("PYTHONPATH")
            else f"{agent_dir}{os.pathsep}{env['PYTHONPATH']}"
        )
    check = subprocess.run(
        [python_exe, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    return check.returncode == 0


def ensure_python_has_webui_deps(
    python_exe: str,
    agent_dir: Path | None = None,
    require_agent: bool = True,
) -> str:
    """Return a Python executable that can run WebUI (and optionally Hermes Agent).

    When require_agent is False (desktop/Tauri mode) only WebUI deps are
    checked; the agent integration is optional and handled separately.
    """
    if _python_can_run_webui_and_agent(python_exe, agent_dir, require_agent=require_agent):
        return python_exe

    agent_candidates: list[Path] = []
    if agent_dir:
        for rel in (
            "venv/bin/python",
            "venv/Scripts/python.exe",
            ".venv/bin/python",
            ".venv/Scripts/python.exe",
        ):
            agent_candidates.append(agent_dir / rel)
        for candidate in agent_candidates:
            if str(candidate) != python_exe and candidate.exists():
                if _python_can_run_webui_and_agent(str(candidate), agent_dir, require_agent=require_agent):
                    return str(candidate)

    venv_dir = REPO_ROOT / ".venv"
    venv_python = venv_dir / (
        "Scripts/python.exe" if platform.system() == "Windows" else "bin/python"
    )
    # REPO_ROOT is already normalized via _win_norm(), so venv_dir / venv_python
    # are clean Win32 paths with no \\?\ prefix.
    venv_python_str = str(venv_python)

    if not venv_python.exists():
        info(f"Creating local virtualenv at {venv_dir}")
        # Use symlinks only on non-Windows: Windows requires Developer Mode or
        # admin rights for symlinks. Without them the python.exe symlink silently
        # fails, leaving the venv broken (ensurepip has no interpreter to run).
        # On macOS/Linux, symlinks are preferred so @executable_path stays valid
        # for shared-library Python builds (mise/asdf).
        use_symlinks = platform.system() != "Windows"
        venv.EnvBuilder(with_pip=True, symlinks=use_symlinks).create(venv_dir)

    info("Installing WebUI dependencies into local virtualenv")
    subprocess.run(
        [venv_python_str, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=True,
    )
    subprocess.run(
        [
            venv_python_str,
            "-m",
            "pip",
            "install",
            "--quiet",
            "-r",
            str(REPO_ROOT / "requirements.txt"),
        ],
        check=True,
    )
    if _python_can_run_webui_and_agent(venv_python_str, agent_dir, require_agent=require_agent):
        return venv_python_str
    raise RuntimeError(
        "Python environment cannot import WebUI dependencies"
        + (" and Hermes Agent" if require_agent else "")
        + ". Set HERMES_WEBUI_PYTHON to a suitable interpreter or install the "
        "WebUI requirements into that environment."
    )


def hermes_command_exists() -> bool:
    return shutil.which("hermes") is not None


def _ensure_uv() -> str:
    """Return path to uv, installing it if necessary."""
    uv = shutil.which("uv")
    if uv:
        return uv
    # Check common install locations that may not be on PATH in a desktop launch
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "uv",
        home / ".cargo" / "bin" / "uv",
    ]
    if platform.system() == "Windows":
        candidates += [
            home / ".local" / "bin" / "uv.exe",
            Path(os.environ.get("APPDATA", "")) / "uv" / "bin" / "uv.exe",
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    info("uv not found — installing uv...")
    if platform.system() == "Windows":
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "ByPass", "-c",
             "irm https://astral.sh/uv/install.ps1 | iex"],
            check=True,
        )
    else:
        subprocess.run(
            ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            check=True,
        )
    # After install, the binary lands in ~/.local/bin or ~/.cargo/bin
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("uv") or "uv"


def install_hermes_agent() -> None:
    uv = _ensure_uv()
    info(f"Hermes Agent not found. Installing via: uv tool install git+https://github.com/tuan3w/hermes-agent@main")
    subprocess.run(
        [uv, "tool", "install", "git+https://github.com/tuan3w/hermes-agent@main"],
        check=True,
    )


def wait_for_health(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    # Validate URL scheme to prevent file:// and other dangerous schemes
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid health check URL: {url}")
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310
                if b'"status": "ok"' in response.read():
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        info(f"Could not open browser automatically: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Hermes Web UI onboarding.")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser tab automatically.",
    )
    parser.add_argument(
        "--skip-agent-install",
        action="store_true",
        help="Fail instead of attempting the official Hermes installer.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Run server.py in this process (via os.execv) instead of spawning a "
            "child. Use this under launchd / systemd / supervisord so the "
            "supervisor sees the long-lived server as the original child. "
            "Implies --no-browser. Skips the post-launch health probe — the "
            "supervisor's own KeepAlive / Restart=on-failure handles liveness."
        ),
    )
    return parser.parse_args()


# Env vars whose presence indicates this process was launched by a supervisor
# that wants to manage the server's lifecycle (KeepAlive, Restart=always, etc.).
# When any is set, we auto-promote to --foreground so we don't double-fork.
#
# - INVOCATION_ID            systemd (set on every service activation)
# - JOURNAL_STREAM           systemd (set when stdio is wired to the journal)
# - NOTIFY_SOCKET            systemd Type=notify, s6 sd_notify-style
# - XPC_SERVICE_NAME         launchd (set to the Label of the running plist)
# - SUPERVISOR_ENABLED       supervisord
# - HERMES_WEBUI_FOREGROUND  explicit user opt-in (=1 / true / yes / on)
#
# Note on XPC_SERVICE_NAME: macOS launchd sets this in EVERY Terminal-launched
# shell too — typical values include "0" (truthy in Python!) and
# "application.com.apple.Terminal.<UUID>". A bare existence check would
# false-positive on every Mac dev machine running ./start.sh interactively.
# We narrow to launchd Label-style names (com.<reverse-dns>.<svc>) — those
# are real services. Verified with `launchctl getenv XPC_SERVICE_NAME` and
# Apple's documented launchd behavior.
_SUPERVISOR_ENV_VARS = (
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "NOTIFY_SOCKET",
    "XPC_SERVICE_NAME",
    "SUPERVISOR_ENABLED",
)


def _is_real_supervisor_value(name: str, value: str) -> bool:
    """Filter out known-noise env-var values that aren't actual supervisors.

    Most env vars in _SUPERVISOR_ENV_VARS are only set by the supervisor we
    care about, so any non-empty value is meaningful. XPC_SERVICE_NAME is the
    exception: macOS launchd sets it in every Terminal-spawned shell with
    values like "0" or "application.com.apple.Terminal.<UUID>". A real
    launchd-managed service has a reverse-DNS Label like "com.example.foo".
    """
    if not value:
        return False
    if name == "XPC_SERVICE_NAME":
        # Reject Apple's noise values; accept Label-style names.
        if value == "0":
            return False
        if value.startswith("application."):
            return False
    return True


def _detect_supervisor() -> str | None:
    """Return the name of the detected supervisor env var, or None.

    Pure inspection of os.environ — no side effects. Returned name is the env
    var that triggered detection, useful for log messages and for tests.
    """
    explicit = os.environ.get("HERMES_WEBUI_FOREGROUND", "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return "HERMES_WEBUI_FOREGROUND"
    for name in _SUPERVISOR_ENV_VARS:
        value = os.environ.get(name, "")
        if _is_real_supervisor_value(name, value):
            return name
    return None


def main() -> int:
    args = parse_args()
    ensure_supported_platform()

    agent_dir = discover_agent_dir()
    if not agent_dir and not hermes_command_exists():
        if args.skip_agent_install:
            # Desktop (Tauri) mode: agent is optional. The server starts without
            # it; agent-backed chat will be unavailable until the user installs it.
            info("Hermes Agent not found — continuing without agent support.")
        else:
            install_hermes_agent()
            agent_dir = discover_agent_dir()

    require_agent = not args.skip_agent_install
    python_exe = ensure_python_has_webui_deps(
        discover_launcher_python(agent_dir), agent_dir, require_agent=require_agent
    )
    state_dir = Path(
        os.getenv("HERMES_WEBUI_STATE_DIR", str(Path.home() / ".hermes" / "webui"))
    ).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    # Mutate os.environ so child (or post-execv) inherits the resolved values.
    os.environ["HERMES_WEBUI_HOST"] = args.host
    os.environ["HERMES_WEBUI_PORT"] = str(args.port)
    os.environ.setdefault("HERMES_WEBUI_STATE_DIR", str(state_dir))
    if agent_dir:
        os.environ["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)

    server_cwd = str(agent_dir or REPO_ROOT)
    server_path = str(REPO_ROOT / "server.py")

    # --foreground (or auto-detected supervisor): replace this process with the
    # server. The supervisor sees the long-lived server as the original child,
    # so KeepAlive / Restart=always / autorestart=true work correctly. No
    # health probe — the supervisor's own restart-on-exit handles liveness.
    foreground_reason = "--foreground" if args.foreground else _detect_supervisor()
    if foreground_reason:
        info(
            f"Starting Hermes Web UI on http://{args.host}:{args.port} "
            f"(foreground mode: {foreground_reason})"
        )
        try:
            os.chdir(server_cwd)
        except OSError as exc:
            raise RuntimeError(
                f"Could not chdir to {server_cwd!r} before exec: {exc}"
            ) from exc
        # Defensive check: if python_exe is missing or non-executable, execv
        # raises OSError, the wrapper catches and SystemExit(1)s, and the
        # supervisor restarts — looping forever, exactly the failure mode this
        # PR is meant to eliminate. Convert to a single visible error.
        if not os.access(python_exe, os.X_OK):
            raise RuntimeError(
                f"Python interpreter at {python_exe!r} is not executable. "
                f"Set HERMES_WEBUI_PYTHON to a working interpreter or fix "
                f"the agent venv at {agent_dir}."
            )
        # os.execv replaces the current process image. Anything after this line
        # only runs if execv itself fails (it raises OSError on failure).
        os.execv(python_exe, [python_exe, server_path])
        # Unreachable — execv either replaces the process or raises.
        raise RuntimeError("os.execv returned unexpectedly")

    # Default (legacy) path: spawn the server as a detached child, probe
    # /health, then return. Suitable for an interactive `bash start.sh` run.
    log_path = state_dir / f"bootstrap-{args.port}.log"

    info(f"Starting Hermes Web UI on http://{args.host}:{args.port}")
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(
            [python_exe, server_path],
            cwd=server_cwd,
            env=os.environ.copy(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    health_url = f"http://{args.host}:{args.port}/health"
    if not wait_for_health(health_url):
        raise RuntimeError(
            f"Web UI did not become healthy at {health_url}. "
            f"Check the log at {log_path}. Server PID: {proc.pid}"
        )

    app_url = (
        f"http://localhost:{args.port}"
        if args.host in ("127.0.0.1", "localhost")
        else f"http://{args.host}:{args.port}"
    )
    info(f"Web UI is ready: {app_url}")
    info(f"Log file: {log_path}")
    if not args.no_browser:
        open_browser(app_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
