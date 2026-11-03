# Valheim Mod Installer

A point-and-click installer for Glutoblop's Valheim mods. No terminal, no manual file copying.

## For players

1. Download this folder (or the zip you were sent) and unzip it anywhere.
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

## How it works (for mod authors)

- `catalog.json` lists the mod repos. Each repo has a `plugin.json` on `main`:

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
- To **add a new mod**: give its repo a `plugin.json` + `dist/` DLL and add it to `catalog.json`.

Settings (your Valheim folder) are saved to `%APPDATA%\ValheimModInstaller\settings.json` (`~/.config/ValheimModInstaller` on Linux, `~/Library/Application Support/ValheimModInstaller` on macOS). If the GUI ever crashes, the error is written to `crash.log` in the same folder.
