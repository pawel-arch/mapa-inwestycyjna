import math
import re

import requests
from requests.exceptions import ReadTimeout, RequestException
from pyproj import Transformer
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

# --- KONFIG MPZP (Wieliczka / mpzp.igeomap.pl) ---

# Adres usługi MPZP dla gminy Wieliczka (Geo-System / IGEOMAP)
MPZP_WFS_URL = "https://mpzp.igeomap.pl/cgi-bin/121905"

# Transformer WGS84 -> PUWG 1992 (EPSG:4326 -> EPSG:2180), potrzebny do BBOX w metrach
# always_xy=True: wejście jako (lon, lat)
transformer_4326_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)


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


# --- 2. MPZP: PRÓBA WFS (mpzp.igeomap.pl) ---

def pobierz_mpzp_z_wfs(punkty):
    """
    Próbuje pobrać dane MPZP z usługi WFS (mpzp.igeomap.pl) dla gminy Wieliczka.

    1. Wyznacza środek wielokąta w EPSG:4326.
    2. Przelicza go do EPSG:2180 (metry).
    3. Robi mały BBOX wokół punktu.
    4. Odczytuje WFS GetCapabilities, żeby znaleźć typeName.
    5. Robi GetFeature z BBOX i buduje prosty HTML z atrybutami pierwszego obiektu.

    Jeśli się nie uda – rzuca wyjątek, który łapiemy wyżej
    i możemy wtedy albo pokazać komunikat, albo spróbować czegoś innego.
    """
    if not punkty:
        raise ValueError("Brak punktów do zapytania WFS MPZP.")

    # 1. środek wielokąta w WGS84
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # 2. transformacja do EPSG:2180 (x, y w metrach)
    x_2180, y_2180 = transformer_4326_2180.transform(center_lon, center_lat)

    # 3. małe okno w metrach (np. 10 m w każdą stronę)
    delta_m = 10.0
    minx = x_2180 - delta_m
    maxx = x_2180 + delta_m
    miny = y_2180 - delta_m
    maxy = y_2180 + delta_m

    # 4. WFS GetCapabilities – bierzemy pierwszy FeatureType jako domyślny
    cap_params = {
        "SERVICE": "WFS",
        "REQUEST": "GetCapabilities",
        "VERSION": "1.1.0",
    }

    try:
        cap_resp = requests.get(MPZP_WFS_URL, params=cap_params, timeout=15)
        cap_resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Błąd GetCapabilities WFS MPZP: {e}")

    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(cap_resp.content)
    except ET.ParseError as e:
        raise RuntimeError(f"Nie można sparsować GetCapabilities WFS MPZP: {e}")

    ns = {
        "wfs": "http://www.opengis.net/wfs",
        "ows": "http://www.opengis.net/ows",
        "xsd": "http://www.w3.org/2001/XMLSchema",
    }

    type_names = [el.text for el in root.findall(".//wfs:FeatureType/wfs:Name", ns)]
    if not type_names:
        raise RuntimeError("Brak FeatureType w WFS MPZP (GetCapabilities).")

    # Na start bierzemy pierwszy typ z listy
    type_name = type_names[0]

    # 5. GetFeature z BBOX w EPSG:2180
    getfeat_params = {
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": type_name,
        "SRSNAME": "EPSG:2180",
        "BBOX": f"{minx},{miny},{maxx},{maxy},EPSG:2180",
        # próbujemy JSON – jeśli serwer nie obsługuje, dostaniemy błąd i poleci wyjątek
        "OUTPUTFORMAT": "application/json",
        "MAXFEATURES": "10",
    }

    try:
        feat_resp = requests.get(MPZP_WFS_URL, params=getfeat_params, timeout=20)
        feat_resp.raise_for_status()
    except ReadTimeout:
        raise RuntimeError("Timeout przy GetFeature WFS MPZP (przekroczono limit 20 s).")
    except RequestException as e:
        raise RuntimeError(f"Błąd GetFeature WFS MPZP: {e}")

    # Parsowanie JSON – zakładamy, że serwer przyjął outputFormat=application/json
    try:
        data = feat_resp.json()
    except ValueError:
        # Nie JSON – serwer zwrócił np. GML; można dalej rozbudować, ale na razie uznajemy za błąd
        raise RuntimeError("WFS MPZP nie zwrócił JSON (OUTPUTFORMAT=application/json).")

    features = data.get("features", [])
    if not features:
        raise RuntimeError("WFS MPZP nie zwrócił obiektów dla wskazanego obszaru.")

    # Bierzemy pierwszy obiekt i wypisujemy jego atrybuty
    props = features[0].get("properties", {})

    if not props:
        raise RuntimeError("WFS MPZP zwrócił obiekt bez atrybutów.")

    # Budujemy prosty HTML z tabelką atrybutów
    rows = []
    for k, v in props.items():
        rows.append(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>")

    html = (
        "<p><b>MPZP (WFS, mpzp.igeomap.pl – Wieliczka)</b></p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )

    return html


# --- 3. SESSION STATE ---

if "punkty_mapy" not in st.session_state:
    st.session_state.punkty_mapy = None

if "wyniki_powierzchni" not in st.session_state:
    st.session_state.wyniki_powierzchni = None

if "mpzp_html" not in st.session_state:
    st.session_state.mpzp_html = None


# --- 4. INTERFEJS UŻYTKOWNIKA ---

col_input, col_map = st.columns([1, 2])

with col_input:
    st.subheader("1. Dane wejściowe")

    dane_wejsciowe = st.text_area(
        "Wklej współrzędne:",
        height=300,
        help="Program sam znajdzie liczby i zignoruje resztę tekstu.",
    )

    zamien_kolejnosc = st.checkbox(
        "🔄 Zamień kolejność (Lat ↔ Lon)",
        value=False,
        help="Zaznacz, jeśli wklejasz współrzędne w formacie Lon, Lat.",
    )

    st.caption(
        "Przykład Lat, Lon: `52.1234 21.1234`. "
        "Przykład Lon, Lat (np. z Geoportalu): `21.1234 52.1234` – wtedy zaznacz checkbox powyżej."
    )

    if st.button("🚀 GENERUJ MAPĘ", use_container_width=True):
        if dane_wejsciowe:
            przetworzone_punkty = parsuj_wspolrzedne(dane_wejsciowe)

            if zamien_kolejnosc:
                # parsuj zakłada Lat,Lon, więc przy Lon,Lat zamieniamy miejscami
                przetworzone_punkty = [[p[1], p[0]] for p in przetworzone_punkty]

            if len(przetworzone_punkty) < 3:
                st.error("Za mało punktów (minimum 3).")
            else:
                # zapis do pamięci sesji
                st.session_state.punkty_mapy = przetworzone_punkty

                # obliczenia powierzchni
                pole_m2 = oblicz_powierzchnie_m2(przetworzone_punkty)
                st.session_state.wyniki_powierzchni = {
                    "m2": pole_m2,
                    "ar": pole_m2 / 100.0,
                    "ha": pole_m2 / 10000.0,
                }

                # MPZP – próbujemy WFS; jeśli się nie uda, pokażemy komunikat
                try:
                    st.session_state.mpzp_html = pobierz_mpzp_z_wfs(
                        przetworzone_punkty
                    )
                except Exception as e:
                    st.session_state.mpzp_html = (
                        "<p><b>MPZP (WFS):</b> nie udało się pobrać danych z serwera "
                        f"mpzp.igeomap.pl dla tej lokalizacji. Szczegóły: {e}</p>"
                    )
        else:
            st.warning("Wklej najpierw dane!")


# --- 5. WYŚWIETLANIE WYNIKÓW (MAPA + MPZP) ---

with col_map:
    if st.session_state.punkty_mapy is not None:
        punkty = st.session_state.punkty_mapy
        wyniki = st.session_state.wyniki_powierzchni

        # metryki powierzchni
        m1, m2c, m3 = st.columns(3)
        m1.metric("Metry kwadratowe", f"{wyniki['m2']:,.0f} m²")
        m2c.metric("Ary", f"{wyniki['ar']:.2f} ar")
        m3.metric("Hektary", f"{wyniki['ha']:.4f} ha")

        # MPZP – informacja tekstowa
        st.subheader("Informacja o MPZP (WFS – mpzp.igeomap.pl / Wieliczka)")
        if st.session_state.mpzp_html:
            st.markdown(st.session_state.mpzp_html, unsafe_allow_html=True)
        else:
            st.info(
                "Brak informacji z WFS MPZP lub odpowiedź była pusta. "
                "Możliwe, że dla tej działki brak wektorowego planu lub serwer nie zwrócił obiektu."
            )

        # mapa Folium
        srodek = punkty[0]  # [lat, lon]
        m = folium.Map(location=srodek, zoom_start=18)

        # Ortofotomapa
        folium.raster_layers.WmsTileLayer(
            url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution",
            layers="Raster",
            name="Ortofotomapa",
            fmt="image/png",
            transparent=True,
            attr="GUGiK",
        ).add_to(m)

        # Działki (Krajowa Integracja EGiB)
        folium.raster_layers.WmsTileLayer(
            url="https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow",
            layers="dzialki",
            name="Działki",
            fmt="image/png",
            transparent=True,
            attr="GUGiK",
        ).add_to(m)

        # MPZP – możesz zostawić krajowy KIMPZP jako podgląd graficzny
        folium.raster_layers.WmsTileLayer(
            url=(
                "https://mapy.geoportal.gov.pl/wss/ext/"
                "KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"
            ),
            layers="granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
            name="MPZP (krajowy)",
            fmt="image/png",
            transparent=True,
            attr="GUGiK / Krajowa Integracja MPZP",
        ).add_to(m)

        # poligon działki
        folium.Polygon(
            locations=punkty,
            color="red",
            weight=3,
            fill=True,
            fill_color="blue",
            fill_opacity=0.3,
            popup=f"Powierzchnia: {wyniki['m2']:,.0f} m²",
        ).add_to(m)

        # kontrola warstw
        folium.LayerControl().add_to(m)

        # render mapy w Streamlit
        st_folium(m, width=800, height=600)

        # przycisk czyszczenia
        if st.button("Wyczyść mapę"):
            st.session_state.punkty_mapy = None
            st.session_state.wyniki_powierzchni = None
            st.session_state.mpzp_html = None
            st.rerun()
    else:
        st.info("Wklej współrzędne po lewej stronie i kliknij „GENERUJ MAPĘ”.")
