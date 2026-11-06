<div align="center">

<img src="docs/images/logo.svg" width="88" alt="windoPane logo">

# windoPane

**A boot-to-video-wall appliance for Raspberry Pi and PCs.**<br>
Up to 16 live tiles per screen on up to four screens, run from a web UI on your phone.

![Raspberry Pi 4 | 5](https://img.shields.io/badge/Raspberry%20Pi-4%20%7C%205-c51a4a?logo=raspberrypi&logoColor=white)
![Debian | Ubuntu](https://img.shields.io/badge/PC-Debian%20%7C%20Ubuntu-a81d33?logo=debian&logoColor=white)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)
![Version 0.6.1](https://img.shields.io/badge/version-0.6.1-ffb020)

<img src="docs/images/wall.jpg" alt="A 2×2 wall: a video tile, a live ADS-B aircraft table, a web dashboard and a second video tile" width="900">

</div>

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [The web UI](#the-web-ui)
- [Adding streams](#adding-streams)
- [Playlists and live channels](#playlists-and-live-channels)
- [ADS-B tiles](#ads-b-tiles)
- [Multiple screens](#multiple-screens)
- [Audio and controls](#audio-and-controls)
- [Layouts](#layouts)
- [Performance](#performance)
- [Running on a PC](#running-on-a-pc)
- [OLED ticker (Freenove cases)](#oled-ticker-freenove-cases)
- [Managing it](#managing-it)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Development](#development)

## Features

- 📺 **Any stream:** YouTube, Twitch and Kick (via yt-dlp), RTSP cameras, HLS/DASH, UDP/SRT, plain files.
- 🌐 **Live web pages as tiles:** tar1090/dump1090 maps, radar, Grafana, any dashboard.
- ✈️ **ADS-B text table:** a plain aircraft list from dump1090, with distance and bearing from your Maidenhead locator or lat/long.
- 🔴 **Live takeover:** a channel can take over a tile when it goes live, ranked by priority, and give the tile back when the broadcast ends.
- 🎞️ **Playlists:** YouTube playlists play in order or shuffled, and removed or private videos are skipped.
- 🔇 **Muted by default:** one tile is audible at a time, picked from a keyboard, a USB/Bluetooth controller or the web UI.
- 🧩 **16 layouts:** from single to 4 × 4, chosen per screen.
- 🖥️ **Up to four screens:** two on a Pi, four on a PC, at up to 4K.
- ⚙️ **PC support:** mpv with GPU decoding, and AMD/Intel/NVIDIA driver switching from the web UI.
- 📟 **Optional OLED ticker** for Freenove Pi 5 cases.

## Quick start

Start from a fresh **Raspberry Pi OS Lite (64-bit), Bookworm or newer**, with SSH and network set up in Raspberry Pi Imager. For a PC, see [Running on a PC](#running-on-a-pc).

```bash
git clone https://github.com/<your-user>/windopane.git
cd windopane
sudo ./install.sh --password 'pick-one'
sudo reboot
```

<details>
<summary>Installing from a release tarball instead</summary>

```bash
scp windopane-0.6.1.tar.gz pi@<pi-address>:
ssh pi@<pi-address>
tar xzf windopane-0.6.1.tar.gz && cd windopane
sudo ./install.sh --password 'pick-one'
sudo reboot
```

</details>

After the reboot, the screen shows the empty layout with the web UI address on it. Open that address, add streams, and assign them to tiles.

| Installer option | Default | |
|---|---|---|
| `--user NAME` | the sudo user | account the wall runs as |
| `--port N` | `8090` | web UI port (8080 is left free for dump1090/SkyAware) |
| `--password X` | none | HTTP login for the web UI, any username |
| `--no-autostart` | autostart on | don't boot into the wall |
| `--no-4k60` | on | skip `hdmi_enable_4kp60=1` for Pi 4 |
| `--gpu X` | `auto` | PCs: `amd`, `intel`, `nvidia`, `none` or `skip` ([details](#running-on-a-pc)) |

Re-running `install.sh` upgrades in place and keeps your streams.

## The web UI

<img src="docs/images/web-ui.png" alt="windoPane web UI: a 2×2 screen preview with per-tile controls and the layout picker" width="720">

Every tile in the preview has its stream picker, a live status pill, and listen, fill-screen and reload buttons. Changes reach the wall immediately. Below the preview are the layout picker, the stream list, the controls reference and the settings.

### How it fits together

```mermaid
flowchart LR
    UI["Web UI / JSON API<br/>:8090"] --> C["Controller<br/>config.json"]
    K["Keyboards, gamepads<br/>(evdev)"] --> C
    LW["Live watcher<br/>(yt-dlp)"] --> C
    C --> W["Wall<br/>one GTK window per screen"]
    W --> V["Video tiles<br/>libVLC on Pi · mpv on PC"]
    W --> P["Web tiles<br/>WebKitGTK"]
    W --> A["ADS-B tiles<br/>dump1090 table"]
```

The wall runs on a bare X server with no desktop, started by autologin on tty1, and restarts itself if anything crashes.

## Adding streams

Paste a URL and **Auto-detect** picks the tile type. Use **Test** to check a URL before it goes on screen.

| Source | Example URL | Tile |
|---|---|---|
| YouTube video or live stream | `https://www.youtube.com/watch?v=…` | video, via yt-dlp |
| YouTube playlist | `https://www.youtube.com/playlist?list=…` | video, plays the whole list |
| YouTube channel, whenever it's live | `https://www.youtube.com/@channel/live` | video, waits for live |
| Twitch / Kick channel | `https://www.twitch.tv/channelname` | video, waits for live |
| IP camera | `rtsp://user:pass@192.168.1.50:554/stream1` | video (RTSP over TCP) |
| HLS / DASH | `https://…/index.m3u8` | video |
| Multicast / SRT | `udp://@239.0.0.1:1234`, `srt://host:port` | video |
| tar1090 map | `http://<adsb-pi>/tar1090/` (same Pi: `http://localhost/tar1090/`) | web |
| dump1090 SkyAware | `http://<adsb-pi>:8080/` | web |
| ADS-B text table | `http://<adsb-pi>/tar1090/` with type *ADS-B table*, a `.../aircraft.json` URL, or `tcp://<adsb-pi>:30003` | ADS-B |
| Any dashboard | `https://…` | web |

Set the type to **Video** to run an unknown site through yt-dlp, or to **Web page** to show a video site as a page. Each stream can also have a quality cap (video), and a page zoom and auto-reload interval (web).

## Playlists and live channels

A video stream has a **Behaviour**. *Auto* decides from the link, or you can pick one yourself:

| Behaviour | What it does |
|---|---|
| **Single video / stream** | Plays the link, and loops it if it ends. |
| **Play the playlist** | Plays every video in turn and loops at the end. **Shuffle** gives a random order, reshuffled on each pass. Deleted, private and region-blocked videos are skipped. The tile shows its place ("3/42") and has a skip button, and the list is re-read every 30 minutes so new videos show up on their own. Auto picks this for `youtube.com/playlist?list=…` links; a `watch?v=…&list=…` link plays only that one video unless you choose this. |
| **Wait for the channel to go live** | For a channel's `/live` link, or a Twitch or Kick channel. While the channel is off air, the tile shows a quiet "Not live right now" card (or "Scheduled: starts in 25 minutes") and checks again every 2 minutes (*Check every* changes that). It starts playing by itself when the channel goes live, muted like everything else, and goes back to waiting when the broadcast ends. **Test** in the form reports whether the channel is live right now. |

`N` / `B` (or the keyboard's media keys) skip forward and back on the playlist tile you're listening to.

### Live takeover

A channel set to wait for live doesn't need a tile of its own. In its form, set **When live, take over** to a screen and tile, and pick a **Priority** from 1 (highest) to 9. The channel is then checked in the background on its *Check every* interval:

- **Goes live:** it replaces that tile's stream on the wall. The stream you assigned stays assigned, and the tile's menu in the web UI still shows it.
- **Broadcast ends:** the tile goes back to the stream you assigned, or back to empty.
- **Several channels on one tile:** the highest priority shows; with equal priorities, the one that went live first wins. The others wait their turn and come on as soon as the tile is free, if they're still live.

In the web UI the tile gets a red **LIVE** tag and a **Send back** button. Send back puts your own stream back until that broadcast ends; the channel takes over again the next time it goes live.

> [!NOTE]
> Audio follows the tile, not the stream. If you were listening to that tile, you'll hear the live channel, then your own stream again afterwards. After a broadcast ends, the channel isn't checked again for one interval, because YouTube can still report a just-finished stream as live for a minute or so.

> [!TIP]
> No YouTube account is needed for public or unlisted playlists and channels. Private playlists, Watch Later, Liked videos, members-only and age-restricted videos need a signed-in session, which windoPane doesn't support; making a playlist *Unlisted* gets around that. Each live check is one page load from your connection, which is fine at the default 2 minutes even with several channels.

## ADS-B tiles

A plain-text aircraft table with no map. Columns: flight, hex, squawk, altitude (with a climb/descent arrow), vertical speed, ground speed, heading, **distance and bearing from your location**, RSSI and age.

- **Emergencies first:** squawks 7500, 7600 and 7700, and dump1090's emergency flag, pin the aircraft to the top in red. The rest are sorted nearest first, and aircraft without a position go last.
- **Fits small tiles:** columns drop away in small tiles, and rows that don't fit are counted ("+N more") instead of scrolling.
- **Source:** any dump1090-family web address (dump1090-fa, readsb, tar1090, SkyAware, dump1090-mutability). A bare receiver address is fine; the usual `aircraft.json` paths are tried until one answers. You can also use the raw BaseStation feed, `tcp://host:30003`, which every dump1090 build can output (`--net`).
- **Location:** a Maidenhead locator (`EM15`, `EM15fl`, `EM15fl42`) or `lat, lon` in decimal degrees (`35.47, -97.52` or `35.47N 97.52W`). Leave it empty to use the receiver's own location, if it publishes one in `receiver.json`.
- **Per tile:** distance units (nautical miles, kilometres or miles) and an optional max range.

The table refreshes every second. Tiles that use the same BaseStation feed share one connection, which closes after five minutes if nothing is showing it.

## Multiple screens

Each connected monitor becomes a screen with its own layout and up to 16 tiles: two screens on a Pi (its two HDMI ports), up to four on a PC. You can set up a screen from its tab in the web UI before anything is plugged in. It starts as soon as a monitor appears, and stops its streams when the monitor is unplugged. Screens 3 and 4 only appear in the web UI once a monitor is connected or they have been set up.

| | |
|---|---|
| **Order** | HDMI ports first, then DisplayPort, each by number. On a Pi, the port nearest the power socket is screen 1. |
| **Arrangement** | Screens are always extended, never mirrored. *Auto* puts them side by side when that fits the GPU's maximum desktop size, and in a two-column grid when it doesn't. Four 4K screens fit in a row on the usual 16384-pixel limit. |
| **Audio** | Only one tile is audible across all screens. On a Pi, video audio plays from the HDMI port of that tile's screen. On a PC, pick each screen's output in Settings. Web tiles use the system default output. |
| **Per screen** | Resolution and audio output, in Settings. Any screen except the first can be switched off. |
| **Pi 4** | 4K60 works on one port only. With two 4K screens both run at 30 Hz, so 1080p on the second screen is a good choice. A Pi 5 drives two 4K60 screens. |

The decoding budget is shared across screens: see [Performance](#performance).

## Audio and controls

All tiles start muted after every boot. Selecting a tile unmutes it and mutes the rest; selecting it again mutes it. The audible tile gets an amber frame, and its name appears briefly on screen.

Tiles are numbered 1–16 on each screen. You pick a screen, then a tile on it. The *focused* screen is the one with the audible tile, or else the last one picked.

| Action | Keyboard | Controller |
|---|---|---|
| Pick screen 1–4 (audio follows) | <kbd>F1</kbd>–<kbd>F4</kbd>, or <kbd>S</kbd> for the next | right stick click: next screen |
| Listen to a tile on the focused screen | <kbd>1</kbd>–<kbd>9</kbd>; for tiles 10–16, <kbd>1</kbd> then the second digit | A B X Y = tiles 1–4 |
| Mute all | <kbd>0</kbd> <kbd>M</kbd> | Start |
| Next / previous tile, across all screens | <kbd>→</kbd> <kbd>Tab</kbd> / <kbd>←</kbd> | RB / LB, d-pad left/right |
| Volume | <kbd>↑</kbd> <kbd>↓</kbd> <kbd>+</kbd> <kbd>-</kbd> | d-pad up/down |
| Next / previous layout (focused screen) | <kbd>L</kbd> / <kbd>K</kbd> | Select |
| Zoom the audible tile to full screen | <kbd>Z</kbd> <kbd>Enter</kbd> | Guide, left stick click |
| Reload the audible tile | <kbd>R</kbd> | |
| Next / previous video on a playlist tile | <kbd>N</kbd> / <kbd>B</kbd>, media keys | |

Number entry works like a TV remote: on screens with 10 or more tiles, <kbd>1</kbd> waits about a second for a second digit (<kbd>1</kbd> <kbd>2</kbd> is tile 12; <kbd>1</kbd> on its own is tile 1). Every other digit acts immediately. Devices are read directly, so no window focus is needed, and hot-plugging works. On a Pi 4 the TV remote may also work over HDMI-CEC, if your TV passes its buttons through.

## Layouts

Single, Side by side, Stacked, Picture in picture, Main + 2, Trio, 2 × 2, Main + 3 right, Main + 3 bottom, Main + 3 insets, 3 × 2, Main + 5, 3 × 3, Main + 7, Main + 12 and 4 × 4.

Each screen picks its own layout in the web UI, or press <kbd>L</kbd> to cycle the focused screen. Streams stay assigned to their tile numbers when you switch layouts, so tile 1 is always the big one.

## Performance

Each tile fetches the lowest standard quality that still fills it, capped by board and by how many video tiles are on all screens together:

| | GPU decode | 1–2 video tiles | 3–4 | 5–8 | 9 or more |
|---|---|---|---|---|---|
| Pi 4 | H.264, HEVC | up to 1080p | 720p | 480p | 360p |
| Pi 5 | HEVC only (fast CPU for H.264) | up to 1080p | 720p | 480p | 360p |
| PC with mpv | VA-API / NVDEC | up to 4K | 1080p | 720p | 480p |

- These caps apply to YouTube/Twitch-style sources. Cameras send whatever their URL asks for, so use camera sub-streams for small tiles.
- yt-dlp prefers H.264 over VP9/AV1. You can override the cap per stream (Max quality) or for everything (Settings → Quality cap).
- Web tiles cost CPU and RAM too. A 2 GB board is fine for four tiles; more RAM helps with several web tiles.
- Use the official PSU and a heatsink or fan. The web UI turns the board chip red when the Pi is throttling.
- Streams that error, stall (no new video for 45 s) or end restart automatically with backoff. Finished files and VODs loop.

<details>
<summary><b>Sizing a big PC wall</b></summary>

Most consumer GPUs drive four displays. Decoder throughput is the real limit on tiles: one 4K60 stream costs about as much as eight 1080p30 streams, and a current GPU typically handles at least one, often several, 4K60 streams. Plan on roughly 10–25 live 1080p tiles and verify with your actual feeds. Each mpv tile uses about 150–250 MB of RAM, and each web or ADS-B tile about 150–300 MB.

</details>

## Running on a PC

A mini PC or a desktop with a graphics card works too; AMD is the best-tested path. Install **Debian 12/13 or Ubuntu 24.04 Server** with no desktop (the installer makes the box boot straight into the wall), then run the same installer:

```bash
sudo ./install.sh --password 'pick-one'            # auto-detect the GPU
sudo ./install.sh --password 'pick-one' --gpu amd  # or choose: amd | intel | nvidia | none | skip
```

On a PC the installer also:

- plays video with **mpv**, one process per tile, decoding on the GPU with VA-API (AMD, Intel) or NVDEC (NVIDIA)
- detects the GPU and installs its video drivers (AMD, then Intel; NVIDIA only with `--gpu nvidia`, because its proprietary driver can need Secure Boot handling)
- adds tear-free output for AMD cards
- lets the web UI switch drivers later, through a sudo rule for the driver helper only

Screens are ordered HDMI first, then DisplayPort. Set each screen's audio output in Settings (*Auto* uses the system default on a PC).

<details>
<summary><b>Switching GPU drivers from the web UI</b></summary>

The **Graphics** panel (PCs only) shows the detected GPU, the installed driver set, and which codecs the GPU actually decodes. Pick AMD, Intel, NVIDIA or None, tick the confirmation box, and press **Switch drivers**. The log streams live, and a **Reboot now** button appears when it's done.

- The new vendor's drivers are installed first. Only if that succeeds are the other vendors' video packages purged, followed by `apt autoremove`. A failed switch leaves the previous drivers in place.
- Only fixed package lists are touched. The shared Mesa/OpenGL libraries the screen needs are never removed, and firmware packages are kept.
- On Debian, NVIDIA needs the `contrib non-free non-free-firmware` components enabled in your apt sources.

> [!WARNING]
> This changes system packages. Set a web UI password before using it.

</details>

<details>
<summary><b>Video player and GPU decoding</b></summary>

Settings → **Video player** picks the engine: *Auto* (VLC on a Pi, mpv on a PC), *VLC* or *mpv*. Each mpv runs as its own process drawing into its tile, so a misbehaving stream can't take the wall down; only that tile restarts.

- **mpv** (PC default) decodes on the GPU through VA-API on AMD and Intel, and NVDEC on NVIDIA. The Graphics panel's VA-API line lists the codecs your card decodes (an RX 6600 XT should list H.264, HEVC, VP9 and AV1).
- **VLC** on Debian/Ubuntu is built without VA-API (it doesn't work with FFmpeg 5+), so it can only use the older VDPAU interface, which Mesa removed upstream in September 2025. Ubuntu 24.04 and Debian 13 still ship it; newer releases won't. Choose VLC on a PC only if mpv can't play something.

</details>

## OLED ticker (Freenove cases)

<img src="docs/images/oled.png" align="right" width="200" alt="OLED ticker frames: muted, listening, ADS-B and retrying">

A small status display for the 0.96" OLED in Freenove's Raspberry Pi 5 cases (FNK0100, FNK0107), or any SSD1306 128×64 OLED on I2C.

- **Top line:** which tile you're hearing (`S2·T3` with several screens, `LIVE` during a takeover) and the clock.
- **Middle:** a scrolling ticker with the stream name and title. ADS-B tiles scroll the aircraft count, any emergency squawks, and the nearest aircraft with distance and bearing.
- **Bottom line:** state, volume and CPU temperature.

The featured tile is the one you're listening to. When everything is muted it's tile 1 of the focused screen, marked `MUTED`.

```bash
sudo ./extras/oled/install-oled.sh                         # defaults
sudo ./extras/oled/install-oled.sh --sleep 23:00-07:00     # display off overnight
sudo ./extras/oled/install-oled.sh --contrast 40           # dimmer
sudo ./extras/oled/install-oled.sh --rotate 180            # if it's upside down
sudo ./extras/oled/install-oled.sh --uninstall
```

<br clear="right">

- **Setup:** enables I2C if needed (the first time may ask for a reboot) and runs as its own service, separate from the wall (`journalctl -u windopane-oled -f`). It reads the wall's port and password from the wall's config, so changing the web password needs no extra step.
- **Orientation:** detected from the case. The FNK0107 mounts the OLED upside down, and the case's controller board reports which case it is.
- **Freenove's case software:** if it's installed, only its OLED screen is switched off, so the two don't fight over the display; fan and LED control keep running. `--uninstall` gives the OLED back. If you later turn the OLED on in Freenove's app, they will clash again.
- **Burn-in:** OLEDs burn in. The default contrast is reduced, the static lines move a pixel every few minutes, and `--sleep` turns the display off for set hours.

## Managing it

```bash
windopane-ctl status        # running?, web address, yt-dlp version
windopane-ctl restart       # restart the wall (a few seconds)
windopane-ctl logs          # follow the log
sudo windopane-ctl stop     # back to the console on HDMI, autostart off
sudo windopane-ctl start    # autostart on again
sudo windopane-ctl update-ytdlp
```

- **yt-dlp updates:** yt-dlp, plus Deno (which YouTube now requires), updates daily at about 4:20 am. The wall restarts itself if the version changed.
- **Files:** config is in `/var/lib/windopane/config.json`, logs in `/var/lib/windopane/logs/`.
- **Advanced:** `player.extra_vlc_args` in the config passes options straight to libVLC.
- **Uninstall:** `sudo ./uninstall.sh` (add `--purge` to delete your streams too). Installed GPU drivers are left in place.

## Troubleshooting

<details>
<summary><b>No picture on a Pi 5</b></summary>

Check `windopane-ctl logs`. The installer adds `/etc/X11/xorg.conf.d/99-windopane-vc4.conf` so X drives the HDMI GPU; Raspberry Pi OS sometimes ships its own equivalent instead.

</details>

<details>
<summary><b>No sound</b></summary>

With Video audio out on *Auto*, each screen uses its own HDMI port when the Pi exposes two HDMI audio devices, and otherwise PipeWire's default. `wpctl status` lists outputs and `wpctl set-default <id>` changes the default. Settings lets you pick a device per screen.

</details>

<details>
<summary><b>Screen 2 stays black</b></summary>

Check the Screens line under Settings and `windopane-ctl logs` (look for `outputs:`). If you arrange displays yourself with xrandr, start windoPane with `--no-xrandr` (edit `/opt/windopane/bin/windopane-xinit`).

</details>

<details>
<summary><b>YouTube or Twitch fails</b></summary>

Run `sudo windopane-ctl update-ytdlp`. The Settings panel shows the yt-dlp version and whether Deno is found.

</details>

<details>
<summary><b>4K looks like 30 Hz on a Pi 4</b></summary>

`hdmi_enable_4kp60` needs a reboot after install, and a cable rated for 4K60.

</details>

<details>
<summary><b>Choppy video</b></summary>

Lower the quality cap, use fewer video tiles, or raise Settings → Network buffer.

</details>

## Security

> [!CAUTION]
> The web UI controls what's on screen and accepts any URL. Set a password, keep it on your LAN, and don't port-forward it.

Web tiles have popups, permission prompts and context menus disabled.

## Development

Run from the `app/` folder:

```bash
python3 -m windopane --window 1600x900   # screen 1 in a desktop window (Esc quits)
python3 -m windopane --monitors 960x540+0+0,960x540+960+0   # fake two screens on one display
python3 -m windopane --headless          # web UI only
python3 tests/test_core.py
```

Needs `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-webkit2-4.1`, `python3-vlc`, `python3-flask` and `python3-evdev`.

<details>
<summary>Project layout</summary>

```
app/windopane/      the wall, web UI and API (Python, GTK, Flask)
  static/           web UI (plain HTML/CSS/JS, no build step)
system/             launcher, session scripts, windopane-ctl, GPU helper
extras/oled/        OLED ticker for Freenove cases
tests/              unit tests and a fake yt-dlp for offline testing
docs/images/        README images
install.sh          installer (Pi OS, Debian, Ubuntu)
uninstall.sh
```

</details>
