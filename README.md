# sonos-flip

Use two Sonos speakers both as your soundbar's rear surrounds and as a stereo pair somewhere else, such as a desk, and switch between the two automatically.

Sonos turns off AirPlay, line-in and Bluetooth on speakers that are bonded to a soundbar as surrounds. sonos-flip moves the two speakers between two modes:

- **Theater:** the speakers are the soundbar's rear surrounds.
- **Desk:** the speakers are a standalone stereo pair, so AirPlay, line-in and Bluetooth work again.

It switches to theater when the TV starts playing through the soundbar, and back to desk when the TV stops. You can also switch by hand over a small HTTP API, from a hotkey, a shortcut or a home-automation system.

It runs as one small Docker container on any always-on machine on your home network. It only uses the speakers' local network API (no Sonos account, no cloud) and has no dependencies beyond Python's standard library.

## Requirements

- A Sonos soundbar (Arc, Arc Ultra, Beam, Ray, …) and two Sonos speakers that Sonos allows both as its rear surrounds and as a stereo pair. That means two of the same model, for example two Era 100 or two One SL.
- A Linux machine running Docker on the same network as the speakers (a home server, NAS or Raspberry Pi). Images are built for amd64 and arm64.
- **Host networking** (`--network host`). The container finds and controls the speakers on the local network. On Docker Desktop for Mac or Windows, host networking may not reach the speakers; give the speakers' IP addresses instead of room names, or use a Linux host.

Tested with an Arc Ultra and two Era 100 SL.

## Quick start

1. Create a folder for the config and start the container once:

   ```bash
   mkdir -p ~/sonos-flip
   docker run -d --name sonos-flip --network host --restart unless-stopped \
     -v ~/sonos-flip:/config -e TZ=Europe/Berlin andreykats/sonos-flip
   ```

   With no config yet, it writes a commented template to `~/sonos-flip/config.toml` and stops.

2. Fill in the `[speakers]` section of `config.toml`, using the room names from the Sonos app:

   ```toml
   [speakers]
   soundbar = "Living Room"
   left = "Desk Left"
   right = "Desk Right"
   pair_name = "Desk"
   ```

   Give `left` and `right` the names the two speakers have **on their own**, before they're paired or bonded. If they're already paired or bonded, give any names: the service recognises them by their place in the pair or on the soundbar. You can also use IP addresses instead of names.

3. Start it again and check the log:

   ```bash
   docker start sonos-flip
   docker logs -f sonos-flip
   ```

   ```text
   2026-11-04 16:36:33 INFO    sonos-flip 0.1.0 starting, config /config/config.toml
   2026-11-04 16:36:33 INFO    HTTP API listening on 0.0.0.0:8080
   2026-11-04 16:36:34 INFO    Found soundbar "Living Room" at 192.168.1.20
   2026-11-04 16:36:34 INFO    Soundbar:      "Living Room" at 192.168.1.20, RINCON_…
   2026-11-04 16:36:34 INFO    Left speaker:  "Desk" at 192.168.1.21, RINCON_… (the left half of the "Desk" pair)
   2026-11-04 16:36:34 INFO    Right speaker: "Desk" at 192.168.1.22, RINCON_… (the right half of the "Desk" pair)
   2026-11-04 16:36:34 INFO    Current mode: desk
   2026-11-04 16:36:34 INFO    Watching the soundbar every 3 s (switch to theater when TV audio starts: on; switch to desk: as soon as TV audio stops)
   ```

With Docker Compose, use [docker-compose.yml](docker-compose.yml) from this repository: `docker compose up -d`. It keeps the config in `./config`.

## Configuration

Everything is in `config.toml` in the folder mounted at `/config`. [config.example.toml](config.example.toml) explains every setting. Restart the container after a change. If a setting is wrong, the container stops at startup and the log names the setting and the problem, for example:

```text
2026-11-04 16:40:02 ERROR   Config error: line_in.volume must be between 0 and 100, got 120. Fix /config/config.toml and restart.
```

