"""
Valheim Mod Installer
=====================
A small, dependency-free (Python standard library only) GUI that:

  1. finds (or lets you pick) your Valheim install folder,
  2. installs / updates BepInExPack_Valheim (the mod loader every Valheim mod needs),
  3. shows each mod from catalog.json with its installed version, latest version and the
     Valheim version it was last tested on,
  4. installs / updates / removes the mods you tick.

Run it with pythonw (no console window), or use Start-Installer.bat / .ps1 / .sh.
"""

import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Valheim Mod Installer"
APP_VERSION = "1.0.0"
USER_AGENT = "ValheimModInstaller/" + APP_VERSION
VALHEIM_APPID = "892970"
HERE = Path(__file__).resolve().parent

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

DEFAULT_CATALOG = {
    "bepinex": {"thunderstore_namespace": "denikson", "thunderstore_name": "BepInExPack_Valheim"},
    "plugins": [
        {"repo": "Glutoblop/HealthCheckup", "branch": "main"},
        {"repo": "Glutoblop/HotkeySliding", "branch": "main"},
        {"repo": "Glutoblop/RecipeSearch", "branch": "main"},
    ],
}

# Marker files the installer writes so it can tell what it installed and at which version.
INSTALLED_MANIFEST = "plugin.json"                      # copied into each plugin's install_dir
BEPINEX_MARKER = "BepInEx/.bepinexpack_version"         # written after installing BepInExPack


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------

def settings_path():
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ValheimModInstaller" / "settings.json"


def load_settings():
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data):
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_version(v):
    """'1.2.10' -> (1, 2, 10). Non-numeric parts are ignored."""
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url):
    return json.loads(http_get(url).decode("utf-8"))


def raw_url(repo, branch, path):
    return "https://raw.githubusercontent.com/{}/{}/{}".format(repo, branch, path.lstrip("/"))


def no_window_flags():
    return 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW


def is_valheim_running():
    try:
        if IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq valheim.exe", "/NH"],
                                 capture_output=True, text=True, creationflags=no_window_flags()).stdout
            return "valheim.exe" in out.lower()
        out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True).stdout
        return any(line.strip().lower().startswith("valheim") for line in out.splitlines())
    except Exception:
        return False


def open_folder(path):
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # noqa
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


# --------------------------------------------------------------------------------------------
# Steam / Valheim discovery
# --------------------------------------------------------------------------------------------

def steam_roots():
    roots = []
    if IS_WINDOWS:
        try:
            import winreg
            for hive, key, val in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        roots.append(Path(winreg.QueryValueEx(k, val)[0]))
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                roots.append(Path(os.environ[env]) / "Steam")
    elif IS_MAC:
        roots.append(Path.home() / "Library" / "Application Support" / "Steam")
    else:
        roots += [
            Path.home() / ".steam" / "steam",
            Path.home() / ".local" / "share" / "Steam",
            Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ]
    seen, out = set(), []
    for r in roots:
        try:
            key = str(r.resolve()).lower()
        except Exception:
            key = str(r).lower()
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def steam_libraries():
    libs = []
    for root in steam_roots():
        libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                libs.append(Path(m.group(1).replace("\\\\", "\\")))
        except Exception:
            pass
    return libs


def is_valheim_dir(path):
    if not path:
        return False
    p = Path(path)
    return any((p / n).exists() for n in ("valheim.exe", "valheim.x86_64", "valheim.app", "valheim_Data"))


def find_valheim():
    for lib in steam_libraries():
        cand = lib / "steamapps" / "common" / "Valheim"
        if is_valheim_dir(cand):
            return cand
    return None


