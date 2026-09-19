"""Agentihooks / bundle / agentihub integration.

agentihooks is a pip dependency of agenticore (declared in pyproject.toml).
PyPI is the default install path. ``AGENTICORE_AGENTIHOOKS_URL`` triggers a
clone + ``uv pip install -e`` for running a fork or branch. The runtime
only clones once at boot (or on demand); no periodic re-sync.

Path layout: each repo lives at ``<CLONE_ROOT>/<dir-from-url>`` where
``CLONE_ROOT`` is ``AGENTICORE_CLONE_ROOT`` when set, otherwise
``AGENTICORE_SHARED_FS_ROOT``. The dir is the last URL segment minus
``.git``. URLs determine WHAT to clone AND WHERE. Bundle is optional.
Hub is required for agent mode. Resolved paths are stored on
``Config.runtime`` for downstream consumers.

Splitting ``CLONE_ROOT`` from ``SHARED_FS_ROOT`` lets clones live on an
ephemeral volume (``emptyDir`` at ``/app/clones``) while real state
(``$HOME``, ``.claude/``, ``job-state/``) stays on a PVC. This avoids
the dirty-tree → ``git checkout -B`` failure pattern when the runtime
renders into the cloned source between syncs.
"""

import fcntl
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.git_credentials import git_askpass_env
from agenticore.repos import _run_git, _with_redis_lock, resolve_github_token

logger = logging.getLogger(__name__)


def _scoped_lock_key(base_key: str) -> str:
    """Return a pod-scoped Redis lock key when storage is isolated (PVC).

    Pods on shared NFS need global lock keys for coordination.
    Pods on isolated PVC (local-path) get pod-specific keys to avoid contention.
    Controlled by AGENTICORE_SHARED_LOCKS env var (default: true for backwards compat).
    """
    shared = os.environ.get("AGENTICORE_SHARED_LOCKS", "true").lower() in ("true", "1", "yes")
    if shared:
        return base_key
    hostname = os.environ.get("HOSTNAME", "unknown")
    return f"{base_key}:{hostname}"


def _dir_from_url(url: str) -> str:
    """Last URL segment minus .git → directory name.

    https://github.com/foo/agentihooks-bundle.git → "agentihooks-bundle"
    """
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _clone_on_shared_fs() -> bool:
    """True when the clone destination lives on a multi-pod shared FS.

    When ``AGENTICORE_CLONE_ROOT`` is explicit it points at a per-pod
    volume (typically ``emptyDir``) — no cross-pod coordination is
    needed and a local file flock is sufficient. When only
    ``AGENTICORE_SHARED_FS_ROOT`` is set (legacy single-mount setups),
    clones land on the PVC and we need the Redis lock to serialize
    pods writing to the same path.
    """
    cfg = get_config()
    if cfg.repos.clone_root:
        return False
    return bool(cfg.repos.shared_fs_root)


def resolve_repo_paths(cfg=None):
    """Resolve on-disk destinations for agentihooks / bundle / hub.

    Each destination is ``<CLONE_ROOT>/<dir-from-url>``. ``CLONE_ROOT``
    is ``AGENTICORE_CLONE_ROOT`` when set, otherwise falls back to
    ``AGENTICORE_SHARED_FS_ROOT``, otherwise ``~/.agenticore`` (local
    dev). If a URL is unset, that slot returns None (the repo isn't
    part of this deployment).

    Returns (hooks_path, bundle_path, hub_path) — all Optional[Path].
    """
    if cfg is None:
        cfg = get_config()
    clone_root = cfg.repos.clone_root or cfg.repos.shared_fs_root
    base = Path(clone_root) if clone_root else Path.home() / ".agenticore"
    hooks = base / _dir_from_url(cfg.agentihooks_url) if cfg.agentihooks_url else None
    bundle = base / _dir_from_url(cfg.agentihooks_bundle_url) if cfg.agentihooks_bundle_url else None
    hub = base / _dir_from_url(cfg.agentihub_url) if cfg.agentihub_url else None
    return hooks, bundle, hub


def _install_dir() -> Optional[Path]:
    """Where agentihooks gets cloned (None when no URL set)."""
    hooks, _, _ = resolve_repo_paths()
    return hooks


