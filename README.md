# Enphase Local Monitoring

This project turns data from an Enphase solar system into a set of local dashboards and forecasts. It runs on your own computer or home server using Docker Compose. InfluxDB stores the time-series data, a Python monitor collects it, and Grafana presents it in a browser.

The stack can show Gateway reachability, live energy flow, historical production and consumption, weather-adjusted forecasts, and optional household scenarios such as EV charging. It is designed to be adapted: homes differ in their equipment, tariffs, time zones, and the information they want to see.

## Getting started

You will need:

- A computer or home server that can stay on while collecting data.
- Docker Desktop (Windows or macOS) or Docker Engine with the Docker Compose plugin (Linux).
- Access to your Enphase system on your home network.
- An Enphase local access token for authenticated Gateway data. Some features also use Enphase's cloud API and require separate developer credentials.
- A browser on the host computer. Other devices on your home Wi-Fi can access Grafana too; see [Access from another device](#access-from-another-device).

Start with the detailed, step-by-step [installation and configuration guide](docs/installation.md). It covers Docker, Grafana and InfluxDB credentials, finding the Gateway address, Enphase tokens and API resources, phone access, and optional local DNS. Keep real credentials and household information in local ignored files, never in a shared copy of the project.

### Recommended: ask an AI coding agent to set it up

If you use an AI coding agent, this is usually the easiest route:

1. Download the project as a ZIP and unzip it into a folder on the computer that will run it.
2. Open that folder in an agent such as Codex, Claude Code, or Gemini (for example, Antigravity).
3. Ask the agent to read `README.md`, `AGENTS.md`, and `docs/installation.md`, then guide you through installing and configuring the project for your Enphase system. Ask it to explain each step and keep credentials in `.env` rather than in chat or tracked files.
4. Ask the agent to identify the inverters reported by your Gateway using the private discovery command in the installation guide. It should confirm the count with you and help assign private aliases, without copying serials into chat or tracked files. Panel positions are optional; the agent should offer to help record panel locations, roof directions, and inclinations if you want a map or future geometry-aware analysis.
5. Follow the agent's prompts, provide configuration details locally, and ask it to start the stack and help you confirm that the dashboards are receiving data.

An agent can also help tailor the project once it is running. For example, you could ask it to:

- Add or adjust dashboard panels for the equipment and metrics you care about.
- Configure optional EV, hot-water, tariff, weather, and calendar inputs to match your home.
- Explain a dashboard, investigate a data gap, or create a repeatable report from the recorded history.
- Add support for another data source or make the interface easier for your household to use.
- Help document and safely share a useful improvement with the community.

Review proposed changes before sharing them, especially `.env`, exports, device identifiers, household records, and roof images. We welcome feedback about the app. If you build something useful or discover a cool way to use it, we would appreciate you sharing the idea with others.

### Manual setup

If you prefer to configure it yourself:

1. Download and unzip the project, then open a terminal in the project folder.
2. Install and start Docker Desktop or Docker Engine with Compose. Confirm that `docker compose version` works.
3. Copy `.env.example` to `.env` and edit `.env` for your installation. At minimum, set strong, unique `INFLUXDB_PASSWORD`, `INFLUXDB_TOKEN`, and `GRAFANA_PASSWORD` values; set the Gateway host and local Enphase access token; and enter your location and time zone for weather forecasts. Add current import and feed-in tariffs if you want cost estimates. Grafana and InfluxDB use these credentials when their persistent volumes are first initialized; changing `.env` values later may not reset existing accounts or tokens.
4. Start the stack:

   ```powershell
   docker compose up -d --build
   ```

5. Open Grafana at <http://localhost:3000> and sign in with the `GRAFANA_USERNAME` and `GRAFANA_PASSWORD` from `.env` (the example credentials are placeholders; change them before use).
6. Check the container state and monitor logs if data is not appearing:

   ```powershell
   docker compose ps
   docker compose logs --tail 100 monitor
   ```

7. Run the [private inverter identification step](docs/installation.md#identify-your-inverters) and review the count and aliases locally. Panel locations are optional. Follow the installation guide for optional features; start with EV support disabled if you do not have an EV. Charger telemetry, hot-water estimates, and a household calendar are also optional.

The project does not require Enphase cloud credentials for the basic local Gateway monitoring path. Cloud API credentials are only needed for features that explicitly use Enphase cloud data, such as some EV charger telemetry workflows. Do not put secrets into a public repository or paste them into a chat.

### Access from another device

By default, Grafana is published on port `3000` on the computer running Docker. On a phone or another computer connected to the same home network, open `http://<host-computer-address>:3000`, replacing the placeholder with that computer's local network address. The host firewall may need to allow access from your private network. Do not expose Grafana directly to the public internet.

For a convenient name instead of an IP address, configure a local DNS entry or router hostname (for example, `http://energy-box:3000`). The exact steps depend on your router and DNS service; [the installation guide](docs/installation.md) has guidance on choosing and setting this up. A stable DHCP reservation for the Docker host helps keep the address consistent.

## Dashboards

After setup, open Grafana at <http://localhost:3000>. The provisioned dashboards are:

- **Reachability** tracks whether the local Enphase Gateway responds and how long it takes. This is a basic network signal; it does not prove that authenticated data collection is working.
- **Enphase Energy Flow** shows current solar production, household consumption, net import or export, meter readings, and inverter output when those measurements are available.
- **History Analysis** explores imported historical Enphase production and energy data over time.
- **Prediction Forecast** combines weather and observed household history to estimate upcoming solar production and energy use. EV charging scenarios are optional and appear only when enabled and supported by suitable data.

Dashboards update as the monitor collects data. Historical analysis needs history to be imported, and forecast quality depends on the available system history and local weather settings. Forecasts are estimates for planning, not guarantees.

## Configuration overview

Use `.env.example` as the starting template and keep your private `.env` file local. Common settings include:

- Gateway hostname or IP and local access token.
- InfluxDB and Grafana usernames, passwords, and storage token.
- Latitude, longitude, and time zone for weather data.
- Import and feed-in tariffs for approximate cost estimates.
- Optional EV and charger telemetry settings, hot-water estimate times, and household calendar path.

Homes without an EV can leave EV support disabled. The forecast still uses ordinary household demand, and EV recommendations remain unavailable unless enabled and supported by sufficient data. See [forecast methods and limitations](monitor/record/history/FORECASTING.md) for more detail.

Inverter identification is part of setup, but identifying each panel's physical position is optional. If you want a panel map, use the [solar layout guide](docs/solar-layout-mapping.md); it supports a local roof image or screenshot. You may also record roof direction and panel inclination privately for future calculations. Current forecasts do not use individual panel positions or inclinations. Keep maps and images in the ignored local folder; do not commit installation-specific screenshots, coordinates, serial numbers, or site information.

## Sharing and feedback

Before sharing a fork or project archive, inspect it for `.env` files, tokens, household records, raw energy exports, device serials, site identifiers, and personal roof maps or images. Git ignore rules help prevent new private files from being added, but do not remove data already tracked or committed. See [AGENTS.md](AGENTS.md) for privacy and safe-sharing guidance.

We welcome feedback about what works, what is confusing, and what would make the app more useful. Contributions to the code, documentation, features, and dashboard types are welcome too. If you create a dashboard, report, or workflow that could help other Enphase owners, please share the idea or open a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md) for a short guide.

## License

This project is available under the [MIT License](LICENSE).
