"""
Build the release package for the Valheim Mod Installer and publish it as a GitHub release with
the GitHub CLI (gh). Plugins are released by their own release scripts (../<Plugin>/release/).

    python release/make_release.py              # build + publish if this version isn't released yet
    python release/make_release.py --build-only # just build the zip into release/out/

Asset (name is stable, so .../releases/latest/download/ValheimModInstaller.zip always works):
    ValheimPlugins  v<APP_VERSION>  ValheimModInstaller.zip

A release whose tag already exists is skipped; release/release.ps1 bumps APP_VERSION for you.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent           # the ValheimPlugins repo
OUT = ROOT / "release" / "out"
INSTALLER_REPO = "Glutoblop/ValheimPlugins"
INSTALLER_FILES = ["Start-Installer.bat", "Start-Installer.ps1", "start-installer.sh",
                   "launcher.pyw", "valheim_mod_installer.pyw", "sources.json", "README.md"]


def find_gh():
    gh = shutil.which("gh")
    if gh:
        return gh
    for p in (Path(r"C:\Program Files\GitHub CLI\gh.exe"), Path(r"C:\Program Files (x86)\GitHub CLI\gh.exe")):
        if p.exists():
            return str(p)
    return None


def installer_version():
    src = (ROOT / "valheim_mod_installer.pyw").read_text(encoding="utf-8")
    return re.search(r'^APP_VERSION = "([^"]+)"', src, re.M).group(1)


def add_file(z, src, arcname, executable=False):
    info = zipfile.ZipInfo(arcname, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16
    data = Path(src).read_bytes()
    if arcname.endswith(".sh"):
        data = data.replace(b"\r\n", b"\n")  # must run on Linux/macOS
    z.writestr(info, data)


def build_installer():
    version = installer_version()
    zpath = OUT / "ValheimModInstaller.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in INSTALLER_FILES:
            add_file(z, ROOT / f, "ValheimModInstaller/" + f, executable=f.endswith(".sh"))
    notes = (
        "## Valheim Mod Installer {v}\n\n"
        "**Download [`ValheimModInstaller.zip`](https://github.com/{repo}/releases/latest/download/ValheimModInstaller.zip)**, "
        "unzip it anywhere and double-click:\n\n"
        "- **Windows:** `Start-Installer.bat`\n"
        "- **Linux / Steam Deck / macOS:** `start-installer.sh`\n\n"
        "Python is installed for you if it's missing (you're asked first). Then pick your Valheim folder, "
        "tick the mods you want and click **Install / Update**. BepInEx is installed automatically.\n\n"
        "Included mods: {mods}. Add any other GitHub-hosted plugin from **File > Add plugin source**."
    ).format(v=version, repo=INSTALLER_REPO,
             mods=", ".join(s["name"] for s in json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))))
    return {"repo": INSTALLER_REPO, "tag": "v" + version, "title": "Valheim Mod Installer v" + version,
            "assets": [zpath], "notes": notes}



def gh_run(gh, args, check=True):
    return subprocess.run([gh] + args, capture_output=True, text=True, check=check)


def publish(gh, rel):
    exists = gh_run(gh, ["release", "view", rel["tag"], "--repo", rel["repo"]], check=False).returncode == 0
    if exists:
        print("  {} {} already released - skipped (bump the version to publish a new one)".format(rel["repo"], rel["tag"]))
        return
    notes_file = OUT / "notes.md"
    notes_file.write_text(rel["notes"], encoding="utf-8")
    r = gh_run(gh, ["release", "create", rel["tag"], *[str(a) for a in rel["assets"]],
                    "--repo", rel["repo"], "--target", "main", "--title", rel["title"],
                    "--notes-file", str(notes_file), "--latest"], check=False)
    if r.returncode != 0:
        raise SystemExit("gh release create failed for {}:\n{}".format(rel["repo"], r.stderr.strip()))
    print("  published {}".format(r.stdout.strip()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    releases = [build_installer()]
    for r in releases:
        print("built {:<28} {:<8} {}".format(r["repo"], r["tag"], ", ".join(a.name for a in r["assets"])))
    if args.build_only:
        print("\nPackages are in", OUT)
        return

    gh = find_gh()
    if not gh:
        raise SystemExit("GitHub CLI not found. Install it (winget install GitHub.cli) and run: gh auth login")
    if gh_run(gh, ["auth", "status"], check=False).returncode != 0:
        raise SystemExit("GitHub CLI isn't logged in. Run: gh auth login")
    print("\nPublishing...")
    for r in releases:
        publish(gh, r)
    print("\nDone. Share: https://github.com/{}/releases/latest".format(INSTALLER_REPO))


if __name__ == "__main__":
    sys.exit(main())
