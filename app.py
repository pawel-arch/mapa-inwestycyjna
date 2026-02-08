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

# --- KONFIGURACJA MPZP (NAPRAWIONA) ---

# Używamy Integratora Krajowego GUGiK.
# To usługa, która "wie", gdzie szukać planów dla każdej gminy w Polsce
# i obsługuje standardowe nazwy warstw (app.Rysunki...).
MPZP_WFS_URL = "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"

# Nazwy warstw specyficzne dla standardu krajowego
MPZP_WFS_TYPENAMES = "app.RysunkiAktuPlanowania.MPZP,app.DokumentFormalny.MPZP"

# Transformer WGS84 -> Web Mercator (EPSG:4326 -> EPSG:3857)
# Wiele usług MPZP WFS wymaga współrzędnych w metrach (3857)
transformer_4326_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


# --- 1. FUNKCJE POMOCNICZE ---

def parsuj_wspolrzedne(tekst: str):
    """
    Wyciąga wszystkie liczby z tekstu i grupuje w pary [lat, lon].
    """
    liczby = re.findall(r"-?\d+\.?\d*", tekst)
    liczby_float = [float(x) for x in liczby]

    # Jeśli liczba wartości jest nieparzysta, odetnij ostatnią
    if len(liczby_float) % 2 != 0:
        liczby_float = liczby_float[:-1]

    punkty = []
    for i in range(0, len(liczby_float), 2):
        # Domyślnie zakładamy [Lat, Lon]
        punkty.append([liczby_float[i], liczby_float[i + 1]])

    return punkty


def oblicz_powierzchnie_m2(punkty):
    """
    Liczy przybliżoną powierzchnię wielokąta (wzór Gaussa + rzutowanie).
    """
    if not punkty:
        return 0.0

    # Środek geometryczny
    center_lat = sum(p[0] for p in punkty) / len(punkty)
    center_lon = sum(p[1] for p in punkty) / len(punkty)

    R = 6378137  # promień Ziemi
    lat_rad = math.radians(center_lat)
    metry_na_stopien_lat = 111132.954
    metry_na_stopien_lon = (math.pi / 180) * R * math.cos(lat_rad)

    # Rzutowanie na płaszczyznę
    xy = []
    for lat, lon in punkty:
        y = (lat - center_lat) * metry_na_stopien_lat
        x = (lon - center_lon) * metry_na_stopien_lon
        xy.append((x, y))

    # Wzór Gaussa (shoelace formula)
    area = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        area += x1 * y2 - x2 * y1

    return abs(area) / 2.0


# --- 2. MPZP: WFS (Integrator Krajowy) ---

