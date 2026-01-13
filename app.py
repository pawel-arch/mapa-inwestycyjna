import streamlit as st
import folium
from streamlit_folium import st_folium
import math
import re

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
    if not punkty: return 0
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

            # Tutaj był Twój błąd - teraz linia jest kompletna:
            if zamien_kolejnosc:
                przetworzone_punkty = [[p[1], p[0]] for p in przetworzone_punkty]

            if len(przetworzone_punkty) < 3:
                st.error("Za mało punktów (minimum 3).")
            else:
                # ZAPIS DO PAMIĘCI SESJI
                st.session_state.punkty_mapy = przetworzone_punkty

                # OBLICZENIA
                m2 = oblicz_powierzchnie_m2(przetworzone_punkty)
                st.session_state.wyniki_powierzchni = {
                    'm2': m2,
                    'ar': m2 / 100.0,
                    'ha': m2 / 10000.0
                }
        else:
            st.warning("Wklej najpierw dane!")

# --- 3. WYŚWIETLANIE WYNIKÓW (z pamięci sesji) ---
with col_map:
    # Jeśli w sesji są dane, wyświetl mapę (niezależnie od kliknięcia przycisku)
    if st.session_state.punkty_mapy is not None:
        punkty = st.session_state.punkty_mapy
        wyniki = st.session_state.wyniki_powierzchni

        # Wyświetlenie wyników liczbowych
        m1, m2, m3 = st.columns(3)
        m1.metric("Metry kwadratowe", f"{wyniki['m2']:,.0f} m²")
        m2.metric("Ary", f"{wyniki['ar']:.2f} ar")
        m3.metric("Hektary", f"{wyniki['ha']:.4f} ha")

        # Generowanie mapy Folium
        srodek = punkty[0]
        m = folium.Map(location=srodek, zoom_start=18)

        # Warstwa Ortofotomapy
        folium.raster_layers.WmsTileLayer(
            url='https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution',
            layers='Raster', name='Ortofotomapa', fmt='image/png', transparent=True, attr='GUGiK'
        ).add_to(m)

        # Warstwa Działek
        folium.raster_layers.WmsTileLayer(
            url='https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow',
            layers='dzialki', name='Działki', fmt='image/png', transparent=True, attr='GUGiK'
        ).add_to(m)

        # Rysowanie obszaru
        folium.Polygon(
            locations=punkty,
            color="red", weight=3, fill=True, fill_color="blue", fill_opacity=0.3,
            popup=f"Powierzchnia: {wyniki['m2']:,.0f} m²"
        ).add_to(m)

        # Wyrenderowanie mapy
        st_folium(m, width=800, height=600)

        # Przycisk czyszczenia
        if st.button("Wyczyść mapę"):
            st.session_state.punkty_mapy = None
            st.rerun()