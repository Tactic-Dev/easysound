#!/usr/bin/env python3
"""EasySound: a simple, reliable sound tray icon (GTK3 + PipeWire/PulseAudio via pactl).

Replaces Cinnamon's sound applet, whose libcvc backend loses track of Bluetooth cards and then shows
or selects the wrong output. EasySound reads the audio server directly (`pactl subscribe` + JSON
lists), so what you see is what's actually playing.

Tray: left-click opens the panel · scroll changes volume · middle-click mutes · right-click menu.
Panel: volume slider, outputs (click to switch, playing apps follow), microphone.
"""
import hashlib, json, os, re, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, GdkPixbuf, Pango
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ValueError, ImportError):
    XApp = None

VERSION = "1.0.0"
APP_ID = "dev.tacticdev.easysound"
AUTOSTART = os.path.join(GLib.get_user_config_dir(), "autostart", "easysound.desktop")
STEP = 5          # % per scroll notch / key
ART_CACHE = os.path.join(GLib.get_user_cache_dir(), "easysound", "art")
MPRIS = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
# Players offered under "Launch player" when installed (same idea as Cinnamon's sound applet).
KNOWN_PLAYERS = ["com.github.neithern.g4music", "vlc", "org.gnome.Rhythmbox3", "rhythmbox",
                 "io.github.celluloid_player.Celluloid", "audacious", "spotify", "com.spotify.Client",
                 "org.gnome.Lollypop", "org.strawberrymusicplayer.strawberry", "org.kde.elisa", "banshee"]
MAX_VOL = 100     # never boost past 100 % (that's what made the laptop speakers distort)

CSS = b"""
window.easysound { background-color: transparent; }
.es-card {
  background-color: #2c2c2c; color: #ffffff;
  border: 1px solid rgba(255,255,255,0.08); border-radius: 18px;
  padding: 14px 14px 10px;
}
.es-title { font-weight: bold; }
.es-dim { color: rgba(255,255,255,0.55); font-size: 9pt; }
.es-section { font-weight: bold; font-size: 9pt; color: rgba(255,255,255,0.6); margin-top: 6px; }
.es-row { padding: 7px 10px; border-radius: 10px; }
.es-row:hover { background-color: rgba(255,255,255,0.06); }
.es-row.active { background-color: rgba(75,144,237,0.18); }
.es-row.active .es-name { font-weight: bold; }
.es-iconbtn { padding: 4px; min-width: 0; min-height: 0; border-radius: 10px; }
.es-link { padding: 6px 10px; border-radius: 10px; }
.es-media { background-color: rgba(255,255,255,0.05); border-radius: 14px; padding: 10px; }
.es-player { font-size: 8.5pt; color: rgba(255,255,255,0.5); }
.es-ctrl { padding: 4px 8px; min-width: 0; min-height: 0; border-radius: 10px; }
.es-launch { padding: 4px 10px; border-radius: 10px; }
"""