def pobierz_mpzp_z_wfs(punkty):
    """
    Pobiera dane MPZP z Krajowej Integracji GUGiK.
    """
    if not punkty:
        raise ValueError("Brak punktów do zapytania WFS MPZP.")

    # 1. Środek wielokąta w WGS84
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # 2. Transformacja do EPSG:3857 (x, y w metrach)
    # UWAGA: transformer.transform(lon, lat) bo always_xy=True
    x_3857, y_3857 = transformer_4326_3857.transform(center_lon, center_lat)

    # 3. BBOX (okno wyszukiwania) - np. 20 metrów wokół środka
    delta_m = 20.0
    minx = x_3857 - delta_m
    maxx = x_3857 + delta_m
    miny = y_3857 - delta_m
    maxy = y_3857 + delta_m

    # 4. Parametry zapytania WFS GetFeature
    getfeat_params = {
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": MPZP_WFS_TYPENAMES,
        "SRSNAME": "EPSG:3857",
        "BBOX": f"{minx},{miny},{maxx},{maxy},EPSG:3857",
        "MAXFEATURES": "5", # Wystarczy kilka obiektów
    }

    try:
        # Zwiększony timeout do 30s, bo usługi krajowe bywają wolne
        feat_resp = requests.get(MPZP_WFS_URL, params=getfeat_params, timeout=30)
        feat_resp.raise_for_status()
    except ReadTimeout:
        raise RuntimeError("Serwer GUGiK nie odpowiedział w ciągu 30 sekund (Timeout).")
    except RequestException as e:
        raise RuntimeError(f"Błąd połączenia z WFS: {e}")

    # Parsowanie XML (GML)
    try:
        root_feat = ET.fromstring(feat_resp.content)
    except ET.ParseError as e:
        # Czasami serwer zwraca błąd tekstowy zamiast XML
        raise RuntimeError(f"Nie można odczytać odpowiedzi XML: {e}. Treść: {feat_resp.text[:100]}...")

    # Namespaces (przestrzenie nazw) w GML
    # GUGiK często używa 'gml' lub domyślnych
    ns = {"gml": "http://www.opengis.net/gml"}

    # Szukamy featureMember (dowolnego obiektu w odpowiedzi)
    # W zależności od wersji GML może to być gml:featureMember lub featureMember
    fm = root_feat.find(".//gml:featureMember", ns)
    if fm is None:
        fm = root_feat.find(".//featureMember") # próba bez namespace
    
    if fm is None:
        raise RuntimeError("Serwer zwrócił pusty wynik (brak obiektu 'featureMember'). Prawdopodobnie brak wektorowego planu w tym punkcie.")

    # Wyciągamy pierwszy znaleziony obiekt
    feature = None
    for child in fm:
        # Szukamy elementu, który jest tagiem (obiektem)
        if isinstance(child.tag, str):
            feature = child
            break

    if feature is None:
        raise RuntimeError("Znaleziono featureMember, ale jest pusty.")

    # Parsowanie atrybutów do tabelki HTML
    props = {}
    for child in feature:
        if not isinstance(child.tag, str):
            continue
        # Usuwamy namespace z nazwy pola (np. {http://...}nazwa -> nazwa)
        local_name = child.tag.split("}")[-1]
        
        # Pomijamy geometrię (nie chcemy wyświetlać tysięcy współrzędnych)
        if local_name.lower() in ("geom", "geometry", "the_geom", "msgeometry", "shape"):
            continue
            
        text = (child.text or "").strip()
        if text:
            props[local_name] = text

    if not props:
        raise RuntimeError("Obiekt nie posiada czytelnych atrybutów tekstowych.")

    # Budowanie tabelki HTML
    rows = []
    for k, v in props.items():
        rows.append(f"<tr><td style='font-weight:bold; background-color:#f0f2f6;'>{k}</td><td>{v}</td></tr>")

    html = (
        "<div style='overflow-x:auto;'>"
        "<table border='1' style='border-collapse: collapse; width:100%; border:1px solid #ddd;'>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )

    return html


# --- 3. SESSION STATE (Pamięć podręczna aplikacji) ---

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
        help="Zaznacz, jeśli Twoje dane to Długość, Szerokość (np. z Geoportalu: 21.01, 52.22).",
    )

    st.info(
        "Domyślny format: Szerokość (50...), Długość (20...).\n"
        "Jeśli po wygenerowaniu mapa pokazuje Afrykę/Ocean -> Zaznacz checkbox powyżej."
    )

    if st.button("🚀 GENERUJ MAPĘ", use_container_width=True):
        if dane_wejsciowe:
            # 1. Parsowanie
            surowe_punkty = parsuj_wspolrzedne(dane_wejsciowe)

            # 2. Ewentualna zamiana kolejności
            if zamien_kolejnosc:
                # Zamieniamy [Lon, Lat] -> [Lat, Lon]
                finalne_punkty = [[p[1], p[0]] for p in surowe_punkty]
            else:
                finalne_punkty = surowe_punkty

            if len(finalne_punkty) < 3:
                st.error("Za mało punktów (minimum 3).")
            else:
                # 3. Zapis do sesji
                st.session_state.punkty_mapy = finalne_punkty

                # 4. Obliczenia
                pole_m2 = oblicz_powierzchnie_m2(finalne_punkty)
                st.session_state.wyniki_powierzchni = {
                    "m2": pole_m2,
                    "ar": pole_m2 / 100.0,
                    "ha": pole_m2 / 10000.0,
                }

                # 5. Pobranie MPZP (z obsługą błędów)
                try:
                    with st.spinner("Pobieranie danych o MPZP z GUGiK..."):
                        html_res = pobierz_mpzp_z_wfs(finalne_punkty)
                        st.session_state.mpzp_html = html_res
                except Exception as e:
                    st.session_state.mpzp_html = (
                        f"<div style='color:red; padding:10px; border:1px solid red; background:#fff5f5;'>"
                        f"<b>Nie udało się pobrać danych MPZP.</b><br>Przyczyna: {e}</div>"
                    )
        else:
            st.warning("Wklej najpierw dane!")


# --- 5. WYŚWIETLANIE WYNIKÓW ---

with col_map:
    if st.session_state.punkty_mapy is not None:
        punkty = st.session_state.punkty_mapy
        wyniki = st.session_state.wyniki_powierzchni

        # Metryki
        m1, m2c, m3 = st.columns(3)
        m1.metric("Metry kwadratowe", f"{wyniki['m2']:,.0f} m²")
        m2c.metric("Ary", f"{wyniki['ar']:.2f} ar")
        m3.metric("Hektary", f"{wyniki['ha']:,.4f} ha")

        st.markdown("---")
        
        # Sekcja MPZP
        st.subheader("📋 Informacja z MPZP (Krajowa Integracja)")
        if st.session_state.mpzp_html:
            st.markdown(st.session_state.mpzp_html, unsafe_allow_html=True)
        else:
            st.info("Brak danych MPZP w pamięci.")

        st.markdown("---")

        # Mapa
        srodek = punkty[0]
        m = folium.Map(location=srodek, zoom_start=17)

        # 1. Ortofotomapa
        folium.raster_layers.WmsTileLayer(
            url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution",
            layers="Raster",
            name="Ortofotomapa",
            fmt="image/png",
            transparent=True,
            attr="GUGiK",
        ).add_to(m)

        # 2. Działki
        folium.raster_layers.WmsTileLayer(
            url="https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow",
            layers="dzialki",
            name="Działki Ewid.",
            fmt="image/png",
            transparent=True,
            attr="GUGiK",
        ).add_to(m)

        # 3. MPZP (Warstwa wizualna - Integracja Krajowa)
        folium.raster_layers.WmsTileLayer(
            url="https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego",
            layers="granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
            name="MPZP (Rysunek)",
            fmt="image/png",
            transparent=True,
            attr="GUGiK MPZP",
        ).add_to(m)

        # Poligon
        folium.Polygon(
            locations=punkty,
            color="#FF0000",
            weight=3,
            fill=True,
            fill_color="#FF0000",
            fill_opacity=0.2,
            popup=f"Powierzchnia: {wyniki['m2']:,.0f} m²",
        ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width=800, height=600)

        # Reset
        if st.button("Wyczyść wszystko"):
            for key in ["punkty_mapy", "wyniki_powierzchni", "mpzp_html"]:
                st.session_state[key] = None
            st.rerun()
    else:
        st.info("👈 Wklej współrzędne w panelu po lewej i kliknij GENERUJ.")
