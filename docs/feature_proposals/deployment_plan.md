# Deployment & CI/CD Strategy (Hot Reload Edition)

**Goal**: Deploy `TradeBot` to production with **Zero Downtime Updates** for strategies, utilizing an "Action-First" workflow.

## 1. Hosting Architecture
### VPS + Docker Compose (Bind Mounts)
*   **Host Path**: `/opt/tradebot/src`
*   **Container Path**: `/app/src`
*   **Mechanism**: Docker Volume (Bind Mount) for live code updates.

### Database Persistence & Safety
*   **Location**: The SQLite database (`trades.db`) will reside in `./data` on the **Host VPS filesystem**.
*   **Protection**: The `deploy-sync` pipeline will explicitly **EXCLUDE** the `data/` directory to prevent overwriting live data with empty local files.
*   **Backup**: We will add a cron job on the VPS to backup the DB daily.

## 2. Implementation Phases (Reordered)

### Phase 1: CI/CD Pipeline Definition (Priority)
**Status**: ✅ Complete
*   [x] Create `.github/workflows/deploy-sync.yml`
    *   Trigger: Push to `main`.
    *   Action: `rsync` `src/`, `scripts/`, `requirements.txt` to VPS.
    *   **CRITICAL**: Exclude `data/*`, `.env`.

### Phase 2: Infrastructure Configuration
**Status**: ✅ Complete
*   [x] Create `Dockerfile` (Python 3.11 optimized).
*   [x] Create `docker-compose.yml`:
    *   Volumes: `./src:/app/src`
    *   Restart Policy: `always`.

### Phase 3: Bot Code Patching (Hot Reload)
**Status**: ✅ Complete
*   [x] Update `StrategyManager.reconcile_strategies`:
    *   Added `importlib.reload(module)` logic to force code updates without restart.


### Phase 4: Provisioning & Go Live
**Objective**: Switch on the lights.
*   [ ] Provision VPS (Ubuntu 22.04).
*   [ ] Configure GitHub Repo Secrets.
*   [ ] Initial Push (Trigger Phase 1).
*   [ ] SSH to VPS and run `docker-compose up -d`.

### Phase 5: Data Synchronization (Production -> Local)
**Objective**: Analyze live performance locally.
*   [ ] Create `scripts/sync_db_down.sh`:
    *   Uses `scp` to download `trades.db` from VPS to local `data/trades.db`.
    *   **Note**: This overwrites local development data with production truth.
*   [ ] Workflow:
    1.  Run `scripts/sync_db_down.sh`.
    2.  Run `analyze_backtest_results.py` (defaults to `data/trades.db`).

