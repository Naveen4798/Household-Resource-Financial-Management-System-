# HomeBase

**Household Resource & Financial Management System**

A self-hosted, privacy-first household ERP built on SQLite and Docker. Tracks finances, pantry inventory, HSA eligibility, home maintenance, security cameras, energy usage, vehicles, and more — all from a single Python file backed by 25 containerized services.

## What It Does

- **Financial ledger** with double-entry accounting, bank CSV reconciliation, and transaction void/reversal
- **Receipt ingestion** from Walmart, Sam's Club, Amazon, Target, Costco (structured data, email parsing)
- **HSA tracking** — automatically flags IRS Section 213(d) eligible items from 30+ patterns
- **Sales tax extraction** per merchant with year-end summaries
- **Pantry inventory** with multi-pack multipliers (a 4-pack of butter = 4 sticks), low-stock alerts, and physical count audits
- **Security cameras** via Frigate NVR + Wyze (wz_mini_hacks) — person/package/vehicle detection, all local, no subscription
- **Energy tracking** via Ecobee API — HVAC runtime, cost estimates with mode-aware kW rates
- **Vehicle tracking** — mileage, fuel costs, business vs personal split, IRS deduction calculator
- **Pi-hole DNS analytics** — query volume, ad blocking stats, network visibility
- **Subscriptions & bills** — tracking with due-date alerts, monthly/annual totals
- **Home maintenance** — 8 pre-seeded tasks (HVAC filter, gutters, water heater...) with scheduling
- **Warranty tracking** with expiration alerts
- **Multi-household & family member** support
- **Data export** — CSV and JSON for tax prep, insurance claims, or migration
- **Push notifications** via Ntfy for all alerts
- **Full audit log** of every financial modification

## Requirements

- **Python 3.10+** (standard library only — no pip installs required for core features)
- **Docker + Docker Compose** (for the full service stack)
- **Hardware**: Ubuntu Server on any x86_64 machine
  - **Minimum**: 8GB RAM, 128GB SSD (runs core services only)
  - **Recommended**: 16GB RAM, 128GB SSD + 1TB external HDD (runs everything including Immich + Frigate)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/your-username/homebase.git
cd homebase

# Run the demo (no Docker needed — just Python)
python3 homebase.py

# Or with a custom database location
HOMEBASE_DB=/path/to/my/homebase.db python3 homebase.py
```

The demo creates a fresh database, seeds it with sample data (receipts, energy readings, vehicle trips), runs every subsystem, and verifies data integrity.

## Configuration

Create a `config.json` in the same directory as `homebase.py`:

```json
{
  "db_path": "/opt/homebase/data/homebase.db",
  "currency": "USD",
  "irs_mileage_rate": 0.70,
  "energy_kwh_rate": 0.12,
  "hvac_kw": {
    "cool": 3.5,
    "heat": 10.0,
    "fan": 0.5,
    "off": 0.0
  },
  "ntfy_url": "http://ntfy:8093",
  "ntfy_topic": "homebase",
  "actual_budget_url": "http://actual-budget:5006",
  "grocy_url": "http://grocy:9283",
  "mealie_url": "http://mealie:9925",
  "frigate_url": "http://frigate:5000",
  "pihole_url": "http://pihole:80",
  "immich_url": "http://immich-server:2283"
}
```

All settings can also be set via environment variables (prefixed `HOMEBASE_`, `ACTUAL_BUDGET_URL`, `GROCY_URL`, etc.). Environment variables override `config.json`.

### Customizing for Your Home

**Energy rates**: Look up your local electricity rate (kWh price) from your utility bill. Update `energy_kwh_rate`. If you have a heat pump, your `heat` kW is closer to 3-4, not 10.

**IRS mileage rate**: Check the current year's rate at [irs.gov](https://www.irs.gov/tax-professionals/standard-mileage-rates). Update `irs_mileage_rate`.

**HSA catalog**: The built-in catalog covers common OTC medications and medical supplies. Add your own patterns by inserting rows into the `hsa_catalog` table:

```python
conn.execute("INSERT INTO hsa_catalog (match_pattern, category) VALUES ('%MY PRESCRIPTION%', 'Prescription')")
```

**Maintenance tasks**: The 8 default tasks are common US single-family home tasks. Add your own:

```python
conn.execute("INSERT INTO maintenance_schedule (task, location, frequency_days, estimated_cost_cents, next_due) VALUES ('Clean pool filter', 'Backyard', 30, 0, '2026-10-15')")
```

## Docker Stack Deployment

The system generates a complete `docker-compose.yml` for 25 services. To deploy:

```bash
# Generate the compose file
python3 -c "from homebase import generate_docker_compose; print(generate_docker_compose())" > docker-compose.yml

# Create a .env file with your secrets (NEVER commit this)
cat > .env << 'EOF'
PIHOLE_PASSWORD=your-secure-password
POSTGRES_PASSWORD=your-db-password
AUTHELIA_JWT_SECRET=$(openssl rand -hex 32)
AUTHELIA_SESSION_SECRET=$(openssl rand -hex 32)
EOF

