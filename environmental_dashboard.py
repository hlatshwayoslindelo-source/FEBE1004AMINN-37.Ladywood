from pathlib import Path
import json

import pandas as pd
import requests
import streamlit as st


# =========================
# PAGE SETTINGS
# =========================
st.set_page_config(page_title="Ladywood Environmental Dashboard", layout="wide")

st.markdown("""
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
""", unsafe_allow_html=True)

st.title("Ladywood Environmental Dashboard")

st.markdown(
    "<div class='info-card'><b>MIIN Group 37 FEBE1004A</b><br>"
    "This interactive dashboard uses local uploaded data files for brownfield and surface-water flooding, "
    "while air quality and rainfall update from online live data sources.<br><br>"
    "<b>Members:</b><br>"
    "Sanelisiwe Ntshangase<br>"
    "Christopher Matsimela<br>"
    "Slindelokuhle Hlatshwayo</div>",
    unsafe_allow_html=True
)


# =========================
# FILES AND LIVE LINKS
# =========================
BASE_DIR = Path(__file__).parent

# These two files must be uploaded to the same GitHub folder as this Python file.
BROWNFIELD_FILE = BASE_DIR / "brownfield.xlsx"
FLOOD_FILE = BASE_DIR / "surface_water_flooding.json"

# Ladywood / Birmingham approximate coordinate
LATITUDE = 52.4797
LONGITUDE = -1.9027

# These two update from online APIs.
AIR_QUALITY_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={LATITUDE}&longitude={LONGITUDE}"
    "&hourly=pm10,pm2_5,nitrogen_dioxide,ozone"
    "&timezone=Europe/London"
)

RAINFALL_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LATITUDE}&longitude={LONGITUDE}"
    "&hourly=precipitation,rain,showers"
    "&timezone=Europe/London"
)

YEARS = [2026, 2025, 2024, 2023, 2022]


# =========================
# HELPER FUNCTIONS
# =========================
def clean_cols(df):
    df = df.copy()
    df.columns = (
        df.columns.astype(str)
        .str.replace("\xa0", " ", regex=False)
        .str.strip()
    )
    return df


def make_numeric(df):
    if df.empty:
        return df

    output = df.copy()

    for col in output.columns:
        if output[col].dtype == "object":
            converted = (
                output[col]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.replace(" ", "", regex=False)
            )
            numeric = pd.to_numeric(converted, errors="coerce")

            if numeric.notna().sum() > 0:
                output[col] = numeric

    return output


def find_col(df, words):
    for col in df.columns:
        low = str(col).lower()
        if any(w in low for w in words):
            return col
    return None


def search_rows(df, text):
    if df.empty or not text:
        return df

    mask = df.astype(str).apply(
        lambda row: row.str.contains(text, case=False, na=False).any(),
        axis=1
    )
    return df[mask]


def filter_ladywood(df):
    if df.empty:
        return df

    mask = df.astype(str).apply(
        lambda row: row.str.contains("Ladywood", case=False, na=False).any(),
        axis=1
    )

    result = df[mask]
    return result if not result.empty else df


def filter_year(df, year):
    if df.empty:
        return df

    possible_year_columns = [
        col for col in df.columns
        if any(word in str(col).lower() for word in ["year", "date", "time", "updated", "added", "permission"])
    ]

    for col in possible_year_columns:
        dates = pd.to_datetime(df[col], errors="coerce", dayfirst=True)

        if dates.notna().sum() and (dates.dt.year == year).sum():
            return df[dates.dt.year == year]

        text_match = df[col].astype(str).str.contains(str(year), na=False)
        if text_match.sum():
            return df[text_match]

    return df


