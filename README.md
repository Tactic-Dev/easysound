# EasySound 1.1.0

A simple, reliable sound tray icon for Linux desktops (GTK3, PipeWire or PulseAudio via `pactl`).
It replaces Cinnamon's sound applet, whose libcvc backend loses track of Bluetooth speakers and then
shows or selects the wrong output.

- **Tray:** left-click opens the panel, scroll changes volume, middle-click mutes. While music plays
  the icon becomes the album cover, and the tooltip shows the track.
- **Panel:** Now Playing (cover, title, artist, previous / play-pause / next; click the cover to
  bring the player forward), Launch player buttons, a volume slider capped at 100%, outputs (click to
  switch, and playing apps follow), microphone mute, level and device.
- **Right-click menu:** play/pause, next, outputs, Launch player ▸, mute, sound settings, quit.
- Reads the audio server directly (`pactl subscribe` + JSON), so it never shows a stale state.

Run: `python3 easysound.py` (`--hidden` at login, `--show` toggles the panel, `--quit`).
Needs: python3-gi, gir1.2-gtk-3.0, pulseaudio-utils (pactl ≥ 16 for JSON), optional gir1.2-xapp-1.0.
