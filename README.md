# ncore-autorss

nCore RSS → Deepseek filter → qBittorrent auto-seeder, with Prometheus/Grafana
monitoring. Stdlib only (Python 3).

Fetches the nCore RSS feed, keeps `[Film` items, asks Deepseek whether each new
film is a mainstream, recently-released title worth seeding, and (if yes) adds it
to qBittorrent under the `auto-film` category. A companion manager monitors peers,
caps the category's on-disk size, emits metrics, and refreshes a Grafana panel.

The design is tuned to maximise the nCore "ERTEK" seeding score: keep many fresh,
high-demand torrents seeding uninterrupted, and treat deletion as a last resort.

## Files

| File | Role |
|------|------|
| `autorss.py` | Main pipeline: fetch RSS, filter (remux/720p), ask Deepseek, queue downloads (cron, every 15 min). |
| `torrent-manager.py` | `monitor` (metrics + Grafana panel, cron every 2h) and `ensure-space` (cap the category by deleting completed 0-peer seeders). |
| `autorss_common.py` | Shared constants + helpers: locking, qB API via `docker exec`, locked state I/O. |
| `test_autorss.py` | Tests. |
| `.env.example` | Template for the local `.env` (gitignored). |

## How it works

- **qBittorrent** runs in a Docker container with localhost-auth-bypass; host
  requests get 403 through Docker NAT, so all API calls go via
  `docker exec qbittorrent curl ...` (see `autorss_common.docker_curl`).
- **Filtering**: remux/full-disc and non-720p releases are skipped before Deepseek;
  a per-film clean-title key dedups quality variants and caches the Deepseek verdict.
- **Concurrency**: up to `MAX_ACTIVE_DOWNLOADS` parallel downloads; newest-approved
  first to catch peak leecher demand.
- **Disk policy**: bounded by the auto-film category's committed size, not host free
  space. Above `CATEGORY_HARD_GB` the manager trims completed 0-peer seeders; above
  `CATEGORY_CAP_GB` autorss holds the queue rather than delete live seeders.
- **State**: `state.json` (films) and `manager-state.json` (timers, counters) are
  machine-local and gitignored. All writes go through a shared file lock to stay
  consistent across the two cron jobs.

## Configuration

Tunables live in `autorss_common.py` (`CATEGORY_HARD_GB`, `CATEGORY_CAP_GB`,
`MAX_ACTIVE_DOWNLOADS`, `MAX_ADD_ATTEMPTS`, paths). Secrets live in `.env`
(see `.env.example`); the Deepseek key is read from a separate trading `.env`.

## Metrics

`torrent-manager.py monitor` writes a Prometheus textfile
(`autorss.prom` in the node_exporter textfile-collector dir), including
`autorss_seeding_total`, `autorss_disk_used_gb`, decision/peer counters, and the
monotonic, deletion-safe `autorss_uploaded_bytes_total` /
`autorss_downloaded_bytes_total`. A Grafana "Auto-Film" dashboard row visualises
these, including daily uploaded/downloaded GB (`increase(...[24h])`) and a 7-day
upload/download rate graph (`increase(...[1h])`).

## Cron

```
*/15 * * * * /usr/bin/python3 /home/fbazsa/ncore-autorss/autorss.py
0 */2 * * *  /usr/bin/python3 /home/fbazsa/ncore-autorss/torrent-manager.py monitor
```