def game_info(game_dir):
    """Return what we can learn about the installed game: version, network version, steam build, flavour."""
    info = {"version": None, "network_version": None, "steam_build": None, "flavour": None}
    g = Path(game_dir)
    if (g / "valheim.exe").exists():
        info["flavour"] = "windows"
    elif (g / "valheim.x86_64").exists():
        info["flavour"] = "linux"
    elif (g / "valheim.app").exists():
        info["flavour"] = "mac"

    # Steam build id: <library>/steamapps/appmanifest_892970.acf (game dir is <library>/steamapps/common/Valheim)
    acf = g.parent.parent / ("appmanifest_%s.acf" % VALHEIM_APPID)
    try:
        m = re.search(r'"buildid"\s+"(\d+)"', acf.read_text(encoding="utf-8", errors="ignore"))
        if m:
            info["steam_build"] = int(m.group(1))
    except Exception:
        pass

    # Game version: printed by Valheim into BepInEx's log on every launch with BepInEx.
    log = g / "BepInEx" / "LogOutput.log"
    try:
        text = log.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"Valheim version:\s*([\d.]+)\s*\(network version (\d+)\)", text, re.I) \
            or re.search(r"Valheim Version:\s*([\d.]+)", text, re.I)
        if m:
            info["version"] = m.group(1).rstrip(".")
            if m.lastindex and m.lastindex >= 2:
                info["network_version"] = int(m.group(2))
    except Exception:
        pass
    return info


def bepinex_installed_version(game_dir):
    g = Path(game_dir)
    if not (g / "BepInEx" / "core" / "BepInEx.dll").exists():
        return None
    try:
        return (g / BEPINEX_MARKER).read_text(encoding="utf-8").strip() or "unknown"
    except Exception:
        pass
    try:
        text = (g / "BepInEx" / "LogOutput.log").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"BepInExPack Valheim version ([\d.]+)", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"


# --------------------------------------------------------------------------------------------
# Install logic (runs on a worker thread)
# --------------------------------------------------------------------------------------------

def fetch_bepinex_latest(catalog):
    bx = catalog.get("bepinex") or DEFAULT_CATALOG["bepinex"]
    url = "https://thunderstore.io/api/experimental/package/{}/{}/".format(
        bx["thunderstore_namespace"], bx["thunderstore_name"])
    data = http_json(url)
    latest = data["latest"]
    return {"version": latest["version_number"], "download_url": latest["download_url"]}


