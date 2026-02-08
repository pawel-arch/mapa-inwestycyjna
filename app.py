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
st.set_page_config(page_title="Mapa Inwestycyjna (Uniwersalna)", layout="wide")

st.title("🗺️ Mapa Inwestycyjna + MPZP (Polska)")
st.markdown(
    "Uniwersalne narzędzie do sprawdzania MPZP w całej Polsce (przez Integrację Krajową GUGiK). "
    "Wklej współrzędne, aby sprawdzić, czy gmina udostępnia dane wektorowe."
)

# Transformer WGS84 -> Web Mercator (EPSG:4326 -> EPSG:3857)
transformer_4326_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


# --- 1. FUNKCJE POMOCNICZE (WSPÓŁRZĘDNE I POWIERZCHNIA) ---

def parsuj_wspolrzedne(tekst: str):
    """Parsuje tekst na listę punktów [lat, lon]."""
    liczby = re.findall(r"-?\d+\.?\d*", tekst)
    liczby_float = [float(x) for x in liczby]

    if len(liczby_float) % 2 != 0:
        liczby_float = liczby_float[:-1]

    punkty = []
    for i in range(0, len(liczby_float), 2):
        punkty.append([liczby_float[i], liczby_float[i + 1]])

    return punkty


def oblicz_powierzchnie_m2(punkty):
    """Liczy powierzchnię w m2 (wzór Gaussa)."""
    if not punkty:
        return 0.0

    center_lat = sum(p[0] for p in punkty) / len(punkty)
    center_lon = sum(p[1] for p in punkty) / len(punkty)

    R = 6378137
    lat_rad = math.radians(center_lat)
    metry_na_stopien_lat = 111132.954
    metry_na_stopien_lon = (math.pi / 180) * R * math.cos(lat_rad)

    xy = []
    for lat, lon in punkty:
        y = (lat - center_lat) * metry_na_stopien_lat
        x = (lon - center_lon) * metry_na_stopien_lon
        xy.append((x, y))

    area = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        area += x1 * y2 - x2 * y1

    return abs(area) / 2.0


# --- 2. MPZP: UNIWERSALNY POBIERACZ (GUGiK + FALLBACK) ---

