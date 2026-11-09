"""
Valheim Mod Installer
=====================
A small, dependency-free (Python standard library only) GUI that:

  1. finds (or lets you pick) your Valheim install folder,
  2. installs / updates BepInExPack_Valheim (the mod loader every Valheim mod needs),
  3. shows each mod (the online default list plus your own sources.json) with its installed version, latest version and the
     Valheim version it was last tested on (fuzzy-searchable by name / git URL),
  4. installs / updates / removes the mods you tick.

Mod sources are GitHub repos. A repo either has a plugin.json (see README) or, for anyone else's
plugin, its latest GitHub release is used (.dll or .zip assets).

Run it with pythonw (no console window), or use Start-Installer.bat / .ps1 / .sh.
"""

import difflib
import json
import os
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

if sys.version_info < (3, 8):
    raise SystemExit("Python 3.8 or later is required.")

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Valheim Mod Installer"
APP_VERSION = "1.3.4"
USER_AGENT = "ValheimModInstaller/" + APP_VERSION
VALHEIM_APPID = "892970"
INSTALLER_REPO = "Glutoblop/ValheimPlugins"   # where the installer itself is released
HERE = Path(__file__).resolve().parent

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

BEPINEX = {"thunderstore_namespace": "denikson", "thunderstore_name": "BepInExPack_Valheim"}

# Mod sources come from two lists, merged on load:
#  - the default list, downloaded from DEFAULT_SOURCES_URL (cached in app data; DEFAULT_SOURCES is the
#    last-resort fallback), so new mods can be shipped without releasing a new installer;
#  - the user's own sources.json: {"sources": [{"name": "...", "url": "..."}, ...], "hidden": ["owner/repo"]}.
#    A user entry for a default repo overrides it; "hidden" removes defaults from the list.
SOURCES_FILE = HERE / "sources.json"
DEFAULT_SOURCES_URL = "https://raw.githubusercontent.com/{}/main/default-sources.json".format(INSTALLER_REPO)
DEFAULT_SOURCES = [
    {"name": "Achievement Enabler", "url": "https://github.com/Glutoblop/AchievementEnabler"},
    {"name": "Health Check Up", "url": "https://github.com/Glutoblop/HealthCheckup"},
    {"name": "Hotkey Sliding", "url": "https://github.com/Glutoblop/HotkeySliding"},
    {"name": "Recipe Search", "url": "https://github.com/Glutoblop/RecipeSearch"},
]

# Marker files the installer writes so it can tell what it installed and at which version.
INSTALLED_MANIFEST = "plugin.json"                      # written into each plugin's install_dir
BEPINEX_MARKER = "BepInEx/.bepinexpack_version"         # written after installing BepInExPack


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------

def app_data_dir():
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ValheimModInstaller"


def settings_path():
    return app_data_dir() / "settings.json"


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
    """'v1.2.10' -> (1, 2, 10). Non-numeric parts are ignored."""
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def http_get(url, timeout=30, accept=None):
    headers = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, accept=None):
    return json.loads(http_get(url, accept=accept).decode("utf-8"))


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


def open_path(path):
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # noqa
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def safe_folder_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "", s.replace(" ", "")) or "Plugin"


# --------------------------------------------------------------------------------------------
# Sources (sources.json)
# --------------------------------------------------------------------------------------------

