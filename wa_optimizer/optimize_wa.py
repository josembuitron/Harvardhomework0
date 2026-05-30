#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optimizador de asignacion de paquetes a Work Areas (WA) para RouteSmart DRO / FedEx.

Reglas de negocio (definidas con el usuario):
  - Cada WA "regular" debe tener entre MIN_STOPS y MAX_STOPS entregas (100-200).
  - El peso individual de una entrega = "Peso total" / "Paquetes".
  - Las entregas con peso individual > HEAVY_KG (100 kg) se asignan al WA 2217
    (ruta nueva de sobrepeso, EXENTA del minimo/maximo).
  - Se reutilizan los numeros de WA existentes; el WA mas pequeno (0604, con 5
    paradas) se consolida en el WA vecino mas cercano.
  - Las paradas sin asignar se reparten al WA geograficamente mas cercano.

Geolocalizacion:
  - Intenta geocodificar las direcciones con el Census Bureau (batch, gratis, sin
    API key) y, para las que fallen, con Nominatim (OpenStreetMap).
  - Si la red bloquea los geocodificadores (entornos restringidos), cae a un
    modo de "geografia relativa" derivado de Ruta + Secuencia + ETA + nombre de
    calle (la geografia que RouteSmart ya calculo). En ese modo el balanceo sigue
    siendo valido; solo el mapa pierde precision de mapa base.

Salidas:
  - Excel con la reasignacion propuesta, resumen por WA, lista de overrides para
    el WA 2217 e instrucciones para aplicarlo en DRO.
  - Mapa interactivo HTML (folium) coloreado por WA (cuando hay coordenadas reales).
  - Imagen PNG con el esquema de agrupacion por WA.

Uso:
    python optimize_wa.py --input data/stop_information.csv --outdir output
    python optimize_wa.py --input data/stop_information.csv --no-geocode   # forzar modo offline
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import math
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------

MIN_STOPS = 100          # minimo de entregas por WA regular
MAX_STOPS = 200          # maximo de entregas por WA regular
HEAVY_KG = 100.0         # umbral de peso individual para sobrepeso
WA_HEAVY = "2217"        # WA destino de paquetes de sobrepeso
WA_TO_DISSOLVE = "0604"  # WA pequeno que se consolida en un vecino

# Centro aproximado de Cumming, GA (para anclar el modo de geografia relativa)
CUMMING_LAT, CUMMING_LON = 34.2073, -84.1402

# Nombres de columnas en el CSV de DRO
COL_ID = "ID de la parada"
COL_WA = "N.º del WA"
COL_ROUTE = "Ruta"
COL_SEQ = "Secuencia"
COL_PKGS = "Paquetes"
COL_WEIGHT = "Peso total"
COL_ADDR = "Dirección"
COL_CITY = "Ciudad"
COL_STATE = "Estado"
COL_ZIP = "Código postal"
COL_NAME = "Nombre de la empresa"
COL_STOPTYPE = "Tipo de parada"
COL_ETA = "ETA"

# Paleta de colores estable por WA (hex)
PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#393b79",
    "#637939", "#8c6d31", "#843c39", "#7b4173", "#5254a3",
]


# ----------------------------------------------------------------------------
# Carga y limpieza
# ----------------------------------------------------------------------------

def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    # Normaliza WA a texto con ceros a la izquierda (4 digitos) cuando aplica
    df["wa_actual"] = df[COL_WA].fillna("").str.strip()
    df["wa_actual"] = df["wa_actual"].replace({"": np.nan})
    # Numericos
    df["paquetes_n"] = pd.to_numeric(df[COL_PKGS], errors="coerce").fillna(0)
    df["peso_total_n"] = pd.to_numeric(df[COL_WEIGHT], errors="coerce").fillna(0.0)
    # Peso individual = peso total / paquetes (si paquetes==0, usa peso total)
    with np.errstate(divide="ignore", invalid="ignore"):
        peso_ind = np.where(df["paquetes_n"] > 0,
                            df["peso_total_n"] / df["paquetes_n"],
                            df["peso_total_n"])
    df["peso_individual"] = np.round(peso_ind, 3)
    df["es_sobrepeso"] = df["peso_individual"] > HEAVY_KG
    df["sin_asignar"] = df["wa_actual"].isna()
    # Direccion completa para geocodificar
    df["direccion_full"] = (
        df[COL_ADDR].fillna("").str.replace(r",\s*-\s*$", "", regex=True).str.strip()
        + ", " + df[COL_CITY].fillna("").str.strip()
        + ", " + df[COL_STATE].fillna("").str.strip()
        + " " + df[COL_ZIP].fillna("").str.strip()
    ).str.replace(r"\s+", " ", regex=True).str.strip(", ")
    return df


