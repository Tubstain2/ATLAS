"""Weather skill — Open-Meteo (free, no API key)."""

import requests


def skill_info():
    return {
        "name": "weather",
        "triggers": ["atlas weather", "what is the weather", "what's the weather",
                     "how hot is it", "is it raining", "temperature outside",
                     "what is the temperature", "weather today", "weather forecast"],
        "description": "Current weather and forecast via Open-Meteo",
    }


def execute(query: str, context: dict) -> str:
    config = context.get("config", {})
    lat    = config.get("location_lat", 51.5)
    lon    = config.get("location_lon", -0.1)

    try:
        url    = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude":          lat,
            "longitude":         lon,
            "current_weather":   True,
            "hourly":            "relativehumidity_2m",
            "temperature_unit":  "celsius",
            "windspeed_unit":    "kmh",
            "timezone":          "auto",
        }
        r    = requests.get(url, params=params, timeout=8)
        data = r.json()
        cw   = data.get("current_weather", {})

        temp   = cw.get("temperature", "?")
        wind   = cw.get("windspeed", "?")
        code   = int(cw.get("weathercode", 0))

        condition = _wmo_description(code)
        return (f"It is currently {temp} degrees Celsius and {condition}, Boss. "
                f"Wind speed is {wind} kilometres per hour.")
    except Exception as exc:
        return f"I couldn't fetch the weather right now: {exc}"


def _wmo_description(code: int) -> str:
    mapping = {
        0:  "clear sky",
        1:  "mainly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "icy fog",
        51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
        61: "light rain", 63: "moderate rain", 65: "heavy rain",
        71: "light snow", 73: "moderate snow", 75: "heavy snow",
        80: "light showers", 81: "moderate showers", 82: "heavy showers",
        95: "thunderstorms", 96: "thunderstorms with hail",
    }
    return mapping.get(code, "mixed conditions")