def parse_repo_url(url):
    """Accepts https://github.com/owner/repo(.git)(/tree/branch), git@github.com:owner/repo.git or owner/repo.
    Returns (\"owner/repo\", branch_or_None) or raises ValueError."""
    u = (url or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?(?:/tree/([^\s#?]+))?/?(?:[#?].*)?$", u, re.I) \
        or re.match(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?$", u, re.I) \
        or re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$", u)
    if not m:
        raise ValueError("Not a GitHub repo URL: {!r} (expected https://github.com/owner/repo)".format(url))
    branch = m.group(3) if m.lastindex and m.lastindex >= 3 else None
    return "{}/{}".format(m.group(1), m.group(2)), branch


def repo_key(url):
    """'https://github.com/Owner/Repo' -> 'owner/repo' (raises ValueError)."""
    return parse_repo_url(url)[0].lower()


def validate_sources(data):
    """Raises ValueError with a readable message if the sources list is malformed."""
    if not isinstance(data, list):
        raise ValueError("Sources must be a JSON list: [ {\"name\": ..., \"url\": ...}, ... ]")
    seen = set()
    for i, s in enumerate(data, 1):
        if not isinstance(s, dict) or not s.get("url"):
            raise ValueError("Entry {} needs at least a \"url\".".format(i))
        repo, _ = parse_repo_url(s["url"])
        if repo.lower() in seen:
            raise ValueError("Entry {} ({}) is listed twice.".format(i, repo))
        seen.add(repo.lower())
    return data


def validate_user_sources(data):
    """Normalises the user's sources.json to {"sources": [...], "hidden": [...]} or raises ValueError.
    A plain list (the pre-1.4 format) is accepted as "sources"."""
    if isinstance(data, list):
        data = {"sources": data}
    if not isinstance(data, dict):
        raise ValueError("sources.json must be {\"sources\": [...], \"hidden\": [...]}")
    unknown = set(data) - {"sources", "hidden"}
    if unknown:
        raise ValueError("Unknown key(s) in sources.json: {}".format(", ".join(sorted(unknown))))
    sources = validate_sources(data.get("sources", []))
    hidden = data.get("hidden", [])
    if not isinstance(hidden, list) or not all(isinstance(h, str) for h in hidden):
        raise ValueError("\"hidden\" must be a list of repo URLs or owner/repo strings.")
    for h in hidden:
        parse_repo_url(h)
    return {"sources": sources, "hidden": hidden}


def default_sources_cache():
    return app_data_dir() / "default-sources.json"


def load_default_sources(fetch=False):
    """Returns (sources, warning). With fetch=True the online list is downloaded and cached;
    otherwise (or if that fails) the cached copy is used, then the built-in DEFAULT_SOURCES."""
    warn = None
    if fetch:
        try:
            data = validate_sources(json.loads(http_get(DEFAULT_SOURCES_URL, timeout=10).decode("utf-8")))
            try:
                p = default_sources_cache()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
            return data, None
        except Exception as e:
            warn = "Couldn't download the default mod list ({}); using the last saved copy.".format(e)
    try:
        return validate_sources(json.loads(default_sources_cache().read_text(encoding="utf-8"))), warn
    except Exception:
        return list(DEFAULT_SOURCES), warn


def load_user_sources():
    """Returns ({"sources": [...], "hidden": [...]}, warning). Migrates a pre-1.4 plain-list file by
    dropping the entries that are built-in defaults (they now come from the default list)."""
    empty = {"sources": [], "hidden": []}
    try:
        raw = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        save_user_sources(empty)
        return empty, None
    except Exception as e:
        return empty, "sources.json is invalid ({}); showing only the default mods.".format(e)
    try:
        data = validate_user_sources(raw)
    except ValueError as e:
        return empty, "sources.json is invalid ({}); showing only the default mods.".format(e)
    if isinstance(raw, list):
        builtin = {repo_key(s["url"]) for s in DEFAULT_SOURCES}
        data["sources"] = [s for s in data["sources"] if repo_key(s["url"]) not in builtin]
        try:
            save_user_sources(data)
        except OSError:
            pass
    return data, None


def merge_sources(defaults, user):
    """Defaults first (in their order), then the user's own entries. A user entry for a default repo
    replaces it in place; hidden repos are dropped. Each entry gets "origin": "default" or "custom"."""
    hidden = {repo_key(h) for h in user["hidden"]}
    overrides = {repo_key(s["url"]): s for s in user["sources"]}
    merged, seen = [], set()
    for s in defaults:
        k = repo_key(s["url"])
        if k in hidden or k in seen:
            continue
        seen.add(k)
        merged.append(dict(overrides.get(k, s), origin="default"))
    for s in user["sources"]:
        k = repo_key(s["url"])
        if k not in seen and k not in hidden:
            seen.add(k)
            merged.append(dict(s, origin="custom"))
    return merged


def load_sources(fetch=False):
    """Returns (merged sources, [warnings]). fetch=True downloads the default list (call off the UI thread)."""
    defaults, w1 = load_default_sources(fetch)
    user, w2 = load_user_sources()
    return merge_sources(defaults, user), [w for w in (w1, w2) if w]


def save_user_sources(data):
    SOURCES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Fuzzy search
# --------------------------------------------------------------------------------------------

def _token_score(tok, text):
    """Score one query token against one text (both lower case). None = no match."""
    if not tok:
        return 0.0
    if not text:
        return None
    i = text.find(tok)
    if i >= 0:
        # Substring: best when it starts the text or a word.
        start_bonus = 30 if i == 0 else (15 if not text[i - 1].isalnum() else 0)
        return 100.0 + start_bonus + 20.0 * len(tok) / len(text)
    # Subsequence ("rcpsrch" -> "recipesearch"): reward consecutive runs, penalise gaps.
    pos, score, prev = 0, 0.0, -2
    for ch in tok:
        j = text.find(ch, pos)
        if j < 0:
            break
        score += 3.0 if j == prev + 1 else 1.0
        score -= min(j - pos, 10) * 0.1
        prev, pos = j, j + 1
    else:
        return 40.0 + score
    # Typo tolerance: compare against individual words.
    best = 0.0
    for word in re.split(r"[^a-z0-9]+", text):
        if word:
            best = max(best, difflib.SequenceMatcher(None, tok, word).ratio())
            if len(word) > len(tok):  # also compare against the word's prefix ("serch" ~ "search...")
                best = max(best, difflib.SequenceMatcher(None, tok, word[:len(tok) + 1]).ratio())
    return 30.0 * best if best >= 0.72 else None


def fuzzy_score(query, fields):
    """Every whitespace-separated token in query must match at least one field. Higher = better."""
    tokens = query.lower().split()
    if not tokens:
        return 0.0
    texts = [f.lower() for f in fields if f]
    total = 0.0
    for tok in tokens:
        best = None
        for t in texts:
            s = _token_score(tok, t)
            if s is not None and (best is None or s > best):
                best = s
        if best is None:
            return None
        total += best
    return total


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

    acf = g.parent.parent / ("appmanifest_%s.acf" % VALHEIM_APPID)
    try:
        m = re.search(r'"buildid"\s+"(\d+)"', acf.read_text(encoding="utf-8", errors="ignore"))
        if m:
            info["steam_build"] = int(m.group(1))
    except Exception:
        pass

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
# Fetching mod info (worker thread)
# --------------------------------------------------------------------------------------------

def fetch_bepinex_latest():
    url = "https://thunderstore.io/api/experimental/package/{}/{}/".format(
        BEPINEX["thunderstore_namespace"], BEPINEX["thunderstore_name"])
    latest = http_json(url)["latest"]
    return {"version": latest["version_number"], "download_url": latest["download_url"]}


def _pick_release_assets(assets):
    """Prefer a mod zip (not source code), else all .dll assets."""
    usable = [a for a in assets if not re.search(r"(source|src|debug|\.pdb$|\.mdb$)", a["name"], re.I)]
    zips = [a for a in usable if a["name"].lower().endswith(".zip")]
    dlls = [a for a in usable if a["name"].lower().endswith(".dll")]
    if dlls and not zips:
        return dlls
    if zips:
        return zips[:1]
    return []


def fetch_source_manifest(source):
    """Turn a sources.json entry into a manifest dict. Never raises: failures become {'error': ...}."""
    name = source.get("name")
    try:
        repo, branch = parse_repo_url(source["url"])
    except ValueError as e:
        return {"id": source.get("url", "?"), "name": name or source.get("url", "?"), "repo": source.get("url", ""),
                "url": source.get("url", ""), "error": str(e)}
    branch = source.get("branch") or branch
    base = {"repo": repo, "url": "https://github.com/" + repo}

    # 1) Repo with a plugin.json (our own format).
    for br in ([branch] if branch else ["main", "master"]):
        try:
            m = http_json(raw_url(repo, br, "plugin.json") + "?t={}".format(int(time.time())))
            if isinstance(m, dict) and m.get("version") and m.get("files"):
                m.update(base)
                m["branch"] = br
                m["source"] = "plugin.json"
                m.setdefault("id", repo.split("/")[1])
                m.setdefault("install_dir", "BepInEx/plugins/" + safe_folder_name(m["id"]))
                if name:
                    m["name"] = name
                return m
        except Exception:
            pass

    # 2) Anyone else's plugin: latest GitHub release.
    repo_name = repo.split("/")[1]
    try:
        rel = http_json("https://api.github.com/repos/{}/releases/latest".format(repo),
                        accept="application/vnd.github+json")
    except urllib.error.HTTPError as e:
        msg = "no plugin.json and no GitHub release found" if e.code == 404 else \
              ("GitHub rate limit hit - try Refresh again later" if e.code in (403, 429) else "GitHub error {}".format(e.code))
        return dict(base, id=repo_name, name=name or repo_name, error=msg)
    except Exception as e:
        return dict(base, id=repo_name, name=name or repo_name, error="couldn't reach GitHub ({})".format(e))

    assets = _pick_release_assets(rel.get("assets", []))
    body = (rel.get("body") or "").strip().splitlines()
    desc = next((l.strip("# ").strip() for l in body if l.strip("# ").strip()), "")
    m = dict(base,
             id=repo_name,
             name=name or repo_name,
             version=(rel.get("tag_name") or rel.get("name") or "?").lstrip("vV"),
             description=desc[:200] or "Installed from the latest GitHub release.",
             source="release",
             release_url=rel.get("html_url"),
             assets=[{"name": a["name"], "url": a["browser_download_url"]} for a in assets],
             install_dir="BepInEx/plugins/" + safe_folder_name(repo_name),
             valheim={})
    if not assets:
        m["error"] = "latest release has no .dll or .zip to install"
    return m


# --------------------------------------------------------------------------------------------
# Install logic (worker thread)
# --------------------------------------------------------------------------------------------

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
                if rel.startswith("BepInEx/config/") and dest.exists():
                    continue  # never clobber the user's existing BepInEx settings
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


def _write_file(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download")
    tmp.write_bytes(data)
    os.replace(str(tmp), str(target))


def _extract_mod_zip(zpath, game_dir, install_dir, log):
    """Install a mod zip. Handles the common layouts:
       - contains BepInEx/...            -> extracted relative to the game folder
       - Thunderstore style plugins/ (+ config/, patchers/) -> into BepInEx/plugins/<mod>/ etc.
       - anything else                   -> all files into BepInEx/plugins/<mod>/ (folders flattened only at top level)
    Returns the list of installed paths relative to the game folder."""
    g = Path(game_dir)
    installed = []
    with zipfile.ZipFile(zpath) as z:
        names = [n.replace("\\", "/") for n in z.namelist()]
        files = [n for n in names if not n.endswith("/")]
        # Strip a single wrapping folder (e.g. "MyMod-1.0/...").
        tops = {n.split("/", 1)[0] for n in files}
        strip = ""
        if len(tops) == 1 and all("/" in n for n in files):
            strip = next(iter(tops)) + "/"
        rels = [(orig, n[len(strip):]) for orig, n in zip([i.filename for i in z.infolist() if not i.filename.endswith("/")], files)]
        lower = [r.lower() for _, r in rels]
        has_bepinex = any(r.startswith("bepinex/") for r in lower)
        has_ts = any(r.startswith(("plugins/", "config/", "patchers/")) for r in lower)

        skip_meta = {"manifest.json", "icon.png", "readme.md", "changelog.md", "license", "license.txt", "license.md"}
        for orig, rel in rels:
            parts = Path(rel).parts
            if not parts or ".." in parts:
                continue
            rl = rel.lower()
            if has_bepinex:
                if not rl.startswith("bepinex/"):
                    continue
                dest_rel = Path(rel)
                if rl.startswith("bepinex/config/") and (g / dest_rel).exists():
                    continue  # keep existing user config
            elif has_ts:
                if rl.startswith("plugins/"):
                    dest_rel = Path(install_dir) / Path(*parts[1:])
                elif rl.startswith("config/"):
                    dest_rel = Path("BepInEx/config") / Path(*parts[1:])
                    if (g / dest_rel).exists():
                        continue  # keep existing user config
                elif rl.startswith("patchers/"):
                    dest_rel = Path("BepInEx/patchers") / safe_folder_name(Path(install_dir).name) / Path(*parts[1:])
                else:
                    if rl in skip_meta:
                        continue
                    dest_rel = Path(install_dir) / rel
            else:
                if rl in skip_meta:
                    continue
                dest_rel = Path(install_dir) / rel
            with z.open(orig) as src:
                _write_file(g / dest_rel, src.read())
            installed.append(dest_rel.as_posix())
    log("  extracted {} file(s)".format(len(installed)))
    return installed


def install_plugin(game_dir, manifest, log):
    g = Path(game_dir)
    dest_dir = g / manifest["install_dir"]
    installed = []
    if manifest.get("source") == "release":
        tmp = Path(tempfile.mkdtemp(prefix="vmi_mod_"))
        try:
            for a in manifest["assets"]:
                log("  downloading {}".format(a["name"]))
                data = http_get(a["url"], timeout=180)
                if a["name"].lower().endswith(".zip"):
                    zp = tmp / "mod.zip"
                    zp.write_bytes(data)
                    installed += _extract_mod_zip(zp, g, manifest["install_dir"], log)
                else:
                    if not data.startswith(b"MZ"):
                        raise RuntimeError("{} did not download as a valid DLL".format(a["name"]))
                    _write_file(dest_dir / a["name"], data)
                    installed.append((Path(manifest["install_dir"]) / a["name"]).as_posix())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not any(p.lower().endswith(".dll") for p in installed):
            raise RuntimeError("the release didn't contain any .dll plugin")
    else:
        for f in manifest["files"]:
            log("  downloading {}".format(f))
            data = http_get(raw_url(manifest["repo"], manifest.get("branch", "main"), f), timeout=120)
            if f.lower().endswith(".dll") and not data.startswith(b"MZ"):
                raise RuntimeError("{} did not download as a valid DLL".format(f))
            _write_file(dest_dir / Path(f).name, data)
            installed.append((Path(manifest["install_dir"]) / Path(f).name).as_posix())
    record = dict(manifest, installed_files=installed)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / INSTALLED_MANIFEST).write_text(json.dumps(record, indent=2), encoding="utf-8")


def uninstall_plugin(game_dir, manifest, installed):
    g = Path(game_dir).resolve()
    plugins_root = (g / "BepInEx" / "plugins").resolve()
    for rel in (installed or {}).get("installed_files", []):
        p = (g / rel).resolve()
        # Only ever delete inside the game folder, and leave user config alone.
        if g in p.parents and "config" not in [x.lower() for x in Path(rel).parts[:2]] and p.is_file():
            p.unlink()
    d = (g / manifest["install_dir"]).resolve()
    if d.exists() and d != plugins_root and plugins_root in d.parents:
        shutil.rmtree(d)


def dll_plugin_version(dll_path, guid):
    """Read the version from a BepInEx plugin DLL's [BepInPlugin(guid, name, version)] attribute.
    Custom attribute blobs are a 0x01 0x00 prolog then length-prefixed UTF-8 strings."""
    try:
        data = Path(dll_path).read_bytes()
        gb = guid.encode("utf-8")
        needle = b"\x01\x00" + bytes([len(gb)]) + gb
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


def missing_files(game_dir, manifest, installed):
    g = Path(game_dir)
    if installed and installed.get("installed_files"):
        return [f for f in installed["installed_files"] if not (g / f).exists()]
    return [f for f in manifest.get("files", []) if not (g / manifest["install_dir"] / Path(f).name).exists()]


# --------------------------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------------------------

COL_OK = "#2e7d32"
COL_WARN = "#b26a00"
COL_BAD = "#c62828"
COL_INFO = "#1565c0"
COL_MUTED = "#666666"


class SourcesEditor(tk.Toplevel):
    """File > Edit plugin sources: a JSON editor for sources.json with validation."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Edit plugin sources - sources.json")
        self.geometry("{}x{}".format(app._px(720), app._px(480)))
        self.transient(app)

        top = ttk.Frame(self, padding=8)
        top.pack(fill="both", expand=True)
        ttk.Label(top, justify="left", foreground=COL_MUTED, text=(
            "\"sources\": your own plugins - a name you choose and its GitHub URL. Optional \"branch\".\n"
            "Repos with a plugin.json use it; any other repo installs its latest GitHub release (.dll / .zip).\n"
            "The default mods are updated online. List one in \"sources\" to override it, or in \"hidden\" to hide it.")
        ).pack(anchor="w", pady=(0, 2))
        defaults, _ = load_default_sources()
        ttk.Label(top, justify="left", foreground=COL_MUTED, wraplength=app._px(700), text="Defaults: " + ", ".join(
            "{} ({})".format(s.get("name") or repo_key(s["url"]), repo_key(s["url"])) for s in defaults)
        ).pack(anchor="w", pady=(0, 6))

        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", undo=True, font=("Consolas", 10))
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)

        load_user_sources()  # creates / migrates the file
        try:
            content = SOURCES_FILE.read_text(encoding="utf-8")
        except Exception:
            content = json.dumps({"sources": [], "hidden": []}, indent=2) + "\n"
        self.text.insert("1.0", content)
        self.text.edit_reset()

        self.status = tk.Label(top, text="", anchor="w", fg=COL_BAD)
        self.status.pack(fill="x", pady=(6, 0))

        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Save && Reload", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        ttk.Button(btns, text="Check", command=self.check).pack(side="left")
        ttk.Button(btns, text="Format", command=self.format).pack(side="left", padx=6)
        ttk.Button(btns, text="Open in text editor", command=self.external).pack(side="left")
        self.bind("<Control-s>", lambda e: self.save())
        self.text.focus_set()

    def _parse(self):
        raw = self.text.get("1.0", "end").strip() or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.status.configure(text="JSON error on line {}, column {}: {}".format(e.lineno, e.colno, e.msg), fg=COL_BAD)
            self.text.mark_set("insert", "{}.{}".format(e.lineno, max(e.colno - 1, 0)))
            self.text.see("insert")
            return None
        try:
            return validate_user_sources(data)
        except ValueError as e:
            self.status.configure(text=str(e), fg=COL_BAD)
            return None

    def check(self):
        data = self._parse()
        if data is not None:
            self.status.configure(text="Looks good: {} source(s), {} hidden.".format(
                len(data["sources"]), len(data["hidden"])), fg=COL_OK)

    def format(self):
        data = self._parse()
        if data is not None:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", json.dumps(data, indent=2) + "\n")
            self.status.configure(text="Formatted.", fg=COL_OK)

    def save(self):
        data = self._parse()
        if data is None:
            return
        try:
            save_user_sources(data)
        except Exception as e:
            self.status.configure(text="Couldn't save: {}".format(e), fg=COL_BAD)
            return
        self.destroy()
        self.app.reload_sources()

    def external(self):
        data = self._parse()
        if data is not None:
            save_user_sources(data)
        open_path(SOURCES_FILE)
        self.destroy()
        self.app.log("Opened {} - use File > Reload plugin sources after saving it.".format(SOURCES_FILE))


class AddSourceDialog(tk.Toplevel):
    """File > Add plugin source: name + GitHub URL."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Add plugin source")
        self.transient(app)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        self.name = tk.StringVar()
        self.url = tk.StringVar()
        try:
            clip = app.clipboard_get().strip()
            parse_repo_url(clip)
            self.url.set(clip)
            self.name.set(clip.rstrip("/").split("/")[-1].replace(".git", ""))
        except Exception:
            pass
        ttk.Label(f, text="Name:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=self.name, width=50).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="GitHub URL:").grid(row=1, column=0, sticky="w", pady=3)
        e = ttk.Entry(f, textvariable=self.url, width=50)
        e.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(f, foreground=COL_MUTED, text="e.g. https://github.com/owner/repo").grid(row=2, column=1, sticky="w")
        self.status = tk.Label(f, text="", fg=COL_BAD, anchor="w")
        self.status.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Add", style="Accent.TButton", command=self.add).pack(side="right", padx=6)
        self.bind("<Return>", lambda ev: self.add())
        self.bind("<Escape>", lambda ev: self.destroy())
        e.focus_set()

    def add(self):
        url, name = self.url.get().strip(), self.name.get().strip()
        try:
            repo, _ = parse_repo_url(url)
        except ValueError as ex:
            self.status.configure(text=str(ex))
            return
        key = repo.lower()
        user, _ = load_user_sources()
        hidden = [h for h in user["hidden"] if repo_key(h) != key]
        sources, _ = load_sources()
        if len(hidden) == len(user["hidden"]) and any(repo_key(s["url"]) == key for s in sources):
            self.status.configure(text="{} is already in the list.".format(repo))
            return
        user["hidden"] = hidden  # adding a hidden default un-hides it
        if not any(repo_key(s["url"]) == key for s in user["sources"]):
            user["sources"].append({"name": name or repo.split("/")[1], "url": "https://github.com/" + repo})
        try:
            save_user_sources(user)
        except Exception as ex:
            self.status.configure(text="Couldn't save sources.json: {}".format(ex))
            return
        self.destroy()
        self.app.log("Added {} ({}).".format(name or repo, repo))
        self.app.reload_sources()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("{} {}".format(APP_NAME, APP_VERSION))
        self._scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.geometry("{}x{}".format(self._px(960), self._px(740)))
        self.minsize(self._px(800), self._px(600))

        self.settings = load_settings()
        self.q = queue.Queue()
        self.busy = False

        self.game_dir = tk.StringVar(value=self.settings.get("game_dir", ""))
        self.search_var = tk.StringVar()
        self.bepinex_var = tk.BooleanVar(value=False)
        self.bepinex_latest = None
        self.game = {}
        self.plugins = []  # dicts: manifest, installed, var, state, widgets

        self._build_menu()
        self._build_ui()
        self.after(100, self._pump)

        if not is_valheim_dir(self.game_dir.get()):
            found = find_valheim()
            if found:
                self.game_dir.set(str(found))
        self.refresh()

    def _px(self, n):
        return int(n * self._scale)

    # ---------- setup ----------

    def _build_menu(self):
        bar = tk.Menu(self)
        fm = tk.Menu(bar, tearoff=0)
        fm.add_command(label="Add plugin source...", accelerator="Ctrl+N", command=self.add_source)
        fm.add_command(label="Edit plugin sources...", accelerator="Ctrl+E", command=self.edit_sources)
        fm.add_command(label="Open sources.json in text editor", command=lambda: open_path(SOURCES_FILE))
        fm.add_command(label="Reload plugin sources", accelerator="F5", command=self.reload_sources)
        fm.add_separator()
        fm.add_command(label="Open Valheim folder", command=lambda: open_path(self.game_dir.get()))
        fm.add_command(label="Open BepInEx config folder", command=self.open_config)
        fm.add_separator()
        fm.add_command(label="Exit", command=self.destroy)
        bar.add_cascade(label="File", menu=fm)
        hm = self.help_menu = tk.Menu(bar, tearoff=0)
        hm.add_command(label="Check for new version...", command=self.check_for_update)
        hm.add_command(label="Open releases page",
                       command=lambda: webbrowser.open("https://github.com/{}/releases".format(INSTALLER_REPO)))
        hm.add_separator()
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            APP_NAME, "{} {}\n\nSources: {}".format(APP_NAME, APP_VERSION, SOURCES_FILE)))
        bar.add_cascade(label="Help", menu=hm)
        self.config(menu=bar)
        self.bind_all("<Control-n>", lambda e: self.add_source())
        self.bind_all("<Control-e>", lambda e: self.edit_sources())
        self.bind_all("<F5>", lambda e: self.reload_sources())
        self.bind_all("<Control-f>", lambda e: (self.search_entry.focus_set(), self.search_entry.select_range(0, "end")))

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

        title = ttk.Frame(outer)
        title.pack(fill="x")
        ttk.Label(title, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(title, text="v" + APP_VERSION, foreground=COL_MUTED,
                  font=("Segoe UI", 11)).pack(side="left", padx=(8, 0), pady=(5, 0))
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
        search = ttk.Frame(s3)
        search.pack(fill="x", pady=(0, 6))
        ttk.Label(search, text="Search:").pack(side="left")
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        ttk.Button(search, text="x", width=3, command=lambda: self.search_var.set("")).pack(side="left", padx=(4, 0))
        self.match_label = ttk.Label(search, text="", foreground=COL_MUTED, width=16, anchor="e")
        self.match_label.pack(side="left", padx=(6, 0))
        self.search_var.trace_add("write", lambda *a: self._render_rows())

        tools = ttk.Frame(s3)
        tools.pack(fill="x", pady=(0, 4))
        ttk.Button(tools, text="Select all", command=lambda: self._select(lambda p: True)).pack(side="left")
        ttk.Button(tools, text="Select updates", command=lambda: self._select(lambda p: p["state"] in ("update", "missing_files"))).pack(side="left", padx=4)
        ttk.Button(tools, text="Select none", command=lambda: self._select(lambda p: False)).pack(side="left")
        ttk.Button(tools, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(tools, text="+ Add source", command=self.add_source).pack(side="right", padx=4)

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
        self.sel_label = ttk.Label(actions, text="", foreground=COL_MUTED)
        self.sel_label.pack(side="left", padx=8)
        ttk.Button(actions, text="Launch Valheim", command=self.launch).pack(side="right")
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=self._px(120))
        self.progress.pack(side="right", padx=8)

        # Log
        logf = ttk.Frame(outer)
        logf.pack(fill="x", pady=(8, 0))
        self.log_text = tk.Text(logf, height=6, wrap="word", state="disabled", font=("Consolas", 9),
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
            w = self.winfo_containing(event.x_root, event.y_root)
            if w is None or not str(w).startswith(str(self.canvas)):
                return
        except Exception:
            return
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
            return False
        self._set_busy(True)

        def runner():
            err = None
            try:
                work()
            except Exception as e:  # noqa
                err = e
                self.log("ERROR: {}".format(e))
            finally:
                def fin():
                    self._set_busy(False)
                    if done:
                        done(err)
                self.ui(fin)

        threading.Thread(target=runner, daemon=True).start()
        return True

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.install_btn.configure(state=state)
        self.uninstall_btn.configure(state=state)
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)

    # ---------- sources ----------

    def add_source(self):
        AddSourceDialog(self)

    def edit_sources(self):
        SourcesEditor(self)

    def reload_sources(self):
        if self.busy:
            self.after(500, self.reload_sources)
            return
        self.refresh()

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
        parts = ["Valheim {}".format(self.game["version"]) if self.game.get("version") else "Valheim found"]
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
        def work():
            sources, warns = load_sources(fetch=True)
            for w in warns:
                self.log(w)
            self.log("Checking {} plugin source(s) for latest versions...".format(len(sources)))
            try:
                bx = fetch_bepinex_latest()
            except Exception as e:
                bx = None
                self.log("Couldn't reach Thunderstore for BepInEx: {}".format(e))
            results = [None] * len(sources)

            def one(i, s):
                results[i] = dict(fetch_source_manifest(s), origin=s.get("origin"))

            threads = [threading.Thread(target=one, args=(i, s), daemon=True) for i, s in enumerate(sources)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for m in results:
                if m.get("error"):
                    self.log("{}: {}".format(m.get("name"), m["error"]))
            self.ui(lambda: self._apply_refresh(gd, bx, results))

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

        prev_sel = {p["manifest"]["repo"].lower(): p["var"].get() for p in self.plugins}
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.plugins = []

        lf = self.list_frame
        lf.columnconfigure(0, weight=1)
        bold = ("Segoe UI", 9, "bold")
        for i, txt in enumerate(("Mod", "Installed", "Latest", "Tested on Valheim", "Status")):
            ttk.Label(lf, text=txt, font=bold).grid(row=0, column=i, sticky="w", padx=6, pady=(0, 2))
        ttk.Separator(lf).grid(row=1, column=0, columnspan=5, sticky="ew")
        self.empty_label = ttk.Label(lf, foreground=COL_MUTED, text="")

        for m in manifests:
            err = m.get("error")
            installed = installed_manifest(gd, m) if (gd and not err and m.get("install_dir")) else None
            if err:
                state, text, colour = "error", "Can't install: " + err, COL_BAD
            else:
                state, text, colour = self._status_for(gd, m, installed)
            var = tk.BooleanVar(value=prev_sel.get(m["repo"].lower(), state in ("update", "missing_files")))
            var.trace_add("write", lambda *a: self._update_selection_label())

            left = ttk.Frame(lf)
            cb = ttk.Checkbutton(left, text=m.get("name", m["id"]), variable=var)
            cb.pack(anchor="w")
            if err:
                var.set(False)
                cb.state(["disabled"])
            desc = tk.Label(left, text=m.get("description", ""), fg=COL_MUTED, justify="left", anchor="w", wraplength=300)
            if m.get("description"):
                desc.pack(anchor="w", fill="x", padx=(22, 0))
            link = tk.Label(left, text="github.com/{}".format(m["repo"]) + ("  (GitHub release)" if m.get("source") == "release" else "")
                            + ("  - custom" if m.get("origin") == "custom" else ""),
                            fg=COL_INFO, cursor="hand2", anchor="w")
            link.pack(anchor="w", padx=(22, 0))
            link.bind("<Button-1>", lambda e, u=m.get("url") or ("https://github.com/" + m["repo"]): webbrowser.open(u))

            vh = m.get("valheim") or {}
            tested = vh.get("version", "-")
            if vh.get("last_tested"):
                tested += "\n({})".format(vh["last_tested"])
            widgets = [
                left,
                ttk.Label(lf, text=(installed or {}).get("version", "-")),
                ttk.Label(lf, text=m.get("version", "?") if not err else "-"),
                ttk.Label(lf, text=tested),
                tk.Label(lf, text=text, fg=colour, anchor="nw", justify="left", wraplength=self._px(220)),
                ttk.Separator(lf),
            ]
            self.plugins.append({"manifest": m, "installed": installed, "var": var, "state": state,
                                 "widgets": widgets, "desc": desc})

        self._render_rows()
        if gd:
            n_upd = sum(1 for p in self.plugins if p["state"] == "update")
            self.log("Ready. {} mod(s) listed{}.".format(len(self.plugins), ", {} update(s)".format(n_upd) if n_upd else ""))

    def _render_rows(self):
        """Lay out the rows that match the search box, best match first."""
        if not self.plugins and not hasattr(self, "empty_label"):
            return
        q = self.search_var.get().strip()
        ranked = []
        for idx, p in enumerate(self.plugins):
            m = p["manifest"]
            for w in p["widgets"]:
                w.grid_forget()
            score = fuzzy_score(q, [m.get("name", ""), m.get("repo", ""), m.get("url", ""),
                                    "github.com/" + m.get("repo", ""), m.get("id", "")])
            if score is not None:
                ranked.append((-score if q else 0, idx, p))
        ranked.sort(key=lambda t: (t[0], t[1]))

        self._visible = {id(p) for _, _, p in ranked}
        r = 2
        for _, _, p in ranked:
            left, inst, latest, tested, status, sep = p["widgets"]
            left.grid(row=r, column=0, sticky="nsew", padx=6, pady=6)
            inst.grid(row=r, column=1, sticky="nw", padx=6, pady=8)
            latest.grid(row=r, column=2, sticky="nw", padx=6, pady=8)
            tested.grid(row=r, column=3, sticky="nw", padx=6, pady=8)
            status.grid(row=r, column=4, sticky="nw", padx=6, pady=8)
            sep.grid(row=r + 1, column=0, columnspan=5, sticky="ew")
            r += 2

        if hasattr(self, "empty_label"):
            if not self.plugins:
                self.empty_label.configure(text="No plugin sources yet. Use File > Add plugin source.")
                self.empty_label.grid(row=2, column=0, columnspan=5, sticky="w", padx=8, pady=8)
            elif not ranked:
                self.empty_label.configure(text="No mods match \"{}\".".format(q))
                self.empty_label.grid(row=2, column=0, columnspan=5, sticky="w", padx=8, pady=8)
            else:
                self.empty_label.grid_forget()

        self.match_label.configure(text="{} of {} shown".format(len(ranked), len(self.plugins)) if q else "")
        self.canvas.yview_moveto(0)
        self._update_selection_label()
        self.after(30, self._rewrap)

    def _update_selection_label(self):
        sel = [p for p in self.plugins if p["var"].get()]
        visible = getattr(self, "_visible", set())
        hidden = [p for p in sel if id(p) not in visible]
        txt = "{} selected".format(len(sel)) if sel else ""
        if hidden:
            txt += " ({} hidden by search)".format(len(hidden))
        self.sel_label.configure(text=txt)

    def _rewrap(self, event=None):
        """Wrap mod descriptions to the width of the first column."""
        try:
            self.list_frame.update_idletasks()
            bbox = self.list_frame.grid_bbox(0, 0)
            width = max(self._px(160), bbox[2] - self._px(40)) if bbox else self._px(300)
            for p in self.plugins:
                p["desc"].configure(wraplength=width)
        except Exception:
            pass

    def _status_for(self, gd, m, installed):
        compat = self._compat_note(m)
        if installed is None:
            return "missing", "Not installed" + compat, COL_MUTED
        iv = installed.get("version", "unknown")
        if iv == "unknown":
            return "update", "Installed manually (version unknown) - reinstall to track updates" + compat, COL_WARN
        if missing_files(gd, m, installed):
            return "missing_files", "Files missing - reinstall" + compat, COL_BAD
        if parse_version(iv) < parse_version(m.get("version")) or \
                (m.get("source") == "release" and iv != m.get("version") and not parse_version(m.get("version"))):
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

    def _select(self, pred):
        # Bulk selection applies to the rows currently shown by the search filter.
        visible = getattr(self, "_visible", set())
        for p in self.plugins:
            if p["state"] == "error" or id(p) not in visible:
                continue
            p["var"].set(bool(pred(p)))

    # ---------- actions ----------

    def _require_game(self):
        if not self._update_game_status():
            messagebox.showwarning(APP_NAME, "Please choose your Valheim folder first (Step 1).")
            return None
        if is_valheim_running():
            messagebox.showwarning(APP_NAME, "Valheim is running. Please close the game first, then try again.")
            return None
        return self.game_dir.get().strip()

    def install(self):
        gd = self._require_game()
        if not gd:
            return
        chosen = [p["manifest"] for p in self.plugins if p["var"].get() and p["state"] != "error"]
        need_bx = self.bepinex_var.get()
        if chosen and bepinex_installed_version(gd) is None and not need_bx:
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
            (messagebox.showwarning if results["fail"] else messagebox.showinfo)(APP_NAME, "\n\n".join(x for x in msg if x))

        self.run_bg(work, done)

    def _launch_hint(self, gd):
        flavour = game_info(gd).get("flavour")
        if IS_WINDOWS:
            return "Start Valheim through Steam as normal - mods load automatically."
        if flavour == "windows":
            return ("You're running the Windows build through Proton. In Steam, set Valheim's launch options to:\n\n"
                    '    WINEDLLOVERRIDES="winhttp=n,b" %command%')
        if flavour in ("linux", "mac"):
            return "In Steam, set Valheim's launch options to:\n\n    ./start_game_bepinex.sh %command%"
        return ""

    def uninstall(self):
        gd = self._require_game()
        if not gd:
            return
        chosen = [p for p in self.plugins if p["var"].get() and p["installed"] is not None]
        if not chosen:
            messagebox.showinfo(APP_NAME, "Tick at least one installed mod to remove.")
            return
        names = ", ".join(p["manifest"].get("name", p["manifest"]["id"]) for p in chosen)
        if not messagebox.askyesno(APP_NAME, "Remove these mods?\n\n{}\n\n(Their config files in BepInEx/config are kept.)".format(names)):
            return

        def work():
            for p in chosen:
                uninstall_plugin(gd, p["manifest"], p["installed"])
                self.log("Removed {}".format(p["manifest"].get("name", p["manifest"]["id"])))

        self.run_bg(work, lambda err: self.refresh())

    def open_plugins(self):
        gd = self.game_dir.get().strip()
        p = Path(gd) / "BepInEx" / "plugins"
        open_path(p if p.exists() else gd)

    # ---------- self update ----------

    def check_for_update(self, automatic=False):
        result = {}

        def work():
            rel = http_json("https://api.github.com/repos/{}/releases/latest".format(INSTALLER_REPO),
                            accept="application/vnd.github+json")
            result["tag"] = rel.get("tag_name", "")
            result["url"] = rel.get("html_url") or "https://github.com/{}/releases/latest".format(INSTALLER_REPO)
            if automatic and not any(a.get("name") == "ValheimModInstaller.zip" for a in rel.get("assets", [])):
                result["tag"] = ""

        def done(err):
            if err:
                if not automatic:
                    messagebox.showerror(APP_NAME, "Couldn't check for a new version:\n{}".format(err))
                return
            tag = result["tag"]
            if parse_version(tag) <= parse_version(APP_VERSION):
                if not automatic:
                    self.log("Installer is up to date (v{}).".format(APP_VERSION))
                    messagebox.showinfo(APP_NAME, "You have the latest version (v{}).".format(APP_VERSION))
                return
            self.log("New installer version available: {} (you have v{}).".format(tag, APP_VERSION))
            if automatic:
                self.help_menu.entryconfigure(0, label="Update installer to {}...".format(tag),
                                              command=lambda: self.offer_update(tag, result["url"]))
            else:
                self.offer_update(tag, result["url"])

        if automatic:
            def check():
                try:
                    work()
                except Exception:
                    return  # Offline startup is normal.
                self.ui(lambda: done(None))

            threading.Thread(target=check, daemon=True).start()
        elif not self.run_bg(work, done):
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish, then try again.")
        else:
            self.log("Checking for a new installer version...")

    def offer_update(self, tag, url):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish, then try again.")
            return
        launcher = HERE / "launcher.pyw"
        if not launcher.exists():
            if messagebox.askyesno(APP_NAME, "Version {} is available (you have v{}).\n\n"
                                   "Open the download page?".format(tag, APP_VERSION)):
                webbrowser.open(url)
            return
        if messagebox.askyesno(APP_NAME, "Version {} is available (you have v{}).\n\n"
                               "Update now? The installer will close, update itself and reopen."
                               .format(tag, APP_VERSION)):
            kw = {"cwd": str(HERE)}
            if not IS_WINDOWS:
                kw["start_new_session"] = True
            subprocess.Popen([sys.executable, str(launcher)], **kw)
            self.destroy()

    def open_config(self):
        gd = self.game_dir.get().strip()
        p = Path(gd) / "BepInEx" / "config"
        open_path(p if p.exists() else gd)

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
        if "--check-for-updates" in sys.argv:
            app.after_idle(lambda: app.check_for_update(automatic=True))
        app.mainloop()
    except Exception:
        # No console when run with pythonw: surface fatal errors in a dialog and a log file.
        err = traceback.format_exc()
        try:
            p = app_data_dir() / "crash.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(err, encoding="utf-8")
        except Exception:
            pass
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, "The installer crashed:\n\n" + err[-1500:])
        except Exception:
            return 1


if __name__ == "__main__":
    sys.exit(main())
