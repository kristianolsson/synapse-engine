"""Per-session browser daemon: owns Firefox and serves JSON commands on a unix socket.

Started detached by client.spawn_daemon(); not meant to be run by hand.
One newline-terminated JSON request and one JSON response per connection.
Browser commands run one at a time; ping/status/stop are answered immediately.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import protocol, runtime
from .actions import Actions
from .errors import BrowserError
from .session import Session

DAEMON_MODULE = "services.ingestion.tools.browser.daemon"
logger = logging.getLogger("browser.daemon")


class Daemon:
    def __init__(self, name: str, paths: runtime.SessionPaths, session: Session, info: dict):
        self.name = name
        self.paths = paths
        self.session = session
        self.info = info
        self.actions = Actions(session)
        self.lock = asyncio.Lock()
        self.started = time.monotonic()
        self.last_activity = self.started
        self.closing = False
        self.stopped = asyncio.Event()
        self.server = None
        self.command_tasks: set = set()

    async def serve(self) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.paths.sock))
        os.chmod(self.paths.sock, 0o600)

    async def _handle(self, reader, writer) -> None:
        try:
            req = json.loads(await reader.readline())
            resp = await self._dispatch(req)
        except BrowserError as e:
            resp = e.to_response()
        except Exception as e:  # never let one request kill the daemon
            logger.exception("request failed")
            resp = {"ok": False, "error": f"daemon error: {e!r}", "code": "daemon_error"}
        try:
            writer.write(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pong": True}
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "stop":
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.shutdown("stop")))
            return {"ok": True, "message": f"session {self.name} stopping"}
        if self.closing:
            raise BrowserError(f"session {self.name} is shutting down — retry", "daemon_error")
        async with self.lock:
            self.last_activity = time.monotonic()
            deadline = protocol.command_deadline(req)
            task = asyncio.ensure_future(self.actions.execute(req))
            self.command_tasks.add(task)
            try:
                return await asyncio.wait_for(task, deadline)
            except asyncio.TimeoutError:
                raise BrowserError(f"{cmd} exceeded its {deadline:g}s deadline and was aborted", "timeout")
            except asyncio.CancelledError:
                if self.closing:
                    raise BrowserError(f"session {self.name} shut down during {cmd}", "daemon_error")
                raise
            finally:
                self.command_tasks.discard(task)
                self.last_activity = time.monotonic()

    def status(self) -> dict:
        now = time.monotonic()
        pgid = self.info.get("browser_pgid")
        page = self.session.pages[self.session.current] if self.session.pages else None
        return {
            "session": self.name,
            "age_s": round(now - self.started),
            "idle_s": round(now - self.last_activity),
            "rss_mb": round(runtime.group_rss_mb(pgid)) if pgid else None,
            "url": page.url if page else None,
            "profile": self.info.get("profile_name"),
        }

    async def watchdog(self) -> None:
        last_rss_check = 0.0
        pgid = self.info.get("browser_pgid")
        while not self.closing:
            await asyncio.sleep(protocol.watchdog_s())
            now = time.monotonic()
            reason = None
            if not self.lock.locked() and now - self.last_activity > protocol.idle_s():
                reason = "idle timeout"
            elif pgid and not runtime.group_alive(pgid, self.info["profile"]):
                reason = "browser exited"
            elif now - self.started > protocol.max_lifetime_s():
                reason = "max lifetime"
            elif pgid and now - last_rss_check >= protocol.rss_check_s():
                last_rss_check = now
                rss = runtime.group_rss_mb(pgid)
                if rss > protocol.max_rss_mb():
                    reason = f"memory guard ({rss:.0f} MB)"
            if reason:
                await self.shutdown(reason)
                return

    async def watch_browser(self) -> None:
        await self.session.closed.wait()
        await self.shutdown("browser exited")

    async def shutdown(self, reason: str) -> None:
        if self.closing:
            await self.stopped.wait()
            return
        self.closing = True
        logger.info("shutting down session %s: %s", self.name, reason)
        if self.server:
            self.server.close()
        pending = set(self.command_tasks)
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=5)
            for task in still_running:
                task.cancel()
        try:
            await asyncio.wait_for(self.session.close(), 10)
        except Exception:
            logger.warning("graceful browser close failed; killing process group", exc_info=True)
        pgid, profile = self.info.get("browser_pgid"), self.info.get("profile")
        if pgid and profile and runtime.group_alive(pgid, profile):
            runtime.kill_group(pgid)
        runtime.cleanup(self.paths)
        self.stopped.set()


class _Ready:
    """One-shot startup report to the spawning client."""

    def __init__(self, fd):
        self.fd = fd
        if fd is not None:
            os.set_inheritable(fd, False)  # keep it out of Firefox, or the client never sees EOF

    def send(self, msg: dict) -> None:
        if self.fd is None:
            print(json.dumps(msg), flush=True)
            return
        try:
            os.write(self.fd, json.dumps(msg).encode() + b"\n")
        finally:
            os.close(self.fd)
            self.fd = None


def _setup_logging(path: Path) -> None:
    logging.basicConfig(
        filename=str(path), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _run(args) -> int:
    paths = runtime.session_paths(args.session)
    paths.dir.mkdir(parents=True, exist_ok=True)
    ready = _Ready(args.ready_fd)
    lock_fd = runtime.try_lock(paths.lock, wait_s=0.5)
    if lock_fd is None:
        ready.send({"ok": True, "already_running": True})
        return 0
    _setup_logging(paths.log)
    session = None
    try:
        for action in runtime.sweep(paths):
            logger.info("startup sweep: %s", action)
        live = runtime.live_sessions()
        if len(live) > protocol.max_sessions():
            others = ", ".join(n for n in live if n != args.session)
            raise BrowserError(
                f"session cap reached ({protocol.max_sessions()}): {others} — run 'browser stop --session <name>'",
                "session_cap",
            )
        if args.profile:
            profile = runtime.profile_dir(args.profile)
            profile.mkdir(parents=True, exist_ok=True)
            if runtime.try_lock(profile / runtime.PROFILE_LOCK) is None:  # fd held for the process lifetime
                raise BrowserError(f"profile {args.profile} is in use by another session", "profile_locked")
            runtime.clear_profile_locks(profile)
        else:
            profile = paths.temp_profile
        session = Session(profile, paths.artifacts, headed=args.headed, allow_private=args.allow_private)
        try:
            await session.start()
        except Exception as e:
            session = None
            raise BrowserError(f"failed to launch Firefox: {e}", "daemon_start_failed") from e
        browser = runtime.find_browser(profile)
        info = {
            "daemon_pid": os.getpid(),
            "session": args.session,
            "browser_pid": browser.pid if browser else None,
            "browser_pgid": browser.pgid if browser else None,
            "profile": str(profile),
            "profile_name": args.profile,
            "ephemeral": not args.profile,
            "started_at": time.time(),
            "options": {"profile": args.profile, "headed": args.headed, "allow_private": args.allow_private},
        }
        runtime.write_info(paths, info)
        daemon = Daemon(args.session, paths, session, info)
        await daemon.serve()
    except BrowserError as e:
        logger.error("startup failed: %s", e.message)
        if session is not None:
            await session.close()
        runtime.cleanup(paths)
        ready.send(e.to_response())
        return 1

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(daemon.shutdown(f"signal {s.name}")))
    watchers = [asyncio.ensure_future(daemon.watchdog()), asyncio.ensure_future(daemon.watch_browser())]
    logger.info("session %s ready (profile %s)", args.session, profile)
    ready.send({"ok": True})
    await daemon.stopped.wait()
    for w in watchers:
        w.cancel()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="browser-daemon")
    parser.add_argument("--session", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--allow-private", action="store_true")
    parser.add_argument("--ready-fd", type=int)
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