# ----------------------------------------------------------------------------
# Geocodificacion
# ----------------------------------------------------------------------------

def _clean_addr_field(s: str) -> str:
    return ("" if s is None else str(s)).replace(",", " ").replace('"', " ").strip()


def geocode_census_batch(df: pd.DataFrame, chunk: int = 1000, timeout: int = 120):
    """Geocodifica con el batch del US Census Bureau (gratis, sin key).
    Devuelve dict: id_parada -> (lat, lon). Lanza excepcion si la red lo bloquea."""
    import requests
    url = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
    coords: dict[str, tuple[float, float]] = {}
    rows = df[[COL_ID, COL_ADDR, COL_CITY, COL_STATE, COL_ZIP]].copy()
    for start in range(0, len(rows), chunk):
        sub = rows.iloc[start:start + chunk]
        buf = io.StringIO()
        for _, r in sub.iterrows():
            buf.write(",".join([
                str(r[COL_ID]),
                _clean_addr_field(r[COL_ADDR]),
                _clean_addr_field(r[COL_CITY]),
                _clean_addr_field(r[COL_STATE]),
                _clean_addr_field(r[COL_ZIP]),
            ]) + "\n")
        files = {"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")}
        data = {"benchmark": "Public_AR_Current"}
        resp = requests.post(url, files=files, data=data, timeout=timeout)
        resp.raise_for_status()
        # Respuesta CSV sin encabezado: id, input, match, type, matched, lon,lat, tigerid, side
        for line in csv_lines(resp.text):
            if len(line) >= 6 and line[2] == "Match":
                try:
                    lon, lat = line[5].split(",")
                    coords[line[0]] = (float(lat), float(lon))
                except (ValueError, IndexError):
                    continue
        time.sleep(0.3)
    return coords


def csv_lines(text: str):
    import csv as _csv
    return list(_csv.reader(io.StringIO(text)))


def geocode_nominatim(addresses: dict[str, str], timeout: int = 15, pause: float = 1.05):
    """Geocodifica direcciones sueltas con Nominatim (1 req/seg). dict id->addr."""
    import requests
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "WA-Optimizer/1.0 (uso interno reasignacion FedEx)"}
    coords: dict[str, tuple[float, float]] = {}
    for sid, addr in addresses.items():
        try:
            r = requests.get(url, params={"q": addr, "format": "json", "limit": 1},
                             headers=headers, timeout=timeout)
            r.raise_for_status()
            js = r.json()
            if js:
                coords[sid] = (float(js[0]["lat"]), float(js[0]["lon"]))
        except Exception:
            pass
        time.sleep(pause)
    return coords


def proxy_coords(df: pd.DataFrame) -> np.ndarray:
    """Coordenadas APROXIMADAS (modo offline) derivadas de la geografia que el
    propio ruteo ya codifica: Ruta (cluster), Secuencia/ETA (posicion en la ruta)
    y nombre de calle (vecindad fina). No son lat/lon reales: sirven para mantener
    juntas las paradas que ya van juntas y para visualizar la agrupacion.
    """
    from sklearn.preprocessing import StandardScaler
    from sklearn.decomposition import PCA

    # Secuencia normalizada dentro de la ruta (0..1)
    seq = pd.to_numeric(df[COL_SEQ], errors="coerce")
    eta_min = parse_eta_minutes(df[COL_ETA])
    order = seq.fillna(eta_min).fillna(0)
    df_local = df.assign(_order=order)
    df_local["_seq_norm"] = df_local.groupby(COL_ROUTE)["_order"].transform(
        lambda s: (s - s.min()) / (s.max() - s.min() + 1e-9))

    # One-hot de ruta (peso alto -> separa rutas) y de calle (peso bajo -> vecindad)
    route_oh = pd.get_dummies(df[COL_ROUTE].fillna("NA")).to_numpy(dtype=float) * 6.0
    street = df[COL_ADDR].fillna("").str.extract(r"^\s*\d*\s*(.*?)(?:,|$)")[0].str.upper().str.strip()
    street_oh = pd.get_dummies(street).to_numpy(dtype=float) * 1.2
    seqn = df_local["_seq_norm"].to_numpy(dtype=float).reshape(-1, 1) * 2.0

    feats = np.hstack([route_oh, street_oh, seqn])
    feats = StandardScaler(with_mean=False).fit_transform(feats)
    xy = PCA(n_components=2, random_state=0).fit_transform(feats)

    # Escala alrededor de Cumming, GA (caja ~0.10 x 0.13 grados)
    def _scale(v, lo, hi):
        v = (v - v.min()) / (np.ptp(v) + 1e-9)
        return lo + v * (hi - lo)
    lat = _scale(xy[:, 1], CUMMING_LAT - 0.05, CUMMING_LAT + 0.05)
    lon = _scale(xy[:, 0], CUMMING_LON - 0.065, CUMMING_LON + 0.065)
    return np.column_stack([lat, lon])


