# Optimizador de Work Areas (WA) — RouteSmart DRO / FedEx

Herramienta que toma el export **Stop Information** de RouteSmart DRO (CSP) y
propone una **reasignación optimizada de paquetes a Work Areas**, generando un
Excel con la propuesta y un mapa de agrupación por WA.

---

## 1. Reglas de negocio implementadas

| Regla | Implementación |
|-------|----------------|
| Cada WA regular: **100–200 entregas** | Balanceo capacitado; ninguna WA activa queda fuera de rango |
| **Peso individual** de una entrega | `Peso total ÷ Paquetes` |
| Entregas **> 100 kg/paquete → WA 2217** | Ruta nueva de sobrepeso, **exenta** del mínimo/máximo |
| **Reutilizar** los WA existentes | Se conservan los números actuales |
| WA **0604** (5 paradas) | Se **consolida** en el WA vecino más cercano |
| **Paradas sin asignar** (37) | Se reparten al WA geográficamente más cercano |
| **Cambio mínimo** | Cada parada conserva su WA salvo que sea necesario moverla |

---

## 2. Hallazgos del análisis

**Archivo `stop_information.csv`** — 1.070 paradas, todas en **Cumming, GA** (ZIP
30041/30040):

- 10 Work Areas activas + **37 paradas sin asignar**.
- Desbalance: WA **0617** con 190 (al límite), WAs **0583/0589/0629** por debajo
  de 100, y WA **0604** con solo **5** paradas.
- **10 paradas** con peso individual **> 100 kg** (máx. 145 kg) → deben ir al WA 2217.
- El WA 2217 **no existe** aún en los datos.

**Manual DRO** — claves para *aplicar* la propuesta (ver §6):

- El CSV de entrada **es** el *Export to CSV* de la tabla Stop Information de DRO.
- **DRO no importa asignaciones por CSV** (solo exporta). Se aplica con
  **Stop Overrides** (por parada) y **Anchor Areas** (por zona), modeladas en
  **Sandbox** y publicadas con *Copy Route Plan*.

---

## 3. Geolocalización — 3 modos

El cálculo de densidad real y el mapa preciso necesitan coordenadas (lat/long).
El script las obtiene así, en orden:

1. **Google Maps Geocoding API** (recomendada / la más precisa) — requiere API key.
   `maps.googleapis.com`. Devuelve `location_type` (ROOFTOP, RANGE_INTERPOLATED,
   GEOMETRIC_CENTER, APPROXIMATE) y `partial_match`, que el script usa para
   **marcar direcciones erradas o de baja precisión** y **detectar puntos fuera
   del área de operación**. Volumen de 1.070 direcciones = dentro del **tier
   gratuito de 10.000/mes** → costo $0. Se pasa con `--google-key` o la variable
   de entorno `GOOGLE_MAPS_API_KEY`.
2. **US Census Bureau batch geocoder** — gratis, sin API key, hasta 10.000
   direcciones por lote. (`geocoding.geo.census.gov`)
3. **Nominatim / OpenStreetMap** — fallback. (`nominatim.openstreetmap.org`)
4. **Geografía relativa (proxy, offline)** — si no hay red ni key, deriva una
   geografía aproximada de **Ruta + Secuencia + ETA + nombre de calle**. El
   **balanceo sigue siendo válido**; solo el mapa pierde precisión (se marca con aviso).

> ⚠️ **Claude Code on the web (allowlist de red):** Census y Nominatim devuelven
> HTTP 403, pero **`maps.googleapis.com` SÍ es alcanzable**. Por eso, con una
> **API key de Google** el script geocodifica con precisión **desde el entorno web**.
> Sin key, ejecútalo **en tu equipo local** (tu red alcanza el Census gratis).
> Docs de red: https://code.claude.com/docs/en/claude-code-on-the-web

### Calidad de geocodificación y direcciones a revisar

