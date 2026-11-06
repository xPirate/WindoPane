

A boot-to-video-wall appliance for Raspberry Pi 4 and 5, or a PC (AMD, Intel or NVIDIA graphics). Up to 16 tiles per screen on up to four screens (two on a Pi), at 4K or 1080p, each playing a VLC stream (YouTube, Twitch, RTSP cameras, HLS, UDP/SRT, files) or a live web page (dump1090/tar1090 maps, radar, dashboards). Everything starts muted; a keyboard, USB/Bluetooth controller or the web UI picks which tile you hear.

 HDMI/DP 1-4 ─► bare X server ──► windopane (one GTK window per screen)
                              ├─ video tiles: libVLC on a Pi, mpv on PCs (+ yt-dlp for YouTube/Twitch/…)
                              ├─ web tiles:   WebKitGTK
                              ├─ ADS-B tiles: text aircraft table from dump1090
                              ├─ input:       evdev (keyboards, gamepads, media keys)
                              └─ web UI/API:  http://<pi>:8090
Install

Start from a fresh Raspberry Pi OS Lite (64-bit), Bookworm or newer, with SSH and network set up in Raspberry Pi Imager.
(Has been tested to still work on a full install, but you will lose your desktop environment while it is installed)

bash
scp windopane-0.6.0.tar.gz pi@<pi-address>:
ssh pi@<pi-address>
tar xzf windopane-0.6.0.tar.gz && cd windopane
sudo ./install.sh --password 'pick-one'
sudo reboot

After the reboot the screen shows the empty layout with the web UI address on it. Open that address, add streams, and assign them to slots.

Installer options:

Option	Default	
--user NAME	the sudo user	account the wall runs as
--port N	8090	web UI port (8080 is left free for dump1090/SkyAware)
--password X	none	HTTP login for the web UI, any username
--no-autostart	autostart on	don't boot into the wall
--no-4k60	on	skip hdmi_enable_4kp60=1 for Pi 4
--gpu X	auto	PCs: amd, intel, nvidia, none or skip (see below)

Re-running install.sh upgrades in place and keeps your streams (a 0.1 single-screen setup becomes screen 1).

Running on a PC (AMD first)

A mini PC or a desktop with a graphics card works too. Install Debian 12/13 or Ubuntu 24.04 Server (no desktop: the installer makes the box boot straight into the wall), then run the same installer. On a PC it also:

plays video with mpv (one process per tile), which decodes on the GPU with VA-API on AMD and Intel, or NVDEC on NVIDIA
detects the GPU and installs its video drivers (AMD, then Intel; NVIDIA only when you ask with --gpu nvidia, because its proprietary driver can need Secure Boot handling)
adds tear-free output for AMD cards
allows the web UI to switch drivers later (a sudo rule for the driver helper only)
bash
sudo ./install.sh --password 'pick-one'            # auto-detect the GPU
sudo ./install.sh --password 'pick-one' --gpu amd  # or choose: amd | intel | nvidia | none | skip
Switching drivers from the web UI

The Graphics panel (PCs only) shows the detected GPU, the installed driver set, and which codecs the GPU actually decodes. Pick AMD, Intel, NVIDIA or None, tick the confirmation box, and press Switch drivers. The log streams live; a Reboot now button appears when it's done.

The new vendor's drivers are installed first. Only if that succeeds are the other vendors' video packages purged, followed by apt autoremove. A failed switch leaves the previous drivers in place.
Only fixed package lists are touched. The shared Mesa/OpenGL libraries the screen needs are never removed, and firmware packages are kept.
On Debian, NVIDIA needs the contrib non-free non-free-firmware components enabled in your apt sources.
Because this changes system packages, set a web UI password.
Video player and GPU decoding

Settings → Video player picks the engine: Auto (VLC on a Pi, mpv on a PC), VLC or mpv. Each mpv runs as its own process drawing into its tile, so a misbehaving stream can't take the wall down; it just restarts that tile.