def parse_eta_minutes(series: pd.Series) -> pd.Series:
    def to_min(x):
        try:
            h, m = str(x).split(":")[:2]
            return int(h) * 60 + int(m)
        except Exception:
            return np.nan
    return series.map(to_min)


def add_coordinates(df: pd.DataFrame, do_geocode: bool):
    """Anade columnas lat/lon y devuelve (df, modo) donde modo in {'real','proxy'}."""
    df = df.copy()
    df["lat"] = np.nan
    df["lon"] = np.nan
    source = "proxy"
    if do_geocode:
        coords = {}
        try:
            print("[geo] Intentando Census batch geocoder ...", flush=True)
            coords = geocode_census_batch(df)
            print(f"[geo] Census devolvio {len(coords)} coincidencias", flush=True)
        except Exception as e:
            print(f"[geo] Census no disponible ({type(e).__name__}: {e})", flush=True)
        # Nominatim para las que falten
        missing = {r[COL_ID]: r["direccion_full"]
                   for _, r in df.iterrows() if r[COL_ID] not in coords}
        if coords and missing:
            try:
                print(f"[geo] Nominatim para {len(missing)} direcciones restantes ...", flush=True)
                coords.update(geocode_nominatim(missing))
            except Exception as e:
                print(f"[geo] Nominatim no disponible ({e})", flush=True)
        if coords:
            for i, r in df.iterrows():
                if r[COL_ID] in coords:
                    df.at[i, "lat"], df.at[i, "lon"] = coords[r[COL_ID]]
            ok = df["lat"].notna().mean()
            print(f"[geo] Cobertura de geocodificacion: {ok:.0%}", flush=True)
            if ok >= 0.6:
                # rellena faltantes con proxy para no perder paradas
                if df["lat"].isna().any():
                    px = proxy_coords(df)
                    miss = df["lat"].isna().to_numpy()
                    df.loc[miss, "lat"] = px[miss, 0]
                    df.loc[miss, "lon"] = px[miss, 1]
                return df, "real"
        print("[geo] Sin geocodificacion suficiente -> modo geografia relativa (proxy).", flush=True)
    px = proxy_coords(df)
    df["lat"], df["lon"] = px[:, 0], px[:, 1]
    return df, source


# ----------------------------------------------------------------------------
# Optimizacion (clustering capacitado sembrado en los WA actuales)
# ----------------------------------------------------------------------------

def _centroid_dists(coords: np.ndarray, idxs, centroid: np.ndarray) -> np.ndarray:
    return np.linalg.norm(coords[idxs] - centroid, axis=1)


