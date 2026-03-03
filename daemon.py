"""Daemon process: runs orchestrator + web dashboard in background."""

import datetime
import logging
import os
import signal
import sys

import uvicorn

from core.config import load_config
from core.orchestrator import Orchestrator
from web.app import app, set_orchestrator

PID_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "daemon.pid"
)


def setup_logging(config: dict) -> str:
    """Configure logging to a timestamped file. Returns the actual log file path."""
    base = config["logging"]["file"]  # e.g. logs/agent.log
    stem, ext = os.path.splitext(base)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"{stem}_{ts}{ext}"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    level = getattr(logging, config["logging"]["level"].upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
        ],
    )
    return log_file


def write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def read_pid() -> int:
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return 0
    return 0


def is_running() -> bool:
    pid = read_pid()
    if pid == 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start(config_path: str = None, foreground: bool = False):
    """Start the daemon."""
    if is_running():
        print(f"Daemon already running (pid={read_pid()})")
        return

    config = load_config(config_path)

    if not foreground:
        # Fork to background — logging is set up only in the child
        pid = os.fork()
        if pid > 0:
            # Parent: print info and exit
            print(f"Daemon started (pid={pid})")
            print(f"Dashboard: http://localhost:{config['web']['port']}")
            print(f"Logs: {os.path.dirname(os.path.abspath(config['logging']['file']))}")
            return
        # Child process
        os.setsid()

    log_file = setup_logging(config)
    log = logging.getLogger("daemon")
    log.info("Log file: %s", log_file)

    write_pid()
    log.info("Daemon starting (pid=%d)", os.getpid())

    def handle_signal(signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        orch.stop()
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Initialize orchestrator
    orch = Orchestrator(config)
    set_orchestrator(orch)

    # Start orchestrator loop
    orch.start()
    log.info("Orchestrator started, launching web dashboard on port %d", config["web"]["port"])

    # Run web server (blocks)
    uvicorn.run(
        app,
        host=config["web"]["host"],
        port=config["web"]["port"],
        log_level="warning",
    )


def stop():
    """Stop the daemon."""
    pid = read_pid()
    if pid == 0 or not is_running():
        print("Daemon is not running")
        remove_pid()
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Daemon stopped (pid={pid})")
    remove_pid()


def status():
    """Check daemon status."""
    pid = read_pid()
    if is_running():
        print(f"Daemon is running (pid={pid})")
    else:
        print("Daemon is not running")
        if pid:
            remove_pid()
