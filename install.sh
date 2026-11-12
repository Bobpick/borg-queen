#!/usr/bin/env bash
# ==============================================================================
# BORG QUEEN CONTROL — INSTALLER
# Pickett Applied Technologies Laboratories (PATL)
# Automated AI Agent Hive Mind for Obsidian Second Brain
# ==============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
PURPLE='\033[0;35m'
NC='\033[0m'

echo -e "${GREEN}"
cat << "EOF"
  ██████╗  ██████╗ ██████╗  ██████╗      ██████╗ ██╗   ██╗███████╗███████╗███╗   ██╗
  ██╔══██╗██╔═══██╗██╔══██╗██╔════╝     ██╔═══██╗██║   ██║██╔════╝██╔════╝████╗  ██║
  ██████╔╝██║   ██║██████╔╝██║  ███╗    ██║   ██║██║   ██║█████╗  █████╗  ██╔██╗ ██║
  ██╔══██╗██║   ██║██╔══██╗██║   ██║    ██║▄▄ ██║██║   ██║██╔══╝  ██╔══╝  ██║╚██╗██║
  ██████╔╝╚██████╔╝██║  ██║╚██████╔╝    ╚██████╔╝╚██████╔╝███████╗███████╗██║ ╚████║
  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝      ╚══▀▀═╝  ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═══╝
                      P I C K E T T   A P P L I E D   T E C H
EOF
echo -e "${NC}"
echo -e "${CYAN}==============================================================================${NC}"
echo -e "${CYAN} Installing Borg Queen Control for Pickett Applied Technologies Laboratories...${NC}"
echo -e "${CYAN}==============================================================================${NC}"

TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME=$(eval echo "~$TARGET_USER")
APP_DIR="$TARGET_HOME/.borg_queen"
SECOND_BRAIN_DIR="$TARGET_HOME/Documents/PATL/Lab_Second_brain"
VAULT_DIR="$SECOND_BRAIN_DIR/wiki"
INBOX_DIR="$SECOND_BRAIN_DIR/inbox"
DEFAULT_PORT=8082
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo -e "\n${BLUE}[*] Target Environment Configuration:${NC}"
echo -e "    User:           $TARGET_USER"
echo -e "    App Directory:  $APP_DIR"
echo -e "    Wiki Vault:     $VAULT_DIR (5,000+ notes knowledge base)"
echo -e "    Studies Inbox:  $INBOX_DIR (Daily intake destination)"
echo -e "    Default Port:   $DEFAULT_PORT (bypassing Docker on 8080)"

# 1. Verify Python 3 & SQLite FTS5
echo -e "\n${BLUE}[1/6] Verifying Python 3 & SQLite FTS5...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERROR] Python 3 is required.${NC}"
    exit 1
fi
python3 -c "import sqlite3; con = sqlite3.connect(':memory:'); con.execute('CREATE VIRTUAL TABLE t USING fts5(c);')" 2>/dev/null || {
    echo -e "${RED}[ERROR] Python sqlite3 must have FTS5 support enabled.${NC}"
    exit 1
}
echo -e "${GREEN}[OK] Python 3 with FTS5 engine verified.${NC}"

# Install aiohttp if missing
if ! python3 -c "import aiohttp, pymupdf" 2>/dev/null; then
    echo -e "${YELLOW}[!] Installing aiohttp and pymupdf...${NC}"
    python3 -m pip install --user -r "$SCRIPT_DIR/requirements.txt" || python3 -m pip install --user aiohttp pymupdf
fi
echo -e "${GREEN}[OK] Asynchronous web framework ready.${NC}"

