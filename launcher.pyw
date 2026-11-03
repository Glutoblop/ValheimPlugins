"""
Valheim Mod Installer - self-updating launcher
==============================================
A small window that runs before the real installer GUI:

  1. reads APP_VERSION from valheim_mod_installer.pyw (the version currently on disk),
  2. asks GitHub for the latest ValheimPlugins release,
  3. if that release is newer, downloads ValheimModInstaller.zip and unpacks it over this folder
     (your sources.json is kept - any new built-in sources are merged into it; settings in
     %APPDATA% / ~/.config are never touched),
  4. starts valheim_mod_installer.pyw and exits.

Any network / GitHub problem just skips the update and opens the installer as normal.
Pass --no-update to skip the check.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import ttk

APP_NAME = "Valheim Mod Installer"
REPO = "Glutoblop/ValheimPlugins"
ASSET = "ValheimModInstaller.zip"
ZIP_PREFIX = "ValheimModInstaller/"      # every file in the release zip lives under this folder
HERE = Path(__file__).resolve().parent
MAIN_SCRIPT = HERE / "valheim_mod_installer.pyw"
SOURCES_FILE = "sources.json"            # user-editable: merged, never blindly overwritten
CHECK_TIMEOUT = 8


def parse_version(v):
    """'v1.2.10' -> (1, 2, 10). Non-numeric parts are ignored."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def current_version():
    try:
        src = MAIN_SCRIPT.read_text(encoding="utf-8")
        m = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
        return m.group(1) if m else "0"
    except OSError:
        return "0"  # main script missing/broken: any release is an upgrade


def http_get(url, timeout=CHECK_TIMEOUT, accept=None):
    headers = {"User-Agent": "ValheimModInstaller-Launcher", "Cache-Control": "no-cache"}
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def latest_release():
    """Returns (version_tag, asset_download_url) or raises."""
    rel = json.loads(http_get("https://api.github.com/repos/{}/releases/latest".format(REPO),
                              accept="application/vnd.github+json").decode("utf-8"))
    for a in rel.get("assets", []):
        if a.get("name") == ASSET:
            return rel.get("tag_name", ""), a["browser_download_url"]
    raise RuntimeError("Release {} has no {}".format(rel.get("tag_name"), ASSET))


def _repo_key(url):
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    return u.split("github.com/")[-1].split("github.com:")[-1]


def merge_sources(old_text, new_text):
    """Keep the user's list (order, names, custom entries); append new built-in sources they don't have.
    Returns the text to write, or None to leave the user's file untouched."""
    try:
        old = json.loads(old_text)
        new = json.loads(new_text)
        if not isinstance(old, list) or not isinstance(new, list):
            return None
    except ValueError:
        return None  # user's file is hand-edited/broken: don't clobber it
    have = {_repo_key(s.get("url")) for s in old if isinstance(s, dict)}
    added = [s for s in new if isinstance(s, dict) and _repo_key(s.get("url")) not in have]
    if not added:
        return None
    return json.dumps(old + added, indent=2) + "\n"


def apply_update(zip_path, status):
    """Unpack the release zip over HERE. Files are staged then swapped in one at a time."""
    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory(prefix="vmi-update-") as stage:
        stage = Path(stage)
        files = []
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.startswith(ZIP_PREFIX):
                continue
            rel = name[len(ZIP_PREFIX):]
            parts = Path(rel).parts
            if not rel or Path(rel).is_absolute() or ".." in parts or ":" in rel:
                continue  # never write outside the install folder
            out = stage / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not sys.platform.startswith("win"):
                os.chmod(out, mode)
            files.append(rel)
        if "valheim_mod_installer.pyw" not in files:
            raise RuntimeError("Update package is missing valheim_mod_installer.pyw")

        for rel in files:
            status("Updating {}...".format(rel))
            target = HERE / rel
            staged = stage / rel
            if rel == SOURCES_FILE and target.exists():
                merged = merge_sources(target.read_text(encoding="utf-8"), staged.read_text(encoding="utf-8"))
                if merged is None:
                    continue
                staged.write_text(merged, encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".new")
            shutil.copy2(staged, tmp)
            os.replace(tmp, target)


class Launcher(tk.Tk):
    def __init__(self, skip_update):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=APP_NAME, font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        self.msg = ttk.Label(frm, text="Checking for updates...", width=48)
        self.msg.pack(anchor="w", pady=(6, 8))
        self.bar = ttk.Progressbar(frm, mode="indeterminate", length=320)
        self.bar.pack(fill="x")
        self.bar.start(12)
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry("+{}+{}".format((self.winfo_screenwidth() - w) // 2, (self.winfo_screenheight() - h) // 3))
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # finish the update before closing

        self._status = None
        self._done = False
        if skip_update:
            self.after(10, self.launch_main)
        else:
            threading.Thread(target=self.work, daemon=True).start()
            self.after(100, self.poll)

    def set_status(self, text):
        self._status = text  # read by poll() on the Tk thread

    def work(self):
        try:
            cur = current_version()
            tag, url = latest_release()
            if parse_version(tag) <= parse_version(cur):
                self.set_status("Up to date ({}).".format(cur))
            else:
                self.set_status("Downloading update {} (you have {})...".format(tag, cur))
                with tempfile.TemporaryDirectory(prefix="vmi-dl-") as d:
                    zp = Path(d) / ASSET
                    zp.write_bytes(http_get(url, timeout=60))
                    apply_update(zp, self.set_status)
                self.set_status("Updated to {}.".format(tag))
        except Exception as e:
            self.set_status("Update check skipped ({}).".format(type(e).__name__))
        self._done = True

    def poll(self):
        if self._status:
            self.msg.configure(text=self._status)
        if self._done:
            self.after(400, self.launch_main)
        else:
            self.after(100, self.poll)

    def launch_main(self):
        if not MAIN_SCRIPT.exists():
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, "Can't find valheim_mod_installer.pyw next to the launcher.\n"
                                           "Re-download ValheimModInstaller.zip from GitHub.")
        else:
            kw = {"cwd": str(HERE)}
            if not sys.platform.startswith("win"):
                kw["start_new_session"] = True
            subprocess.Popen([sys.executable, str(MAIN_SCRIPT)], **kw)
        self.destroy()


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    Launcher(skip_update="--no-update" in sys.argv).mainloop()
    sys.exit(0)


if __name__ == "__main__":
    main()
