# Valheim Mod Installer

A point-and-click installer for Glutoblop's Valheim mods. No terminal, no manual file copying.

## [⬇ Download ValheimModInstaller.zip](https://github.com/Glutoblop/ValheimPlugins/releases/latest/download/ValheimModInstaller.zip)

([all releases](https://github.com/Glutoblop/ValheimPlugins/releases))

## For players

1. Download [**ValheimModInstaller.zip**](https://github.com/Glutoblop/ValheimPlugins/releases/latest/download/ValheimModInstaller.zip) and unzip it anywhere.
2. Double-click the launcher for your system:

   | System | Launcher |
   |---|---|
   | Windows | `Start-Installer.bat` (or right-click `Start-Installer.ps1` > *Run with PowerShell*) |
   | Linux / Steam Deck (desktop mode) / macOS | `start-installer.sh` |

   If Python isn't installed you'll be asked whether to install it (one-time, ~30 MB).
3. **Step 1:** the installer finds Valheim through Steam automatically. If it can't, click **Browse** and pick the folder with `valheim.exe` (in Steam: right-click Valheim > Manage > Browse local files).
4. **Step 2:** BepInEx (the mod loader) is ticked automatically if it's missing or out of date.
5. **Step 3:** tick the mods you want and click **Install / Update selected**.
6. Start Valheim through Steam as normal.

Type in the **Search** box (Ctrl+F) to filter the list. It fuzzy-matches the mod name and its GitHub URL, so `rcpsrch`, `serch` or `glutoblop health` all work.

On Windows, the installer opens first and checks for updates in the background. If a new version is available, use the **Help** menu to apply it and restart. On Linux and macOS, the launcher checks for and applies updates before opening the installer. Updates preserve your `sources.json` and settings.

Open the installer again any time: mods with a newer version say **Update available**, and **Select updates** ticks them all for you. A note appears if a mod was last tested on a different Valheim version than yours.

Uninstalling keeps your mod settings (`BepInEx/config/*.cfg`).

### Linux / macOS notes
- **Proton (Windows build on Linux / Steam Deck):** set Valheim's Steam launch options to `WINEDLLOVERRIDES="winhttp=n,b" %command%`.
- **Native Linux / macOS build:** set the launch options to `./start_game_bepinex.sh %command%`.

The installer reminds you of this after installing.

## Prerequisites it handles

| Prerequisite | How |
|---|---|
| Python 3.8+ with Tkinter | Launcher detects it; on Windows installs Python 3.12 per-user via `winget` (or the python.org installer as fallback); on Linux uses your package manager (`python3-tk`), on macOS Homebrew |
| [BepInExPack_Valheim](https://thunderstore.io/c/valheim/p/denikson/BepInExPack_Valheim/) | Latest version downloaded from Thunderstore and unpacked into the game folder. Existing `BepInEx/config` files are never overwritten |

Valheim mods need nothing else: BepInEx runs on the game's own bundled .NET runtime.

## Adding other plugins (File menu)

The mod list is two lists merged together:

- **Default mods**: [`default-sources.json`](default-sources.json) in this repo, downloaded each time the installer refreshes (cached in the settings folder for offline use). Adding a line there and pushing makes a new mod show up for everyone, with no installer release needed.
- **Your own mods**: **`sources.json`** next to the installer, which the installer and its updates never overwrite.

```json
{
  "sources": [
    { "name": "Jotunn", "url": "https://github.com/Valheim-Modding/Jotunn" }
  ],
  "hidden": ["Glutoblop/HotkeySliding"]
}
```

`sources` adds your own plugins (a name you choose and a GitHub URL). Listing a default repo there overrides its entry (e.g. a different name or `branch`). `hidden` removes default mods from your list. Custom rows are marked "custom". An old plain-list `sources.json` is converted automatically.

- **File > Add plugin source...** (Ctrl+N): enter a name + URL (a GitHub URL on your clipboard is pre-filled). The **+ Add source** button does the same.
- **File > Edit plugin sources...** (Ctrl+E): edit the JSON in-app. It is checked before saving (bad JSON shows the line/column; bad or duplicate URLs are rejected).
- **File > Open sources.json in text editor**, then **File > Reload plugin sources** (F5).

URLs can be `https://github.com/owner/repo`, `.../repo.git`, `.../tree/<branch>`, `git@github.com:owner/repo.git` or just `owner/repo`. An optional `"branch"` key is also accepted.

How a source is installed:
1. If the repo has a `plugin.json` (format below), it's used: version, files, last-tested Valheim version.
2. Otherwise the repo's **latest GitHub release** is used: its tag is the version, and its `.zip` (or `.dll` assets) is installed. Source-code, debug and symbol files are skipped. Zips work in Thunderstore layout (`plugins/`, `config/`), with a `BepInEx/` folder, or flat. Existing config files are never overwritten, and every installed file is recorded so **Uninstall** removes exactly what was added.
3. If neither exists, the row shows why it can't be installed.

GitHub allows 60 unauthenticated API calls an hour, which is plenty for normal use. Only release-based sources use the API.

## How it works (for mod authors)

- Glutoblop's mods each have a `plugin.json` on `main`:

  ```json
  {
    "id": "RecipeSearch",
    "name": "Recipe Search",
    "guid": "jordan.valheim.recipesearch",
    "version": "0.2.0",
    "description": "...",
    "repo": "Glutoblop/RecipeSearch",
    "branch": "main",
    "files": ["dist/RecipeSearch.dll"],
    "install_dir": "BepInEx/plugins/RecipeSearch",
    "requires": { "BepInExPack_Valheim": "5.4.2360" },
    "valheim": { "version": "1.0.21", "network_version": 41, "steam_build": 26249040, "last_tested": "2026-11-03" },
    "changelog": { "0.2.0": "..." }
  }
  ```

- On start the installer downloads every `plugin.json` from `raw.githubusercontent.com` and compares `version` with what's installed. The installed version comes from the DLL's own `[BepInPlugin]` attribute (so hand-copied DLLs are detected too), falling back to the copy of `plugin.json` the installer writes next to the DLL.
- **Installing** downloads each file in `files` into `<Valheim>/<install_dir>/`.
- **Game version** is read from `BepInEx/LogOutput.log` (after the game has been launched once with BepInEx) and the Steam build id from `steamapps/appmanifest_892970.acf`; both are compared against the mod's `valheim` block.
- To **ship an update**: bump the version in `src/Plugin.cs` and `plugin.json`, build (the csproj copies the DLL to `dist/`), commit and push. Everyone sees it next time they open the installer.
- To **add a new mod**: give its repo a `plugin.json` + `dist/` DLL (or just publish GitHub releases) and add it to `default-sources.json` (or your own `sources.json`).

Settings (your Valheim folder) are saved to `%APPDATA%\ValheimModInstaller\settings.json` (`~/.config/ValheimModInstaller` on Linux, `~/Library/Application Support/ValheimModInstaller` on macOS). If the GUI ever crashes, the error is written to `crash.log` in the same folder.

## Publishing releases (maintainer)

Double-click **`release\Release.bat`** (or `release\Release.bat --build-only` to just build). It:

1. rebuilds any sibling plugin whose source is newer than its `dist/` DLL (`dotnet build -c Release`),
2. checks the installer and every plugin repo is committed and pushed (dist DLLs and `plugin.json` are served from `main`),
3. runs `python release/make_release.py`.

`make_release.py` on its own builds `ValheimModInstaller.zip` plus a `<Plugin>.zip` / `<Plugin>.dll` for every sibling plugin folder, and publishes any version that doesn't have a GitHub release yet (needs the GitHub CLI: `winget install GitHub.cli`, then `gh auth login`). Bump `APP_VERSION` in `valheim_mod_installer.pyw`, or a plugin's `plugin.json` + `Plugin.cs`, to publish a new version. `--build-only` just builds the zips into `release/out/`.
