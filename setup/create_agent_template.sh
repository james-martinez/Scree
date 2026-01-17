#!/bin/bash
#
# Proxmox Agent VM Template Setup Script
#
# This script prepares an Ubuntu VM to be used as a template for autonomous coding agents.
# Run this inside the VM that will become the template.
#
# Usage:
#   1. Create a new VM in Proxmox with Ubuntu 22.04
#   2. SSH into the VM
#   3. Run: curl -sSL https://your-server/create_agent_template.sh | bash
#   4. Shutdown the VM
#   5. Convert to template in Proxmox UI
#
# Requirements:
#   - Ubuntu 22.04 LTS
#   - At least 20GB disk space
#   - Internet access for package installation
#

set -e

echo "=========================================="
echo "  Autonomous Coding Agent Template Setup"
echo "=========================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Configuration
AGENT_USER="agent"
AGENT_HOME="/home/${AGENT_USER}"
AGENT_DIR="/opt/agent"
NODE_VERSION="20"
GO_VERSION="1.22.0"
PYTHON_VERSION="3.11"

echo "[1/10] Updating system packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

echo "[2/10] Installing base dependencies..."
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl wget git jq \
    build-essential gcc g++ make cmake \
    python3 python3-pip python3-venv \
    ripgrep fd-find \
    unzip zip tar \
    openssh-client \
    ca-certificates gnupg \
    qemu-guest-agent \
    cloud-init cloud-utils \
    sudo

echo "[3/10] Installing Node.js ${NODE_VERSION}..."
# Install Node.js via NodeSource
curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash -
apt-get install -y nodejs

# Install npm packages globally
npm install -g npm@latest
npm install -g yarn pnpm

echo "[4/10] Installing Go ${GO_VERSION}..."
wget -q "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -O /tmp/go.tar.gz
rm -rf /usr/local/go
tar -C /usr/local -xzf /tmp/go.tar.gz
rm /tmp/go.tar.gz

# Add Go to system-wide PATH
cat > /etc/profile.d/go.sh << 'EOF'
export PATH=$PATH:/usr/local/go/bin
export GOPATH=$HOME/go
export PATH=$PATH:$GOPATH/bin
EOF

echo "[5/10] Installing Rust..."
# Install Rust system-wide
export RUSTUP_HOME=/opt/rust
export CARGO_HOME=/opt/rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path

# Add Rust to system-wide PATH
cat > /etc/profile.d/rust.sh << 'EOF'
export RUSTUP_HOME=/opt/rust
export CARGO_HOME=/opt/rust
export PATH=$PATH:/opt/rust/bin
EOF

echo "[6/10] Creating agent user..."
# Create agent user if not exists
if ! id "${AGENT_USER}" &>/dev/null; then
    useradd -m -s /bin/bash "${AGENT_USER}"
fi

# Configure sudo for agent (limited commands)
cat > /etc/sudoers.d/agent << 'EOF'
# Allow agent to run specific commands without password
agent ALL=(ALL) NOPASSWD: /usr/bin/apt-get update
agent ALL=(ALL) NOPASSWD: /usr/bin/apt-get install *
agent ALL=(ALL) NOPASSWD: /usr/bin/npm install *
agent ALL=(ALL) NOPASSWD: /usr/bin/pip3 install *
EOF
chmod 440 /etc/sudoers.d/agent

echo "[7/10] Setting up agent directory..."
mkdir -p ${AGENT_DIR}
mkdir -p ${AGENT_HOME}/workspace
chown -R ${AGENT_USER}:${AGENT_USER} ${AGENT_DIR}
chown -R ${AGENT_USER}:${AGENT_USER} ${AGENT_HOME}

# Create Python virtual environment for agent
python3 -m venv ${AGENT_DIR}/venv
source ${AGENT_DIR}/venv/bin/activate
pip install --upgrade pip
pip install openai aiohttp requests

# Copy agent runtime script (this will be injected by cloud-init in production)
cat > ${AGENT_DIR}/main.py << 'AGENT_SCRIPT'
#!/usr/bin/env python3
"""Placeholder - Real agent script will be injected via cloud-init or downloaded from orchestrator."""
print("Agent script not yet configured. This placeholder will be replaced at runtime.")
AGENT_SCRIPT

chmod +x ${AGENT_DIR}/main.py
chown -R ${AGENT_USER}:${AGENT_USER} ${AGENT_DIR}

echo "[8/10] Configuring cloud-init..."
# Configure cloud-init for dynamic configuration
cat > /etc/cloud/cloud.cfg.d/99_agent.cfg << 'EOF'
# Cloud-init configuration for agent VMs
datasource_list: [ NoCloud, ConfigDrive, None ]

# Preserve hostname set by Proxmox
preserve_hostname: false

# Allow cloud-init to run on every boot for reconfiguration
cloud_init_modules:
  - seed_random
  - bootcmd
  - write-files
  - growpart
  - resizefs
  - set_hostname
  - update_hostname
  - update_etc_hosts
  - ca-certs
  - rsyslog
  - users-groups
  - ssh

cloud_config_modules:
  - emit_upstart
  - disk_setup
  - mounts
  - ssh-import-id
  - locale
  - set-passwords
  - grub-dpkg
  - apt-pipelining
  - apt-configure
  - package-update-upgrade-install
  - timezone
  - disable-ec2-metadata
  - runcmd
  - byobu

cloud_final_modules:
  - rightscale_userdata
  - scripts-vendor
  - scripts-per-once
  - scripts-per-boot
  - scripts-per-instance
  - scripts-user
  - ssh-authkey-fingerprints
  - keys-to-console
  - phone-home
  - final-message
  - power-state-change
EOF

echo "[9/10] Enabling services..."
# Enable QEMU guest agent for Proxmox communication
systemctl enable qemu-guest-agent
systemctl start qemu-guest-agent

# Enable cloud-init
systemctl enable cloud-init

echo "[10/10] Cleaning up for templating..."
# Clean package cache
apt-get clean
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*

# Clean cloud-init for fresh runs
cloud-init clean --logs

# Clear bash history
cat /dev/null > ~/.bash_history
history -c

# Clear SSH host keys (will regenerate on first boot)
rm -f /etc/ssh/ssh_host_*

# Clear machine ID (will regenerate)
echo "" > /etc/machine-id
rm -f /var/lib/dbus/machine-id

echo ""
echo "=========================================="
echo "  Template Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Shutdown this VM: sudo shutdown -h now"
echo "  2. In Proxmox UI, right-click the VM"
echo "  3. Select 'Convert to template'"
echo "  4. Note the VMID for your pipeline configuration"
echo ""
echo "The template includes:"
echo "  - Python ${PYTHON_VERSION} with venv"
echo "  - Node.js ${NODE_VERSION} with npm/yarn/pnpm"
echo "  - Go ${GO_VERSION}"
echo "  - Rust (latest stable)"
echo "  - Git, build tools, ripgrep"
echo "  - QEMU guest agent (for Proxmox communication)"
echo "  - Cloud-init (for runtime configuration)"
echo "  - Agent user with limited sudo access"
echo ""