def pactl(*args, json_out=False):
    cmd = ["pactl"] + (["-f", "json"] if json_out else []) + list(args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None if json_out else ""
    if not json_out:
        return out
    try:
        return json.loads(out)
    except ValueError:
        return None


def vol_percent(volume_dict):
    try:
        vals = [int(v["value_percent"].rstrip("%")) for v in volume_dict.values()]
        return round(sum(vals) / len(vals))
    except (KeyError, ValueError, ZeroDivisionError, AttributeError):
        return 0


def friendly_sink(s):
    name, desc = s.get("name", ""), s.get("description", "")
    props = s.get("properties", {}) or {}
    port = (s.get("active_port") or "").lower()
    if name.startswith("bluez_output"):
        label = props.get("device.description") or desc
        ff = props.get("device.form_factor", "")
        icon = "audio-headphones-symbolic" if ff in ("headphone", "headset", "hands-free") else "audio-speakers-symbolic"
        return label, icon, "Bluetooth"
    if "ThinkPad_USB-C_Dock" in name or "Dock" in desc:
        return "Dock headphone jack", "audio-headphones-symbolic", "Wired"
    if "hdmi" in name.lower() or "HDMI" in desc or "DisplayPort" in desc:
        return "Monitor (HDMI/DisplayPort)", "video-display-symbolic", "Wired"
    if name.startswith("alsa_output.pci"):
        if "headphone" in port:
            return "Laptop headphone jack", "audio-headphones-symbolic", "Built-in"
        return "Laptop speakers", "audio-speakers-symbolic", "Built-in"
    if name.startswith("alsa_output.usb"):
        return re.sub(r"\s+(Analog|Digital) Stereo.*$", "", desc), "audio-card-symbolic", "USB"
    return desc or name, "audio-card-symbolic", ""


def friendly_source(s):
    name, desc = s.get("name", ""), s.get("description", "")
    if name.startswith("bluez_input"):
        return (s.get("properties", {}) or {}).get("device.description") or desc
    if name.startswith("alsa_input.pci"):
        return "Laptop microphone"
    if "Dock" in desc:
        return "Dock microphone"
    return re.sub(r"\s+(Analog|Digital) (Stereo|Mono).*$|\s+Mono$", "", desc) or name


class Audio:
    """Snapshot of the audio server + change notifications from `pactl subscribe`."""
    def __init__(self, on_change):
        self.on_change = on_change
        self.sinks, self.sources, self.default_sink, self.default_source = [], [], "", ""
        self._pending = None
        self.reload()
        threading.Thread(target=self._subscribe, daemon=True).start()

    def reload(self):
        sinks = pactl("list", "sinks", json_out=True) or []
        sources = pactl("list", "sources", json_out=True) or []
        info = pactl("info", json_out=True) or {}
        self.sinks = [s for s in sinks if s.get("name")]
        self.sources = [s for s in sources if s.get("name") and not s["name"].endswith(".monitor")
                        and (s.get("properties", {}) or {}).get("device.class") != "monitor"]
        self.default_sink = info.get("default_sink_name", "")
        self.default_source = info.get("default_source_name", "")

    def _subscribe(self):
        while True:
            try:
                p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True)
                for line in p.stdout:
                    if any(k in line for k in (" sink ", " source ", " server ", " card ")) and "sink-input" not in line:
                        GLib.idle_add(self._schedule)
            except Exception:
                pass
            time.sleep(2)  # audio server restarted: resubscribe

    def _schedule(self):
        if self._pending: GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(120, self._fire)
        return False

    def _fire(self):
        self._pending = None
        self.reload(); self.on_change()
        return False

    # ---- queries ----
    def sink(self, name=None):
        name = name or self.default_sink
        return next((s for s in self.sinks if s["name"] == name), None)

    def source(self, name=None):
        name = name or self.default_source
        return next((s for s in self.sources if s["name"] == name), None)

    # ---- actions (fire and forget; the subscription refreshes the UI) ----
    def _bg(self, *cmds):
        def run():
            for c in cmds:
                subprocess.run(["pactl"] + list(c), capture_output=True, timeout=4)
        threading.Thread(target=run, daemon=True).start()

    def set_default_sink(self, name):
        """Switch output and move everything that's playing to it."""
        def run():
            subprocess.run(["pactl", "set-default-sink", name], capture_output=True, timeout=4)
            for inp in pactl("list", "sink-inputs", json_out=True) or []:
                subprocess.run(["pactl", "move-sink-input", str(inp["index"]), name], capture_output=True, timeout=4)
        threading.Thread(target=run, daemon=True).start()

    def set_default_source(self, name):
        def run():
            subprocess.run(["pactl", "set-default-source", name], capture_output=True, timeout=4)
            for out in pactl("list", "source-outputs", json_out=True) or []:
                subprocess.run(["pactl", "move-source-output", str(out["index"]), name], capture_output=True, timeout=4)
        threading.Thread(target=run, daemon=True).start()

    def set_volume(self, pct, kind="sink"):
        target = "@DEFAULT_SINK@" if kind == "sink" else "@DEFAULT_SOURCE@"
        pct = max(0, min(MAX_VOL, int(pct)))
        cmds = [(f"set-{kind}-volume", target, f"{pct}%")]
        if pct > 0:
            cmds.append((f"set-{kind}-mute", target, "0"))
        self._bg(*cmds)

    def toggle_mute(self, kind="sink"):
        self._bg((f"set-{kind}-mute", "@DEFAULT_SINK@" if kind == "sink" else "@DEFAULT_SOURCE@", "toggle"))