def assign_min_change(df: pd.DataFrame, coords: np.ndarray, heavy: np.ndarray,
                      target_was: list[str], centroids: dict[str, np.ndarray]):
    """Asignacion de CAMBIO MINIMO: cada parada conserva su WA actual salvo que
    sea necesario moverla. Solo se reubican:
      - paradas de sobrepeso (-> WA 2217),
      - paradas del WA que se consolida (0604),
      - paradas sin asignar,
      - el minimo de paradas frontera para que cada WA quede en [MIN, MAX].
    """
    n = len(df)
    prop = np.array([None] * n, dtype=object)
    wa_actual = df["wa_actual"].to_numpy()
    for i in range(n):
        w = wa_actual[i]
        prop[i] = None if (w is None or (isinstance(w, float) and np.isnan(w))) else w
    # Sobrepeso -> 2217
    prop[heavy] = WA_HEAVY
    # WA a consolidar -> al pool (None) si no es pesado
    dissolve = (df["wa_actual"] == WA_TO_DISSOLVE).to_numpy() & (~heavy)
    prop[dissolve] = None

    def counts():
        c = {w: 0 for w in target_was}
        for p in prop:
            if p in c:
                c[p] += 1
        return c

    # Pool = paradas sin WA valido (sin asignar originales + consolidadas), no pesadas
    pool = [i for i in range(n) if prop[i] is None and not heavy[i]]

    # --- Fase A: llenar deficits (<MIN) con paradas del pool mas cercanas ---
    cnt = counts()
    progress = True
    while pool and progress:
        progress = False
        needy = sorted([w for w in target_was if cnt[w] < MIN_STOPS],
                       key=lambda w: cnt[w])
        for w in needy:
            if cnt[w] >= MIN_STOPS or not pool:
                continue
            d = _centroid_dists(coords, pool, centroids[w])
            p = pool[int(np.argmin(d))]
            prop[p] = w
            pool.remove(p)
            cnt[w] += 1
            progress = True

    # --- Fase B: colocar el resto del pool en el WA mas cercano con espacio (<MAX) ---
    for p in list(pool):
        cand = [w for w in target_was if cnt[w] < MAX_STOPS]
        if not cand:
            cand = target_was
        w = min(cand, key=lambda w: np.linalg.norm(coords[p] - centroids[w]))
        prop[p] = w
        cnt[w] += 1
    pool = []

    # --- Fase C: deficits remanentes -> mover paradas frontera desde donantes ---
    for w in target_was:
        guard = 0
        while cnt[w] < MIN_STOPS and guard < 10000:
            guard += 1
            donors = [i for i in range(n)
                      if prop[i] in cnt and prop[i] != w and cnt[prop[i]] > MIN_STOPS]
            if not donors:
                break
            d = _centroid_dists(coords, donors, centroids[w])
            p = donors[int(np.argmin(d))]
            cnt[prop[p]] -= 1
            prop[p] = w
            cnt[w] += 1

    # --- Fase D: excesos (>MAX) -> mover las mas lejanas a un WA con espacio ---
    for w in target_was:
        guard = 0
        while cnt[w] > MAX_STOPS and guard < 10000:
            guard += 1
            members = [i for i in range(n) if prop[i] == w]
            d = _centroid_dists(coords, members, centroids[w])
            p = members[int(np.argmax(d))]
            room = [x for x in target_was if x != w and cnt[x] < MAX_STOPS]
            if not room:
                break
            tgt = min(room, key=lambda x: np.linalg.norm(coords[p] - centroids[x]))
            cnt[w] -= 1
            prop[p] = tgt
            cnt[tgt] += 1

    return prop


def optimize(df: pd.DataFrame):
    """Devuelve df con columnas wa_propuesto, cambia, motivo + dict de centroides."""
    df = df.copy()
    coords_all = df[["lat", "lon"]].to_numpy(dtype=float)

    # 1) Sobrepeso -> WA 2217
    heavy_mask = df["es_sobrepeso"].to_numpy()

    # 2) WAs objetivo = existentes menos el que se disuelve
    target_was = sorted([w for w in df["wa_actual"].dropna().unique()
                         if w != WA_TO_DISSOLVE])
    # Centroides ancla = miembros actuales NO pesados de cada WA (geografia actual)
    centroids = {}
    for w in target_was:
        m = (df["wa_actual"] == w).to_numpy() & (~heavy_mask)
        centroids[w] = coords_all[m].mean(axis=0) if m.any() else coords_all.mean(axis=0)

    # 3) Asignacion de cambio minimo
    df["wa_propuesto"] = assign_min_change(df, coords_all, heavy_mask, target_was, centroids)

    # 4) Recalcular centroides con la asignacion final (para mapa/etiquetas)
    for w in target_was + [WA_HEAVY]:
        m = (df["wa_propuesto"] == w).to_numpy()
        if m.any():
            centroids[w] = coords_all[m].mean(axis=0)

    # 5) Motivos
    df["cambia"] = df["wa_actual"].fillna("(sin asignar)") != df["wa_propuesto"]
    motivos = []
    for _, r in df.iterrows():
        if r["es_sobrepeso"]:
            motivos.append(f"Sobrepeso {r['peso_individual']:.1f} kg (>{HEAVY_KG:.0f}) -> WA {WA_HEAVY}")
        elif r["sin_asignar"]:
            motivos.append(f"Sin asignar -> WA {r['wa_propuesto']} (mas cercana)")
        elif r["wa_actual"] == WA_TO_DISSOLVE:
            motivos.append(f"WA {WA_TO_DISSOLVE} consolidada -> WA {r['wa_propuesto']}")
        elif r["cambia"]:
            motivos.append(f"Rebalanceo {r['wa_actual']} -> {r['wa_propuesto']} (cumplir 100-200)")
        else:
            motivos.append("Sin cambio")
    df["motivo"] = motivos
    return df, centroids


