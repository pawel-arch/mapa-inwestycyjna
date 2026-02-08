import streamlit as st
import folium
from streamlit_folium import st_folium
import math
import re
import requests  # <-- NOWE: do zapytań HTTP (MPZP)

# --- KONFIGURACJA STRONY ---
st.set_page_config(page_title="Kalkulator Działek", layout="wide")

st.title("🗺️ Mapa Inwestycyjna + Kalkulator")
st.markdown("Wklej współrzędne w dowolnym formacie (z nawiasami, bez, z przecinkami lub spacjami).")

# --- INICJALIZACJA PAMIĘCI SESJI (SESSION STATE) ---
# Zapobiega znikaniu mapy po kliknięciu
if 'punkty_mapy' not in st.session_state:
    st.session_state.punkty_mapy = None
if 'wyniki_powierzchni' not in st.session_state:
    st.session_state.wyniki_powierzchni = None
if 'mpzp_html' not in st.session_state:
    st.session_state.mpzp_html = None  # <-- MPZP: przechowujemy ostatnią odpowiedź

# --- 1. FUNKCJE POMOCNICZE ---
def parsuj_wspolrzedne(tekst):
    # Znajdź wszystkie liczby w tekście
    liczby = re.findall(r'-?\d+\.?\d*', tekst)
    liczby_float = [float(x) for x in liczby]

    punkty = []
    # Usuń ostatnią liczbę jeśli jest nie do pary
    if len(liczby_float) % 2 != 0:
        liczby_float = liczby_float[:-1]

    # Grupuj po dwie (lat, lon)
    for i in range(0, len(liczby_float), 2):
        punkty.append([liczby_float[i], liczby_float[i+1]])
    return punkty

def oblicz_powierzchnie_m2(punkty):
    if not punkty:
        return 0
    # Oblicz środek geometryczny
    center_lat = sum(p[0] for p in punkty) / len(punkty)
    center_lon = sum(p[1] for p in punkty) / len(punkty)

    R = 6378137 # Promień Ziemi
    lat_rad = math.radians(center_lat)
    metry_na_stopien_lat = 111132.954
    metry_na_stopien_lon = (math.pi / 180) * R * math.cos(lat_rad)

    # Rzutowanie na płaszczyznę
    xy = []
    for lat, lon in punkty:
        y = (lat - center_lat) * metry_na_stopien_lat
        x = (lon - center_lon) * metry_na_stopien_lon
        xy.append((x, y))

    # Wzór Gaussa na pole powierzchni
    area = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0

# --- MPZP: FUNKCJA POBIERANIA INFORMACJI Z KIMPZP ---

def pobierz_mpzp_html(punkty):
    """
    punkty – lista [lat, lon] w WGS84 (EPSG:4326)
    Zwraca HTML z odpowiedzi GetFeatureInfo z usługi
    KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego.
    """
    if not punkty:
        return None

    # Środek wielokąta z Twoich punktów (w stopniach)
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # Małe okno w stopniach (~10 m w każdą stronę)
    # 1 stopień ≈ 111 km, więc 10 m ≈ 0.00009°
    delta_deg = 0.0001
    min_lon = center_lon - delta_deg
    max_lon = center_lon + delta_deg
    min_lat = center_lat - delta_deg
    max_lat = center_lat + delta_deg

    # Parametry WMS GetFeatureInfo (wersja 1.1.1 -> SRS + BBOX jako lon/lat)
    url = "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"

    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetFeatureInfo",
        "VERSION": "1.1.1",
        "SRS": "EPSG:4326",
        # typowe warstwy z przykładowego zapytania Geoportalu
        "LAYERS": "granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
        "QUERY_LAYERS": "granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "WIDTH": 101,
        "HEIGHT": 101,
        "X": 50,   # środek rastra
        "Y": 50,
        "FORMAT": "image/png",
        "INFO_FORMAT": "text/html",  # dostaniemy HTML gotowy do pokazania
        "TRANSPARENT": "TRUE",
    }

    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
    except Exception as e:
        return f"<p><b>Błąd pobierania informacji z MPZP:</b> {e}</p>"

    text = r.text.strip()
    if not text:
        return "<p>Brak informacji o MPZP (pusta odpowiedź usługi).</p>"

    # Czasami serwer może zwrócić lakoniczny komunikat; nie filtrujemy go na siłę,
    # bo zależy od konkretnej jednostki. Wyświetlamy „jak jest”.
    return text

# --- 2. INTERFEJS UŻYTKOWNIKA ---

col_input, col_map = st.columns([1, 2])

with col_input:
    st.subheader("1. Dane wejściowe")
    dan_wejsciowe = st.text_area(
        "Wklej współrzędne:",
        height=300,
        help="Program sam znajdzie liczby i zignoruje resztę tekstu."
    )

    zamien_kolejnosc = st.checkbox("🔄 Zamień kolejność (Lat <-> Lon)", value=False)

    # Przycisk uruchamia logikę i zapisuje do sesji
    if st.button("🚀 GENERUJ MAPĘ", use_container_width=True):
        if dan_wejsciowe:
            przetworzone_punkty = parsuj_wspolrzedne(dan_wejsciowe)

            if zamien_kolejnosc:
                przetworzone_punkty = [[p[1], p[0]] for p in przetworzone_punkty]

            if len(przetworzone_punkty) < 3:
                st.error("Za mało punktów (minimum 3).")
            else:
                # ZAPIS DO PAMIĘCI SESJI
                st.session_state.punkty_mapy = przetworzone_punkty

                # OBLICZENIA POWIERZCHNI
                m2 = oblicz_powierzchnie_m2(przetworzone_punkty)
                st.session_state.wyniki_powierzchni = {
                    'm2': m2,
                    'ar': m2 / 100.0,
                    'ha': m2 / 10000.0
                }

                # MPZP: pobieramy dane dla nowej działki i też zapisujemy w sesji
                st.session_state.mpzp_html = pobierz_mpzp_html(przetworzone_punkty)
        else:
            st.warning("Wklej najpierw dane!")

# --- 3. WYŚWIETLANIE WYNIKÓW (z pamięci sesji) ---
with col_map:
    # Jeśli w sesji są dane, wyświetl mapę (niezależnie od kliknięcia przycisku)
    if st.session_state.punkty_mapy is not None:
        punkty = st.session_state.punkty_mapy
        wyniki = st.session_state.wyniki_powierzchni

        # Wyświetlenie wyników liczbowych
        m1, m2c, m3 = st.columns(3)
        m1.metric("Metry kwadratowe", f"{wyniki['m2']:,.0f} m²")
        m2c.metric("Ary", f"{wyniki['ar']:.2f} ar")