class Media:
    """Media players over MPRIS: what's playing, cover art, play/pause/next, and launchable players."""
    def __init__(self, on_change):
        self.on_change = on_change
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.players = {}   # bus name -> {"owner", "identity", "desktop", "status", "meta", "can", "changed"}
        self._pending = None
        os.makedirs(ART_CACHE, exist_ok=True)
        try:
            names = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                       "ListNames", None, None, 0, 2000, None).unpack()[0]
        except GLib.Error:
            names = []
        for n in names:
            if n.startswith(MPRIS): self._add(n)
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                  "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE, self._owner_changed)
        self.bus.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged", MPRIS_PATH, None,
                                  Gio.DBusSignalFlags.NONE, self._props_changed)

    def _get_all(self, name, iface):
        try:
            return self.bus.call_sync(name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                                      GLib.Variant("(s)", (iface,)), None, 0, 1500, None).unpack()[0]
        except GLib.Error:
            return {}

    def _add(self, name):
        try:
            owner = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                       "GetNameOwner", GLib.Variant("(s)", (name,)), None, 0, 1500, None).unpack()[0]
        except GLib.Error:
            return
        root = self._get_all(name, "org.mpris.MediaPlayer2")
        pl = self._get_all(name, "org.mpris.MediaPlayer2.Player")
        self.players[name] = {
            "owner": owner, "identity": root.get("Identity") or name[len(MPRIS):].split(".")[0].title(),
            "desktop": root.get("DesktopEntry", ""), "status": pl.get("PlaybackStatus", "Stopped"),
            "meta": pl.get("Metadata", {}) or {}, "changed": time.time(),
            "can": {k: pl.get(k, False) for k in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause")},
        }
        self._fetch_art(name)
        self._schedule()

    def _owner_changed(self, conn, sender, path, iface, signal, params):
        name, old, new = params.unpack()
        if not name.startswith(MPRIS): return
        if new: GLib.timeout_add(300, lambda: (self._add(name), False)[-1])   # give it a moment to export
        else:
            self.players.pop(name, None); self._schedule()

    def _props_changed(self, conn, sender, path, iface, signal, params):
        ifname, changed, _inv = params.unpack()
        if ifname != "org.mpris.MediaPlayer2.Player": return
        for name, p in self.players.items():
            if p["owner"] == sender:
                if "PlaybackStatus" in changed:
                    p["status"] = changed["PlaybackStatus"]; p["changed"] = time.time()
                if "Metadata" in changed:
                    p["meta"] = changed["Metadata"] or {}; self._fetch_art(name)
                for k in p["can"]:
                    if k in changed: p["can"][k] = changed[k]
                self._schedule()
                break

    def _schedule(self):
        if self._pending: GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(80, self._fire)

    def _fire(self):
        self._pending = None; self.on_change(); return False

    # ---- what to show ----
    def active(self):
        """Playing player (most recent) > paused one with a track > none."""
        ps = [(n, p) for n, p in self.players.items() if p["meta"].get("xesam:title") or p["status"] == "Playing"]
        if not ps: return None, None
        ps.sort(key=lambda np: (np[1]["status"] != "Playing", np[1]["status"] != "Paused", -np[1]["changed"]))
        return ps[0]

    def art_path(self, p):
        url = (p or {}).get("meta", {}).get("mpris:artUrl", "")
        if url.startswith("file://"):
            path = urllib.parse.unquote(url[7:])
            return path if os.path.exists(path) else None
        if url.startswith(("http://", "https://")):
            path = os.path.join(ART_CACHE, hashlib.sha1(url.encode()).hexdigest() + ".img")
            return path if os.path.exists(path) else None
        return None

    def _fetch_art(self, name):
        url = self.players.get(name, {}).get("meta", {}).get("mpris:artUrl", "")
        if not url.startswith(("http://", "https://")): return
        path = os.path.join(ART_CACHE, hashlib.sha1(url.encode()).hexdigest() + ".img")
        if os.path.exists(path): return
        def dl():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "EasySound"})
                with urllib.request.urlopen(req, timeout=8) as r, open(path + ".part", "wb") as f:
                    f.write(r.read(5_000_000))
                os.replace(path + ".part", path)
                GLib.idle_add(self._schedule)
            except Exception:
                pass
        threading.Thread(target=dl, daemon=True).start()

    # ---- control ----
    def call(self, name, method, iface="org.mpris.MediaPlayer2.Player"):
        self.bus.call(name, MPRIS_PATH, iface, method, None, None, 0, 2000, None, None)

    def launchable(self):
        """Installed players from KNOWN_PLAYERS plus any player that has been running, as DesktopAppInfo."""
        ids, apps = list(KNOWN_PLAYERS), []
        for p in self.players.values():
            if p["desktop"] and p["desktop"] not in ids: ids.append(p["desktop"])
        for i in ids:
            try:
                a = Gio.DesktopAppInfo.new(i + ".desktop")
            except TypeError:
                a = None
            if a and a.get_id() not in [x.get_id() for x in apps]:
                apps.append(a)
        return apps

    def running_for(self, app):
        base = app.get_id().replace(".desktop", "")
        for n, p in self.players.items():
            if p["desktop"] == base or base.lower() in n.lower():
                return n
        return None