def _clone_or_fetch(url: str, dest: Path, branch: str = "") -> None:
    """Clone or update agentihooks repo, flock/Redis-protected."""
    t0 = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / f".{dest.name}.clone.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                if branch:
                    _run_git(
                        ["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"], extra_env=extra_env
                    )
                else:
                    _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
                _run_git(["git", "-C", str(dest), "clean", "-fdx", "-e", "*.env"], extra_env=extra_env)
            else:
                cmd = ["git", "clone"]
                if branch:
                    cmd += ["--branch", branch]
                cmd += [url, str(dest)]
                _run_git(cmd, extra_env=extra_env)

    if _clone_on_shared_fs():
        _with_redis_lock(_scoped_lock_key(f"agenticore:lock:clone:{dest.name}"), _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    logger.info("_clone_or_fetch agentihooks done in %.2fs", time.monotonic() - t0)


def _agentihub_install_dir() -> Optional[Path]:
    """Where agentihub gets cloned (None when no URL set)."""
    _, _, hub = resolve_repo_paths()
    return hub


def _clone_or_fetch_agentihub(url: str, dest: Path, branch: str = "") -> None:
    """Clone or update agentihub repo (no profile build — agent mode handles provisioning)."""
    t0 = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / f".{dest.name}.clone.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                if branch:
                    _run_git(
                        ["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"], extra_env=extra_env
                    )
                else:
                    _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
                _run_git(["git", "-C", str(dest), "clean", "-fdx"], extra_env=extra_env)
            else:
                cmd = ["git", "clone"]
                if branch:
                    cmd += ["--branch", branch]
                cmd += [url, str(dest)]
                _run_git(cmd, extra_env=extra_env)

    if _clone_on_shared_fs():
        _with_redis_lock(_scoped_lock_key(f"agenticore:lock:clone:{dest.name}"), _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    logger.info("_clone_or_fetch_agentihub done in %.2fs", time.monotonic() - t0)


def sync_agentihub(url: str = "") -> Optional[Path]:
    """Clone/fetch agentihub. Returns the install directory, or None when
    no URL is configured. Populates ``cfg.runtime.hub_dir`` for downstream
    consumers (agent_mode/initializer.py)."""
    cfg = get_config()
    url = url or cfg.agentihub_url
    if not url:
        return None
    dest = _agentihub_install_dir()
    if not dest:
        return None
    if dest.exists() and (dest / ".git").exists():
        _clone_or_fetch_agentihub(url, dest, cfg.agentihub_branch)
    elif dest.exists():
        # Pre-mounted checkout (dev bind-mount): trust it, no fetch.
        logger.info("agentihub: pre-mounted checkout at %s, skipping clone", dest)
    else:
        _clone_or_fetch_agentihub(url, dest, cfg.agentihub_branch)
    cfg.runtime.hub_dir = dest
    logger.info("agentihub → %s (branch=%s)", dest, cfg.agentihub_branch or "HEAD")
    return dest


def _bundle_dir() -> Optional[Path]:
    """Where the agentihooks bundle gets cloned (None when no URL set)."""
    _, bundle, _ = resolve_repo_paths()
    return bundle


def _clone_or_fetch_bundle(url: str, dest: Path, branch: str = "") -> None:
    """Clone or update agentihooks bundle repo, with GitHub App auth."""
    t0 = time.monotonic()
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / f".{dest.name}.clone.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                if branch:
                    _run_git(
                        ["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"], extra_env=extra_env
                    )
                else:
                    _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
            else:
                cmd = ["git", "clone"]
                if branch:
                    cmd += ["--branch", branch]
                cmd += [url, str(dest)]
                _run_git(cmd, extra_env=extra_env)

    if _clone_on_shared_fs():
        _with_redis_lock(_scoped_lock_key(f"agenticore:lock:clone:{dest.name}"), _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    logger.info("_clone_or_fetch_bundle done in %.2fs", time.monotonic() - t0)


def _venv_python() -> str:
    return os.environ.get("AGENTICORE_VENV_PYTHON", "/opt/venv/bin/python")


def _log_uv_output(result: subprocess.CompletedProcess) -> None:
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("uv: %s", line)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.info("uv stderr: %s", line)


def _pip_install_editable(path: Path) -> None:
    """Editable install from a local path. Used by dev-mode PATH override only.

    Raises on failure — agentihooks is required for the agent to function.
    """
    logger.info("uv pip install -e %s", path)
    result = subprocess.run(
        # Never bare --reinstall: it rewrites every dependency under the running server.
        ["uv", "pip", "install", "--python", _venv_python(), "--reinstall-package", "agentihooks", "-e", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    _log_uv_output(result)
    if result.returncode != 0:
        raise RuntimeError(f"uv pip install -e {path} failed (exit {result.returncode})")


def _pip_install_pypi(pkg: str) -> None:
    """Install a package from PyPI into the agenticore venv via uv.

    Raises on failure — agentihooks is required for the agent to function.
    """
    logger.info("uv pip install %s (from PyPI)", pkg)
    result = subprocess.run(
        ["uv", "pip", "install", "--python", _venv_python(), pkg],
        check=False,
        capture_output=True,
        text=True,
    )
    _log_uv_output(result)
    if result.returncode != 0:
        raise RuntimeError(f"uv pip install {pkg} failed (exit {result.returncode})")


def sync_agentihooks(url: str = "") -> Optional[Path]:
    """Install agentihooks at runtime. Image is lean — only ``uv`` is baked.

    Resolution order:

    1. ``AGENTICORE_AGENTIHOOKS_URL`` set: clone (or fetch) into
       ``<SHARED_FS_ROOT>/<dir-from-url>`` and ``uv pip install -e <clone>``.
       Use this to run a fork or branch that isn't on PyPI yet. Pre-mounted
       checkouts at the same destination are trusted as-is (no fetch).
       Returns the clone path; sets ``cfg.runtime.agentihooks_dir``.

    2. URL unset: ``uv pip install agentihooks`` from PyPI. Returns
       ``None``.
    """
    cfg = get_config()
    resolved_url = url or cfg.agentihooks_url
    if resolved_url:
        dest = _install_dir()
        if dest is None:
            logger.warning("agentihooks: URL set but resolve_repo_paths returned None — cannot proceed")
            return None
        if dest.exists() and (dest / ".git").exists():
            _clone_or_fetch(resolved_url, dest, cfg.agentihooks_branch)
        elif dest.exists():
            # Pre-mounted checkout (dev bind-mount): trust it, no fetch.
            logger.info("agentihooks: pre-mounted checkout at %s, skipping clone", dest)
        else:
            _clone_or_fetch(resolved_url, dest, cfg.agentihooks_branch)
        _pip_install_editable(dest)
        cfg.runtime.agentihooks_dir = dest
        logger.info("agentihooks editable from URL → %s (branch=%s)", dest, cfg.agentihooks_branch or "HEAD")
        return dest

    # Default: install from PyPI.
    _pip_install_pypi("agentihooks")
    logger.info("agentihooks: installed from PyPI via uv")
    return None


def sync_bundle() -> Optional[Path]:
    """Clone/fetch the agentihooks bundle repo. Optional addon — returns
    None when no URL is configured. Populates ``cfg.runtime.bundle_dir``."""
    cfg = get_config()
    url = cfg.agentihooks_bundle_url
    if not url:
        return None
    dest = _bundle_dir()
    if dest is None:
        return None
    if dest.exists() and (dest / ".git").exists():
        _clone_or_fetch_bundle(url, dest, cfg.agentihooks_bundle_branch)
    elif dest.exists():
        # Pre-mounted checkout (dev bind-mount): trust it, no fetch.
        logger.info("agentihooks-bundle: pre-mounted checkout at %s, skipping clone", dest)
    else:
        _clone_or_fetch_bundle(url, dest, cfg.agentihooks_bundle_branch)
    cfg.runtime.bundle_dir = dest
    logger.info("agentihooks bundle synced → %s (branch=%s)", dest, cfg.agentihooks_bundle_branch or "HEAD")
    return dest


def run_agentihooks_init(
    hooks_path: Optional[Path] = None,
    bundle_path: Optional[Path] = None,
    force: bool = False,
) -> None:
    """Run ``agentihooks init`` with profile and optional bundle.

    Assumes the ``agentihooks`` CLI is already on PATH — installed by
    :func:`sync_agentihooks` at boot. The ``hooks_path`` parameter is
    accepted for backwards compatibility with existing call sites but is
    no longer used for installation.

    *force* controls whether ``--force`` is passed to agentihooks.
    ``--force`` wipes scoped state (state.json, ~/.claude assets) and
    re-installs from a clean slate — operator's drift-recovery tool,
    NOT a per-restart hammer. Default is False. Caller decides:

    - First-time provisioning of a new ``AGENTIHOOKS_HOME`` (no state.json)
      → force=True
    - Boot when state.json already exists → force=False (idempotent rerun)
    - Operator-triggered ``/admin/sync`` → force=True
    """
    del hooks_path  # Accepted for backwards compat; install is handled in sync_agentihooks.
    t0 = time.monotonic()
    cfg = get_config()
    shared_root = Path(cfg.repos.shared_fs_root or "/shared")

    # Clear stale sync locks from previous pod incarnations. shared_root is a
    # PVC so sync.lock + sync-daemon.pid survive pod restarts. If a prior
    # pod crashed or was force-killed mid-init, the locks stay on disk and
    # new agentihooks invocations deadlock waiting for a process that will
    # never release them. PID in sync-daemon.pid is from the dead pod and
    # references nothing in the new pod's namespace.
    try:
        state_dir = shared_root / ".agentihooks"
        if state_dir.exists():
            for stale in ("sync.lock", "sync-daemon.pid"):
                p = state_dir / stale
                if p.exists():
                    logger.info("Clearing stale %s from /shared (previous pod)", stale)
                    p.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover
        logger.warning("stale-lock cleanup failed: %s", exc)

    profile = get_config().agentihooks_profile

    # Persist the bundle link BEFORE init. Passing --bundle to init is
    # transient — it tells init where to read this one time but doesn't
    # write the link to state.json. Without a persisted link, subsequent
    # agentihooks invocations (including the pre-call MCP render hook and
    # any interactive shell) see "No bundle linked" and refuse to merge
    # the bundle's master .mcp.json into ~/.claude.json. Net effect:
    # Claude Code sessions spawn with zero MCP servers, which silently
    # breaks agents that rely on MCP tool access. Run `bundle link`
    # explicitly so state.json carries the linkage across restarts.
    if bundle_path and bundle_path.exists():
        link_cmd = ["agentihooks", "bundle", "link", str(bundle_path)]
        logger.info("Running: %s", " ".join(link_cmd))
        link_result = subprocess.run(link_cmd, capture_output=True, text=True)
        if link_result.returncode != 0:
            logger.warning(
                "agentihooks bundle link failed (exit %d): %s",
                link_result.returncode,
                link_result.stderr.strip(),
            )
        else:
            for line in (link_result.stdout or "").strip().splitlines():
                logger.info("agentihooks: %s", line)

    cmd = ["agentihooks", "init"]
    if force:
        cmd.append("--force")
    if profile:
        cmd.extend(["--profile", profile])
    if bundle_path and bundle_path.exists():
        cmd.extend(["--bundle", str(bundle_path)])

    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("agentihooks: %s", line)
    if result.returncode != 0:
        logger.error("agentihooks init failed (exit %d):\n%s", result.returncode, result.stderr)
        raise RuntimeError(f"agentihooks init failed: {result.stderr}")

    # Post-init assertion: if a bundle was provided, ~/.claude.json MUST
    # have at least one MCP server registered. Zero servers = bundle link
    # or init silently no-op'd, meaning every subsequent claude subprocess
    # will start with no MCP tools. Fail loudly instead of limping on.
    if bundle_path and bundle_path.exists():
        try:
            import json as _json

            claude_json = shared_root / ".claude.json"
            if claude_json.exists():
                data = _json.loads(claude_json.read_text())
                mcp_count = len(data.get("mcpServers", {}))
                if mcp_count == 0:
                    logger.error(
                        "agentihooks init: bundle linked but ~/.claude.json "
                        "mcpServers is empty. Agent will start with no MCP "
                        "tools. Check bundle .claude/.mcp.json integrity."
                    )
                else:
                    logger.info(
                        "agentihooks init: ~/.claude.json mcpServers count=%d",
                        mcp_count,
                    )
        except Exception as exc:  # pragma: no cover — belt-and-braces observability
            logger.warning("agentihooks init post-check failed: %s", exc)

    logger.info(
        "agentihooks init complete in %.2fs (profile=%s, bundle=%s)",
        time.monotonic() - t0,
        profile or "(agentihooks default)",
        bundle_path,
    )
