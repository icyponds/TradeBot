# TradeBot VPS Setup & Deployment Guide

## 1. Choosing a VPS Provider
For algorithmic trading, **reliability** and **latency** are your top priorities.

### Best Options
1.  **AWS (Amazon Web Services)** (Recommended for Pro)
    *   **Why**: Hyperliquid and many exchanges host in AWS `ap-northeast-1` (Tokyo) or `us-east-1` (N. Virginia). Co-location minimizes ping.
    *   **Instance**: `t3.medium` or `c6i.large` (Compute Optimized).
    *   **Cost**: $20-40/mo.

2.  **Vultr / DigitalOcean** (Recommended for Ease)
    *   **Why**: Simple UI, high-frequency compute instances available.
    *   **Location**: Tokyo or Singapore (if target is Asian-based execution) or generic US.
    *   **Instance**: High Frequency Compute (NVMe), 2 vCPU, 4GB RAM.
    *   **Cost**: ~$24/mo.

3.  **Hetzner** (Budget Performance)
    *   **Why**: Incredible performance/dollar (Ryzen CPUs).
    *   **Cons**: Servers primarily in Germany/Finland (latency risk if exchange is in Asia/US).
    *   **Cost**: ~$10/mo.

**Recommendation**: Start with **Vultr (High Frequency)** or **AWS** in Tokyo region if you want to optimize for Hyperliquid (which is often Asia-centric).

---

## 2. Server Configuration (The "Box" Setup)
Once you have your fresh Ubuntu 22.04/24.04 server IP (`1.2.3.4`):

### A. Initial Security
```bash
# SSH into as root
ssh root@1.2.3.4

# Update system
apt update && apt upgrade -y

# Install tools
apt install -y docker.io docker-compose git htop unzip fail2ban chrony

# Enable Docker
systemctl enable --now docker

# Create a non-root user (optional but safer)
useradd -m -s /bin/bash tradebot
usermod -aG docker tradebot
passwd tradebot
```

### B. Directory Setup & Permissions
We need the folder structure that `rsync` expects.
```bash
# Switch to user
su - tradebot

# Create directories
mkdir -p ~/tradebot/src
mkdir -p ~/tradebot/data
mkdir -p ~/tradebot/logs
mkdir -p ~/tradebot/scripts

# Create blank database (bot will auto-initialize schema, but good to have permission set)
touch ~/tradebot/data/trades.db
```

### C. SSH Keys (The "Key" to Deployment)
You need an SSH key pair specifically for GitHub Actions to log in.

**On your LOCAL Mac:**
```bash
# Generate a new key pair (no passphrase for automation)
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ./deploy_key

# Output:
# ./deploy_key (PRIVATE - Goes to GitHub)
# ./deploy_key.pub (PUBLIC - Goes to VPS)
```

**On the VPS:**
```bash
# Add the PUBLIC key content to authorized_keys
mkdir -p ~/.ssh
nano ~/.ssh/authorized_keys
# PASTE CONTENT OF deploy_key.pub HERE
chmod 600 ~/.ssh/authorized_keys
```

---

## 3. GitHub Secrets Configuration
Go to your GitHub Repo -> **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**.

Add these 3 secrets:
1.  **`SSH_HOST`**: Your VPS IP address (e.g., `1.2.3.4`).
2.  **`SSH_USER`**: The user you created (e.g., `tradebot` or `root`).
3.  **`SSH_KEY`**: The **PRIVATE** key content from your local `./deploy_key` file.
    *   *Copy everything including `-----BEGIN OPENSSH PRIVATE KEY-----`*.

---

## 4. Considerations & Gotchas ("What you haven't thought of")

### A. Time Synchronization (NTP)
Crypto/Exchange APIs rely on precise timestamps. If your server clock drifts by >1 second, your requests will fail (`Invalid Timestamp`).
*   **Fix**: Ensure `chrony` is running.
    ```bash
    systemctl status chrony
    timedatectl set-ntp on
    ```

### B. Database Backups
Your deployment explicitly **excludes** `data/` to protect it. But if the server disk dies, your data is gone.
*   **Fix**: Add a simple Cron job on the VPS to backup the DB.
    ```bash
    # Crontab -e
    0 0 * * * cp ~/tradebot/data/trades.db ~/tradebot/data/trades_backup_$(date +\%F).db
    # Better: Use rclone to push to AWS S3 / Google Drive.
    ```

### C. Logs Management
`docker-compose` logs can eat disk space forever.
*   **Fix**: In `docker-compose.yml`, we configure logging drivers (already standard in many setups, but good to verify). Or simply use `logrotate` on the host.

### D. The ".env" File
GitHub Actions does **NOT** copy your `.env` file (it's in the exclude list for security).
*   **Action**: You must manually scp/create the `.env` file on the VPS **once**.
    ```bash
    scp .env tradebot@1.2.3.4:~/tradebot/.env
    ```

### E. First Run
The first time you deploy:
1.  Push code to GitHub (Actions will sync files).
2.  SSH to VPS.
3.  Run `docker-compose up -d --build`.
4.  Subsequent updates happen automatically via Hot Reload (rsync), but the container needs to be running first.