def track_text(p):
    m = p["meta"]
    title = m.get("xesam:title") or "Unknown track"
    artist = m.get("xesam:artist") or []
    artist = ", ".join(artist) if isinstance(artist, list) else str(artist)
    return title, artist


def volume_icon(pct, muted):
    if muted or pct == 0: return "audio-volume-muted-symbolic"
    if pct < 34: return "audio-volume-low-symbolic"
    if pct < 67: return "audio-volume-medium-symbolic"
    return "audio-volume-high-symbolic"


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self._updating = False
        self._vol_timer = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        css = Gtk.CssProvider(); css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.audio = Audio(self.refresh)
        self.media = Media(lambda: self.refresh() if hasattr(self, "scale") else None)
        self.build()
        self.setup_tray()
        self.refresh()

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--version" in args:
            cl.print_literal(f"EasySound {VERSION}\n"); return 0
        if "--quit" in args:
            self.quit(); return 0
        if "--hidden" in args:
            return 0
        self.toggle()
        return 0

    # ---- tray ----
    def setup_tray(self):
        self.tray = None
        if XApp is None:
            return
        t = XApp.StatusIcon(); t.set_name("easysound")
        t.connect("activate", self.on_tray_click)
        t.connect("scroll-event", self.on_tray_scroll)
        self.tray_menu = Gtk.Menu(); t.set_secondary_menu(self.tray_menu)
        self.tray = t

    def on_tray_click(self, icon, button, time_):
        if button == 2:
            self.audio.toggle_mute()
        else:
            self.toggle()

    def on_tray_scroll(self, icon, amount, direction, time_):
        s = self.audio.sink()
        if not s: return
        cur = vol_percent(s.get("volume", {}))
        up = direction in (XApp.ScrollDirection.UP, XApp.ScrollDirection.RIGHT)
        self.audio.set_volume(cur + (STEP if up else -STEP))

    def update_tray(self):
        if not self.tray: return
        s = self.audio.sink()
        if not s:
            self.tray.set_icon_name("audio-volume-muted-symbolic")
            self.tray.set_tooltip_text("No sound output found")
        else:
            pct, muted = vol_percent(s.get("volume", {})), s.get("mute", False)
            name = friendly_sink(s)[0]
            mname, mp = self.media.active()
            art = self.media.art_path(mp) if mp and mp["status"] == "Playing" else None
            # While music plays, the tray shows the album cover (like Cinnamon's "show album art").
            self.tray.set_icon_name(art or volume_icon(pct, muted))
            now = ""
            if mp:
                t, a = track_text(mp)
                now = ("♪ " if mp["status"] == "Playing" else "⏸ ") + t + (f" · {a}" if a else "") + "\n"
            self.tray.set_tooltip_text(now + f"{name} · " + ("muted" if muted else f"{pct}%") +
                                       "\nScroll for volume · middle-click to mute")
        m = self.tray_menu
        for c in m.get_children(): c.destroy()
        def item(label, cb, sensitive=True):
            it = Gtk.MenuItem(label=label); it.set_sensitive(sensitive)
            it.connect("activate", lambda *_: cb()); m.append(it)
        mname, mp = self.media.active()
        if mp:
            t, a = track_text(mp)
            head = Gtk.MenuItem(label=(t + (f" · {a}" if a else ""))[:60]); head.set_sensitive(False); m.append(head)
            item("Pause" if mp["status"] == "Playing" else "Play", lambda: self._media_call("PlayPause"))
            item("Next track", lambda: self._media_call("Next"), mp["can"]["CanGoNext"])
            m.append(Gtk.SeparatorMenuItem())
        for sk in self.sorted_sinks():
            label, _icon, _kind = friendly_sink(sk)
            item(("✓ " if sk["name"] == self.audio.default_sink else "    ") + label,
                 lambda n=sk["name"]: self.audio.set_default_sink(n))
        m.append(Gtk.SeparatorMenuItem())
        apps = self.media.launchable()
        if apps:
            sub_item = Gtk.MenuItem(label="Launch player"); sub = Gtk.Menu()
            for a in apps:
                it = Gtk.MenuItem(label=a.get_name()); it.connect("activate", lambda _i, a=a: self.launch_player(a))
                sub.append(it)
            sub_item.set_submenu(sub); m.append(sub_item)
        muted = bool(s and s.get("mute"))
        item("Unmute" if muted else "Mute", self.audio.toggle_mute, bool(s))
        item("Sound settings…", self.open_settings)
        m.append(Gtk.SeparatorMenuItem())
        item("Quit EasySound", self.quit)
        m.show_all()

    def sorted_sinks(self):
        order = {"Bluetooth": 0, "Built-in": 1, "USB": 2, "Wired": 3}
        return sorted(self.audio.sinks, key=lambda s: (order.get(friendly_sink(s)[2], 4), friendly_sink(s)[0].lower()))

    # ---- panel ----
    def build(self):
        w = Gtk.Window(application=self, title="Sound", decorated=False, resizable=False)
        w.get_style_context().add_class("easysound")
        vis = w.get_screen().get_rgba_visual()
        if vis: w.set_visual(vis)
        w.set_app_paintable(True)
        w.set_keep_above(True); w.set_skip_taskbar_hint(True); w.set_skip_pager_hint(True)
        w.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        w.connect("focus-out-event", lambda *_: (GLib.timeout_add(120, self._maybe_hide), False)[-1])
        w.connect("key-press-event", lambda _w, ev: (w.hide(), True)[-1] if ev.keyval == Gdk.KEY_Escape else False)
        w.connect("delete-event", lambda *_: (w.hide(), True)[-1])
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        card.get_style_context().add_class("es-card"); card.set_size_request(340, -1)

        # now playing
        self.media_box = Gtk.Box(spacing=12); self.media_box.get_style_context().add_class("es-media")
        self.art_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Show player")
        self.art_btn.get_style_context().add_class("es-iconbtn")
        self.art = Gtk.Image(); self.art_btn.add(self.art)
        self.art_btn.connect("clicked", lambda *_: self._media_call("Raise", "org.mpris.MediaPlayer2"))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.m_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=24)
        self.m_title.get_style_context().add_class("es-title")
        self.m_artist = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=26)
        self.m_artist.get_style_context().add_class("es-dim")
        self.m_player = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.m_player.get_style_context().add_class("es-player")
        ctrls = Gtk.Box(spacing=4)
        self.c_prev, self.c_play, self.c_next = [Gtk.Button(relief=Gtk.ReliefStyle.NONE) for _ in range(3)]
        for b, icon, method in ((self.c_prev, "media-skip-backward-symbolic", "Previous"),
                                (self.c_play, "media-playback-start-symbolic", "PlayPause"),
                                (self.c_next, "media-skip-forward-symbolic", "Next")):
            b.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)); b.get_style_context().add_class("es-ctrl")
            b.connect("clicked", lambda _b, m=method: self._media_call(m))
            ctrls.pack_start(b, False, False, 0)
        for wdg in (self.m_title, self.m_artist, self.m_player, ctrls): info.pack_start(wdg, False, False, 0)
        self.media_box.pack_start(self.art_btn, False, False, 0); self.media_box.pack_start(info, True, True, 0)
        self.media_box.set_no_show_all(True)
        card.pack_start(self.media_box, False, False, 0)
        self.launch_box = Gtk.Box(spacing=6)
        self.launch_box.set_no_show_all(True)
        card.pack_start(self.launch_box, False, False, 0)

        # volume
        top = Gtk.Box(spacing=10)
        self.mute_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Mute / unmute")
        self.mute_btn.get_style_context().add_class("es-iconbtn")
        self.mute_img = Gtk.Image(pixel_size=20); self.mute_btn.add(self.mute_img)
        self.mute_btn.connect("clicked", lambda *_: self.audio.toggle_mute())
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, MAX_VOL, 1)
        self.scale.set_draw_value(False); self.scale.set_hexpand(True)
        self.scale.connect("value-changed", self.on_scale)
        self.pct = Gtk.Label(width_chars=4, xalign=1); self.pct.get_style_context().add_class("es-dim")
        top.pack_start(self.mute_btn, False, False, 0); top.pack_start(self.scale, True, True, 0)
        top.pack_start(self.pct, False, False, 0)
        self.now = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END); self.now.get_style_context().add_class("es-title")
        card.pack_start(self.now, False, False, 0)
        card.pack_start(top, False, False, 0)

        # outputs
        h = Gtk.Label(label="Output", xalign=0); h.get_style_context().add_class("es-section")
        card.pack_start(h, False, False, 0)
        self.outputs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.pack_start(self.outputs, False, False, 0)

        # microphone
        h2 = Gtk.Label(label="Microphone", xalign=0); h2.get_style_context().add_class("es-section")
        card.pack_start(h2, False, False, 0)
        mic = Gtk.Box(spacing=10)
        self.mic_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Mute / unmute microphone")
        self.mic_btn.get_style_context().add_class("es-iconbtn")
        self.mic_img = Gtk.Image(pixel_size=18); self.mic_btn.add(self.mic_img)
        self.mic_btn.connect("clicked", lambda *_: self.audio.toggle_mute("source"))
        self.mic_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, MAX_VOL, 1)
        self.mic_scale.set_draw_value(False); self.mic_scale.set_hexpand(True)
        self.mic_scale.connect("value-changed", self.on_mic_scale)
        mic.pack_start(self.mic_btn, False, False, 0); mic.pack_start(self.mic_scale, True, True, 0)
        card.pack_start(mic, False, False, 0)
        self.mic_combo = Gtk.ComboBoxText()
        self.mic_combo.connect("changed", self.on_mic_choice)
        card.pack_start(self.mic_combo, False, False, 0)

        foot = Gtk.Button(label="Sound settings…", relief=Gtk.ReliefStyle.NONE, halign=Gtk.Align.START)
        foot.get_style_context().add_class("es-link")
        foot.connect("clicked", lambda *_: (self.win.hide(), self.open_settings()))
        card.pack_start(foot, False, False, 0)
        w.add(card)
        self.win = w

    def _maybe_hide(self):
        if self.win.get_visible() and not self.win.is_active():
            # the mic dropdown's popup steals focus briefly; don't close for that
            if not self.mic_combo.get_property("popup-shown"):
                self.win.hide()
        return False

    def toggle(self):
        if self.win.get_visible():
            self.win.hide(); return
        self.audio.reload(); self.refresh()
        self.win.show_all()
        self.place_near_pointer()
        self.win.present_with_time(Gtk.get_current_event_time() or int(time.time()))

    def place_near_pointer(self):
        """Open just above/left of the pointer (i.e. above the tray icon), kept on-screen."""
        disp = Gdk.Display.get_default()
        seat = disp.get_default_seat()
        _s, x, y = seat.get_pointer().get_position()
        mon = disp.get_monitor_at_point(x, y).get_workarea()
        ww, wh = self.win.get_size()
        nx = min(max(mon.x + 8, x - ww // 2), mon.x + mon.width - ww - 8)
        ny = y - wh - 12 if y - wh - 12 > mon.y else y + 12
        ny = min(max(mon.y + 8, ny), mon.y + mon.height - wh - 8)
        self.win.move(nx, ny)

    def refresh(self):
        self._updating = True
        try:
            s = self.audio.sink()
            if s:
                pct, muted = vol_percent(s.get("volume", {})), s.get("mute", False)
                self.now.set_text(friendly_sink(s)[0])
                if not self.scale.has_grab():
                    self.scale.set_value(pct)
                self.pct.set_text("Muted" if muted else f"{pct}%")
                self.mute_img.set_from_icon_name(volume_icon(pct, muted), Gtk.IconSize.BUTTON)
                self.scale.set_sensitive(True)
            else:
                self.now.set_text("No sound output found")
                self.scale.set_sensitive(False); self.pct.set_text("")
            for c in self.outputs.get_children(): c.destroy()
            for sk in self.sorted_sinks():
                self.outputs.pack_start(self.output_row(sk), False, False, 0)
            self.outputs.show_all()
            src = self.audio.source()
            if src:
                mp, mm = vol_percent(src.get("volume", {})), src.get("mute", False)
                if not self.mic_scale.has_grab():
                    self.mic_scale.set_value(mp)
                self.mic_img.set_from_icon_name("microphone-sensitivity-muted-symbolic" if mm else
                                                "audio-input-microphone-symbolic", Gtk.IconSize.BUTTON)
            self.mic_scale.set_sensitive(bool(src))
            self.mic_combo.remove_all()
            for i, so in enumerate(self.audio.sources):
                self.mic_combo.append(so["name"], friendly_source(so))
            if self.audio.default_source:
                self.mic_combo.set_active_id(self.audio.default_source)
            self.mic_combo.set_visible(len(self.audio.sources) > 1)
            self.refresh_media()
        finally:
            self._updating = False
        self.update_tray()

    def _media_call(self, method, iface="org.mpris.MediaPlayer2.Player"):
        name, _p = self.media.active()
        if name: self.media.call(name, method, iface)

    def launch_player(self, app):
        running = self.media.running_for(app)
        if running:
            self.media.call(running, "Raise", "org.mpris.MediaPlayer2")
        else:
            try: app.launch([], None)
            except GLib.Error as e: print("launch failed:", e.message, file=sys.stderr)
        self.win.hide()

    def refresh_media(self):
        name, p = self.media.active()
        if p:
            title, artist = track_text(p)
            self.m_title.set_text(title); self.m_artist.set_text(artist or p["meta"].get("xesam:album", ""))
            self.m_artist.set_visible(bool(self.m_artist.get_text()))
            self.m_player.set_text(p["identity"] + ("" if p["status"] == "Playing" else f" · {p['status'].lower()}"))
            playing = p["status"] == "Playing"
            self.c_play.get_child().set_from_icon_name("media-playback-pause-symbolic" if playing else
                                                       "media-playback-start-symbolic", Gtk.IconSize.BUTTON)
            self.c_play.set_tooltip_text("Pause" if playing else "Play")
            self.c_prev.set_sensitive(p["can"]["CanGoPrevious"]); self.c_next.set_sensitive(p["can"]["CanGoNext"])
            art = self.media.art_path(p)
            pb = None
            if art:
                try: pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(art, 64, 64, True)
                except GLib.Error: pb = None
            if pb: self.art.set_from_pixbuf(pb)
            else: self.art.set_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.DIALOG)
            for c in self.media_box.get_children(): c.show_all()
            self.media_box.show()  # show_all() skips no_show_all widgets
        else:
            self.media_box.hide()
        # launch row: players that aren't the one shown above
        for c in self.launch_box.get_children(): c.destroy()
        apps = [a for a in self.media.launchable() if not (name and self.media.running_for(a) == name)]
        if apps:
            lbl = Gtk.Label(label="Launch:" if p else "Launch player:", xalign=0); lbl.get_style_context().add_class("es-dim")
            self.launch_box.pack_start(lbl, False, False, 0)
            for a in apps[:3]:
                b = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text=a.get_name())
                b.get_style_context().add_class("es-launch")
                row = Gtk.Box(spacing=6)
                img = Gtk.Image.new_from_gicon(a.get_icon(), Gtk.IconSize.BUTTON) if a.get_icon() else \
                      Gtk.Image.new_from_icon_name("applications-multimedia-symbolic", Gtk.IconSize.BUTTON)
                img.set_pixel_size(18)
                row.pack_start(img, False, False, 0)
                row.pack_start(Gtk.Label(label=a.get_name(), ellipsize=Pango.EllipsizeMode.END, max_width_chars=14), False, False, 0)
                b.add(row); b.connect("clicked", lambda _b, a=a: self.launch_player(a))
                self.launch_box.pack_start(b, False, False, 0)
            for c in self.launch_box.get_children(): c.show_all()
            self.launch_box.show()  # show_all() skips no_show_all widgets
        else:
            self.launch_box.hide()

    def output_row(self, sk):
        label, icon, kind = friendly_sink(sk)
        active = sk["name"] == self.audio.default_sink
        btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        btn.get_style_context().add_class("es-row")
        if active: btn.get_style_context().add_class("active")
        row = Gtk.Box(spacing=10)
        row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
        name = Gtk.Label(label=label, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        name.get_style_context().add_class("es-name")
        row.pack_start(name, True, True, 0)
        sub = Gtk.Label(label=kind, xalign=1); sub.get_style_context().add_class("es-dim")
        row.pack_start(sub, False, False, 0)
        if active:
            row.pack_start(Gtk.Image.new_from_icon_name("object-select-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        btn.add(row)
        btn.connect("clicked", lambda *_: self.audio.set_default_sink(sk["name"]))
        return btn

    def on_scale(self, sc):
        if self._updating: return
        self.pct.set_text(f"{int(sc.get_value())}%")
        # coalesce drags into ~20 updates/s
        if self._vol_timer: return
        def apply():
            self._vol_timer = None
            self.audio.set_volume(self.scale.get_value()); return False
        self._vol_timer = GLib.timeout_add(50, apply)

    def on_mic_scale(self, sc):
        if self._updating: return
        self.audio.set_volume(sc.get_value(), kind="source")

    def on_mic_choice(self, combo):
        if self._updating: return
        name = combo.get_active_id()
        if name and name != self.audio.default_source:
            self.audio.set_default_source(name)

    def open_settings(self):
        exe = shutil.which("cinnamon-settings")
        subprocess.Popen([exe, "sound"] if exe else ["pavucontrol"], start_new_session=True)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
