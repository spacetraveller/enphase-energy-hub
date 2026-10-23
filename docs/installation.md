# Installation and configuration

This guide covers setup after downloading the project. For an overview and dashboard descriptions, start with the [README](../README.md).

## Requirements

- A computer or home server that can stay on and reach the Enphase IQ Gateway/Envoy on the same home network.
- Docker Compose. On Windows, install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) and leave it running. On Linux, install Docker Engine and the Compose plugin. See Docker's [Compose getting-started guide](https://docs.docker.com/compose/gettingstarted/).
- An Enphase system with an IQ Gateway/Envoy and an Enlighten account that can access it.
- An Enphase **local Gateway access token** for live authenticated readings. This is different from an Enphase Developer API v4 OAuth token.
- Internet access for image downloads and weather forecasts. Weather features also need coordinates and an IANA timezone.

## Configure private credentials

1. Unzip the project into a folder and open a terminal in that folder. Confirm `docker-compose.yml` and `.env.example` are present.
2. Copy the example configuration into a private `.env` file:

   ```powershell
   Copy-Item .env.example .env
   ```

   On macOS or Linux, use `cp .env.example .env`.

3. Edit `.env` before starting the services. Replace example values with unique strong credentials:
   - `GRAFANA_USERNAME` and `GRAFANA_PASSWORD` (do not use `admin` / `admin`).
   - `INFLUXDB_USERNAME`, `INFLUXDB_PASSWORD`, and `INFLUXDB_TOKEN`.
   - `ENPHASE_HOST` with the Gateway's LAN hostname or reserved IP address. Set `ENPHASE_API_HOST` to the host used for its authenticated API, usually the same address.
   - `ENPHASE_ACCESS_TOKEN` with the local Gateway token described below.
   - `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, and `WEATHER_TIMEZONE` for the installation. Use an IANA timezone such as `America/Denver` or `Europe/Paris`, not an abbreviation such as `MST`.
   - Current import/feed-in tariffs if you want cost estimates. Leave them blank if unknown; estimates will use zero until configured.

   Keep `.env` on the host. Do not paste it into an AI chat or commit it. Do not publish tokens, passwords, exact location, Gateway/system identifiers, device serials, energy exports, household calendars, or roof/panel images.

4. Find the Gateway's network address in the router's connected-device list or Enphase app. A DHCP reservation in the router is recommended so its IP does not change. `envoy.local` may work when local name discovery is supported. `ENPHASE_REQUEST_HOST` is optional and is commonly set to `envoy.local` only when the Gateway requires that HTTP Host header.
5. Obtain the local Gateway token using Enphase's official [IQ Gateway Local APIs or UI access using token guide](https://enphase.com/download/iq-gateway-local-apis-or-ui-access-using-token). It describes signing into the Gateway UI with Enphase credentials while online and token-based access where required. Follow the current guide for the Gateway model and software. Put the resulting local API bearer token in `ENPHASE_ACCESS_TOKEN`; never include it in a log, screenshot, issue, or chat.

## Start the services

From the project folder, run:

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 monitor
```

On macOS or Linux, the same Docker commands work in a terminal. Check the monitor logs for connection or configuration errors. A reachable Gateway landing page does not prove authenticated API access works; reachability and production collection are separate signals.

