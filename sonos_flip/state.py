"""state.json: what the service has to remember across restarts.

  saved_volume  the pair's volume before line-in raised it; set back on the next switch to theater
  speakers      {"soundbar"|"left"|"right": {"config": <value from config.toml>, "uid": "RINCON_..."}}
                speakers found earlier, so they can be recognised even while their room name is
                hidden by a bond (as surrounds they take the soundbar's name)
"""
import json
import logging
import os

log = logging.getLogger(__name__)


def load(path):
    """The saved state, or {} if there is none or it is unreadable (logged, never fatal)."""
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable %s (%s); it will be rewritten", path, e)
        return {}
    if not isinstance(st, dict):
        log.warning("Ignoring %s: expected a JSON object", path)
        return {}
    return st


def save(path, st):
    """Write atomically, so a crash mid-write never leaves a half-written file. Raises OSError."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update(path, **changes):
    """Set keys (value None removes the key) and save. Raises OSError."""
    st = load(path)
    for k, v in changes.items():
        if v is None:
            st.pop(k, None)
        else:
            st[k] = v
    save(path, st)


def saved_volume(path):
    v = load(path).get("saved_volume")
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100:
        return v
    if v is not None:
        log.warning("Ignoring invalid saved_volume %r in %s", v, path)
    return None
