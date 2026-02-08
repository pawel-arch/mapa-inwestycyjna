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
    "Domyślnie przyjmujemy kolejność **Lat, Lon** (szerokość, długość geograficzna)."
)

# --- PROSTA KONFIGURACJA GMIN / MPZP LOKALNEGO (pod przyszłe rozszerzenia) ---

MPZP_LOCAL_CONFIG = {
    # Tu możesz potem dopisywać kolejne gminy z konkretnym WFS/WMS
    "Brak / nieznana": {},
    "Wieliczka": {
        "opis": "MPZP obsługiwany na razie tylko z Geoportalu (KIMPZP). "
                "Integracja lokalnego WFS w przygotowaniu."
    },
    # "Kraków": {...}
}


# --- 1. FUNKCJE POMOCNICZE (GEOMETRIA) ---

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


def policz_centroid(punkty):
    """
    Liczy prosty centroid (średnia arytmetyczna) w układzie [lat, lon].
    Wystarczy do zapytania WMS GetFeatureInfo.
    """
    if not punkty:
        return None
    lats = [p[0] for p in punkty]
    lons = [p[1] for p in punkty]
    return sum(lats) / len(lats), sum(lons) / len(lons)


# --- 2. MPZP – KRAJOWY (KIMPZP, WMS GetFeatureInfo) ---

def pobierz_mpzp_krajowy_html(punkty):
    """
    punkty – lista [lat, lon] w WGS84 (EPSG:4326).
    Zwraca HTML z odpowiedzi GetFeatureInfo z usługi
    Krajowa Integracja Miejscowych Planów Zagospodarowania Przestrzennego (KIMPZP).

    Jeśli usługa nie odpowie / zwróci błąd, zwracamy krótki HTML z komunikatem.
    Funkcja NIE rzuca wyjątków – wszystko łagodnie.
    """
    if not punkty:
        return "<p>Brak punktów do zapytania MPZP.</p>"

    centroid = policz_centroid(punkty)
    if centroid is None:
        return "<p>Nie udało się policzyć centroidu działki.</p>"

    center_lat, center_lon = centroid

    # małe okno w stopniach (ok. 10 m w każdą stronę)
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
        "LAYERS": "granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
        "QUERY_LAYERS": "granice,raster,wektor-str,wektor-lzb,wektor-lin,wektor-pow,wektor-pkt",
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "WIDTH": 101,
        "HEIGHT": 101,
        "X": 50,  # środek "rastra"
        "Y": 50,
        "FORMAT": "image/png",
        "INFO_FORMAT": "text/html",  # dostajemy gotowy HTML do wyświetlenia
        "TRANSPARENT": "TRUE",
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
    except ReadTimeout:
        return (
            "<p><b>MPZP (krajowy):</b> serwer Geoportalu nie odpowiedział w wyznaczonym czasie "
            "(limit 15 s). Spróbuj ponownie za chwilę lub sprawdź ręcznie w Geoportalu.</p>"
        )
    except RequestException as e:
        return f"<p><b>MPZP (krajowy):</b> błąd zapytania do Geoportalu: {e}</p>"

    text = r.text.strip()
    if not text:
        return "<p>MPZP (krajowy): brak informacji (pusta odpowiedź usługi).</p>"

    return text


def okresl_status_mpzp_krajowy(html: str) -> str:
    """
    Bardzo prosta heurystyka:
    - jeśli HTML pusty / komunikat o braku wyniku → 'Brak danych / możliwe, że brak planu lub tylko raster'
    - jeśli jest treść inna niż 'brak wyniku' → 'Plan prawdopodobnie obowiązuje (zobacz szczegóły poniżej)'
    """
    if not html:
        return "Brak danych z Krajowej Integracji MPZP."

    lower = html.lower()

    if "brak wyniku" in lower or "brak danych" in lower:
        return "Brak danych z MPZP dla tego punktu (możliwy brak planu lub tylko raster)."

    if "mpzp" in lower or "plan miejscowy" in lower or "uchwał" in lower:
        return "Plan miejscowy prawdopodobnie obowiązuje – szczegóły w sekcji MPZP (poniżej)."

    # fallback
    return "Odpowiedź z serwera MPZP wymaga ręcznego sprawdzenia (zobacz sekcję MPZP poniżej)."


# --- 3. MPZP – LOKALNY (STUB / POD ROZBUDOWĘ) ---

def pobierz_mpzp_lokalny_info(nazwa_gminy: str, punkty):
    """
    Stub na przyszłość – miejsce na integrację z lokalnym WFS/WMS.
    Na razie:
      - dla 'Wieliczka' komunikat, że integracja w toku,
      - dla innych gmin – informacja, że brak lokalnego źródła.
    """
    if nazwa_gminy not in MPZP_LOCAL_CONFIG or nazwa_gminy == "Brak / nieznana":
        return "Brak skonfigurowanego lokalnego źródła MPZP dla tej gminy."

    cfg = MPZP_LOCAL_CONFIG[nazwa_gminy]
    opis = cfg.get("opis") or "Lokalne źródło MPZP nie jest jeszcze w pełni zintegrowane."
    return opis