Con Google, el Excel añade columnas `Calidad geo`, `Tipo geo`, `Fuera area?`,
`ZIP coincide?` y `Direccion segun Google`, y una hoja **`Revisar_Direcciones`**
con las paradas dudosas (sin match, baja precisión, fuera del área, o ZIP que no
coincide). Esos puntos **no se usan para calcular los centroides** de los WA (para
no distorsionar los grupos) y aparecen como capa "⚠ Revisar" en el mapa.

---

## 4. Cómo ejecutar

```bash
pip install -r requirements.txt

# PRECISO con Google Maps (recomendado). La key no se guarda en disco:
export GOOGLE_MAPS_API_KEY="TU_API_KEY"     # Windows PowerShell: $env:GOOGLE_MAPS_API_KEY="TU_API_KEY"
python optimize_wa.py --input data/stop_information.csv --outdir output
#   o:  python optimize_wa.py --google-key TU_API_KEY ...

# Sin key: intenta Census/Nominatim y, si no hay red, cae a proxy:
python optimize_wa.py --input data/stop_information.csv --outdir output

# Forzar modo offline (sin geocodificar):
python optimize_wa.py --input data/stop_information.csv --no-geocode
```

En **Windows**: instala Python 3.11+, abre PowerShell en esta carpeta y corre los
mismos comandos. Tu conexión local geocodifica sin problema → mapa preciso.

---

## 5. Salidas (carpeta `output/`)

| Archivo | Contenido |
|---------|-----------|
| `propuesta_reasignacion_WA.xlsx` | **Resumen** (KPIs + antes/después por WA), **Reasignacion_Propuesta** (todas las paradas: WA actual vs propuesto, motivo, peso, coords), **WA_2217_Overrides** (las paradas de sobrepeso), **Como_aplicar_en_DRO** |
| `mapa_WA.html` | Mapa interactivo (folium/Leaflet), una capa por WA, marcadores coloreados; abre en el navegador |
| `mapa_WA.png` | Imagen estática de la agrupación por WA |
| `reasignacion_WA.csv` | Mismo detalle en CSV plano |

---

## 6. Cómo aplicar la propuesta en RouteSmart DRO

> DRO no importa asignaciones por CSV; se usan herramientas nativas. Modela
> primero en **Sandbox** (Switch View → Sandbox View).

**Paso 0 — Crear el WA 2217 (una vez):** Manage → Fleet → *Vehicle Management* →
Add → *Work Area Name* = 2217 (debe existir en el *Work Area Master Table*) →
asigna vehículo y *Route Type* (p. ej. Bulk) → Save.

**Paso 1 — Forzar sobrepeso (>100 kg) al WA 2217:** en *Map*, selecciona en la
tabla las paradas de la hoja `WA_2217_Overrides` (búscalas por dirección) →
**Create Stop Override** → ruta del WA 2217 → Save → activa el override en el/los
*Route Plan(s)*. (Solo 10 paradas.)

**Paso 2 — Rebalanceo geográfico (resto de WAs):** dibuja un **Anchor Area** por
cada WA siguiendo los grupos del mapa (un polígono por color) y asígnalo a la ruta
de ese WA. Alternativa: selecciona en *Map* las paradas de cada `WA propuesto` y
crea Stop Overrides en lote.

**Paso 3 — Validar y publicar:** genera rutas en Sandbox → revisa el *Package
Detail report* (paradas por WA) → confirma 100–200 por WA → **Copy Route Plan** de
Sandbox a producción.

---

## 7. Resultado de referencia (modo proxy, sin geocodificar)

- **52 cambios** sobre 1.070 paradas (10 sobrepeso + 37 sin asignar + 5 de la 0604);
  **1.018 sin cambio**.
- Las 9 WA activas quedan **todas en 100–200**; la 0604 consolidada; la 2217 con los 10 pesados.
- Al geocodificar, el balanceo es el mismo pero las fronteras entre WAs se afinan
  con las ubicaciones reales y el mapa queda preciso.
