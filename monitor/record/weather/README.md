# Weather Forecast Collection

This folder contains repeatable Python tooling for collecting Open-Meteo forecast data.

## Configuration

Set these values in `.env`:

```env
WEATHER_LATITUDE=
WEATHER_LONGITUDE=
WEATHER_TIMEZONE=UTC
WEATHER_FORECAST_DAYS=3
```

Use site-level coordinates where possible. A suburb-level coordinate is usually fine; a city-centre fallback is less useful for solar forecasting.

## Run

```powershell
docker compose run --rm --no-deps monitor python /app/record/weather/fetch_open_meteo.py
```

The script writes hourly forecast fields to InfluxDB measurement `weather_forecast` and stores one-off JSON snapshots under `monitor/record/weather/snapshots`.