# ----------------------------------------------------------------------------
# Validacion
# ----------------------------------------------------------------------------

def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    def agg(group_col):
        g = df.groupby(group_col).agg(
            paradas=(COL_ID, "count"),
            paquetes=("paquetes_n", "sum"),
            peso_total=("peso_total_n", "sum"),
        )
        return g
    before = agg("wa_actual").rename(columns=lambda c: c + "_antes")
    after = agg("wa_propuesto").rename(columns=lambda c: c + "_despues")
    summary = after.join(before, how="outer")
    summary.index.name = "WA"
    summary = summary.fillna(0)
    for c in summary.columns:
        if summary[c].dtype != object:
            summary[c] = summary[c].round(0).astype(int) if "paradas" in c else summary[c].round(1)

    def estado(row):
        wa = row.name
        n = row["paradas_despues"]
        if wa == WA_HEAVY:
            return f"OK (sobrepeso, exenta) - {int(n)} paradas"
        if n == 0:
            return "Consolidada / sin uso (0 paradas)"
        if n < MIN_STOPS:
            return f"BAJO MINIMO ({int(n)}<{MIN_STOPS})"
        if n > MAX_STOPS:
            return f"SOBRE MAXIMO ({int(n)}>{MAX_STOPS})"
        return f"OK ({int(n)} en rango)"
    summary["estado"] = summary.apply(estado, axis=1)
    return summary.reset_index()


def print_report(df: pd.DataFrame, summary: pd.DataFrame, mode: str):
    print("\n" + "=" * 70)
    print("REPORTE DE OPTIMIZACION DE WORK AREAS")
    print("=" * 70)
    print(f"Modo de geografia : {'COORDENADAS REALES (geocodificadas)' if mode=='real' else 'GEOGRAFIA RELATIVA (proxy offline)'}")
    print(f"Total de paradas  : {len(df)}")
    print(f"  - Sobrepeso >100kg -> WA {WA_HEAVY} : {int(df['es_sobrepeso'].sum())}")
    print(f"  - Sin asignar (reasignadas)        : {int(df['sin_asignar'].sum())}")
    print(f"  - Con cambio de WA                 : {int(df['cambia'].sum())}")
    print(f"  - Sin cambio                       : {int((~df['cambia']).sum())}")
    print("-" * 70)
    print(f"{'WA':>6} {'antes':>7} {'despues':>8}   estado")
    for _, r in summary.iterrows():
        print(f"{str(r['WA']):>6} {int(r['paradas_antes']):>7} {int(r['paradas_despues']):>8}   {r['estado']}")
    reg = summary[(summary["WA"] != WA_HEAVY) & (summary["paradas_despues"] > 0)]
    viol = reg[(reg["paradas_despues"] < MIN_STOPS) | (reg["paradas_despues"] > MAX_STOPS)]
    print("-" * 70)
    if viol.empty:
        print("OK: todas las WA regulares (activas) cumplen 100-200 entregas.")
    else:
        print(f"ATENCION: {len(viol)} WA regulares fuera de rango -> {list(viol['WA'])}")
    print("=" * 70 + "\n")
    return viol.empty


# ----------------------------------------------------------------------------
# Excel
# ----------------------------------------------------------------------------