def install_bepinex(game_dir, latest, log):
    g = Path(game_dir)
    log("Downloading BepInExPack_Valheim {} ...".format(latest["version"]))
    blob = http_get(latest["download_url"], timeout=120)
    tmp = Path(tempfile.mkdtemp(prefix="vmi_bepinex_"))
    try:
        zpath = tmp / "pack.zip"
        zpath.write_bytes(blob)
        prefix = "BepInExPack_Valheim/"
        count = 0
        with zipfile.ZipFile(zpath) as z:
            for member in z.infolist():
                name = member.filename.replace("\\", "/")
                if not name.startswith(prefix) or name.endswith("/"):
                    continue
                rel = name[len(prefix):]
                if not rel or ".." in Path(rel).parts:
                    continue
                dest = g / rel
                # Never clobber the user's existing BepInEx settings.
                if rel.startswith("BepInEx/config/") and dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                if rel.endswith(".sh"):
                    try:
                        dest.chmod(0o755)
                    except Exception:
                        pass
                count += 1
        (g / BEPINEX_MARKER).parent.mkdir(parents=True, exist_ok=True)
        (g / BEPINEX_MARKER).write_text(latest["version"], encoding="utf-8")
        (g / "BepInEx" / "plugins").mkdir(parents=True, exist_ok=True)
        log("BepInExPack_Valheim {} installed ({} files).".format(latest["version"], count))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_plugin(game_dir, manifest, log):
    g = Path(game_dir)
    dest_dir = g / manifest["install_dir"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in manifest["files"]:
        url = raw_url(manifest["repo"], manifest.get("branch", "main"), f)
        log("  downloading {}".format(f))
        data = http_get(url, timeout=120)
        if f.lower().endswith(".dll") and not data.startswith(b"MZ"):
            raise RuntimeError("{} did not download as a valid DLL".format(f))
        target = dest_dir / Path(f).name
        tmp = target.with_suffix(target.suffix + ".download")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(target))
    (dest_dir / INSTALLED_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def uninstall_plugin(game_dir, manifest):
    d = Path(game_dir) / manifest["install_dir"]
    if d.exists() and d.resolve() != (Path(game_dir) / "BepInEx" / "plugins").resolve():
        shutil.rmtree(d)


def dll_plugin_version(dll_path, guid):
    """Read the version from a BepInEx plugin DLL's [BepInPlugin(guid, name, version)] attribute.

    Custom attribute blobs are: 0x01 0x00 prolog, then length-prefixed UTF-8 strings. We find the
    guid string and read the two strings that follow it (name, version)."""
    try:
        data = Path(dll_path).read_bytes()
        g = guid.encode("utf-8")
        needle = b"\x01\x00" + bytes([len(g)]) + g
        i = data.find(needle)
        if i < 0:
            return None
        pos = i + len(needle)
        strings = []
        for _ in range(2):
            n = data[pos]
            pos += 1
            strings.append(data[pos:pos + n].decode("utf-8"))
            pos += n
        return strings[1] if re.match(r"^\d+(\.\d+)*$", strings[1]) else None
    except Exception:
        return None


def installed_manifest(game_dir, manifest):
    d = Path(game_dir) / manifest["install_dir"]
    dlls = [d / Path(f).name for f in manifest.get("files", []) if f.lower().endswith(".dll")]
    dll_version = None
    if dlls and manifest.get("guid") and dlls[0].exists():
        dll_version = dll_plugin_version(dlls[0], manifest["guid"])
    try:
        m = json.loads((d / INSTALLED_MANIFEST).read_text(encoding="utf-8"))
        if dll_version:
            m["version"] = dll_version  # the DLL is the truth if someone replaced it by hand
        return m
    except Exception:
        pass
    if any((d / Path(f).name).exists() for f in manifest.get("files", [])):
        return {"version": dll_version or "unknown"}
    return None


# --------------------------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------------------------

COL_OK = "#2e7d32"
COL_WARN = "#b26a00"
COL_BAD = "#c62828"
COL_INFO = "#1565c0"
COL_MUTED = "#666666"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("{} {}".format(APP_NAME, APP_VERSION))
        self._scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.geometry("{}x{}".format(self._px(940), self._px(720)))
        self.minsize(self._px(780), self._px(580))

        self.settings = load_settings()
        self.catalog = self._load_catalog()
        self.q = queue.Queue()
        self.busy = False

        self.game_dir = tk.StringVar(value=self.settings.get("game_dir", ""))
        self.bepinex_var = tk.BooleanVar(value=False)
        self.bepinex_latest = None
        self.game = {}
        self.plugins = []  # list of dicts: manifest, installed, var, widgets

        self._build_ui()
        self.after(100, self._pump)

        if not is_valheim_dir(self.game_dir.get()):
            found = find_valheim()
            if found:
                self.game_dir.set(str(found))
        self.refresh()

    # ---------- setup ----------

    def _load_catalog(self):
        try:
            return json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
        except Exception:
            return DEFAULT_CATALOG

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            if not IS_WINDOWS and "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Step.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, foreground=COL_MUTED,
                  text="1) Tell me where Valheim is.  2) Tick the mods you want.  3) Click Install / Update.").pack(anchor="w", pady=(0, 8))

        # Step 1: game folder
        s1 = ttk.LabelFrame(outer, text=" Step 1 - Valheim folder ", style="Step.TLabelframe", padding=8)
        s1.pack(fill="x")
        row = ttk.Frame(s1)
        row.pack(fill="x")
        ent = ttk.Entry(row, textvariable=self.game_dir)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.refresh())
        ent.bind("<FocusOut>", lambda e: self._on_dir_changed())
        ttk.Button(row, text="Browse...", command=self.browse).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Auto-detect", command=self.autodetect).pack(side="left", padx=(6, 0))
        self.game_status = tk.Label(s1, text="", anchor="w", justify="left")
        self.game_status.pack(fill="x", pady=(6, 0))

        # Step 2: prerequisites
        s2 = ttk.LabelFrame(outer, text=" Step 2 - Prerequisites ", style="Step.TLabelframe", padding=8)
        s2.pack(fill="x", pady=(8, 0))
        r2 = ttk.Frame(s2)
        r2.pack(fill="x")
        ttk.Checkbutton(r2, text="BepInExPack_Valheim (mod loader, required by every mod)",
                        variable=self.bepinex_var).pack(side="left")
        self.bepinex_status = tk.Label(r2, text="", anchor="e")
        self.bepinex_status.pack(side="right")

        # Step 3: mods
        s3 = ttk.LabelFrame(outer, text=" Step 3 - Mods ", style="Step.TLabelframe", padding=8)
        s3.pack(fill="both", expand=True, pady=(8, 0))
        tools = ttk.Frame(s3)
        tools.pack(fill="x", pady=(0, 4))
        ttk.Button(tools, text="Select all", command=lambda: self._select(lambda p: True)).pack(side="left")
        ttk.Button(tools, text="Select updates", command=lambda: self._select(lambda p: p["state"] in ("update", "missing_files"))).pack(side="left", padx=4)
        ttk.Button(tools, text="Select none", command=lambda: self._select(lambda p: False)).pack(side="left")
        ttk.Button(tools, text="Refresh", command=self.refresh).pack(side="right")

        holder = ttk.Frame(s3)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = ttk.Frame(self.canvas)
        self._list_win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # Actions
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 0))
        self.install_btn = ttk.Button(actions, text="Install / Update selected", style="Accent.TButton", command=self.install)
        self.install_btn.pack(side="left")
        self.uninstall_btn = ttk.Button(actions, text="Uninstall selected", command=self.uninstall)
        self.uninstall_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="Open plugins folder", command=self.open_plugins).pack(side="left")
        ttk.Button(actions, text="Launch Valheim", command=self.launch).pack(side="right")
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=140)
        self.progress.pack(side="right", padx=8)

        # Log
        logf = ttk.Frame(outer)
        logf.pack(fill="x", pady=(8, 0))
        self.log_text = tk.Text(logf, height=7, wrap="word", state="disabled", font=("Consolas", 9),
                                background="#f6f6f6", relief="flat")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="x", expand=True)

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self._list_win, width=event.width)
        self.after_idle(self._rewrap)

    def _on_wheel(self, event):
        try:
            if str(event.widget).startswith(str(self.log_text)):
                return
        except Exception:
            pass
        self.canvas.yview_scroll(int(-event.delta / 120) if IS_WINDOWS else int(-event.delta), "units")

    # ---------- thread plumbing ----------

    def log(self, msg):
        self.q.put(("log", msg))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + payload + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def ui(self, fn):
        self.q.put(("call", fn))

    def run_bg(self, work, done=None):
        if self.busy:
            return
        self._set_busy(True)

        def runner():
            err = None
            try:
                work()
            except Exception as e:  # noqa
                err = e
                self.log("ERROR: {}".format(e))
                self.log(traceback.format_exc().strip().splitlines()[-1])
            finally:
                def fin():
                    self._set_busy(False)
                    if done:
                        done(err)
                self.ui(fin)

        threading.Thread(target=runner, daemon=True).start()

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.install_btn.configure(state=state)
        self.uninstall_btn.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(value=0)

    # ---------- step 1 ----------

    def browse(self):
        d = filedialog.askdirectory(title="Select your Valheim folder (the one containing valheim.exe)",
                                    initialdir=self.game_dir.get() or str(Path.home()))
        if d:
            self.game_dir.set(d)
            self.refresh()

    def autodetect(self):
        found = find_valheim()
        if found:
            self.game_dir.set(str(found))
            self.log("Found Valheim at {}".format(found))
            self.refresh()
        else:
            messagebox.showinfo(APP_NAME, "Couldn't find Valheim automatically.\n\n"
                                "Click Browse and select the folder that contains valheim.exe.\n"
                                "In Steam: right-click Valheim > Manage > Browse local files.")

    def _on_dir_changed(self):
        if self.game_dir.get() != self.settings.get("game_dir"):
            self.refresh()

    def _update_game_status(self):
        gd = self.game_dir.get().strip()
        if not gd:
            self.game_status.configure(text="Pick your Valheim folder.", fg=COL_WARN)
            return False
        if not is_valheim_dir(gd):
            self.game_status.configure(
                text="That folder doesn't look like Valheim (no valheim.exe). In Steam: right-click Valheim > Manage > Browse local files.",
                fg=COL_BAD)
            return False
        self.settings["game_dir"] = gd
        save_settings(self.settings)
        self.game = game_info(gd)
        parts = []
        if self.game.get("version"):
            parts.append("Valheim {}".format(self.game["version"]))
        else:
            parts.append("Valheim found")
        if self.game.get("steam_build"):
            parts.append("Steam build {}".format(self.game["steam_build"]))
        if not self.game.get("version"):
            parts.append("exact game version shown after first launch with BepInEx")
        self.game_status.configure(text="OK - " + " | ".join(parts), fg=COL_OK)
        return True

    # ---------- refresh ----------

    def refresh(self):
        ok = self._update_game_status()
        gd = self.game_dir.get().strip() if ok else None
        catalog = self.catalog

        def work():
            self.log("Checking for latest versions...")
            try:
                bx = fetch_bepinex_latest(catalog)
            except Exception as e:
                bx = None
                self.log("Couldn't reach Thunderstore for BepInEx: {}".format(e))
            manifests = []
            for entry in catalog.get("plugins", []):
                repo, branch = entry["repo"], entry.get("branch", "main")
                try:
                    m = http_json(raw_url(repo, branch, "plugin.json") + "?t={}".format(int(time.time())))
                    m.setdefault("repo", repo)
                    m["branch"] = branch
                    manifests.append(m)
                except Exception as e:
                    self.log("Couldn't fetch {}: {}".format(repo, e))
            self.ui(lambda: self._apply_refresh(gd, bx, manifests))

        self.run_bg(work)

    def _apply_refresh(self, gd, bx, manifests):
        self.bepinex_latest = bx
        inst = bepinex_installed_version(gd) if gd else None
        if not gd:
            self.bepinex_status.configure(text="", fg=COL_MUTED)
        elif inst is None:
            self.bepinex_status.configure(text="Not installed - will be installed", fg=COL_BAD)
            self.bepinex_var.set(True)
        elif bx and inst != "unknown" and parse_version(inst) < parse_version(bx["version"]):
            self.bepinex_status.configure(text="Installed {}  ->  update {} available".format(inst, bx["version"]), fg=COL_WARN)
            self.bepinex_var.set(True)
        else:
            latest = " (latest {})".format(bx["version"]) if bx else ""
            self.bepinex_status.configure(text="Installed {}{}".format(inst, latest), fg=COL_OK)
            self.bepinex_var.set(False)

        prev_sel = {p["manifest"]["id"]: p["var"].get() for p in self.plugins}
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.plugins = []

        # One grid for header + rows so the columns line up.
        lf = self.list_frame
        lf.columnconfigure(0, weight=1)
        bold = ("Segoe UI", 9, "bold")
        for i, txt in enumerate(("Mod", "Installed", "Latest", "Tested on Valheim", "Status")):
            ttk.Label(lf, text=txt, font=bold).grid(row=0, column=i, sticky="w", padx=6, pady=(0, 2))
        ttk.Separator(lf).grid(row=1, column=0, columnspan=5, sticky="ew")
        self._desc_labels = []

        if not manifests:
            ttk.Label(lf, foreground=COL_BAD,
                      text="No mods could be loaded. Check your internet connection and click Refresh.").grid(
                row=2, column=0, columnspan=5, sticky="w", padx=8, pady=8)

        for idx, m in enumerate(manifests):
            r = 2 + idx * 2
            installed = installed_manifest(gd, m) if gd else None
            state, text, colour = self._status_for(m, installed)
            var = tk.BooleanVar(value=prev_sel.get(m["id"], state in ("update", "missing_files")))

            left = ttk.Frame(lf)
            left.grid(row=r, column=0, sticky="nsew", padx=6, pady=6)
            ttk.Checkbutton(left, text=m.get("name", m["id"]), variable=var).pack(anchor="w")
            desc = tk.Label(left, text=m.get("description", ""), fg=COL_MUTED, justify="left", anchor="w", wraplength=300)
            desc.pack(anchor="w", fill="x", padx=(22, 0))
            self._desc_labels.append(desc)
            link = tk.Label(left, text="github.com/{}".format(m["repo"]), fg=COL_INFO, cursor="hand2", anchor="w")
            link.pack(anchor="w", padx=(22, 0))
            link.bind("<Button-1>", lambda e, rp=m["repo"]: webbrowser.open("https://github.com/" + rp))

            vh = m.get("valheim", {})
            tested = vh.get("version", "?")
            if vh.get("last_tested"):
                tested += "\n({})".format(vh["last_tested"])
            ttk.Label(lf, text=(installed or {}).get("version", "-")).grid(row=r, column=1, sticky="nw", padx=6, pady=8)
            ttk.Label(lf, text=m.get("version", "?")).grid(row=r, column=2, sticky="nw", padx=6, pady=8)
            ttk.Label(lf, text=tested).grid(row=r, column=3, sticky="nw", padx=6, pady=8)
            tk.Label(lf, text=text, fg=colour, anchor="nw", justify="left", wraplength=self._px(220)).grid(
                row=r, column=4, sticky="nw", padx=6, pady=8)
            ttk.Separator(lf).grid(row=r + 1, column=0, columnspan=5, sticky="ew")

            self.plugins.append({"manifest": m, "installed": installed, "var": var, "state": state})
        self.after(50, self._rewrap)

        if gd:
            n_upd = sum(1 for p in self.plugins if p["state"] == "update")
            self.log("Ready. {} mod(s) available{}.".format(
                len(self.plugins), ", {} update(s)".format(n_upd) if n_upd else ""))

    def _status_for(self, m, installed):
        compat = self._compat_note(m)
        if installed is None:
            return "missing", "Not installed" + compat, COL_MUTED
        iv = installed.get("version", "unknown")
        if iv == "unknown":
            return "update", "Installed manually (version unknown) - reinstall to track updates" + compat, COL_WARN
        missing = [f for f in m.get("files", []) if not (Path(self.game_dir.get()) / m["install_dir"] / Path(f).name).exists()]
        if missing:
            return "missing_files", "Files missing - reinstall" + compat, COL_BAD
        if parse_version(iv) < parse_version(m.get("version")):
            return "update", "Update available ({} -> {})".format(iv, m.get("version")) + compat, COL_WARN
        return "ok", "Up to date" + compat, COL_OK if not compat else COL_WARN

    def _compat_note(self, m):
        vh = m.get("valheim") or {}
        gv, gb = self.game.get("version"), self.game.get("steam_build")
        if not vh or not (gv or gb):
            return ""
        if gv and vh.get("version") and parse_version(gv) == parse_version(vh["version"]):
            return ""
        if not gv and gb and vh.get("steam_build") == gb:
            return ""
        if gv and vh.get("version"):
            return "\nNote: tested on Valheim {}, you have {}".format(vh["version"], gv)
        if gb and vh.get("steam_build") and gb != vh["steam_build"]:
            return "\nNote: tested on Valheim {} (build {}), yours is build {}".format(vh.get("version"), vh["steam_build"], gb)
        return ""

    def _px(self, n):
        return int(n * self._scale)

    def _rewrap(self, event=None):
        """Wrap mod descriptions to the width of the first column."""
        try:
            self.list_frame.update_idletasks()
            bbox = self.list_frame.grid_bbox(0, 0)
            width = max(self._px(160), bbox[2] - self._px(40)) if bbox else self._px(300)
            for d in getattr(self, "_desc_labels", []):
                d.configure(wraplength=width)
        except Exception:
            pass

    def _select(self, pred):
        for p in self.plugins:
            p["var"].set(bool(pred(p)))

    # ---------- actions ----------

    def _require_game(self):
        if not self._update_game_status():
            messagebox.showwarning(APP_NAME, "Please choose your Valheim folder first (Step 1).")
            return None
        gd = self.game_dir.get().strip()
        if is_valheim_running():
            messagebox.showwarning(APP_NAME, "Valheim is running. Please close the game first, then try again.")
            return None
        return gd

    def install(self):
        gd = self._require_game()
        if not gd:
            return
        chosen = [p["manifest"] for p in self.plugins if p["var"].get()]
        need_bx = self.bepinex_var.get()
        has_bx = bepinex_installed_version(gd) is not None
        if chosen and not has_bx and not need_bx:
            need_bx = True
            self.bepinex_var.set(True)
        if not chosen and not need_bx:
            messagebox.showinfo(APP_NAME, "Tick at least one mod (or BepInEx) to install.")
            return
        bx = self.bepinex_latest
        if need_bx and not bx:
            messagebox.showerror(APP_NAME, "Couldn't get BepInEx from Thunderstore. Check your connection and click Refresh.")
            return

        results = {"ok": [], "fail": []}

        def work():
            if need_bx:
                install_bepinex(gd, bx, self.log)
            for m in chosen:
                self.log("Installing {} {} ...".format(m.get("name", m["id"]), m.get("version")))
                try:
                    install_plugin(gd, m, self.log)
                    results["ok"].append(m.get("name", m["id"]))
                    self.log("  done.")
                except Exception as e:
                    results["fail"].append("{}: {}".format(m.get("name", m["id"]), e))
                    self.log("  FAILED: {}".format(e))

        def done(err):
            self.refresh()
            if err:
                messagebox.showerror(APP_NAME, "Installation failed:\n\n{}".format(err))
                return
            msg = []
            if need_bx:
                msg.append("BepInExPack_Valheim {} installed.".format(bx["version"]))
            if results["ok"]:
                msg.append("Installed / updated: " + ", ".join(results["ok"]))
            if results["fail"]:
                msg.append("Failed:\n  " + "\n  ".join(results["fail"]))
            msg.append(self._launch_hint(gd))
            (messagebox.showwarning if results["fail"] else messagebox.showinfo)(APP_NAME, "\n\n".join(m for m in msg if m))

        self.run_bg(work, done)

    def _launch_hint(self, gd):
        flavour = game_info(gd).get("flavour")
        if IS_WINDOWS:
            return "Start Valheim through Steam as normal - mods load automatically."
        if flavour == "windows":
            return ("You're running the Windows build through Proton. In Steam, set Valheim's launch options to:\n\n"
                    '    WINEDLLOVERRIDES="winhttp=n,b" %command%')
        if flavour in ("linux", "mac"):
            return ("In Steam, set Valheim's launch options to:\n\n"
                    "    ./start_game_bepinex.sh %command%")
        return ""

    def uninstall(self):
        gd = self._require_game()
        if not gd:
            return
        chosen = [p["manifest"] for p in self.plugins if p["var"].get() and p["installed"] is not None]
        if not chosen:
            messagebox.showinfo(APP_NAME, "Tick at least one installed mod to remove.")
            return
        names = ", ".join(m.get("name", m["id"]) for m in chosen)
        if not messagebox.askyesno(APP_NAME, "Remove these mods?\n\n{}\n\n(Their config files in BepInEx/config are kept.)".format(names)):
            return

        def work():
            for m in chosen:
                uninstall_plugin(gd, m)
                self.log("Removed {}".format(m.get("name", m["id"])))

        self.run_bg(work, lambda err: self.refresh())

    def open_plugins(self):
        gd = self.game_dir.get().strip()
        p = Path(gd) / "BepInEx" / "plugins"
        open_folder(p if p.exists() else gd)

    def launch(self):
        webbrowser.open("steam://rungameid/" + VALHEIM_APPID)


def main():
    if IS_WINDOWS:
        # Crisp text on high-DPI displays instead of blurry bitmap scaling.
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        app = App()
        app.mainloop()
    except Exception:
        # No console when run with pythonw: surface fatal errors in a dialog and a log file.
        err = traceback.format_exc()
        try:
            p = settings_path().parent / "crash.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(err, encoding="utf-8")
        except Exception:
            pass
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, "The installer crashed:\n\n" + err[-1500:])
        except Exception:
            pass


if __name__ == "__main__":
    main()
