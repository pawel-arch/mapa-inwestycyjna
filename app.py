import math
import re

import requests
from requests.exceptions import ReadTimeout, RequestException
import streamlit as st
import folium
from streamlit_folium import st_folium

# --- KONFIGURACJA STRONY ---
st.set_page_config(page_title="Mapa inwestycyjna + MPZP", layout="wide")

st.title("🗺️ Mapa Inwestycyjna + Kalkulator")
st.markdown(
    "Wklej współrzędne w dowolnym formacie (z nawiasami, bez, z przecinkami lub spacjami). "
    "Domyślnie przyjmujemy kolejność **Lat, Lon** (szerokość, długość)."
)

# --- INICJALIZACJA PAMIĘCI SESJI ---
if "punkty_mapy" not in st.session_state:
    st.session_state.punkty_mapy = None

if "wyniki_powierzchni" not in st.session_state:
    st.session_state.wyniki_powierzchni = None

if "mpzp_html" not in st.session_state:
    st.session_state.mpzp_html = None


# --- 1. FUNKCJE POMOCNICZE ---

def parsuj_wspolrzedne(tekst: str):
    """
    Wyciąga wszystkie liczby z tekstu i grupuje w pary [lat, lon].
    Akceptuje formaty z przecinkami, spacjami, nawiasami itd.
    """
    liczby = re.findall(r"-?\d+\.?\d*", tekst)
    liczby_float = [float(x) for x in liczby]

    # Jeśli liczba wartości jest nieparzysta, odetnij ostatnią
    if len(liczby_float) % 2 != 0:
        liczby_float = liczby_float[:-1]

    punkty = []
    for i in range(0, len(liczby_float), 2):
        # domyślnie: [lat, lon]
        punkty.append([liczby_float[i], liczby_float[i + 1]])

    return punkty


def oblicz_powierzchnie_m2(punkty):
    """
    Liczy przybliżoną powierzchnię wielokąta na podstawie punktów [lat, lon] (WGS84)
    wykorzystując rzutowanie na płaszczyznę i wzór Gaussa.
    Zwraca pole w m2.
    """
    if not punkty:
        return 0.0

    # środek geometryczny (do rzutowania)
    center_lat = sum(p[0] for p in punkty) / len(punkty)
    center_lon = sum(p[1] for p in punkty) / len(punkty)

    R = 6378137  # promień Ziemi
    lat_rad = math.radians(center_lat)
    metry_na_stopien_lat = 111132.954
    metry_na_stopien_lon = (math.pi / 180) * R * math.cos(lat_rad)

    # rzutowanie na płaszczyznę
    xy = []
    for lat, lon in punkty:
        y = (lat - center_lat) * metry_na_stopien_lat
        x = (lon - center_lon) * metry_na_stopien_lon
        xy.append((x, y))

    # wzór Gaussa (shoelace)
    area = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        area += x1 * y2 - x2 * y1

    return abs(area) / 2.0


# --- 2. MPZP: FUNKCJA POBIERANIA INFORMACJI Z KIMPZP ---

def pobierz_mpzp_html(punkty):
    """
    punkty – lista [lat, lon] w WGS84 (EPSG:4326).
    Zwraca HTML z odpowiedzi GetFeatureInfo z usługi
    Krajowa Integracja Miejscowych Planów Zagospodarowania Przestrzennego (KIMPZP).

    Jeśli usługa nie odpowie / zwróci błąd, zwracamy tekstowy komunikat w HTML.
    """
    if not punkty:
        return "<p>Brak punktów do zapytania MPZP.</p>"

    # środek wielokąta
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # małe okno w stopniach (ok. 10 m w każdą stronę)
    # 1 stopień ~ 111 km, więc 10 m ~ 0.00009°
    delta_deg = 0.0001
    min_lon = center_lon - delta_deg
    max_lon = center_lon + delta_deg
    min_lat = center_lat - delta_deg
    max_lat = center_lat + delta_deg

    url = (
        "https://mapy.geoportal.gov.pl/wss/ext/"
        "KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"
    )

    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetFeatureInfo",
        "VERSION": "1.1.1",
        "SRS": "EPSG:4326",
        # warstwy standardowo używane w KIMPZP
        "LAYERS": "granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
        "QUERY_LAYERS": "granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",  # lon/lat,lon/lat
        "WI