def pobierz_mpzp_z_wfs(punkty):
    """
    Pobiera dane MPZP.
    Strategia:
    1. Próba z Integratora Krajowego (GUGiK) - działa dla Przejazdowa i większości Polski.
    2. Jeśli brak danych -> Próba z lokalnego serwera Wieliczki (fallback na życzenie).
    """
    if not punkty:
        raise ValueError("Brak punktów do zapytania WFS MPZP.")

    # 1. Obliczenia geometryczne
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # Transformacja do EPSG:3857 (metry)
    x_3857, y_3857 = transformer_4326_3857.transform(center_lon, center_lat)

    # BBOX (okno wyszukiwania) - 20 metrów wokół środka działki
    delta_m = 20.0
    bbox_str = f"{x_3857 - delta_m},{y_3857 - delta_m},{x_3857 + delta_m},{y_3857 + delta_m},EPSG:3857"

    # --- KONFIGURACJA ŹRÓDEŁ ---
    
    # Źródło 1: GUGiK (Cała Polska)
    url_gugik = "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"
    layers_gugik = "app.RysunkiAktuPlanowania.MPZP,app.DokumentFormalny.MPZP"
    
    # Źródło 2: Wieliczka (Lokalny Geo-System - Fallback)
    url_wieliczka = "https://mpzp.igeomap.pl/cgi-bin/121905"
    layers_wieliczka = "mpzp"

    def wykonaj_zapytanie(url, layers, nazwa_zrodla):
        """Pomocnicza funkcja wykonująca request do konkretnego WFS"""
        params = {
            "SERVICE": "WFS",
            "VERSION": "1.1.0",
            "REQUEST": "GetFeature",
            "TYPENAME": layers,
            "SRSNAME": "EPSG:3857",
            "BBOX": bbox_str,
            "MAXFEATURES": "5" # Pobieramy max 5 obiektów
        }
        try:
            # Timeout 15s
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            
            # Parsowanie XML
            root = ET.fromstring(resp.content)
            
            # Szukanie featureMember (z namespace lub bez)
            ns = {"gml": "http://www.opengis.net/gml"}
            fm = root.find(".//gml:featureMember", ns)
            if fm is None:
                fm = root.find(".//featureMember")
            
            # Sprawdzenie czy w środku jest jakikolwiek obiekt
            if fm is not None and list(fm):
                return fm # Sukces - mamy dane
            else:
                return None # Pusty wynik (brak planu w tym miejscu)
                
        except Exception as e:
            # Błędy połączenia ignorujemy w tej funkcji, by spróbować kolejnego źródła
            # print(f"Błąd źródła {nazwa_zrodla}: {e}")
            return None

    # --- LOGIKA PRZEŁĄCZANIA ---
    
    # KROK 1: Próba GUGiK (Polska)
    feature_member = wykonaj_zapytanie(url_gugik, layers_gugik, "GUGiK")
    zrodlo_sukces = "Krajowa Integracja MPZP (GUGiK)"
    
    # KROK 2: Jeśli GUGiK pusty -> Próba Wieliczka (Lokalny)
    if feature_member is None:
        feature_member = wykonaj_zapytanie(url_wieliczka, layers_wieliczka, "Wieliczka")
        zrodlo_sukces = "Lokalny Serwer (Geo-System)"

    # Jeśli nadal nic:
    if feature_member is None:
        raise RuntimeError(
            "Nie udało się pobrać danych wektorowych MPZP.<br>"
            "Możliwe przyczyny:<br>"
            "1. Gmina nie udostępnia planu w formacie wektorowym (tylko obrazek).<br>"
            "2. Wskazany punkt leży poza obszarem objętym planem (np. droga).<br>"
            "3. Błąd komunikacji z serwerami rządowymi."
        )

    # --- PRZETWARZANIE WYNIKU ---
    
    # Wyciągamy właściwy obiekt (Feature)
    feature = None
    for child in feature_member:
        if isinstance(child.tag, str):
            feature = child
            break

    if feature is None:
        raise RuntimeError("Błąd struktury XML (znaleziono featureMember, ale pusty).")

    # Wyciągamy atrybuty tekstowe
    props = {}
    for child in feature:
        if not isinstance(child.tag, str): continue
        local_name = child.tag.split("}")[-1]
        
        # Ignorujemy geometrię
        if local_name.lower() in ("geom", "geometry", "the_geom", "msgeometry", "shape", "boundedby"):
            continue
            
        text = (child.text or "").strip()
        if text:
            props[local_name] = text

    if not props:
        raise RuntimeError("Znaleziono obiekt MPZP, ale nie posiada on czytelnych atrybutów tekstowych.")

    # Budujemy HTML
    rows = []
    for k, v in props.items():
        # Ładne formatowanie tabeli
        rows.append(f"<tr><td style='font-weight:bold; background-color:#f0f2f6; width:30%;'>{k}</td><td>{v}</td></tr>")

    html = (
        f"<div style='margin-bottom:10px; font-size:0.9em; color:green;'>✅ Źródło danych: {zrodlo_sukces}</div>"
        "<div style='overflow-x:auto;'>"
        "<table border='1' style='border-collapse: collapse; width:100%; border:1px solid #ddd; font-family:sans-serif;'>"
        "<tbody>" + "".join(rows) + "</tbody></table></div>"
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
    st.caption("Sprawdź, czy Twoja działka jest objęta wektorowym planem zagospodarowania.")

    dane_wejsciowe = st.text_area(
        "Wklej współrzędne:",
        height=300,
        help="Wklej punkty z Geoportalu lub Google Maps.",
    )

    zamien_kolejnosc = st.checkbox(
        "🔄 Zamień kolejność (Lat ↔ Lon)",
        value=False,
        help="Zaznacz, jeśli dane to Długość, Szerokość (np. 18.6, 54.3 z Geoportalu dla Przejazdowa).",
    )
    
    st.info("Dla Przejazdowa (Gdańsk) szerokość to ~54.3, a długość ~18.6.")

    if st.button("🚀 GENERUJ MAPĘ", use_container_width=True):
        if dane_wejsciowe:
            # 1. Parsowanie
            surowe_punkty = parsuj_wspolrzedne(dane_wejsciowe)

            # 2. Zamiana kolejności jeśli trzeba
            if zamien_kolejnosc:
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
                st.session_state.mpzp_html = None # Reset poprzedniego wyniku
                try:
                    with st.spinner("Pytam Integrator Krajowy (GUGiK)..."):
                        html_res = pobierz_mpzp_z_wfs(finalne_punkty)
                        st.session_state.mpzp_html = html_res
                except Exception as e:
                    st.session_state.mpzp_html = (
                        f"<div style='color:#a94442; background-color:#f2dede; border-color:#ebccd1; padding:15px; border-radius:4px;'>"
                        f"<b>Brak danych MPZP dla tej lokalizacji.</b><br><br>"
                        f"<i>Komunikat systemu:</i> {e}</div>"
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
        st.subheader("📋 Parametry z MPZP (Wektorowe)")
        if st.session_state.mpzp_html:
            st.markdown(st.session_state.mpzp_html, unsafe_allow_html=True)
        else:
            st.info("Oczekiwanie na dane...")

        st.markdown("---")

        # Mapa
        srodek = punkty[0]
        m = folium.Map(location=srodek, zoom_start=17)

        # Warstwy
        folium.raster_layers.WmsTileLayer(
            url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution",
            layers="Raster", name="Ortofotomapa", fmt="image/png", transparent=True, attr="GUGiK"
        ).add_to(m)

        folium.raster_layers.WmsTileLayer(
            url="https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow",
            layers="dzialki", name="Działki Ewid.", fmt="image/png", transparent=True, attr="GUGiK"
        ).add_to(m)

        # Warstwa wizualna MPZP (rysunek planu) - zawsze warto widzieć, nawet jak nie ma wektora
        folium.raster_layers.WmsTileLayer(
            url="https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego",
            layers="granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
            name="Rysunek MPZP", fmt="image/png", transparent=True, attr="GUGiK MPZP"
        ).add_to(m)

        # Poligon działki
        folium.Polygon(
            locations=punkty, color="#FF0000", weight=3, fill=True, fill_color="#FF0000", fill_opacity=0.2,
            popup=f"Powierzchnia: {wyniki['m2']:,.0f} m²"
        ).add_to(m)

        folium.LayerControl().add_to(m)
        st_folium(m, width=800, height=600)

        if st.button("Wyczyść wszystko"):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()
    else:
        st.info("👈 Wklej współrzędne działki z Przejazdowa (lub innej) po lewej stronie.")