mpv (PC default) decodes on the GPU through VA-API on AMD and Intel and NVDEC on NVIDIA. The Graphics panel's VA-API line lists the codecs your card decodes (an RX 6600 XT should list H.264, HEVC, VP9 and AV1).
VLC on Debian/Ubuntu is built without VA-API (it doesn't work with FFmpeg 5+), so it can only use the older VDPAU interface, which Mesa removed upstream in September 2025. Ubuntu 24.04 and Debian 13 still ship it; newer releases won't. Choose VLC on a PC only if mpv can't play something.

Video is fetched up to 4K on PCs with one or two tiles, 1080p with three or four, and 720p with five to eight.

On a PC, set each screen's audio output in Settings (Auto uses the system default there). Screens are ordered HDMI ports first, then DisplayPort.

Adding streams

Paste a URL; Auto-detect picks the tile type. Use Test to check a URL before it goes on screen.

Source	Example URL	Tile
YouTube video or live stream	https://www.youtube.com/watch?v=…	video, via yt-dlp
YouTube playlist	https://www.youtube.com/playlist?list=…	video, plays the whole list
YouTube channel, whenever it's live	https://www.youtube.com/@channel/live	video, waits for live
Twitch / Kick channel	https://www.twitch.tv/channelname	video, waits for live
IP camera	rtsp://user:pass@192.168.1.50:554/stream1	video (RTSP over TCP)
HLS / DASH	https://…/index.m3u8	video
Multicast / SRT	udp://@239.0.0.1:1234, srt://host:port	video
tar1090 map	http://<adsb-pi>/tar1090/ (same Pi: http://localhost/tar1090/)	web
dump1090 SkyAware	http://<adsb-pi>:8080/	web
ADS-B text table	http://<adsb-pi>/tar1090/ with type ADS-B table, a .../aircraft.json URL, or tcp://<adsb-pi>:30003	ADS-B
Any dashboard	https://…	web

Force Video to run an unknown site through yt-dlp, or Web page to show a video site as a page. Per stream you can also cap quality (video), and set page zoom and an auto-reload interval (web).

Playlists and live channels

A video stream has a Behaviour. Auto decides from the link, or you can pick it:

Single video / stream: plays the link, and loops it if it ends.
Play the playlist: plays every video in turn and loops at the end. Tick Shuffle for a random order, reshuffled on each pass. Deleted, private or region-blocked videos are skipped. The tile shows its place ("3/42") and has a skip button; N / B (or keyboard media keys) skip forward and back on the tile you're listening to. The list is re-read every 30 minutes, so videos added to it show up on their own. Auto picks this for youtube.com/playlist?list=… links; a watch?v=…&list=… link plays only that video unless you choose this.
Wait for the channel to go live: for a channel's /live link, or a Twitch or Kick channel. While it's off air the tile shows a quiet "Not live right now" card (or "Scheduled: starts in 25 minutes") and checks again every 2 minutes (Check every changes that). When the channel goes live it starts playing by itself, muted like everything else, and goes back to waiting when the broadcast ends. Use Test in the form to check the link: it reports whether the channel is live now.
Live takeover

A channel set to wait for live doesn't need a tile of its own. In its form, pick When live, take over → a screen and tile, and a Priority from 1 (highest) to 9. WindoPane then checks the channel in the background on its Check every interval, and:

when the channel goes live, it replaces that tile's stream on the wall. The stream you assigned stays assigned, and the tile's menu in the web UI still shows it;
when the broadcast ends, the tile goes back to what you assigned (or back to empty);
if several takeover channels aim at the same tile, the highest priority shows; equal priorities go to whichever went live first. The others wait their turn and come on as soon as the tile is free, if they're still live.

Audio follows the tile, not the stream: if you were listening to that tile, you'll hear the live channel, then your own stream again afterwards. In the web UI the tile gets a red LIVE tag and a Send back button, which puts your stream back until that broadcast ends; the channel takes over again the next time it goes live. After a broadcast ends, the channel isn't checked again for one interval, because YouTube can still report a just-finished stream as live for a minute or so. The stream list shows each takeover channel's target and whether it's live now.

No YouTube account is needed for public or unlisted playlists and channels. Private playlists, Watch Later, Liked videos, members-only and age-restricted videos need a signed-in session, which WindoPane doesn't support; making a playlist Unlisted is the easy way around that. Each live check is one page load from your home connection, which is fine at the default 2 minutes even with several channels.

ADS-B tiles

A plain-text aircraft table, no map: flight, hex, squawk, altitude (with climb/descent arrow), vertical speed, ground speed, heading, distance and bearing from your location, RSSI and age. Emergency squawks (7500, 7600, 7700) and dump1090's emergency flag pin the aircraft to the top in red. The rest are sorted nearest first, and aircraft without a position go last. Columns drop away gracefully in small tiles, and rows that don't fit are counted ("+N more") instead of scrolling.

Source: any dump1090-family web address (dump1090-fa, readsb, tar1090, SkyAware, dump1090-mutability). A bare receiver address is fine; the usual aircraft.json paths are tried until one answers. Or the raw BaseStation feed, tcp://host:30003, which every dump1090 build can output (--net). Pick type ADS-B table, or let auto-detect spot an aircraft.json URL or port 30003.
Location: a Maidenhead locator (EM15, EM15fl, EM15fl42) or lat, lon in decimal degrees (35.47, -97.52, 35.47N 97.52W). Leave it empty to use the receiver's own location when it publishes one in receiver.json.
Per tile: distance units (nautical miles, kilometres, miles) and an optional max range.
The table refreshes every second. One BaseStation connection is shared by any tiles that use the same feed, and it closes after five minutes if nothing is showing it.
Multiple screens

Each connected monitor becomes a screen with its own layout and up to 16 tiles: two screens on a Pi (its two HDMI ports), up to four on a PC. Set a screen up from its tab in the web UI before anything is plugged in if you like; it starts as soon as a monitor appears and stops its streams when unplugged. Screens 3 and 4 only appear in the web UI once a monitor is connected or they have been set up.

Order: HDMI ports first, then DisplayPort, each by number. On a Pi, the port nearest the power socket is screen 1.
Arrangement: screens are always extended, never mirrored. Settings → Screen arrangement: Auto places them side by side when that fits the GPU's maximum desktop size and in a two-column grid when it doesn't (four 4K screens fit in a row on the usual 16384-pixel limit). Side by side and Grid force one or the other.
Audio: only one tile is audible across all screens. On a Pi, video audio plays out of the HDMI port of that tile's screen. On a PC, pick each screen's output in Settings. Web tiles use the system default output.
Per screen: resolution and audio output in Settings, and any screen but the first can be switched off.
Decoding budget is shared: see Performance below for how quality steps down as video tiles add up across screens.
Pi 4: 4K60 works on one port only; with two 4K screens both run at 30 Hz, so 1080p on the second screen is a good choice. Pi 5 drives two 4K60 screens.
Audio and controls

All tiles start muted after every boot. Selecting a tile unmutes it and mutes the rest; selecting it again mutes it. The audible tile gets an amber frame, and its name appears briefly on screen.

Tiles are numbered 1-16 on each screen. You pick a screen, then a tile on it: the focused screen is the one with the audible tile, or the last one picked.

Action	Keyboard	Controller
Pick screen 1-4 (audio follows)	F1-F4, or S for the next	right stick click: next screen
Listen to a tile on the focused screen	1-9; tiles 10-16: 1 then the second digit	A B X Y = tiles 1-4
Mute all	0 M	Start
Next / previous tile, across all screens	→ Tab / ←	RB / LB, d-pad left/right
Volume	↑ ↓ + -	d-pad up/down
Next / previous layout (focused screen)	L / K	Select
Zoom audible tile to full screen	Z Enter	Guide, left stick click
Reload audible tile	R	
Next / previous video on a playlist tile	N / B, keyboard media keys	

Number entry works like a TV remote: on screens with 10 or more tiles, 1 waits about a second for a second digit (1 2 is tile 12; 1 on its own is tile 1). Every other digit acts immediately. Devices are read directly (no focus needed) and hot-plug works. On a Pi 4 the TV remote may also work over HDMI-CEC if your TV passes its buttons through.

Layouts

Single, Side by side, Stacked, Picture in picture, Main + 2, Trio, 2 × 2, Main + 3 right, Main + 3 bottom, Main + 3 insets, 3 × 2, Main + 5, 3 × 3, Main + 7, Main + 12 and 4 × 4. Each screen picks its own in the web UI, or cycle the focused screen with L. Streams stay assigned to their slot numbers when you switch, so slot 1 is always the big tile.

Performance

Each tile fetches the lowest standard quality that still fills it, capped by board:

	GPU decode	1-2 video tiles	3-4	5-8	9 or more
Pi 4	H.264, HEVC	up to 1080p	720p	480p	360p
Pi 5	HEVC only (fast CPU for H.264)	up to 1080p	720p	480p	360p
PC with mpv	VA-API / NVDEC	up to 4K	1080p	720p	480p

Counts are across all screens, and a tile never fetches more than it can show. These caps apply to YouTube/Twitch-style sources; cameras send whatever their URL asks for, so use camera sub-streams for small tiles.

Sizing a big PC wall: most consumer GPUs drive four displays. Decoder throughput is the real tile limit: one 4K60 stream costs about as much as eight 1080p30 streams, and a current GPU typically handles at least one and often several 4K60 streams, so plan on roughly 10-25 live 1080p tiles and verify with your actual feeds. Each mpv tile uses about 150-250 MB of RAM, and each web or ADS-B tile about 150-300 MB.

yt-dlp is told to prefer H.264 over VP9/AV1. Override per stream (Max quality) or globally (Settings → Quality cap). Web tiles cost CPU and RAM too; a 2 GB board is fine for four tiles, more is better with several web tiles. Use the official PSU and a heatsink or fan: the web UI turns the board chip red when the Pi is throttling.

Streams that error, stall (no new video for 45 s) or end are restarted automatically with backoff; finished files and VODs loop.

OLED ticker (Freenove cases)

A small status display for the 0.96" OLED in Freenove's Raspberry Pi 5 cases (FNK0100, FNK0107), or any SSD1306 128×64 OLED on I2C:

♪ T3                18:42      which tile you're hearing (S2·T3 with several screens), clock
──────────────────────────
 Lofi Radio • lofi hip hop…    scrolling: stream name and title
──────────────────────────
LIVE      vol 70      48°C     state, volume, CPU temperature

The featured tile is the one you're listening to. When everything is muted it's tile 1 of the focused screen, marked MUTED. ADS-B tiles scroll the aircraft count, any emergency squawks and the nearest aircraft with distance and bearing; a failing stream scrolls its error.

bash
sudo ./extras/oled/install-oled.sh                         # defaults
sudo ./extras/oled/install-oled.sh --sleep 23:00-07:00     # display off overnight
sudo ./extras/oled/install-oled.sh --contrast 40           # dimmer
sudo ./extras/oled/install-oled.sh --rotate 180            # if it's upside down
sudo ./extras/oled/install-oled.sh --uninstall
Enables I2C if needed (the first time may ask for a reboot), and runs as its own service (journalctl -u windopane-oled -f), separate from the wall.
Orientation: detected from the case. The FNK0107 mounts the OLED upside down; the case's controller board reports which case it is.
Freenove's case software: if installed, only its OLED screen is switched off so the two don't fight over the display. Fan and LED control keep running. --uninstall gives the OLED back. If you later turn the OLED on in Freenove's app, it will clash again.
Burn-in: OLEDs burn in. The default contrast is reduced, the static lines move a pixel every few minutes, and --sleep turns the display off for set hours.
It reads the wall's port and password from the wall's config, so changing the web password needs no extra step.
Managing it
bash
windopane-ctl status        # running?, web address, yt-dlp version
windopane-ctl restart       # restart the wall (a few seconds)
windopane-ctl logs          # follow the log
sudo windopane-ctl stop     # back to the console on HDMI, autostart off
sudo windopane-ctl start    # autostart on again
sudo windopane-ctl update-ytdlp

yt-dlp (plus Deno, which YouTube now requires) updates daily at about 4:20 am; the wall restarts itself if the version changed. Config lives in /var/lib/windopane/config.json, logs in /var/lib/windopane/logs/. Advanced: player.extra_vlc_args in the config passes options straight to libVLC.

Uninstall: sudo ./uninstall.sh (add --purge to delete your streams too). Installed GPU drivers are left in place.

Troubleshooting
No picture on a Pi 5: check windopane-ctl logs. The installer adds /etc/X11/xorg.conf.d/99-windopane-vc4.conf so X drives the HDMI GPU; Raspberry Pi OS sometimes ships its own equivalent instead.
No sound: with Video audio out on Auto, each screen uses its own HDMI port when the Pi exposes two HDMI audio devices, otherwise PipeWire's default. wpctl status lists outputs and wpctl set-default <id> changes the default; Settings lets you pick a device per screen.
Screen 2 stays black: check the Screens line under Settings and windopane-ctl logs (look for outputs:). If you arrange displays yourself with xrandr, start WindoPane with --no-xrandr (edit /opt/windopane/bin/windopane-xinit).
YouTube or Twitch fails: run sudo windopane-ctl update-ytdlp; the Settings panel shows the yt-dlp version and whether Deno is found.
4K looks like 30 Hz on a Pi 4: it needs a reboot after install for hdmi_enable_4kp60, and a cable rated for 4K60.
Choppy video: lower the quality cap, use fewer video tiles, or raise Settings → Network buffer.
Security

The web UI controls what's on screen and accepts any URL, so set a password and keep it on your LAN; don't port-forward it. Web tiles have popups, permission prompts and context menus disabled.

Development
bash
python3 -m windopane --window 1600x900   # screen 1 in a desktop window (Esc quits)
python3 -m windopane --monitors 960x540+0+0,960x540+960+0   # fake two screens on one display
python3 -m windopane --headless          # web UI only
python3 tests/test_core.py

Run from the app/ folder. Needs python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, python3-vlc, python3-flask, python3-evdev.
