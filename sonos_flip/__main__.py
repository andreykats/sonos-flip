"""Entry point: python -m sonos_flip [healthcheck | --version]"""
import logging
import os
import shutil
import signal
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from . import __version__, config, watcher
from .flip import Flip
from .server import make_handler
from .sonos import SonosError

log = logging.getLogger("sonos_flip")

EX_CONFIG = 78      # sysexits.h: configuration error
DEFAULT_CONFIG = "/config/config.toml"
TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.toml")
CONNECT_RETRY_SEC = (5, 60)   # first and longest wait between attempts to find the speakers


def setup_logging(level="info"):
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        root.addHandler(h)
    root.setLevel(level.upper())


def write_template(path):
    """No config yet: put the commented template there for the user to fill in."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copyfile(TEMPLATE, path)
    except OSError as e:
        log.error("No config file at %s, and a template couldn't be written there (%s). "
                  "Mount a writable folder at %s.", path, e.strerror or e, os.path.dirname(path))
        return EX_CONFIG
    log.error("No config file found, so a template was written to %s. Fill in the [speakers] section "
              "(soundbar, left, right, pair_name), then restart the container.", path)
    return EX_CONFIG


def run_speakers(flip, stop):
    """Background: find the speakers (retrying until they answer), then watch the soundbar."""
    delay = CONNECT_RETRY_SEC[0]
    while not stop.is_set():
        try:
            flip.connect()
            flip.startup_error = None
            break
        except Exception as e:
            flip.startup_error = str(e)
            log.warning("Can't find the speakers yet: %s. Trying again in %d s.", e, delay,
                        exc_info=not isinstance(e, SonosError))
            stop.wait(delay)
            delay = min(delay * 2, CONNECT_RETRY_SEC[1])
    if stop.is_set():
        return
    try:
        log.info("Current mode: %s", flip.state()["mode"])
    except SonosError as e:
        log.warning("Couldn't read the current mode: %s", e)
    if watcher.enabled(flip.cfg):
        watcher.watch(flip, wait=stop.wait)
    else:
        log.info("Automatic switching is off (automation.theater_on_tv = false, desk_after_tv_min = \"off\")")


def healthcheck(path):
    """For Docker HEALTHCHECK: exit 0 if /health answers 200."""
    try:
        cfg = config.load(path)
        host = "127.0.0.1" if cfg.bind == "0.0.0.0" else cfg.bind
        with urllib.request.urlopen(f"http://{host}:{cfg.port}/health", timeout=4) as r:
            return 0 if r.status == 200 else 1
    except Exception as e:
        print(f"unhealthy: {e}", file=sys.stderr)
        return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = os.environ.get("CONFIG_FILE", DEFAULT_CONFIG)
    if argv == ["healthcheck"]:
        return healthcheck(path)
    if argv in (["--version"], ["version"]):
        print(__version__)
        return 0
    if argv:
        print("usage: python -m sonos_flip [healthcheck | --version]", file=sys.stderr)
        return 2

    setup_logging()
    log.info("sonos-flip %s starting, config %s", __version__, path)
    if not os.path.exists(path):
        return write_template(path)
    try:
        cfg = config.load(path)
    except config.ConfigError as e:
        log.error("Config error: %s. Fix %s and restart.", e, path)
        return EX_CONFIG
    setup_logging(cfg.log_level)
    state_dir = os.path.dirname(cfg.state_file)
    if not os.access(state_dir, os.W_OK):
        log.warning("%s isn't writable, so state.json can't be saved: the line-in volume won't be raised "
                    "and found speakers won't be remembered across restarts", state_dir)

    flip = Flip(cfg)
    try:
        server = ThreadingHTTPServer((cfg.bind, cfg.port), make_handler(flip))
    except OSError as e:
        log.error("Can't listen on %s:%d (%s). Change http.port or http.bind in %s.",
                  cfg.bind, cfg.port, e.strerror or e, path)
        return 1
    server.daemon_threads = True
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("Stopping (%s)", signal.Signals(signum).name)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=run_speakers, args=(flip, stop), name="speakers", daemon=True).start()
    log.info("HTTP API listening on %s:%d", cfg.bind, cfg.port)
    server.serve_forever()
    server.server_close()
    if flip.lock.locked():
        log.warning("Stopped in the middle of a switch; the next /desk or /theater request finishes it")
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