| Section | Setting | Default | Meaning |
|---|---|---|---|
| | `log_level` | `"info"` | `debug` also logs every request to the speakers |
| `[speakers]` | `soundbar`, `left`, `right` | required | room name (as in the Sonos app) or IP address |
| | `pair_name` | required | room name for the pair in desk mode; also its AirPlay name |
| `[automation]` | `theater_on_tv` | `true` | switch to theater when TV audio starts |
| | `desk_after_tv_min` | `0` | minutes after TV audio stops before switching to desk; `"off"` = never |
| | `poll_sec` | `3` | how often the soundbar is checked |
| | `tv_source_prefix` | `"x-sonos-htastream:"` | what counts as TV audio (see `GET /soundbar`) |
| `[line_in]` | `speaker` | `"off"` | `"left"` / `"right"`: play that speaker's line-in after every switch to desk |
| | `volume` | `100` | pair volume while line-in plays |
| `[http]` | `port` | `8080` | HTTP API port |
| | `bind` | `"0.0.0.0"` | address to listen on |
| `[timing]` | `settle_timeout_sec`, `retries_not_ready`, `pause_before_bond_sec` | `60`, `8`, `4` | waits while speakers regroup; rarely need changing |

`TZ` (environment variable) sets the time zone of the log timestamps.

The same folder holds `state.json`, which the service writes itself: the speakers it found, and the volume to set back after line-in. Deleting it is safe.

## HTTP API

Replace `HOST` with the machine running the container.

```bash
curl -X POST http://HOST:8080/desk
```

```bash
curl -X POST http://HOST:8080/theater
```

```bash
curl http://HOST:8080/status
```

| Request | What it does |
|---|---|
| `POST /desk` | Switch to desk. Answers when the switch is done (about 30 s). |
| `POST /theater` | Switch to theater (about 10 s). |
| `GET /status` | Current mode (`desk`, `theater` or `unknown`) and the raw bond state. |
| `GET /soundbar` | What the soundbar is playing. Use it to check `tv_source_prefix`. |
| `GET /health` | `200` once the speakers are found; `503` with the reason until then. Used by the Docker health check. |

Responses are JSON. Errors come as `{"error": "..."}`: `503` while the speakers haven't been found yet, `502` when a speaker failed or didn't answer, `404`/`405` for a wrong path or method, and `500` for a bug (with details in the log).

**The API has no password.** Anyone who can reach the port can switch your speakers. Keep it on your home network; don't forward the port to the internet.

[examples/hammerspoon-init.lua](examples/hammerspoon-init.lua) shows Mac hotkeys for both switches.

## How it behaves

- **Last event wins.** If you're at the desk and the TV starts, theater takes the speakers. If you switch to desk while the TV is playing, you get them back, and the TV won't take them again until its audio stops and starts again.
- **Switching to theater happens on the first TV audio,** not when the TV turns on. Soundbars keep their TV input while the TV only shows a menu, so the switch waits until something actually plays.
- **Switching back to desk happens when the soundbar lets go of the TV input,** plus `desk_after_tv_min`. After a TV or streaming box goes to sleep, a soundbar can hold the input for a while. An Arc Ultra with an Apple TV held it for about 10 minutes.
- A `/desk` or `/theater` request cancels a pending automatic switch to desk, but only during the `desk_after_tv_min` wait.
- Anything that makes the soundbar leave its TV input counts as "TV stopped", for example playing music on it from the Sonos app.
- **One switch at a time.** A second request waits for the first to finish. Asking for the mode you're already in changes nothing.
- **A half-finished switch is completed** by the next request, so the speakers never get stuck.
- **Restarting the container never switches anything.** On startup it only reads the current state.
- **The pair keeps its name.** Sonos names a new pair after its left speaker; the service renames it to `pair_name` after every switch, so AirPlay targets and automations keep working.
- **Line-in** (`[line_in] speaker`): after every switch to desk, the pair plays the line-in of the speaker with the Sonos Line-In Adapter, at `[line_in] volume`, so the device plugged into it controls the level. The pair's previous volume is saved and set back on the next switch to theater. If line-in can't start, the switch still completes and the volume isn't touched.