def write_excel(df: pd.DataFrame, summary: pd.DataFrame, mode: str, path: str):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="2F5496")
    head_font = Font(bold=True, color="FFFFFF")
    ok_fill = PatternFill("solid", fgColor="C6EFCE")
    bad_fill = PatternFill("solid", fgColor="FFC7CE")
    chg_fill = PatternFill("solid", fgColor="FFF2CC")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def style_header(ws, ncols, row=1):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=row, column=c)
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border

    def autofit(ws, maxw=48):
        for col in ws.columns:
            length = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(maxw, length + 2)

    # --- Hoja 1: Resumen ---
    ws = wb.active
    ws.title = "Resumen"
    ws["A1"] = "Optimizacion de asignacion de Work Areas (WA) - RouteSmart DRO"
    ws["A1"].font = Font(bold=True, size=14, color="2F5496")
    info = [
        ("Fecha de generacion", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")),
        ("Modo de geografia", "Coordenadas reales (geocodificadas)" if mode == "real"
         else "Geografia relativa (proxy offline) - mapa aproximado"),
        ("Total de paradas", len(df)),
        ("Reglas", f"WA regular entre {MIN_STOPS} y {MAX_STOPS} entregas"),
        ("Sobrepeso", f"Peso individual > {HEAVY_KG:.0f} kg -> WA {WA_HEAVY} (exenta de min/max)"),
        (f"Paradas a WA {WA_HEAVY}", int(df["es_sobrepeso"].sum())),
        ("Paradas sin asignar reubicadas", int(df["sin_asignar"].sum())),
        ("Paradas con cambio de WA", int(df["cambia"].sum())),
    ]
    r = 3
    for k, v in info:
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r, column=2, value=v)
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Comparativo por WA (antes / despues)").font = Font(bold=True, size=12)
    r += 1
    cols = ["WA", "paradas_antes", "paradas_despues", "paquetes_despues",
            "peso_total_despues", "estado"]
    headers = ["WA", "Paradas antes", "Paradas despues", "Paquetes despues",
               "Peso total despues", "Estado vs regla"]
    for j, h in enumerate(headers, start=1):
        ws.cell(row=r, column=j, value=h)
    style_header(ws, len(headers), row=r)
    base = r
    for _, row in summary.iterrows():
        r += 1
        for j, c in enumerate(cols, start=1):
            cell = ws.cell(row=r, column=j, value=row[c])
            cell.border = border
        est = str(row["estado"])
        fill = ok_fill if est.startswith("OK") else bad_fill
        ws.cell(row=r, column=6).fill = fill
    autofit(ws)
    ws.freeze_panes = ws.cell(row=base + 1, column=1)

    # --- Hoja 2: Reasignacion propuesta ---
    ws2 = wb.create_sheet("Reasignacion_Propuesta")
    out_cols = [
        (COL_ID, "ID parada"), (COL_NAME, "Nombre/Empresa"), (COL_ADDR, "Direccion"),
        (COL_CITY, "Ciudad"), (COL_ZIP, "ZIP"), (COL_ROUTE, "Ruta actual"),
        ("wa_actual", "WA actual"), ("wa_propuesto", "WA propuesto"),
        ("cambia", "Cambia?"), ("paquetes_n", "Paquetes"),
        ("peso_total_n", "Peso total"), ("peso_individual", "Peso individual"),
        ("es_sobrepeso", "Sobrepeso?"), ("lat", "Lat"), ("lon", "Lon"),
        ("motivo", "Motivo"),
    ]
    for j, (_, h) in enumerate(out_cols, start=1):
        ws2.cell(row=1, column=j, value=h)
    style_header(ws2, len(out_cols))
    df_sorted = df.sort_values(["wa_propuesto", "wa_actual"], na_position="last")
    for _, row in df_sorted.iterrows():
        ws2.append([_fmt(row.get(src)) for src, _ in out_cols])
    # resaltar filas con cambio
    chg_col = [h for _, h in out_cols].index("Cambia?") + 1
    for i in range(2, ws2.max_row + 1):
        if ws2.cell(row=i, column=chg_col).value in (True, "True", "VERDADERO", "Si"):
            for j in range(1, len(out_cols) + 1):
                ws2.cell(row=i, column=j).fill = chg_fill
    autofit(ws2)
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = f"A1:{get_column_letter(len(out_cols))}{ws2.max_row}"

    # --- Hoja 3: Overrides WA 2217 ---
    ws3 = wb.create_sheet(f"WA_{WA_HEAVY}_Overrides")
    ws3.cell(row=1, column=1,
             value=f"Paradas de sobrepeso (>{HEAVY_KG:.0f} kg) a forzar al WA {WA_HEAVY} mediante Stop Override en DRO")
    ws3.cell(row=1, column=1).font = Font(bold=True, color="C00000")
    hcols = ["ID parada", "Nombre/Empresa", "Direccion", "Ciudad", "ZIP",
             "WA actual", "Peso total", "Paquetes", "Peso individual"]
    for j, h in enumerate(hcols, start=1):
        ws3.cell(row=3, column=j, value=h)
    style_header(ws3, len(hcols), row=3)
    heavy = df[df["es_sobrepeso"]].sort_values("peso_individual", ascending=False)
    for _, row in heavy.iterrows():
        ws3.append([_fmt(row.get(c)) for c in
                    [COL_ID, COL_NAME, COL_ADDR, COL_CITY, COL_ZIP, "wa_actual",
                     "peso_total_n", "paquetes_n", "peso_individual"]])
    autofit(ws3)

    # --- Hoja 4: Como aplicar en DRO ---
    ws4 = wb.create_sheet("Como_aplicar_en_DRO")
    steps = dro_instructions_lines()
    for i, line in enumerate(steps, start=1):
        cell = ws4.cell(row=i, column=1, value=line)
        if line and not line.startswith(" ") and line.strip() and line.isupper():
            cell.font = Font(bold=True, color="2F5496")
        if line.endswith(":") or line.startswith("PASO"):
            cell.font = Font(bold=True)
    ws4.column_dimensions["A"].width = 110

    wb.save(path)


