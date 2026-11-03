#!/usr/bin/env bash
# Valheim Mod Installer launcher (Linux / macOS / Steam Deck desktop mode).
# Finds a Python 3 with Tkinter, offers to install it if missing, then starts the GUI detached.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/launcher.pyw"   # checks for updates, then starts valheim_mod_installer.pyw
TITLE="Valheim Mod Installer"

msg() {
    if [ "$(uname)" = "Darwin" ]; then
        osascript -e "display dialog \"$1\" with title \"$TITLE\" buttons {\"OK\"}" >/dev/null 2>&1 && return
    fi
    command -v zenity  >/dev/null 2>&1 && { zenity --info --title="$TITLE" --text="$1" 2>/dev/null; return; }
    command -v kdialog >/dev/null 2>&1 && { kdialog --title "$TITLE" --msgbox "$1" 2>/dev/null; return; }
    echo "$1"
}

ask() {
    if [ "$(uname)" = "Darwin" ]; then
        osascript -e "display dialog \"$1\" with title \"$TITLE\" buttons {\"No\",\"Yes\"} default button \"Yes\"" 2>/dev/null | grep -q Yes
        return $?
    fi
    command -v zenity  >/dev/null 2>&1 && { zenity --question --title="$TITLE" --text="$1" 2>/dev/null; return $?; }
    command -v kdialog >/dev/null 2>&1 && { kdialog --title "$TITLE" --yesno "$1" 2>/dev/null; return $?; }
    read -r -p "$1 [y/N] " a; [ "$a" = "y" ] || [ "$a" = "Y" ]
}

find_python() {
    for py in python3 python; do
        if command -v "$py" >/dev/null 2>&1 &&
           "$py" -c "import sys, tkinter; assert sys.version_info >= (3, 8)" >/dev/null 2>&1; then
            echo "$py"; return 0
        fi
    done
    return 1
}

install_python() {
    if [ "$(uname)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            brew install python python-tk
        else
            msg "Please install Python 3 from https://www.python.org/downloads/macos/ and run this again."
            open "https://www.python.org/downloads/macos/"
            return 1
        fi
    elif command -v apt-get >/dev/null 2>&1; then pkexec apt-get install -y python3 python3-tk
    elif command -v dnf     >/dev/null 2>&1; then pkexec dnf install -y python3 python3-tkinter
    elif command -v pacman  >/dev/null 2>&1; then pkexec pacman -S --noconfirm python tk
    elif command -v zypper  >/dev/null 2>&1; then pkexec zypper install -y python3 python3-tk
    else
        msg "Please install Python 3 with Tkinter using your package manager, then run this again."
        return 1
    fi
}

PY="$(find_python)"
if [ -z "$PY" ]; then
    if ask "Python 3 with Tkinter is needed to run the installer and wasn't found. Install it now?"; then
        install_python
        PY="$(find_python)"
    fi
fi
if [ -z "$PY" ]; then
    msg "Python 3 with Tkinter wasn't found. Install it (e.g. 'python3-tk' on Debian/Ubuntu) and run this again."
    exit 1
fi

cd "$HERE" || exit 1
nohup "$PY" "$SCRIPT" >/dev/null 2>&1 &
disown 2>/dev/null
exit 0
