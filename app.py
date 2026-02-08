import math
import re

import requests
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
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",  # lon/lat, lon/lat
        "WIDTH": 101,
        "HEIGHT": 101,
        "X": 50,  # środek "rastra"
        "Y": 50,
        "FORMAT": "image/png",
        "INFO_FORMAT": "text/html",  # dostajemy gotowy HTML do wyświetlenia
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

    return text


# --- 3. INTERFEJS UŻYTKOWNIKA ---

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
                # zamiana [lat, lon] -> [lon, lat] odwrotnie – bo parsuj zakłada Lat,Lon
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

                # MPZP – zabezpieczone try/except, żeby w razie błędu nie rozwalić działania apki
                try:
                    st.session_state.mpzp_html = pobierz_mpzp_html(
                        przetworzone_punkty
                    )
                except Exception as e:
                    st.session_state.mpzp_html = (
                        f"<p><b>Błąd zapytania MPZP:</b> {e}</p>"
                    )
        else:
            st.warning("Wklej najpierw dane!")


# --- 4. WYŚWIETLANIE WYNIKÓW (MAPA + MPZP) ---

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
        st.subheader("Informacja o MPZP (Geoportal)")
        if st.session_state.mpzp_html:
            st.markdown(st.session_state.mpzp_html, unsafe_allow_html=True)
        else:
            st.info(
                "Brak informacji z usługi MPZP lub odpowiedź była pusta "
                "(działka prawdopodobnie bez obowiązującego MPZP lub błąd po stronie serwera)."
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

        # MPZP – overlay z KIMPZP
        folium.raster_layers.WmsTileLayer(
            url=(
                "https://mapy.geoportal.gov.pl/wss/ext/"
                "KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"
            ),
            layers="granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
            name="MPZP",
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

