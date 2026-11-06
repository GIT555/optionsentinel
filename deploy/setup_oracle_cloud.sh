#!/usr/bin/env bash
# ==============================================================================
# Option Sentinel - 1-Click Setup for Oracle Cloud Infrastructure (Always Free)
# OS Supported: Ubuntu 22.04 / 24.04 LTS or Oracle Linux 8/9
# ==============================================================================

set -e

echo "=== [1/4] Updating packages & installing Python 3 + Pip ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl ufw

echo "=== [2/4] Installing Python requirements ==="
pip3 install --no-cache-dir -r requirements.txt

echo "=== [3/4] Configuring Linux Firewall (Port 8765) ==="
sudo ufw allow 8765/tcp || true
# For Oracle Linux iptables:
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8765 -j ACCEPT 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || true

echo "=== [4/4] Installing & Starting systemd 24/7 background service ==="
sudo cp deploy/option-sentinel.service /etc/systemd/system/
sudo sed -i "s|/home/ubuntu/OptionTradingDashboard|$(pwd)|g" /etc/systemd/system/option-sentinel.service
sudo sed -i "s|User=ubuntu|User=$(whoami)|g" /etc/systemd/system/option-sentinel.service

sudo systemctl daemon-reload
sudo systemctl enable option-sentinel
sudo systemctl restart option-sentinel

echo ""
echo "================================================================================"
echo " SUCCESS: Option Sentinel is now running 24/7 in the background!"
echo " Check status: sudo systemctl status option-sentinel"
echo " View live logs: sudo journalctl -u option-sentinel -f"
echo " Dashboard URL : http://$(curl -s ifconfig.me):8765/"
echo "================================================================================"
