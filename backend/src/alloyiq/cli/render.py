"""Render deployment helpers (docs/ops/deploy-render-vercel.md; DV-15).

    python -m alloyiq.cli.render exec -- <command...>   run a command with the role URLs set
    python -m alloyiq.cli.render predeploy              bootstrap roles, migrate, seed
    python -m alloyiq.cli.render worker                 MCP server (localhost) + worker

Render's Blueprint passes the database host, port and name, plus one generated password
per application role; it cannot compose URLs. `exec` builds DATABASE_URL,
DATABASE_WORKER_URL, DATABASE_MIGRATOR_URL and ALEMBIC_DATABASE_URL from them (passwords
URL-encoded) and replaces itself with the command.

`predeploy` runs before each worker deploy. With DATABASE_ADMIN_URL (Render's default
database user, which is not a superuser) it creates or updates the three roles that
docker/postgres/init/01-roles.sh creates locally, grants them CONNECT, and lets the
migrator create objects; then it runs `alembic upgrade head` and the idempotent seed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import quote

MCP_PORT = 8100
MCP_HEALTH = f"http://127.0.0.1:{MCP_PORT}/health"
STOP_GRACE_S = 20.0

ROLES = {
    "alloyiq_api": "ALLOYIQ_API_DB_PASSWORD",
    "alloyiq_worker": "ALLOYIQ_WORKER_DB_PASSWORD",
    "alloyiq_migrator": "ALLOYIQ_MIGRATOR_DB_PASSWORD",
}
URL_VARS = {
    "DATABASE_URL": "alloyiq_api",
    "DATABASE_WORKER_URL": "alloyiq_worker",
    "DATABASE_MIGRATOR_URL": "alloyiq_migrator",
}


def _need(name: str, env: dict[str, str]) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def role_urls(env: dict[str, str]) -> dict[str, str]:
    """Role URLs from DB_HOST / DB_PORT / DB_NAME and the per-role passwords."""
    host, name = _need("DB_HOST", env), _need("DB_NAME", env)
    port = env.get("DB_PORT", "5432").strip() or "5432"
    out = {}
    for var, role in URL_VARS.items():
        pw = quote(_need(ROLES[role], env), safe="")
        out[var] = f"postgresql+asyncpg://{role}:{pw}@{host}:{port}/{name}"
    out["ALEMBIC_DATABASE_URL"] = out["DATABASE_MIGRATOR_URL"].replace(
        "postgresql+asyncpg://", "postgresql+psycopg://", 1
    )
    return out


def _with_urls() -> dict[str, str]:
    env = dict(os.environ)
    if "DB_HOST" in env:  # Render; locally Compose already sets the URLs
        env.update(role_urls(env))
    return env


def bootstrap(admin_url: str, env: dict[str, str]) -> None:
    import psycopg
    from psycopg import sql

    url = admin_url.replace("postgres://", "postgresql://", 1)
    with psycopg.connect(url, autocommit=True) as conn:
        db = conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
        for role, var in ROLES.items():
            pw = _need(var, env)
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(
                sql.SQL(
                    "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(pw))
            )
            print(f"role {role}: {'updated' if exists else 'created'}")
        ident = sql.Identifier(db)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident))
        conn.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO alloyiq_api, alloyiq_worker, alloyiq_migrator"
            ).format(ident)
        )
        # The migrator creates the `graph` schema and every table, and grants the others.
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO alloyiq_migrator").format(ident))
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO alloyiq_migrator WITH GRANT OPTION")


def predeploy() -> int:
    env = _with_urls()
    bootstrap(_need("DATABASE_ADMIN_URL", env), env)
    for cmd in (["alembic", "upgrade", "head"], [sys.executable, "-m", "alloyiq.cli.seed"]):
        print("$", " ".join(cmd[-3:]), flush=True)
        code = subprocess.call(cmd, env=env)  # noqa: S603 - fixed commands
        if code:
            return code
    return 0


def _mcp_ready(url: str = MCP_HEALTH) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310 - fixed localhost URL
            return bool(r.status == 200)
    except OSError:
        return False


def _stop(*procs: subprocess.Popen[bytes]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def supervise(
    mcp_cmd: list[str],
    worker_cmd: list[str],
    env: dict[str, str],
    ready: Callable[[], bool] = _mcp_ready,
    ready_timeout_s: float = 90.0,
) -> int:
    """Run the MCP server and the worker in one container (Render has no free private
    services; DV-15). The worker starts once MCP is healthy. If either process exits, the
    other is stopped and the exit code is returned, so Render restarts the container."""

    def on_signal(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)  # Render sends SIGTERM on deploy and shutdown

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    mcp = subprocess.Popen(mcp_cmd, env=env)  # noqa: S603 - fixed commands
    procs = [mcp]
    try:
        deadline = time.monotonic() + ready_timeout_s
        while not ready():
            if mcp.poll() is not None:
                print("mcp exited before it was ready", flush=True)
                return mcp.returncode or 1
            if time.monotonic() > deadline:
                print("mcp did not become ready", flush=True)
                return 1
            time.sleep(0.5)
        worker = subprocess.Popen(worker_cmd, env=env)  # noqa: S603 - fixed commands
        procs.append(worker)
        while True:
            for p, name in ((mcp, "mcp"), (worker, "worker")):
                if p.poll() is not None:
                    print(f"{name} exited with {p.returncode}; stopping the container", flush=True)
                    return p.returncode or 1
            time.sleep(1.0)
    finally:
        _stop(*procs)  # never leave a child behind, whatever ended the loop
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_worker() -> int:
    env = {**_with_urls(), "MCP_HOST": "127.0.0.1", "MCP_URL": f"http://127.0.0.1:{MCP_PORT}/mcp"}
    env["MCP_PORT"] = str(MCP_PORT)
    py = sys.executable
    return supervise([py, "-m", "alloyiq.mcp_server"], [py, "-m", "alloyiq.worker"], env)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["predeploy"]:
        return predeploy()
    if args[:1] == ["worker"]:
        return run_worker()
    if args[:1] == ["exec"]:
        cmd = args[2:] if args[1:2] == ["--"] else args[1:]
        if not cmd:
            raise SystemExit("usage: python -m alloyiq.cli.render exec -- <command...>")
        os.execvpe(cmd[0], cmd, _with_urls())  # noqa: S606 - replaces this process
    raise SystemExit(__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
