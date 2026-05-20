#!/usr/bin/env bash
# Shanty Monitor - Agent Installer
# Deploys the agent to one or more hosts via SSH (passwordless assumed)
#
# Usage:
#   ./install.sh <host1> [host2] [host3] ...
#   ./install.sh --all        # reads agent_hosts from collector/config.yaml (requires python3+pyyaml)
#
# What it does per host:
#   1. Copies agent.py and agent.yaml to /opt/shanty-monitor/
#   2. Installs python3-psutil if possible
#   3. Installs /etc/shanty-monitor/agent.yaml (won't overwrite existing)
#   4. Installs and enables shanty-agent.service
#   5. Starts the agent

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$SCRIPT_DIR/agent"
REMOTE_DIR="/opt/shanty-monitor"
CONF_DIR="/etc/shanty-monitor"
SERVICE_FILE="$AGENT_DIR/shanty-agent.service"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*"; }

install_on_host() {
    local HOST="$1"
    info "=== Installing agent on $HOST ==="

    # Create directories
    ssh "$HOST" "sudo mkdir -p $REMOTE_DIR $CONF_DIR" || { error "SSH failed for $HOST"; return 1; }

    # Copy agent files
    scp "$AGENT_DIR/agent.py" "$HOST:/tmp/shanty_agent.py"
    ssh "$HOST" "sudo mv /tmp/shanty_agent.py $REMOTE_DIR/agent.py && sudo chmod +x $REMOTE_DIR/agent.py"

    # Copy config only if it doesn't already exist
    if ssh "$HOST" "test -f $CONF_DIR/agent.yaml 2>/dev/null"; then
        warn "$HOST: $CONF_DIR/agent.yaml already exists — not overwriting. Check token/URL manually."
    else
        scp "$AGENT_DIR/agent.yaml" "$HOST:/tmp/shanty_agent.yaml"
        ssh "$HOST" "sudo mv /tmp/shanty_agent.yaml $CONF_DIR/agent.yaml"
        info "$HOST: Config installed. Edit $CONF_DIR/agent.yaml to set token/hostname if needed."
    fi

    # Try to install psutil (non-fatal — agent has /proc fallbacks)
    ssh "$HOST" "sudo apt-get install -y -q python3-psutil 2>/dev/null || \
                 sudo pip3 install psutil --break-system-packages 2>/dev/null || \
                 true" && info "$HOST: psutil installed" || warn "$HOST: psutil not installed (using /proc fallback)"

    # Install systemd service
    scp "$SERVICE_FILE" "$HOST:/tmp/shanty-agent.service"
    ssh "$HOST" "sudo mv /tmp/shanty-agent.service /etc/systemd/system/shanty-agent.service && \
                 sudo systemctl daemon-reload && \
                 sudo systemctl enable shanty-agent && \
                 sudo systemctl restart shanty-agent"

    # Check status
    if ssh "$HOST" "sudo systemctl is-active shanty-agent >/dev/null 2>&1"; then
        info "$HOST: Agent is running ✓"
    else
        warn "$HOST: Agent may not be running. Check: ssh $HOST 'sudo journalctl -u shanty-agent -n 20'"
    fi
}

# Parse args
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 <host1> [host2] ..."
    echo "       $0 --all   # reads hosts from collector/config.yaml"
    exit 1
fi

if [[ "$1" == "--all" ]]; then
    # Extract agent host IPs/names from config.yaml
    HOSTS=$(python3 -c "
import yaml, sys
with open('$SCRIPT_DIR/collector/config.yaml') as f:
    cfg = yaml.safe_load(f)
for h in cfg.get('agent_hosts', []):
    print(h['ip'])
" 2>/dev/null)
    if [[ -z "$HOSTS" ]]; then
        error "Could not read hosts from config.yaml (python3/pyyaml required)"
        exit 1
    fi
    for H in $HOSTS; do install_on_host "$H"; done
else
    for H in "$@"; do install_on_host "$H"; done
fi

info "Done."