# Create required directories
mkdir -p traefik pihole/etc-pihole pihole/etc-dnsmasq.d unbound wireguard \
  authelia crowdsec vaultwarden actual-budget grocy mealie \
  paperless/data immich/model-cache immich/db \
  frigate n8n ntfy grafana uptime-kuma homepage portainer

# Start everything
docker compose up -d

# Check status
docker compose ps
```

### Service URLs (after Pi-hole DNS is configured)

| Service | URL | Purpose |
|---|---|---|
| Homepage | `home.home.lan` | Dashboard with all service widgets |
| Actual Budget | `budget.home.lan` | Envelope budgeting |
| Grocy | `pantry.home.lan` | Inventory & shopping lists |
| Mealie | `meals.home.lan` | Recipes & meal planning |
| Paperless-ngx | `docs.home.lan` | Document management & OCR |
| Immich | `photos.home.lan` | Photo management |
| Frigate | `cameras.home.lan` | Security camera NVR |
| Grafana | `dash.home.lan` | Dashboards & analytics |
| n8n | `auto.home.lan` | Workflow automation |
| Pi-hole | `pihole.home.lan` | DNS & ad blocking |
| Ntfy | `ntfy.home.lan` | Push notifications |
| Authelia | `auth.home.lan` | Single sign-on (2FA) |
| Portainer | `manage.home.lan` | Docker management |
| Dozzle | `logs.home.lan` | Container log viewer |
| Uptime Kuma | `status.home.lan` | Service health monitoring |
| Vaultwarden | `vault.home.lan` | Password manager |
| Stirling PDF | `pdf.home.lan` | PDF tools (on-demand) |
| Syncthing | `sync.home.lan` | File sync |
| WireGuard | UDP :51820 | VPN remote access |

### Pi-hole Local DNS Setup

After Pi-hole is running, add a custom DNS record so all `*.home.lan` domains resolve to your server:

```bash
# Add to pihole/etc-dnsmasq.d/05-custom.conf
echo "address=/home.lan/192.168.1.50" > pihole/etc-dnsmasq.d/05-custom.conf
docker compose restart pihole
```

Then set your router's DHCP to hand out Pi-hole's IP as the DNS server for all devices on your network.

## Hardware Setup Guide

### Recommended: MacBook Pro 2012 (or similar vintage hardware)

| Component | Spec | Cost |
|---|---|---|
| RAM | **16GB DDR3** (upgrade from 8GB) | ~$30 |
| SSD | 128GB+ (for OS + Docker + databases) | Already installed |
| External storage | 1TB HDD/SSD via USB 3.0 or Thunderbolt | ~$50 |
| UPS | CyberPower or APC 600VA+ | ~$60 |

**RAM upgrade**: 2x 8GB DDR3 1600MHz SO-DIMMs. 16GB unlocks Immich, Frigate, and Grafana.

**UPS is critical**: Protects against sudden power loss corrupting SQLite databases.

### Storage Layout

```
SSD (128GB):
  /           Ubuntu Server
  /swapfile   4-8GB swap
  /opt/homebase/  Docker images + SQLite databases

External HDD (1TB, ext4, mounted at /mnt/data):
  /mnt/data/docker/paperless/media/    Scanned documents
  /mnt/data/docker/immich/upload/      Photos
  /mnt/data/docker/frigate/media/      Camera recordings
  /mnt/data/docker/syncthing/          Synced files
  /mnt/data/backups/                   Restic local repository
```

Format the external drive:

```bash
sudo mkfs.ext4 -L homebase-data /dev/sdX1
echo 'LABEL=homebase-data /mnt/data ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a
```

### Swap Configuration

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

## Security Hardening

### Network (do these first)

```bash
# UFW firewall
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit 22/tcp              # SSH
sudo ufw allow 80/tcp              # Traefik HTTP
sudo ufw allow 443/tcp             # Traefik HTTPS
sudo ufw allow 53/tcp              # Pi-hole DNS
sudo ufw allow 53/udp
sudo ufw allow 51820/udp           # WireGuard VPN
sudo ufw enable

# Fix Docker/UFW bypass
sudo wget -O /usr/local/bin/ufw-docker https://github.com/chaifeng/ufw-docker/raw/master/ufw-docker
sudo chmod +x /usr/local/bin/ufw-docker
sudo ufw-docker install