def map_if_possible(df):
    lat = find_col(df, ["lat", "latitude"])
    lon = find_col(df, ["lon", "lng", "longitude"])

    if not lat or not lon:
        st.info("No latitude/longitude columns detected for a map.")
        return

    map_df = df.copy()
    map_df[lat] = pd.to_numeric(map_df[lat], errors="coerce")
    map_df[lon] = pd.to_numeric(map_df[lon], errors="coerce")
    map_df = map_df.dropna(subset=[lat, lon])

    if not map_df.empty:
        st.map(map_df.rename(columns={lat: "lat", lon: "lon"})[["lat", "lon"]])
    else:
        st.info("Coordinate columns were found, but no valid points could be mapped.")


def first_coord(geometry):
    if not geometry:
        return None

    current = geometry.get("coordinates")

    try:
        while isinstance(current, list):
            if (
                len(current) >= 2
                and isinstance(current[0], (int, float))
                and isinstance(current[1], (int, float))
            ):
                lon = current[0]
                lat = current[1]
                return lat, lon

            current = current[0]
    except Exception:
        return None

    return None


def flood_table(geojson_data):
    rows = []

    for i, feature in enumerate(geojson_data.get("features", []), start=1):
        row = {
            "feature_number": i,
            "feature_id": feature.get("id", "")
        }

        properties = feature.get("properties", {})
        if isinstance(properties, dict):
            row.update(properties)

        point = first_coord(feature.get("geometry", {}))
        if point:
            row["lat"], row["lon"] = point

        rows.append(row)

    return pd.DataFrame(rows)


def show_chart_and_table(df, chart_label):
    if df.empty:
        st.warning("No records to show.")
        return

    numeric_cols = df.select_dtypes(include="number").columns.tolist()

    if numeric_cols:
        chosen = st.selectbox(f"Choose {chart_label} column to chart", numeric_cols)
        st.line_chart(df[chosen])
    else:
        st.info("The data loaded, but no numeric chart column was detected.")

    st.dataframe(df, use_container_width=True)


# =========================
# DATA LOADERS
# =========================
@st.cache_data(ttl=1800)
def load_brownfield():
    """
    Loads brownfield from the uploaded local Excel file.
    """
    if not BROWNFIELD_FILE.exists():
        raise FileNotFoundError("brownfield.xlsx was not found in the GitHub repo.")

    df = pd.read_excel(BROWNFIELD_FILE)
    return clean_cols(make_numeric(df))


@st.cache_data(ttl=1800)
def load_surface_water_flooding():
    """
    Loads surface-water flooding from the uploaded local GeoJSON/JSON file.
    """
    if not FLOOD_FILE.exists():
        raise FileNotFoundError("surface_water_flooding.json was not found in the GitHub repo.")

    with open(FLOOD_FILE, "r", encoding="utf-8") as file:
        geojson_data = json.load(file)

    df = flood_table(geojson_data)
    return clean_cols(make_numeric(df))


@st.cache_data(ttl=1800)
def load_air_quality():
    """
    Loads live hourly air-quality data from Open-Meteo.
    """
    response = requests.get(AIR_QUALITY_URL, timeout=30)
    response.raise_for_status()

    data = response.json()
    hourly = data.get("hourly", {})

    df = pd.DataFrame(hourly)
    return clean_cols(make_numeric(df))


@st.cache_data(ttl=1800)
def load_rainfall():
    """
    Loads live hourly rainfall and precipitation data from Open-Meteo.
    """
    response = requests.get(RAINFALL_URL, timeout=30)
    response.raise_for_status()

    data = response.json()
    hourly = data.get("hourly", {})

    df = pd.DataFrame(hourly)
    return clean_cols(make_numeric(df))


# =========================
# SIDEBAR
# =========================
section = st.sidebar.radio(
    "Section",
    [
        "Overview",
        "Brownfield",
        "Air Quality",
        "Rainfall",
        "Surface-Water Flooding",
        "Data Sources"
    ]
)

year = st.sidebar.selectbox("Year", YEARS, index=0)

st.sidebar.markdown("---")
st.sidebar.caption("Air quality and rainfall refresh every 30 minutes.")


