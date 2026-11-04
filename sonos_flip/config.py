"""Settings: a TOML file (see config.example.toml), validated into a frozen Config."""
import dataclasses
import os
import tomllib


class ConfigError(Exception):
    """The config file is missing or invalid; the message names the setting and what is wrong."""


@dataclasses.dataclass(frozen=True)
class Config:
    # [speakers]
    soundbar: str
    left: str
    right: str
    pair_name: str
    # top level
    log_level: str = "info"
    # [http]
    port: int = 8080
    bind: str = "0.0.0.0"
    # [automation]
    theater_on_tv: bool = True
    desk_after_tv_min: float | None = 0.0     # None = never ("off" in the file)
    tv_source_prefix: str = "x-sonos-htastream:"
    poll_sec: float = 3.0
    # [line_in]
    line_in: str = "off"                      # "off", "left" or "right"
    line_in_volume: int = 100
    # [timing]
    settle_timeout_sec: float = 60.0
    retries_not_ready: int = 8
    pause_before_bond_sec: float = 4.0
    # not a setting: state.json next to the config file
    state_file: str = "state.json"


# ------------------------------------------------------------ validators ----
# Each takes (name, value) and returns the converted value or raises ConfigError.

def text(name, v):
    if not isinstance(v, str):
        raise ConfigError(f"{name} must be a string in quotes, got {v!r}")
    v = v.strip()
    if not v:
        raise ConfigError(f"{name} must not be empty")
    return v


def boolean(name, v):
    if not isinstance(v, bool):
        raise ConfigError(f"{name} must be true or false, got {v!r}")
    return v


def integer(lo, hi):
    def check(name, v):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(f"{name} must be a whole number, got {v!r}")
        if not lo <= v <= hi:
            raise ConfigError(f"{name} must be between {lo} and {hi}, got {v}")
        return v
    return check


def number(lo, positive=False):
    def check(name, v):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ConfigError(f"{name} must be a number, got {v!r}")
        if v < lo or (positive and v <= lo):
            raise ConfigError(f"{name} must be {'>' if positive else '>='} {lo:g}, got {v}")
        return float(v)
    return check


def choice(*options):
    def check(name, v):
        v = text(name, v).lower()
        if v not in options:
            raise ConfigError(f"{name} must be one of {', '.join(options)}; got {v!r}")
        return v
    return check


def minutes_or_off(name, v):
    if isinstance(v, str) and v.strip().lower() == "off":
        return None
    try:
        return number(0)(name, v)
    except ConfigError:
        raise ConfigError(f'{name} must be a number of minutes >= 0 or "off", got {v!r}') from None


REQUIRED = object()

# table -> key -> (Config field, validator, default). "" = top level.
SCHEMA = {
    "": {
        "log_level": ("log_level", choice("debug", "info", "warning", "error"), "info"),
    },
    "speakers": {
        "soundbar": ("soundbar", text, REQUIRED),
        "left": ("left", text, REQUIRED),
        "right": ("right", text, REQUIRED),
        "pair_name": ("pair_name", text, REQUIRED),
    },
    "http": {
        "port": ("port", integer(1, 65535), 8080),
        "bind": ("bind", text, "0.0.0.0"),
    },
    "automation": {
        "theater_on_tv": ("theater_on_tv", boolean, True),
        "desk_after_tv_min": ("desk_after_tv_min", minutes_or_off, 0.0),
        "tv_source_prefix": ("tv_source_prefix", text, "x-sonos-htastream:"),
        "poll_sec": ("poll_sec", number(0, positive=True), 3.0),
    },
    "line_in": {
        "speaker": ("line_in", choice("off", "left", "right"), "off"),
        "volume": ("line_in_volume", integer(0, 100), 100),
    },
    "timing": {
        "settle_timeout_sec": ("settle_timeout_sec", number(0, positive=True), 60.0),
        "retries_not_ready": ("retries_not_ready", integer(1, 100), 8),
        "pause_before_bond_sec": ("pause_before_bond_sec", number(0), 4.0),
    },
}


def parse(data, path):
    """Validate a parsed TOML dict. path only decides where state.json lives."""
    values = {}
    tables = {k for k in SCHEMA if k}
    for key, v in data.items():
        if key in tables:
            if not isinstance(v, dict):
                raise ConfigError(f"[{key}] must be a table")
        elif key not in SCHEMA[""]:
            hint = " (settings belong under a [table] heading)" if any(key in t for t in SCHEMA.values()) else ""
            raise ConfigError(f"unknown setting {key!r}{hint}")
    for table, keys in SCHEMA.items():
        section = data if table == "" else data.get(table, {})
        prefix = f"{table}." if table else ""
        if table:
            unknown = sorted(set(section) - set(keys))
            if unknown:
                raise ConfigError(f"unknown setting(s) in [{table}]: {', '.join(unknown)}")
        for key, (field, check, default) in keys.items():
            if key in section:
                values[field] = check(prefix + key, section[key])
            elif default is REQUIRED:
                raise ConfigError(f"{prefix}{key} is required (set it under [{table}])")
            else:
                values[field] = default
    cfg = Config(**values, state_file=os.path.join(os.path.dirname(os.path.abspath(path)), "state.json"))
    if cfg.left.lower() == cfg.right.lower():
        raise ConfigError("speakers.left and speakers.right must be different speakers")
    return cfg


def load(path):
    """Read and validate the config file. Raises ConfigError with a readable message."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    return parse(data, path)