# Fail2ban
sudo apt install fail2ban
```

### Remote Access

**Never expose services to the internet.** Use WireGuard VPN instead:

1. Forward only UDP 51820 on your router
2. Install WireGuard client on your phone/laptop
3. Scan the QR code from `./wireguard/peer_phone/peer_phone.png`
4. VPN clients get Pi-hole ad blocking + local DNS for `*.home.lan`

### VLAN Segmentation (if your router supports it)

| VLAN | Subnet | Devices |
|---|---|---|
| 1 (Trusted) | 192.168.1.0/24 | Laptops, phones, server |
| 10 (IoT) | 192.168.10.0/24 | Wyze cameras, Ecobee, smart plugs |
| 20 (Guest) | 192.168.20.0/24 | Visitor Wi-Fi |

IoT devices can't reach trusted devices. Trusted devices can access camera feeds.

### Backups (3-2-1 Rule)

```bash
# Install Restic
sudo apt install restic

# Initialize local backup repo
restic -r /mnt/data/backups/restic-repo init

# Initialize cloud backup (Backblaze B2, ~$0.50/month)
export B2_ACCOUNT_ID="your-id"
export B2_ACCOUNT_KEY="your-key"
restic -r b2:homebase-backup init

# Backup script (run via cron at 3 AM daily)
#!/bin/bash
# Dump live SQLite databases first (never backup a live SQLite file)
sqlite3 /opt/homebase/data/homebase.db ".backup /mnt/data/backups/exports/homebase.db"

restic -r b2:homebase-backup backup \
  /mnt/data/backups/exports/ \
  /mnt/data/docker/paperless/media/ \
  --tag homebase \
  --exclude="*.tmp"

restic -r b2:homebase-backup forget \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
```

## Using as a Library

HomeBase is a single Python file with no external dependencies (for core features). Import it in your own scripts:

```python
from homebase import open_db, ingest_receipt, get_low_stock_alerts, check_and_notify_alerts

conn = open_db("/path/to/homebase.db")

# Ingest a receipt
txn_id = ingest_receipt(conn, {
    "date": "2026-10-01",
    "merchant": "Walmart",
    "payment_method": "Debit Card",
    "items": [
        {"name": "Milk 1 Gal", "qty": 1, "price": 3.48},
        {"name": "Tylenol 100ct", "qty": 1, "price": 9.97},
    ],
    "tax": 0.62,
    "total": 14.07,
})

# Check for alerts and send push notifications
alerts = check_and_notify_alerts(conn)

conn.close()
```

### n8n Integration

Use n8n's HTTP Request node to call your Python scripts on a schedule:

1. **Daily 8 AM**: Check upcoming bills -> Ntfy push notification
2. **After email receipt**: Parse receipt -> ingest_receipt() -> update inventory
3. **Every 30 min**: Poll Ecobee API -> log_energy_reading()
4. **Every 5 min**: Poll Pi-hole API -> log_dns_stats()
5. **Nightly**: Export data -> Restic backup -> verify integrity

## Database Schema

22 tables organized by domain:

| Domain | Tables |
|---|---|
| **Core** | `households`, `family_members`, `audit_log` |
| **Financial** | `accounts`, `merchants`, `transactions`, `journal_entries`, `transaction_items` |
| **HSA/Tax** | `hsa_catalog` |
| **Inventory** | `products`, `inventory`, `receipt_product_mappings`, `inventory_audits` |
| **Banking** | `bank_imports` |
| **Security** | `security_events` |
| **Energy** | `energy_readings` |
| **Vehicles** | `vehicles`, `vehicle_trips` |
| **DNS** | `dns_stats` |
| **Home** | `maintenance_schedule`, `subscriptions`, `warranties` |

All monetary values stored as **integer cents** (never floats). All dates stored as ISO 8601 strings. Foreign keys enforced. WAL mode enabled for concurrent reads.

## Known Limitations

- **Single-file architecture**: Works well for household scale (<100K transactions). For larger deployments, consider splitting into a package structure.
- **No web UI**: HomeBase is a Python library + CLI. Use Actual Budget, Grocy, and Grafana for web interfaces.
- **Bank reconciliation is exact-match**: Matches by date + amount. No fuzzy matching yet.
- **HSA catalog is US-only**: Based on IRS Section 213(d). International users need to customize.
- **Immich/Frigate require 16GB RAM**: Won't run on 8GB systems.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Run the demo to verify nothing breaks (`python3 homebase.py`)
4. Submit a pull request

### Code Quality Expectations

- All financial operations must use `to_cents()` (never `float * 100`)
- All multi-statement DB operations must use `with atomic(conn):`
- All journal entries must pass balance assertion
- All user input must go through `sanitize_str()`
- Add entries to `audit_log` for any data modification

## License

MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgments

Built on the shoulders of these open-source projects:
[Actual Budget](https://actualbudget.org) |
[Grocy](https://grocy.info) |
[Mealie](https://mealie.io) |
[Paperless-ngx](https://docs.paperless-ngx.com) |
[Immich](https://immich.app) |
[Frigate](https://frigate.video) |
[Pi-hole](https://pi-hole.net) |
[Traefik](https://traefik.io) |
[Authelia](https://www.authelia.com) |
[n8n](https://n8n.io) |
[Ntfy](https://ntfy.sh) |
[Grafana](https://grafana.com) |
[Restic](https://restic.net)