# =========================
# LOAD DATA SAFELY
# =========================
errors = []

try:
    brown_all = load_brownfield()
    brown_lady = filter_ladywood(brown_all)
    brown_year = filter_year(brown_lady, year)
except Exception as e:
    brown_all = brown_lady = brown_year = pd.DataFrame()
    errors.append(("Brownfield", e))

try:
    flood_all = load_surface_water_flooding()
    flood_year = filter_year(flood_all, year)
except Exception as e:
    flood_all = flood_year = pd.DataFrame()
    errors.append(("Surface-Water Flooding", e))

try:
    air_all = load_air_quality()
    air_year = filter_year(air_all, year)
except Exception as e:
    air_all = air_year = pd.DataFrame()
    errors.append(("Air Quality", e))

try:
    rainfall_all = load_rainfall()
    rainfall_year = filter_year(rainfall_all, year)
except Exception as e:
    rainfall_all = rainfall_year = pd.DataFrame()
    errors.append(("Rainfall", e))


# =========================
# DASHBOARD SECTIONS
# =========================
if section == "Overview":
    st.header("Overview")

    c1, c2, c3, c4, c5 = st.columns(5)

    c1.metric("Selected year", year)
    c2.metric("Ladywood brownfield records", len(brown_year))
    c3.metric("Surface-water flooding features", len(flood_year if not flood_year.empty else flood_all))
    c4.metric("Air-quality records", len(air_year if not air_year.empty else air_all))
    c5.metric("Rainfall records", len(rainfall_year if not rainfall_year.empty else rainfall_all))

    st.markdown(
        "<div class='info-card'>"
        "Brownfield and surface-water flooding are loaded from the uploaded data files. "
        "Air quality and rainfall update from online APIs."
        "</div>",
        unsafe_allow_html=True
    )

    for name, error in errors:
        st.error(f"{name} could not be loaded.")
        st.exception(error)


elif section == "Brownfield":
    st.header("Brownfield")

    q = st.text_input("Search brownfield records", "")
    df = search_rows(brown_year, q)

    c1, c2, c3 = st.columns(3)
    c1.metric("All Birmingham records", len(brown_all))
    c2.metric("Ladywood records", len(brown_lady))
    c3.metric("Shown", len(df))

    st.dataframe(df, use_container_width=True)
    map_if_possible(df)


elif section == "Surface-Water Flooding":
    st.header("Surface-Water Flooding")

    q = st.text_input("Search flooding records", "")
    df = search_rows(flood_year if not flood_year.empty else flood_all, q)

    st.metric("Features shown", len(df))
    st.dataframe(df, use_container_width=True)
    map_if_possible(df)


elif section == "Air Quality":
    st.header("Air Quality")

    q = st.text_input("Search air-quality records", "")
    df = search_rows(air_year if not air_year.empty else air_all, q)

    st.metric("Records shown", len(df))
    show_chart_and_table(df, "air-quality")


elif section == "Rainfall":
    st.header("Rainfall")

    q = st.text_input("Search rainfall records", "")
    df = search_rows(rainfall_year if not rainfall_year.empty else rainfall_all, q)

    st.metric("Records shown", len(df))
    show_chart_and_table(df, "rainfall")


else:
    st.header("Data Sources")

    st.dataframe(
        pd.DataFrame([
            {
                "Dataset": "Brownfield",
                "Source type": "Uploaded local file",
                "File or URL": "brownfield.xlsx"
            },
            {
                "Dataset": "Surface-Water Flooding",
                "Source type": "Uploaded local file",
                "File or URL": "surface_water_flooding.json"
            },
            {
                "Dataset": "Air Quality",
                "Source type": "Live Open-Meteo API",
                "File or URL": AIR_QUALITY_URL
            },
            {
                "Dataset": "Rainfall",
                "Source type": "Live Open-Meteo API",
                "File or URL": RAINFALL_URL
            }
        ]),
        use_container_width=True
    )