Open [http://localhost:3000](http://localhost:3000) on the host computer and sign in with the Grafana username and password from `.env`. The dashboards are provisioned by the project.

Grafana and InfluxDB usernames, passwords, and the InfluxDB token initialize accounts and access when their persistent data volumes are first created. Changing `.env` later does not reset credentials in an existing volume. Use each service's credential management to rotate them, or intentionally recreate its data volume only after understanding that this deletes stored data. Grafana's password can be changed in its user settings.

## Identify your inverters

Once the local Gateway token and host are configured, run the one-time discovery command from the project folder:

```powershell
docker compose run --rm --no-deps monitor python record/info/discover_inverters.py
```

It reads the Gateway's inverter production endpoint, reports the number found, and saves serial numbers with local aliases such as `INV-01` in the ignored `monitor/record/knowledge/local-solar-layout/inverters.json` file. Open that file locally to compare serials with the Enphase app or installer records and edit aliases if helpful. Re-running discovery keeps your aliases and marks previously known inverters that are absent from the latest response as `present: false`. Do not paste the file into a chat or share it in Git.

If you are using an AI agent, ask it to run this command, report the count, and help resolve any authentication or connectivity error. It should inspect the private file locally when needed, without printing serials in its response. If the endpoint reports zero inverters, check the app or installer records and the Gateway connection; aggregate monitoring can still run. Per-inverter charts populate automatically when the monitor receives inverter readings, without a fixed serial list in the dashboard. The current per-inverter chart displays serials in its legend, so review or redact that legend before sharing screenshots.

Panel locations are optional. After identification, an AI agent should offer to help you record panel positions, which roof face each panel belongs to, its direction and inclination, and optionally a roof screenshot. You can skip this and finish setup. The [solar layout guide](solar-layout-mapping.md) explains how to keep these details in ignored local files. The current forecasting code uses weather and observed production history; it does not yet apply individual panel positions or inclinations.

## Optional Enphase cloud API

The local Gateway connection above is the normal live-monitoring path. Cloud API v4 credentials are separate and only needed for cloud-backed tools such as selected history or telemetry imports. API plans, endpoints, access scopes, quotas, and prices can change; check Enphase's [Developer Portal](https://developer-v4.enphase.com/), [API quick start and OAuth guide](https://developer-v4.enphase.com/docs/quickstart.html), [API documentation](https://developer-v4.enphase.com/docs/monitoring_api), and [current plans](https://developer-v4.enphase.com/developer-plans) before signing up.

In general, create or sign into a developer account, create an application with the required Monitoring API access, note its API key/client ID/client secret, authorize the system owner through the application's authorization URL, and exchange the returned authorization code for OAuth access and refresh tokens. The application key and OAuth tokens are distinct credentials. Store them only in the matching `ENPHASE_CLOUD_*` fields in `.env`. Follow [`monitor/record/cloud/README.md`](../monitor/record/cloud/README.md) for this repository's workflow rather than copying secrets into manual curl commands. Cloud API access is optional and subject to Enphase's current eligibility, plan, scope, and rate limits.

## Access Grafana from another device

The Compose file publishes Grafana on port `3000` on the host. On a phone, tablet, or another computer connected to the same trusted home Wi-Fi, open `http://<host-computer-LAN-IP>:3000` and sign in. Find the host IP in the router's device list. If it does not load, check that the host firewall permits inbound TCP port 3000 from the private LAN and that client isolation or guest Wi-Fi is not blocking device-to-device traffic. Do not forward Grafana or InfluxDB ports from the internet-facing router.

## Optional friendly local DNS name

To avoid remembering the host's IP address, configure local DNS in the router or a LAN DNS service:

1. Create a DHCP reservation for the computer running Docker Compose.
2. Add a local DNS host record such as `solar-monitor.home.arpa` pointing to that reserved LAN IP. `home.arpa` is reserved for private home-network naming; use another local DNS suffix only if your router requires it.
3. Ensure Wi-Fi clients use that router or DNS server, then try `http://solar-monitor.home.arpa:3000`.

Router interfaces differ. Some support both DHCP reservations and DNS host records; otherwise use a LAN DNS service (for example, Pi-hole or AdGuard Home) and configure DHCP to hand out that resolver. A hosts-file entry is a per-device fallback, not shared local DNS. For access away from home, use a private VPN into the LAN; do not expose these service ports directly to the public internet.

## Enable optional features

Start with the generic defaults in `.env.example`; configure only features that fit the site. After changing environment values, recreate affected services with `docker compose up -d` (use `--build` after Python or image changes).

- **Weather-adjusted forecasts:** set location coordinates and `WEATHER_TIMEZONE`. See [forecasting methods and data requirements](../monitor/record/history/FORECASTING.md).
- **EV planning:** set `HOUSEHOLD_EV_ENABLED=true` only if an EV is part of the site's demand profile. Set `HOUSEHOLD_EV_HISTORY_START_DATE` if the start date is known. Keep `EV_CHARGER_TELEMETRY_ENABLED=false` unless compatible charger telemetry is available in InfluxDB. Without adequate telemetry, the ordinary household forecast can still run while EV planning may be unavailable.
- **Hot-water load estimate:** set both `HOUSEHOLD_HOT_WATER_ESTIMATE_START` and `HOUSEHOLD_HOT_WATER_ESTIMATE_END` to the local scheduled window. Leave both blank when unknown; this is a descriptive estimate, not a control schedule.
- **Historical data:** use documented import tools under `monitor/record/history/` or `monitor/record/cloud/`. Imported household data is private and must stay out of shared branches.
- **Solar panel map:** optional after inverter identification. Follow the [solar layout mapping guide](solar-layout-mapping.md). Store a real roof image/screenshot and panel map only in the ignored local layout directory. Use aliases in shareable reports and sanitize image metadata before sharing.

Set `METER_PRODUCTION_EID` and `METER_NET_EID` in the private root `.env` to
enable Grafana's production, import, and export meter panels. Use the meter
identifiers reported by your own Gateway; keep them out of screenshots and
shared files. If either setting is blank, the matching Grafana panels show no
data. Diagnostic scripts can infer meter roles from recent InfluxDB fields
when they are unique, or ask for a local override when they are ambiguous.
The example configuration contains no installation identifiers.