### If something goes wrong

Nothing stops the service except a bad config file. Every failure is logged with what was being done and why:

- Speakers not found at startup (off, wrong name, network down): the log says why and lists the rooms it can see. It retries every few seconds, up to once a minute, until they show up. Until then the API answers `503`.
- The soundbar stops answering (for example, after its IP address changed): polling keeps going and logs once when it starts failing and once when it recovers. A soundbar given by name is found again at its new address.
- A switch fails part-way (a speaker didn't answer or didn't settle): the request returns `502` with the reason. The next `/desk` or `/theater` request finishes the job.

## Troubleshooting

| Log message | What to do |
|---|---|
| `no Sonos speakers answered on the network` | Run the container with `--network host`, on the same network (and VLAN) as the speakers. Or give the soundbar's IP address instead of its name. |
| `speakers.left: no speaker named "…"` | Check the spelling against the Sonos app. The message lists the rooms it can see. Paired or bonded speakers hide their own names; use their IP addresses if they can't be recognised. |
| `… UPnP error 800 (not settled yet …)` repeated, then `still weren't settled` | The speakers took longer than usual to regroup. Ask again; if it keeps happening, raise `[timing] retries_not_ready`. |
| `… didn't come back on its own within 60 s` | A speaker was slow to leave the pair or soundbar. Ask again, or raise `settle_timeout_sec`. |
| `Can't listen on 0.0.0.0:8080` | Another program uses the port. Change `[http] port`. |

**A stale "… Left" speaker in the Mac's AirPlay list.** Each switch briefly announces the left speaker under its own name, and macOS can keep listing it for a while after it's gone. It can't be connected to and expires on its own; turning Wi-Fi off and on clears it right away.

## TV detection, as measured on an Arc Ultra with an Apple TV

The service is in "TV" state when the soundbar reports `PLAYING` and its source starts with `tv_source_prefix`.

| Apple TV | Soundbar playback | Soundbar source | Counts as TV |
|---|---|---|---|
| asleep, or awake on the home screen with nothing played yet | `STOPPED` | its own queue (`x-rincon-queue:`) | no |
| playing | `PLAYING` | TV input (`x-sonos-htastream:…:spdif`) | yes |
| paused, or home screen after playing | `PLAYING` | TV input | yes |
| just went to sleep (first ~10 min) | `PLAYING` | TV input | yes |
| asleep, after the soundbar lets go of the input | `STOPPED` | its own queue | no |

Whether the TV itself was on or off made no difference.

## Development

```bash
python3 -m unittest -v
```

The code is in [sonos_flip/](sonos_flip/): `config` (settings), `sonos` (requests, topology, discovery), `flip` (finding the speakers and switching), `watcher` (automation), `server` (HTTP API), `state` (state.json). It needs Python 3.11 or newer and nothing else.

To run it from the checkout, without Docker:

```bash
CONFIG_FILE=./config.toml python3 -m sonos_flip
```

[examples/deploy-ssh.sh](examples/deploy-ssh.sh) builds the image from the checkout on a remote Docker host and restarts the container there.

Releases: pushing a tag `vX.Y.Z` that matches `sonos_flip.__version__` runs the tests and publishes `andreykats/sonos-flip:X.Y.Z`, `:X.Y` and `:latest` for amd64 and arm64 ([workflow](.github/workflows/docker.yml)).

## Credits

The grouping recipes and settle/retry strategy follow the hardware findings in [willblaschko/chorus](https://github.com/willblaschko/chorus) (`docs/SPIKE_FINDINGS.md`). The Sonos local API reference at [sonos.svrooij.io](https://sonos.svrooij.io) was invaluable.

Not affiliated with or endorsed by Sonos.

## License

[MIT](LICENSE)