def _fmt(v):
    if isinstance(v, (np.bool_, bool)):
        return "Si" if bool(v) else "No"
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return round(float(v), 5)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return v


def dro_instructions_lines():
    return [
        "COMO APLICAR ESTA PROPUESTA EN ROUTESMART DRO (CSP)",
        "",
        "DRO no permite importar asignaciones por CSV (solo exporta). Por eso la",
        "propuesta se aplica con dos herramientas nativas: Stop Overrides (por parada)",
        "y Anchor Areas (por zona geografica). Se recomienda modelar primero en Sandbox.",
        "",
        "PASO 0 - Crear el WA 2217 (una sola vez):",
        "  1. Switch View -> Sandbox View.",
        "  2. Manage -> Fleet -> pestana Vehicle Management -> Add.",
        "  3. En Work Area Name elige/teclea 2217 (debe existir en el Work Area Master",
        "     Table). Asigna un vehiculo y Route Type (p.ej. Bulk para sobrepeso). Save.",
        "",
        "PASO 1 - Forzar las paradas de sobrepeso (>100 kg) al WA 2217:",
        "  Opcion A (recomendada, en lote): Map -> selecciona en la tabla Stop Information",
        "  las paradas de la hoja 'WA_2217_Overrides' (usa el buscador por direccion) y",
        "  pulsa 'Create Stop Override' -> Route = ruta del WA 2217 -> Save -> activa el",
        "  override en el/los Route Plan(s).",
        "  Opcion B (una por una): Manage -> Stop Overrides -> Add -> busca la direccion ->",
        "  Confirm -> Route Designation = WA 2217 -> Save.",
        "",
        "PASO 2 - Aplicar el rebalanceo geografico (resto de WAs):",
        "  Opcion A (recomendada): dibuja un Anchor Area por cada WA siguiendo los grupos",
        "  del mapa adjunto (un poligono por color) y asignalo a la ruta de ese WA.",
        "  (Managing Anchor Areas -> Adding an Anchor Area -> Assigning Anchor Areas to Routes).",
        "  Opcion B: en Map, filtra/selecciona las paradas de cada WA propuesto (hoja",
        "  'Reasignacion_Propuesta', columna 'WA propuesto') y crea Stop Overrides en lote",
        "  hacia la ruta de ese WA.",
        "",
        "PASO 3 - Validar y publicar:",
        "  1. Genera rutas en Sandbox y revisa el Package Detail report (paradas por WA).",
        "  2. Confirma que cada WA regular queda entre 100 y 200 paradas.",
        "  3. Copy Route Plan desde Sandbox a produccion (Copying a Route Plan).",
        "",
        "NOTA: las paradas 'sin asignar' del archivo original quedan ya repartidas en la",
        "columna 'WA propuesto'; al aplicarlas como override/anchor dejaran de ser",
        "Unrouteable.",
    ]


# ----------------------------------------------------------------------------
# Mapas
# ----------------------------------------------------------------------------

def write_map_html(df: pd.DataFrame, centroids: dict, mode: str, path: str):
    import folium
    wa_order = sorted(df["wa_propuesto"].unique())
    color_of = {wa: PALETTE[i % len(PALETTE)] for i, wa in enumerate(wa_order)}
    center = [df["lat"].mean(), df["lon"].mean()]
    m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")

    if mode != "real":
        banner = ("<div style='position:fixed;top:8px;left:50px;z-index:9999;"
                  "background:#fff3cd;border:1px solid #ffc107;padding:6px 10px;"
                  "border-radius:6px;font:13px sans-serif;max-width:520px'>"
                  "<b>Mapa aproximado (geografia relativa).</b> Las posiciones reflejan "
                  "la agrupacion por ruta/secuencia, no la ubicacion exacta. Habilita el "
                  "geocodificador para el mapa preciso.</div>")
        m.get_root().html.add_child(folium.Element(banner))

    for wa in wa_order:
        sub = df[df["wa_propuesto"] == wa]
        fg = folium.FeatureGroup(name=f"WA {wa}  ({len(sub)} paradas)")
        for _, r in sub.iterrows():
            popup = folium.Popup(
                f"<b>WA {wa}</b><br>{_fmt(r.get(COL_ADDR))}<br>"
                f"Paquetes: {int(r['paquetes_n'])} | Peso ind: {r['peso_individual']:.1f} kg"
                f"{'<br><b>SOBREPESO</b>' if r['es_sobrepeso'] else ''}",
                max_width=260)
            folium.CircleMarker(
                location=[r["lat"], r["lon"]], radius=4,
                color=color_of[wa], fill=True, fill_color=color_of[wa],
                fill_opacity=0.85, weight=1, popup=popup,
            ).add_to(fg)
        # centroide
        if wa in centroids:
            folium.map.Marker(
                list(centroids[wa]),
                icon=folium.DivIcon(html=(
                    f"<div style='font:bold 12px sans-serif;color:{color_of[wa]};"
                    f"text-shadow:0 0 3px #fff,0 0 3px #fff'>WA {wa}</div>"))
            ).add_to(fg)
        fg.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(path)