# 2. Check Ollama & Model Roster
echo -e "\n${BLUE}[2/6] Checking Ollama Engine & Model Roster...${NC}"
if curl -s http://localhost:11434/api/tags &> /dev/null; then
    echo -e "${GREEN}[OK] Ollama is active on http://localhost:11434${NC}"
    MODELS_JSON=$(curl -s http://localhost:11434/api/tags)
    for M in "qwen2.5vl:7b" "olmo-3:7b-think" "zarigata/unfiltered-llama3:latest" "cogito:32b" "cogito:14b-16k" "borg_drone:32b" "borg_queen:72b"; do
        if echo "$MODELS_JSON" | grep -q "\"$M\""; then
            echo -e "    ${GREEN}✔${NC} Found model: $M"
        else
            echo -e "    ${YELLOW}⚠${NC} Optional model: $M"
        fi
    done
else
    echo -e "${YELLOW}[!] Ollama is not currently running on localhost:11434.${NC}"
    echo -e "    The system will run in Autonomous Simulation mode until you start Ollama."
fi

# 3. Create Second Brain Directories
echo -e "\n${BLUE}[3/6] Setting up Vault directories...${NC}"
mkdir -p "$APP_DIR/static"
mkdir -p "$VAULT_DIR"
mkdir -p "$INBOX_DIR"
mkdir -p "$VAULT_DIR/daily_briefings"
echo -e "${GREEN}[OK] Ensured presence of:${NC}"
echo -e "     - $VAULT_DIR"
echo -e "     - $INBOX_DIR"

# 4. Deploy Files
echo -e "\n${BLUE}[4/6] Deploying Borg Queen Core to $APP_DIR...${NC}"
cp "$SCRIPT_DIR/server.py" "$APP_DIR/server.py"
cp "$SCRIPT_DIR/pdf_ingest.py" "$APP_DIR/pdf_ingest.py"
cp -r "$SCRIPT_DIR/static/"* "$APP_DIR/static/" 2>/dev/null || true
cp -r "$SCRIPT_DIR/borg_queen.zip" "$APP_DIR/" 2>/dev/null || true

# 5. Populate SQLite FTS5 Index from Wiki
echo -e "\n${BLUE}[5/6] Populating SQLite FTS5 Knowledge Fabric from Wiki...${NC}"
if [ -d "$VAULT_DIR" ]; then
    python3 "$APP_DIR/server.py" --index || true
else
    echo -e "${YELLOW}[!] $VAULT_DIR not yet found, index will populate on first start.${NC}"
fi

# 6. Install to Linux App Launcher Menu & Desktop
echo -e "\n${BLUE}[6/6] Installing to Linux Application Menu & Desktop...${NC}"

# A. Icon installation
ICON_DIR="$TARGET_HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$ICON_DIR"
ICON_SRC="$APP_DIR/static/borg-queen-icon.png"
if [ ! -f "$ICON_SRC" ]; then ICON_SRC="$APP_DIR/static/Borg_Queen.png"; fi
cp "$ICON_SRC" "$ICON_DIR/borg-queen.png"
cp "$ICON_SRC" "$TARGET_HOME/.local/share/icons/borg-queen.png" 2>/dev/null || true

# B. CLI and App launcher script (~/.local/bin/borg-queen)
BIN_DIR="$TARGET_HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat << 'EOF' > "$BIN_DIR/borg-queen"
#!/usr/bin/env bash
TARGET_PORT=${BORG_QUEEN_PORT:-8082}
APP_DIR="$HOME/.borg_queen"

export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_NUM_PARALLEL=2
export OLLAMA_KEEP_ALIVE=1h
export OLLAMA_MAX_LOADED_MODELS=2
export OLLAMA_MAX_QUEUE=4
export BORG_QUEEN_PORT=$TARGET_PORT

# Check if --index flag passed
if [ "$1" = "--index" ]; then
    python3 "$APP_DIR/server.py" --index
    exit 0
fi

# Preload reasoning core into VRAM/RAM: borg-queen --preload [model]
if [ "$1" = "--preload" ]; then
    python3 "$APP_DIR/server.py" --preload "${2:-cogito:32b}"
    exit 0
fi

# If launched as app window (--app)
if [ "$1" = "--app" ]; then
    # Start server in background if not already active
    if ! curl -s "http://localhost:$TARGET_PORT/api/status" &>/dev/null; then
        nohup python3 "$APP_DIR/server.py" --port "$TARGET_PORT" > "$APP_DIR/server.log" 2>&1 &
        # Wait up to 5 seconds for port to open
        for i in {1..10}; do
            if curl -s "http://localhost:$TARGET_PORT/api/status" &>/dev/null; then
                break
            fi
            sleep 0.5
        done
    fi

    # Launch dedicated App Window
    URL="http://localhost:$TARGET_PORT"
    if command -v google-chrome &> /dev/null; then
        google-chrome --app="$URL" --class=borg-queen --name="Borg Queen Control" &
    elif command -v brave-browser &> /dev/null; then
        brave-browser --app="$URL" &
    elif command -v chromium-browser &> /dev/null; then
        chromium-browser --app="$URL" &
    elif command -v chromium &> /dev/null; then
        chromium --app="$URL" &
    elif command -v microsoft-edge &> /dev/null; then
        microsoft-edge --app="$URL" &
    else
        xdg-open "$URL" &
    fi
    exit 0
fi

# Default: Run interactively in terminal
cd "$APP_DIR"
python3 "$APP_DIR/server.py" "$@"
EOF
chmod +x "$BIN_DIR/borg-queen"

# C. Install .desktop to ~/.local/share/applications/ (Linux App Launcher Menu)
APP_MENU_DIR="$TARGET_HOME/.local/share/applications"
mkdir -p "$APP_MENU_DIR"
cat << EOF > "$APP_MENU_DIR/borg-queen.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=Borg Queen Control
GenericName=AI Agent Hive Mind
Comment=Pickett Applied Technologies Laboratories Knowledge Fabric
Exec=$BIN_DIR/borg-queen --app
Icon=$ICON_DIR/borg-queen.png
Terminal=false
Categories=Science;Development;Office;Utility;
Keywords=AI;Hive;Obsidian;PATL;Research;Borg;Queen;
StartupNotify=true
StartupWMClass=borg-queen
EOF
chmod +x "$APP_MENU_DIR/borg-queen.desktop"

# D. Update desktop database if available
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "$APP_MENU_DIR" 2>/dev/null || true
fi

# E. Also create Desktop shortcut if ~/Desktop exists
DESKTOP_DIR="$TARGET_HOME/Desktop"
if [ -d "$DESKTOP_DIR" ]; then
    cp "$APP_MENU_DIR/borg-queen.desktop" "$DESKTOP_DIR/Borg_Queen_Control.desktop"
    chmod +x "$DESKTOP_DIR/Borg_Queen_Control.desktop"
    echo -e "${GREEN}[OK] Desktop shortcut created at ~/Desktop/Borg_Queen_Control.desktop${NC}"
fi

echo -e "${GREEN}[OK] Borg Queen Control installed to your Application Menu!${NC}"

echo -e "\n${GREEN}==============================================================================${NC}"
echo -e "${GREEN} INSTALLATION SUCCESSFUL — BORG QUEEN CONTROL READY${NC}"
echo -e "${GREEN}==============================================================================${NC}"
echo -e "Ways to launch:"
echo -e "  1. ${PURPLE}Application Menu:${NC} Open your app launcher (Super key) and search ${CYAN}Borg Queen Control${NC}"
echo -e "  2. ${PURPLE}Terminal:${NC}         Run ${CYAN}borg-queen${NC} or ${CYAN}borg-queen --app${NC}"
echo -e "  3. ${PURPLE}Re-index Vault:${NC}   Run ${CYAN}borg-queen --index${NC}"
echo -e "  3b. ${PURPLE}Preload 32B Core:${NC}  Run ${CYAN}borg-queen --preload${NC} (pins cogito:32b HOT in VRAM/RAM)"
echo -e "  4. ${PURPLE}Browser:${NC}          Open ${CYAN}http://localhost:${DEFAULT_PORT}${NC}"
echo -e "${CYAN}==============================================================================${NC}\n"
