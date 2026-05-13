# Infrastructure Upgrade Notes

## VPS Upgrade Path (Oracle Cloud Free Tier)

**Current setup (2026-05-13):**
- Instance: AMD VM.Standard.E2.1.Micro (free)
- CPU: 1 OCPU
- RAM: 945 MB
- Disk: 29.4 GB (out of 200 GB free allowance)
- Constraints: signal latency 100–500s on 50-symbol intraday scans

**Recommended upgrade (still free):**
- Instance: **ARM Ampere A1 (VM.Standard.A1.Flex)**
- CPU: up to **4 OCPUs**
- RAM: up to **24 GB**
- Disk: same 200 GB block storage allowance
- Cost: ₹0 — Always Free tier
- Expected impact: signal latency drops 4–10×; can scan 100+ symbols per tick

**Migration plan (do when ready):**
1. Create new Ampere A1 instance in Oracle Cloud console (Ubuntu 22.04 LTS recommended).
2. Generate new SSH key, save to project folder.
3. `scp` the app folder, .env, model files, SQLite DBs from old instance to new.
4. Install Python 3.11, deps from `requirements.txt`.
5. Copy `/etc/systemd/system/nse-*.{service,timer}` from old to new; `daemon-reload`.
6. Copy crontab (heartbeat watchdog) from old to new.
7. Verify dashboard reachable; verify intraday timer fires.
8. Update DNS / bookmarks if dashboard URL changes.
9. Terminate old AMD instance after 1 week of clean parallel run.

**Disk expansion (no migration needed):**
If 19.6 GB free becomes tight (unlikely):
- Console → Compute → Boot Volumes → Edit → 200 GB
- Inside VM: `sudo growpart /dev/sda 1 && sudo xfs_growfs /` (or equivalent)