def write_map_png(df: pd.DataFrame, centroids: dict, mode: str, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    wa_order = sorted(df["wa_propuesto"].unique())
    color_of = {wa: PALETTE[i % len(PALETTE)] for i, wa in enumerate(wa_order)}
    fig, ax = plt.subplots(figsize=(12, 10))
    for wa in wa_order:
        sub = df[df["wa_propuesto"] == wa]
        ax.scatter(sub["lon"], sub["lat"], s=18, c=color_of[wa],
                   label=f"WA {wa} ({len(sub)})", alpha=0.8, edgecolors="none")
    for wa, c in centroids.items():
        ax.annotate(f"WA {wa}", (c[1], c[0]), fontsize=11, fontweight="bold",
                    ha="center", va="center",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color_of.get(wa, "#333"), alpha=0.85))
    # paradas de sobrepeso
    heavy = df[df["es_sobrepeso"]]
    if len(heavy):
        ax.scatter(heavy["lon"], heavy["lat"], s=120, marker="*", c="black",
                   label=f"Sobrepeso -> WA {WA_HEAVY}", zorder=5)
    title = ("Agrupacion propuesta por Work Area"
             + ("  (coordenadas reales)" if mode == "real"
                else "  (esquema de geografia relativa)"))
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_xlabel("Longitud" if mode == "real" else "x (relativo)")
    ax.set_ylabel("Latitud" if mode == "real" else "y (relativo)")
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=9, framealpha=0.9)
    ax.grid(True, alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Optimizador de Work Areas para RouteSmart DRO")
    ap.add_argument("--input", default="data/stop_information.csv")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--no-geocode", action="store_true",
                    help="No intentar geocodificar; usar geografia relativa (offline)")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    print(f"[1/5] Cargando {args.input} ...")
    df = load_data(args.input)
    print(f"      {len(df)} paradas | WAs actuales: {sorted(df['wa_actual'].dropna().unique())}")
    print(f"      Sin asignar: {int(df['sin_asignar'].sum())} | Sobrepeso >100kg: {int(df['es_sobrepeso'].sum())}")

    print("[2/5] Geolocalizando ...")
    df, mode = add_coordinates(df, do_geocode=not args.no_geocode)

    print("[3/5] Optimizando asignacion (clustering capacitado 100-200) ...")
    df, centroids = optimize(df)
    summary = build_summary(df)
    feasible = print_report(df, summary, mode)

    print("[4/5] Escribiendo Excel ...")
    xlsx = os.path.join(args.outdir, "propuesta_reasignacion_WA.xlsx")
    write_excel(df, summary, mode, xlsx)
    print(f"      -> {xlsx}")

    print("[5/5] Generando mapas ...")
    html = os.path.join(args.outdir, "mapa_WA.html")
    png = os.path.join(args.outdir, "mapa_WA.png")
    write_map_html(df, centroids, mode, html)
    write_map_png(df, centroids, mode, png)
    print(f"      -> {html}")
    print(f"      -> {png}")

    # CSV plano de soporte (mismo contenido que la hoja de reasignacion)
    csv_out = os.path.join(args.outdir, "reasignacion_WA.csv")
    df.sort_values(["wa_propuesto"]).to_csv(csv_out, index=False, encoding="utf-8-sig")
    print(f"      -> {csv_out}")
    print("\nListo." + ("" if feasible else "  (Revisa las WA fuera de rango en el reporte.)"))


if __name__ == "__main__":
    main()
