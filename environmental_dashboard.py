from pathlib import Path
from io import BytesIO, StringIO
from datetime import datetime, timezone
import json

import pandas as pd
import requests
import streamlit as st

# ============================================================
# LADYWOOD ENVIRONMENTAL DASHBOARD - LIVE DATA VERSION
# ============================================================
# What this version does:
# 1. Air quality updates from a live online air-quality API.
# 2. Rainfall updates from a live online weather/rainfall API.
# 3. Brownfield and surface-water flooding update from public URLs
#    when you add those URLs in data_sources.json or Streamlit secrets.
# 4. If no URL is provided, it falls back to files in your GitHub repo.
# ============================================================

st.set_page_config(page_title="Ladywood Environmental Dashboard", layout="wide")

st.markdown(
    """
    <style>
    .stApp { background-color:#050A14; color:#EAF2FF; }
    h1,h2,h3 { color:#4DA3FF; }
    [data-testid="stSidebar"] { background-color:#07111F; }
    [data-testid="stMetric"] {
        background-color:#071827;
        border:1px solid #145DA0;
        border-radius:14px;
        padding:15px;
    }
    .info-card {
        background-color:#071827;
        border:1px solid #145DA0;
        border-radius:14px;
        padding:18px;
        margin-bottom:16px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ------------------------------------------------------------
# BASIC SETTINGS
# ------------------------------------------------------------
DATA = Path(".")
LOCAL_BROWNFIELD_FILE = DATA / "brownfield.xlsx"
LOCAL_AIR_FILE = DATA / "air_quality_2026.csv"
LOCAL_FLOOD_FILE = DATA / "surface_water_flooding.json"
LOCAL_RAIN_FILE = DATA / "rainfall.csv"
CONFIG_FILE = DATA / "data_sources.json"

# Ladywood / Birmingham approximate coordinates.
LADYWOOD_LAT = 52.4775
LADYWOOD_LON = -1.9164
LIVE_REFRESH_SECONDS = 1800  # 30 minutes
YEARS = [2026, 2025, 2024, 2023, 2022]

# Live APIs used by default for rainfall and air quality.
LIVE_RAINFALL_API = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LADYWOOD_LAT}&longitude={LADYWOOD_LON}"
    "&hourly=precipitation,rain,showers"
    "&daily=precipitation_sum,rain_sum"
    "&timezone=Europe%2FLondon"
)

LIVE_AIR_QUALITY_API = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={LADYWOOD_LAT}&longitude={LADYWOOD_LON}"
    "&hourly=pm10,pm2_5,nitrogen_dioxide,ozone"
    "&timezone=Europe%2FLondon"
)


def safe_secret(name: str, default: str = "") -> str:
    """Read Streamlit secrets safely. Works even when no secrets file exists."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def load_config() -> dict:
    """Load optional data source URLs from data_sources.json."""
    default_config = {
        "brownfield_url": safe_secret("BROWNFIELD_URL", ""),
        "surface_water_flooding_url": safe_secret("SURFACE_WATER_FLOODING_URL", ""),
        "air_quality_url": safe_secret("AIR_QUALITY_URL", LIVE_AIR_QUALITY_API),
        "rainfall_url": safe_secret("RAINFALL_URL", LIVE_RAINFALL_API),
    }

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_config = json.load(f)
            for key, value in file_config.items():
                if value:
                    default_config[key] = value
        except Exception:
            pass

    return default_config


SOURCE_CONFIG = load_config()


# ------------------------------------------------------------
# GENERAL HELPERS
# ------------------------------------------------------------
def clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = df.columns.astype(str).str.replace("\xa0", " ", regex=False).str.strip()
    return df


def find_col(df: pd.DataFrame, words: list[str]):
    for col in df.columns:
        low = str(col).lower()
        if any(word in low for word in words):
            return col
    return None


def search_rows(df: pd.DataFrame, text: str) -> pd.DataFrame:
    if df.empty or not text:
        return df
    mask = df.astype(str).apply(
        lambda row: row.str.contains(text, case=False, na=False).any(), axis=1
    )
    return df[mask]


def filter_ladywood(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df.astype(str).apply(
        lambda row: row.str.contains("Ladywood", case=False, na=False).any(), axis=1
    )
    result = df[mask]
    return result if not result.empty else df


def filter_year(df: pd.DataFrame, year: int) -> pd.DataFrame:
    if df.empty:
        return df

    candidates = [
        col
        for col in df.columns
        if any(word in str(col).lower() for word in ["year", "date", "time", "updated", "added", "permission"])
    ]

    for col in candidates:
        dates = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
        if dates.notna().sum() and (dates.dt.year == year).sum():
            return df[dates.dt.year == year]

        text_mask = df[col].astype(str).str.contains(str(year), na=False)
        if text_mask.sum():
            return df[text_mask]

    return df


def numeric_columns(df: pd.DataFrame) -> list[str]:
    cols = []
    for col in df.columns:
        numeric = pd.to_numeric(df[col], errors="coerce")
        if numeric.notna().sum() > 0:
            cols.append(col)
    return cols


def chart_numeric(df: pd.DataFrame, label: str):
    nums = numeric_columns(df)
    if not nums:
        st.info(f"No numeric {label} column was found for a chart.")
        return

    selected = st.selectbox(f"Choose {label} chart field", nums)
    chart_df = df.copy()
    chart_df[selected] = pd.to_numeric(chart_df[selected], errors="coerce")

    date_col = find_col(chart_df, ["date", "time", "month"])
    if date_col:
        dates = pd.to_datetime(chart_df[date_col], errors="coerce", dayfirst=True)
        if dates.notna().sum() > 0:
            chart_df = (
                chart_df.assign(_date=dates)
                .dropna(subset=["_date", selected])
                .sort_values("_date")
                .set_index("_date")
            )

    st.line_chart(chart_df[[selected]].dropna())


def map_if_possible(df: pd.DataFrame):
    lat = find_col(df, ["lat", "latitude"])
    lon = find_col(df, ["lon", "lng", "longitude"])

    if not lat or not lon:
        st.info("No latitude/longitude columns detected for a map.")
        return

    mapped = df.copy()
    mapped[lat] = pd.to_numeric(mapped[lat], errors="coerce")
    mapped[lon] = pd.to_numeric(mapped[lon], errors="coerce")
    mapped = mapped.dropna(subset=[lat, lon])

    if mapped.empty:
        st.info("Coordinate columns were found, but no valid points could be mapped.")
        return

    st.map(mapped.rename(columns={lat: "lat", lon: "lon"})[["lat", "lon"]])


def read_csv_flexible_bytes(content: bytes) -> pd.DataFrame:
    """Read a CSV even when it has metadata lines before the actual header."""
    text = content.decode("utf-8", errors="replace")
    lines = text.splitlines()

    best_df = pd.DataFrame()
    for skip in range(min(30, len(lines))):
        try:
            candidate = pd.read_csv(StringIO("\n".join(lines[skip:])))
            candidate = clean_cols(candidate)
            real_cols = [c for c in candidate.columns if not str(c).lower().startswith("unnamed")]
            if len(real_cols) >= 2 and len(candidate) > 0:
                return candidate.dropna(how="all")
            if len(candidate.columns) > len(best_df.columns):
                best_df = candidate
        except Exception:
            continue

    return clean_cols(best_df).dropna(how="all")


def response_to_table(content: bytes, url_or_name: str) -> pd.DataFrame:
    lower = url_or_name.lower()
    if lower.endswith((".xlsx", ".xls")):
        return clean_cols(pd.read_excel(BytesIO(content))).dropna(how="all")
    return read_csv_flexible_bytes(content)


@st.cache_data(ttl=LIVE_REFRESH_SECONDS, show_spinner=False)
def fetch_bytes(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 Ladywood Environmental Dashboard",
        "Accept": "text/csv, application/json, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
    }
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    return response.content


@st.cache_data(ttl=LIVE_REFRESH_SECONDS, show_spinner=False)
def fetch_json(url: str) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 Ladywood Environmental Dashboard"}
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


# ------------------------------------------------------------
# LIVE LOADERS
# ------------------------------------------------------------
@st.cache_data(ttl=LIVE_REFRESH_SECONDS)
def load_brownfield_from_source(url: str) -> tuple[pd.DataFrame, str]:
    if url:
        data = fetch_bytes(url)
        return response_to_table(data, url), "Live URL"

    if LOCAL_BROWNFIELD_FILE.exists():
        return clean_cols(pd.read_excel(LOCAL_BROWNFIELD_FILE)).dropna(how="all"), "Local GitHub file"

    return pd.DataFrame(), "Missing source"


@st.cache_data(ttl=LIVE_REFRESH_SECONDS)
def load_surface_flooding_from_source(url: str) -> tuple[dict, str]:
    if url:
        return fetch_json(url), "Live URL"

    if LOCAL_FLOOD_FILE.exists():
        with open(LOCAL_FLOOD_FILE, "r", encoding="utf-8") as f:
            return json.load(f), "Local GitHub file"

    return {}, "Missing source"


@st.cache_data(ttl=LIVE_REFRESH_SECONDS)
def load_air_quality_from_source(url: str) -> tuple[pd.DataFrame, str]:
    if url:
        try:
            data = fetch_json(url)
            hourly = data.get("hourly", {})
            if hourly:
                return clean_cols(pd.DataFrame(hourly)), "Live air-quality API"
        except Exception:
            # If URL is not JSON, try CSV/XLSX.
            data = fetch_bytes(url)
            return response_to_table(data, url), "Live URL"

    if LOCAL_AIR_FILE.exists():
        return read_csv_flexible_bytes(LOCAL_AIR_FILE.read_bytes()), "Local GitHub file"

    return pd.DataFrame(), "Missing source"


@st.cache_data(ttl=LIVE_REFRESH_SECONDS)
def load_rainfall_from_source(url: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if url:
        try:
            data = fetch_json(url)
            hourly = clean_cols(pd.DataFrame(data.get("hourly", {})))
            daily = clean_cols(pd.DataFrame(data.get("daily", {})))
            return hourly, daily, "Live rainfall API"
        except Exception:
            data = fetch_bytes(url)
            table = response_to_table(data, url)
            return table, pd.DataFrame(), "Live URL"

    if LOCAL_RAIN_FILE.exists():
        return read_csv_flexible_bytes(LOCAL_RAIN_FILE.read_bytes()), pd.DataFrame(), "Local GitHub file"

    return pd.DataFrame(), pd.DataFrame(), "Missing source"


# ------------------------------------------------------------
# FLOODING HELPERS
# ------------------------------------------------------------
def first_coord(geometry):
    if not geometry:
        return None
    current = geometry.get("coordinates")
    try:
        while isinstance(current, list):
            if len(current) >= 2 and isinstance(current[0], (int, float)) and isinstance(current[1], (int, float)):
                return current[1], current[0]
            current = current[0]
    except Exception:
        return None
    return None


def flood_table(geojson_data: dict) -> pd.DataFrame:
    rows = []
    for i, feature in enumerate(geojson_data.get("features", []), start=1):
        row = {"feature_number": i, "feature_id": feature.get("id", "")}
        row.update(feature.get("properties", {}))
        point = first_coord(feature.get("geometry", {}))
        if point:
            row["lat"], row["lon"] = point
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------
# SIDEBAR
# ------------------------------------------------------------
st.title("Ladywood Environmental Dashboard")
st.markdown(
    """
    <div class='info-card'>
    <b>MIIN Group 37 FEBE1004A</b><br>
    This interactive dashboard updates from live online sources where available.
    Air quality and rainfall use live APIs by default. Brownfield and surface-water flooding can update from public data URLs added once in <code>data_sources.json</code> or Streamlit secrets.
    <br><br>
    Members: Sanelisiwe Ntshangase; Christopher Matsimela; Slindelokuhle Hlatshwayo
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Controls")
    section = st.radio(
        "Section",
        ["Overview", "Brownfield", "Air Quality", "Surface-Water Flooding", "Rainfall", "Data Sources"],
    )
    year = st.selectbox("Year filter", YEARS)
    st.caption(f"Live refresh interval: {LIVE_REFRESH_SECONDS // 60} minutes")

    with st.expander("Optional live source URLs"):
        st.write("Paste URLs here to test them. For permanent deployment, put them in data_sources.json or Streamlit secrets.")
        brownfield_url = st.text_input("Brownfield URL", value=SOURCE_CONFIG.get("brownfield_url", ""))
        flood_url = st.text_input("Surface-water flooding GeoJSON URL", value=SOURCE_CONFIG.get("surface_water_flooding_url", ""))
        air_url = st.text_input("Air-quality URL/API", value=SOURCE_CONFIG.get("air_quality_url", LIVE_AIR_QUALITY_API))
        rain_url = st.text_input("Rainfall URL/API", value=SOURCE_CONFIG.get("rainfall_url", LIVE_RAINFALL_API))


# ------------------------------------------------------------
# LOAD DATA
# ------------------------------------------------------------
errors = []
loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

try:
    brown_all, brown_source = load_brownfield_from_source(brownfield_url)
    brown_lady = filter_ladywood(brown_all)
    brown_year = filter_year(brown_lady, year)
except Exception as error:
    brown_all = brown_lady = brown_year = pd.DataFrame()
    brown_source = "Error"
    errors.append(("Brownfield", error))

try:
    air, air_source = load_air_quality_from_source(air_url)
    air_year = filter_year(air, year)
except Exception as error:
    air = air_year = pd.DataFrame()
    air_source = "Error"
    errors.append(("Air Quality", error))

try:
    flood_json, flood_source = load_surface_flooding_from_source(flood_url)
    flood = flood_table(flood_json)
    flood_year = filter_year(flood, year)
except Exception as error:
    flood = flood_year = pd.DataFrame()
    flood_source = "Error"
    errors.append(("Surface-Water Flooding", error))

try:
    rainfall_hourly, rainfall_daily, rainfall_source = load_rainfall_from_source(rain_url)
    rainfall_year = filter_year(rainfall_hourly, year)
except Exception as error:
    rainfall_hourly = rainfall_daily = rainfall_year = pd.DataFrame()
    rainfall_source = "Error"
    errors.append(("Rainfall", error))


# ------------------------------------------------------------
# PAGES
# ------------------------------------------------------------
if section == "Overview":
    st.header("Overview")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Selected year", year)
    c2.metric("Brownfield records", len(brown_year if not brown_year.empty else brown_lady))
    c3.metric("Air-quality records", len(air_year if not air_year.empty else air))
    c4.metric("Flooding features", len(flood_year if not flood_year.empty else flood))
    c5.metric("Rainfall records", len(rainfall_year if not rainfall_year.empty else rainfall_hourly))

    st.markdown(
        f"""
        <div class='info-card'>
        <b>Last dashboard load:</b> {loaded_at}<br>
        <b>Brownfield source:</b> {brown_source}<br>
        <b>Air-quality source:</b> {air_source}<br>
        <b>Surface-water flooding source:</b> {flood_source}<br>
        <b>Rainfall source:</b> {rainfall_source}
        </div>
        """,
        unsafe_allow_html=True,
    )

    for name, error in errors:
        st.error(f"{name} could not be loaded.")
        st.exception(error)

elif section == "Brownfield":
    st.header("Brownfield")
    st.caption(f"Source: {brown_source}")
    display = brown_year if not brown_year.empty else brown_lady
    q = st.text_input("Search brownfield records", "")
    display = search_rows(display, q)

    c1, c2, c3 = st.columns(3)
    c1.metric("All records", len(brown_all))
    c2.metric("Ladywood records", len(brown_lady))
    c3.metric("Shown", len(display))

    st.dataframe(display, use_container_width=True)
    chart_numeric(display, "brownfield")
    map_if_possible(display)

elif section == "Air Quality":
    st.header("Air Quality")
    st.caption(f"Source: {air_source}")
    display = air_year if not air_year.empty else air
    q = st.text_input("Search air-quality records", "")
    display = search_rows(display, q)

    st.metric("Records shown", len(display))
    chart_numeric(display, "air-quality")
    st.dataframe(display, use_container_width=True)

elif section == "Surface-Water Flooding":
    st.header("Surface-Water Flooding")
    st.caption(f"Source: {flood_source}")
    display = flood_year if not flood_year.empty else flood
    q = st.text_input("Search flooding records", "")
    display = search_rows(display, q)

    st.metric("Features shown", len(display))
    st.dataframe(display, use_container_width=True)
    map_if_possible(display)

elif section == "Rainfall":
    st.header("Rainfall")
    st.caption(f"Source: {rainfall_source}")

    tab1, tab2 = st.tabs(["Hourly / table", "Daily rainfall"])

    with tab1:
        display = rainfall_year if not rainfall_year.empty else rainfall_hourly
        q = st.text_input("Search rainfall records", "")
        display = search_rows(display, q)
        st.metric("Rainfall records shown", len(display))
        chart_numeric(display, "rainfall")
        st.dataframe(display, use_container_width=True)

    with tab2:
        if rainfall_daily.empty:
            st.info("No separate daily rainfall table was returned by this source.")
        else:
            chart_numeric(rainfall_daily, "daily rainfall")
            st.dataframe(rainfall_daily, use_container_width=True)

else:
    st.header("Data Sources")
    st.write("For brownfield and surface-water flooding, add a direct public file/API URL once. The app will refresh automatically after the cache time.")

    sources = pd.DataFrame(
        [
            {"Dataset": "Brownfield", "Source used": brown_source, "Configured URL": brownfield_url or "Not set; using local fallback"},
            {"Dataset": "Air Quality", "Source used": air_source, "Configured URL": air_url or "Not set"},
            {"Dataset": "Surface-Water Flooding", "Source used": flood_source, "Configured URL": flood_url or "Not set; using local fallback"},
            {"Dataset": "Rainfall", "Source used": rainfall_source, "Configured URL": rain_url or "Not set"},
        ]
    )
    st.dataframe(sources, use_container_width=True)

    st.subheader("Permanent configuration file")
    st.write("Create a file named data_sources.json in the same GitHub repo if you want permanent live source URLs.")
    st.code(
        json.dumps(
            {
                "brownfield_url": "PASTE_PUBLIC_BROWNFIELD_XLSX_OR_CSV_URL_HERE",
                "surface_water_flooding_url": "PASTE_PUBLIC_FLOODING_GEOJSON_URL_HERE",
                "air_quality_url": LIVE_AIR_QUALITY_API,
                "rainfall_url": LIVE_RAINFALL_API,
            },
            indent=2,
        ),
        language="json",
    )
