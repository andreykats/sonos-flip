"""Automatic switching, driven by whether the soundbar is playing TV audio."""
import logging
import time

from .sonos import SonosError

log = logging.getLogger(__name__)

REPEAT_ERROR_SEC = 600   # while polling keeps failing, log it again at most this often


def enabled(cfg):
    return cfg.theater_on_tv or cfg.desk_after_tv_min is not None


def describe(cfg):
    theater = "on" if cfg.theater_on_tv else "off"
    m = cfg.desk_after_tv_min
    desk = "off" if m is None else ("as soon as TV audio stops" if m == 0 else f"{m:g} min after TV audio stops")
    return f"switch to theater when TV audio starts: {theater}; switch to desk: {desk}"


def watch(flip, wait=None):
    """Edge-triggered on the soundbar's TV audio:
      not TV -> TV: switch to theater (if automation.theater_on_tv).
      TV -> not TV: once it has stayed off for automation.desk_after_tv_min (0 = at once), and no
                    /desk or /theater request came after it stopped, switch to desk.
    The first successful poll only records the baseline: TV already playing at startup is not an
    edge, and TV already off at startup starts no countdown (it is unknown when it stopped).
    wait(seconds) sleeps between polls and returns True to stop (threading.Event.wait does both)."""
    if wait is None:
        def wait(seconds):
            time.sleep(seconds)
            return False
    cfg = flip.cfg
    log.info("Watching the soundbar every %g s (%s)", cfg.poll_sec, describe(cfg))
    prev = None
    tv_off_at = None        # monotonic time TV audio stopped, while a desk countdown is pending
    failing_since = None    # monotonic time polling started failing
    last_error_log = None
    while True:
        try:
            t = flip.soundbar_transport()
            if failing_since is not None:
                log.info("Soundbar answering again after %.0f s", time.monotonic() - failing_since)
                failing_since = None
            now = t["transport_state"] == "PLAYING" and (t["current_uri"] or "").startswith(cfg.tv_source_prefix)
            if prev is None:
                log.info("Soundbar is %s", "playing TV audio" if now else "not playing TV audio")
            elif now and not prev:
                if tv_off_at is not None:
                    log.info("TV audio is back; the switch to desk is cancelled")
                tv_off_at = None
                if cfg.theater_on_tv:
                    log.info("TV audio started -> theater")
                    try:
                        flip.to_theater()
                    except Exception:
                        pass        # switch() already logged it
                else:
                    log.info("TV audio started (automation.theater_on_tv is off, staying as is)")
            elif prev and not now:
                if cfg.desk_after_tv_min is None:
                    log.info("TV audio stopped (automation.desk_after_tv_min is off, staying as is)")
                else:
                    tv_off_at = time.monotonic()
                    log.info("TV audio stopped; switching to desk %s",
                             f"in {cfg.desk_after_tv_min:g} min unless it starts again"
                             if cfg.desk_after_tv_min else "now")
            prev = now
        except Exception as e:
            nowm = time.monotonic()
            if failing_since is None:
                failing_since = last_error_log = nowm
                log.warning("Can't check the soundbar: %s. Will keep trying every %g s.", e, cfg.poll_sec,
                            exc_info=not isinstance(e, SonosError))
            elif nowm - last_error_log >= REPEAT_ERROR_SEC:
                last_error_log = nowm
                log.warning("Still can't check the soundbar (%.0f min): %s", (nowm - failing_since) / 60, e)

        if tv_off_at is not None and time.monotonic() - tv_off_at >= cfg.desk_after_tv_min * 60:
            off_at, tv_off_at = tv_off_at, None
            if flip.last_request > off_at:
                log.info("Not switching to desk: a /desk or /theater request came after TV audio stopped")
            else:
                log.info("TV audio stopped -> desk")
                try:
                    flip.to_desk()
                except Exception:
                    pass            # switch() already logged it
        if wait(cfg.poll_sec):
            log.info("Stopped watching the soundbar")
            return