# --- 4. SESSION STATE ---

if "punkty_mapy" not in st.session_state:
    st.session_state.punkty_mapy = None

if "wyniki_powierzchni" not in st.session_state:
    st.session_state.wyniki_powierzchni = None

if "mpzp_krajowy_html" not in st.session_state:
    st.session_state.mpzp_krajowy_html = None

if "mpzp_krajowy_status" not in st.session_state:
    st.session_state.mpzp_krajowy_status = None

if "mpzp_lokalny_info" not in st.session_state:
    st.session_state.mpzp_lokalny_info = None

if "wybrana_gmina" not in st.session_state:
    st.session_state.wybrana_gmina = "Brak / nieznana"


# --- 5. INTERFEJS UŻYTKOWNIKA ---

col_input, col_map = st.columns([1, 2])

with col_input:
    st.subheader("1. Parametry działki")

    # NOWOŚĆ: wybór gminy (pod przyszły MPZP lokalny)
    gmina = st.selectbox(
        "Gmina (dla MPZP lokalnego):",
        options=list(MPZP_LOCAL_CONFIG.keys()),
        index=list(MPZP_LOCAL_CONFIG.keys()).index(st.session_state.wybrana_gmina),
    )
    st.session_state.wybrana_gmina = gmina

    dane_wejsciowe = st.text_area(
        "Wklej współrzędne:",
        height=250,
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

    if st.button("🚀 GENERUJ MAPĘ + RAPORT", use_container_width=True):
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

                # MPZP – krajowy (KIMPZP)
                html_krajowy = pobierz_mpzp_krajowy_html(przetworzone_punkty)
                st.session_state.mpzp_krajowy_html = html_krajowy
                st.session_state.mpzp_krajowy_status = okresl_status_mpzp_krajowy(html_krajowy)

                # MPZP – lokalny (stub pod przyszłą integrację)
                st.session_state.mpzp_lokalny_info = pobierz_mpzp_lokalny_info(
                    gmina, przetworzone_punkty
                )
        else:
            st.warning("Wklej najpierw współrzędne!")


# --- 6. WYŚWIETLANIE RAPORTU + MAPY ---

with col_map:
    if st.session_state.punkty_mapy is not None:
        punkty = st.session_state.punkty_mapy
        wyniki = st.session_state.wyniki_powierzchni
        gmina = st.session_state.wybrana_gmina

        # --- RAPORT ZBIORCZY ---
        st.subheader("📋 Raport dla działki")

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Powierzchnia [m²]", f"{wyniki['m2']:,.0f}")
        col_b.metric("Powierzchnia [ar]", f"{wyniki['ar']:.2f}")
        col_c.metric("Powierzchnia [ha]", f"{wyniki['ha']:.4f}")

        # centroid – do informacji
        centroid = policz_centroid(punkty)
        if centroid:
            lat_c, lon_c = centroid
            st.caption(f"Centroid działki (przybliżony): lat={lat_c:.6f}, lon={lon_c:.6f}")

        # status MPZP – krajowy
        st.markdown("### MPZP – krajowy (Geoportal, Krajowa Integracja MPZP)")
        if st.session_state.mpzp_krajowy_status:
            st.info(st.session_state.mpzp_krajowy_status)

        # status MPZP – lokalny
        st.markdown(f"### MPZP – lokalny ({gmina})")
        if st.session_state.mpzp_lokalny_info:
            st.write(st.session_state.mpzp_lokalny_info)

        st.markdown("---")
        st.markdown("### Szczegółowa odpowiedź z Krajowej Integracji MPZP (HTML)")

        if st.session_state.mpzp_krajowy_html:
            st.markdown(st.session_state.mpzp_krajowy_html, unsafe_allow_html=True)
        else:
            st.caption("Brak treści z serwera MPZP (możliwy brak planu lub błąd po stronie usługi).")

        st.markdown("---")
        st.markdown("### Mapa działki i warstw referencyjnych")

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

        # MPZP – krajowy overlay (rysunek planu)
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

        folium.LayerControl().add_to(m)

        st_folium(m, width=800, height=600)

        # przycisk czyszczenia
        if st.button("Wyczyść mapę i raport"):
            st.session_state.punkty_mapy = None
            st.session_state.wyniki_powierzchni = None
            st.session_state.mpzp_krajowy_html = None
            st.session_state.mpzp_krajowy_status = None
            st.session_state.mpzp_lokalny_info = None
            st.rerun()
    else:
        st.info("Wklej współrzędne po lewej stronie, wybierz gminę i kliknij „GENERUJ MAPĘ + RAPORT”.")
