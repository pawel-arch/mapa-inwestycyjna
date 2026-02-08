import math
import re
import xml.etree.ElementTree as ET

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

# Typy obiektów – na podstawie dokumentacji innych gmin w mpzp.igeomap.pl
# (rysunki + dokumenty MPZP)
MPZP_WFS_TYPENAMES = "app.RysunkiAktuPlanowania.MPZP,app.DokumentFormalny.MPZP"

# Transformer WGS84 -> Web Mercator (EPSG:4326 -> EPSG:3857)
# Wiele usług MPZP WFS działa właśnie w 3857
transformer_4326_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


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


# --- 2. MPZP: WFS (mpzp.igeomap.pl) z GML w EPSG:3857 ---

def pobierz_mpzp_z_wfs(punkty):
    """
    Próbuje pobrać dane MPZP z usługi WFS (mpzp.igeomap.pl) dla gminy Wieliczka.

    1. Wyznacza środek wielokąta w EPSG:4326.
    2. Przelicza go do EPSG:3857 (metry w pseudo-Mercator).
    3. Robi BBOX wokół punktu.
    4. Robi GetFeature dla typów:
       - app.RysunkiAktuPlanowania.MPZP
       - app.DokumentFormalny.MPZP
    5. Parsuje GML i buduje HTML z atrybutami pierwszego obiektu.

    Jeśli nic nie znajdzie albo coś pójdzie nie tak – rzuca RuntimeError.
    """
    if not punkty:
        raise ValueError("Brak punktów do zapytania WFS MPZP.")

    # 1. środek wielokąta w WGS84
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # 2. transformacja do EPSG:3857 (x, y w metrach)
    x_3857, y_3857 = transformer_4326_3857.transform(center_lon, center_lat)

    # 3. okno w metrach (np. 50 m w każdą stronę – trochę większe niż wcześniej)
    delta_m = 50.0
    minx = x_3857 - delta_m
    maxx = x_3857 + delta_m
    miny = y_3857 - delta_m
    maxy = y_3857 + delta_m

    # 4. GetFeature dla z góry znanych typów APP.*
    getfeat_params = {
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": MPZP_WFS_TYPENAMES,
        "SRSNAME": "EPSG:3857",
        "BBOX": f"{minx},{miny},{maxx},{maxy},EPSG:3857",
        "MAXFEATURES": "10",
        "OUTPUTFORMAT": "GML2",  # zgodnie z przykładami z innych gmin
    }

    try:
        feat_resp = requests.get(MPZP_WFS_URL, params=getfeat_params, timeout=20)
        feat_resp.raise_for_status()
    except ReadTimeout:
        raise RuntimeError("Timeout przy GetFeature WFS MPZP (przekroczono limit 20 s).")
    except RequestException as e:
        raise RuntimeError(f"Błąd GetFeature WFS MPZP: {e}")

    # Parsowanie GML
    try:
        root_feat = ET.fromstring(feat_resp.content)
    except ET.ParseError as e:
        raise RuntimeError(f"Nie można sparsować GML z WFS MPZP: {e}")

    ns_gml = {"gml": "http://www.opengis.net/gml"}

    # Szukamy pierwszego featureMember
    fm = root_feat.find(".//gml:featureMember", ns_gml)
    if fm is None:
        raise RuntimeError("WFS MPZP nie zwrócił featureMember dla wskazanego obszaru.")

    # Pierwszy element potomny featureMember to zwykle sam obiekt (feature)
    feature = None
    for child in fm:
        if isinstance(child.tag, str):
            feature = child
            break

    if feature is None:
        raise RuntimeError("WFS MPZP: featureMember nie zawiera obiektu feature.")

    # Zbieramy atrybuty (pomijając geometrię)
    props = {}
    for child in feature:
        if not isinstance(child.tag, str):
            continue
        local_name = child.tag.split("}")[-1]
        # prosta heurystyka: pomijamy pola geometryczne
        if local_name.lower() in ("geom", "geometry", "the_geom", "msgeometry"):
            continue
        text = (child.text or "").strip()
        if text:
            props[local_name] = text

    if not props:
        raise RuntimeError("WFS MPZP zwrócił obiekt bez atrybutów (lub tylko geometrię).")

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
        m3.metric("Hektary", f"{wyniki['ha']:,.4f} ha")

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

        # MPZP – krajowy KIMPZP jako podgląd graficzny
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
