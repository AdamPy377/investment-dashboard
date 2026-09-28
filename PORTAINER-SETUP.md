# GitHub and Portainer setup

This matches the SPW pattern: source files live in GitHub, Portainer builds the image from that repository, and the real portfolio data stays in host directories. There is no portfolio data or password in GitHub.

## 1. Create the GitHub repository

Create a new **private** repository named `investment-dashboard` on GitHub. Leave the GitHub 'add README' and template options off. On your Mac, unzip the supplied release and open Terminal inside the **investment-dashboard** folder (the folder containing `Dockerfile` and `docker-compose.yml`). Then run:

```bash
git init -b main
git add .
git commit -m "Initial investment dashboard"
git remote add origin https://github.com/YOUR-USERNAME/investment-dashboard.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username. GitHub Desktop can also publish that folder as a private repository. Check that the GitHub repository root contains `app.py`, `Dockerfile`, `docker-compose.yml`, `backup.py`, `worker.py`, `static/`, and this guide. The `.env` file and `data/` are excluded by `.gitignore`; **never upload passwords, the database or tax files**.

## 2. Prepare persistent host folders

On the Ubuntu Docker host where Portainer runs:

```bash
sudo mkdir -p /home/adam/docker/investment-dashboard/data /home/adam/docker/investment-dashboard/backups
sudo chown -R 10001:10001 /home/adam/docker/investment-dashboard
```

These are the default paths in Compose. You can instead store backups on your DAS, for example `/mnt/media/investment-dashboard/backups`, after making sure that mount is available and writable by UID 10001. Set `HOST_BACKUP_DIR` to the chosen path in Portainer. Keep live SQLite on local storage, as with your newer SPW setup. If your Docker host uses another username or path, set `HOST_DATA_DIR` and `HOST_BACKUP_DIR` accordingly; the container itself still needs write access as UID 10001.

## 3. Add the Portainer Git stack

In Portainer select the Docker environment → **Stacks** → **Add stack** → **Git Repository**. Name it `investment-dashboard`. Select or create the source for your GitHub repository, choose branch `main` (older Portainer versions may ask for `refs/heads/main`), and set **Compose path** to `docker-compose.yml`.

For a private repository, configure repository authentication in Portainer when creating its Git source. Use a read-only credential with access to just this repository; do not put that credential into the repository or Compose file. If you deliberately choose a public repository, Git authentication is unnecessary, but your application code will be public.

Under **Environment variables** in the Portainer stack, enter:

| Variable | Value |
|---|---|
| `SECRET_KEY` | A long random value, e.g. the output of `openssl rand -hex 32` on your Mac |
| `ADMIN_USERNAME` | `adam` (or your choice) |
| `ADMIN_PASSWORD` | Your unique password, 12+ characters |
| `VIEWER_USERNAME` | Your brother's chosen login name |
| `VIEWER_PASSWORD` | His unique password, 12+ characters |
| `HOST_DATA_DIR` | `/home/adam/docker/investment-dashboard/data` |
| `HOST_BACKUP_DIR` | `/home/adam/docker/investment-dashboard/backups` (or your DAS backup path) |
| `HOST_PORT` | `3005` |
| `COOKIE_SECURE` | `0` for local HTTP |

Optional: `REFRESH_SECONDS` (blank defaults to 900 seconds between worker checks); `BACKUP_RETENTION_DAYS` (default `30`). Yahoo Finance requires no API key. You can remove old `ALPHA_VANTAGE_API_KEY` and `EODHD_API_KEY` stack variables: this release does not use them. The account passwords are used to create users only when the database is first initialized; changing these Portainer variables later will **not** reset existing accounts. Users can change their password from inside the app.

Select **Deploy the stack**. The `investment-dashboard`, `portfolio-price-worker`, and `portfolio-backup` containers should be running; the main container should show healthy. You don't need the Portainer 'relative path volumes' switch because this stack uses **absolute host paths**.

If the web container reports **unhealthy**, the other services can still start so Portainer can show the container logs. Open **Containers → investment-dashboard → Logs** and inspect the health check's output. If the logs show a database permission error, confirm `HOST_DATA_DIR` points to the folder you prepared on the Docker host and that UID 10001 can write there. Do not delete the data directory to troubleshoot.

## 4. Open it and enter data

From a device on your home network, open `http://YOUR-UBUNTU-SERVER-IP:3005` and sign in with your admin username and password. Your brother uses his separate login at the same address. Your admin account can switch portfolios at the top; his account is locked to his own portfolio on the server.

In **Manage**, create each account, add holdings and enter your earliest deposits and all trades, dividends, fees, interest, transfers and FX conversions. The worker fetches Yahoo Finance daily closes and backfills from each holding’s earliest buy, including sold holdings. Prices can still be corrected manually. Published closes are checked from 5 pm in the relevant market’s time zone. The holding detail page displays your stored close and TradingView's Symbol Overview chart, with a link if the widget cannot load; symbols can be edited in Manage → Holdings. You can upload tax statements in Manage and find them in Documents or on the linked holding page.

## 5. Updates and backups

For an update, change the source code, commit and push to `main`, then open the stack in Portainer and use **Pull and redeploy** with **Re-pull image and redeploy turned OFF**. This image is built locally from the Git checkout; `investment-dashboard:local` does not exist on Docker Hub. The Compose file sets `pull_policy: build` for all three services. The host mounts preserve your data when the containers are replaced.

The daily backups are in your `HOST_BACKUP_DIR`, each in a dated folder with `portfolio.sqlite3` and `documents/`. Back up that folder to another device as well. To restore, stop the stack, copy one dated backup's database and documents back to `HOST_DATA_DIR`, correct ownership to `10001:10001`, then start the stack.

### If the build fails

Some Portainer versions cannot run Compose `build:` against a **remote Docker environment** connected through an Agent. If you see `Unable to upgrade to tcp, received 200`, the Git repository itself is fine: build and push the image through a registry (such as GHCR), and switch the Compose services to that image instead of `build:`. Your older SPW Git build suggests your setup may support direct building; this fallback applies only if you hit that error.
