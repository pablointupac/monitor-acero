"""Actualiza los datos del monitor: acero HRC/galvanizado China y USD/CLP.

Escribe data/latest.json (spot + proyecciones) y agrega una fila a data/history.csv.
Se ejecuta a diario en GitHub Actions; también se puede correr a mano:
    python scripts/update.py
"""
import csv
import io
import json
import os
import sys
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import requests

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "monitor-acero (github actions)"

HISTORY_FIELDS = [
    "fecha", "hrc_spot_cny", "cny_por_usd", "usdclp", "tpm_pct",
    "hrc_fob_usd", "galv_fob_usd",
    "hrc_fob_usd_1m", "hrc_fob_usd_3m", "hrc_fob_usd_6m",
    "usdclp_1m", "usdclp_3m", "usdclp_6m",
]


# ---------------------------------------------------------------- fuentes

def hrc_spot():
    """Spot HRC Shanghái (CNY/t, con IVA) del último día hábil chino."""
    d = date.today()
    for _ in range(10):
        try:
            df = ak.futures_spot_price(d.strftime("%Y%m%d"), vars_list=["HC"])
            if df is not None and len(df):
                return d.isoformat(), float(df.iloc[0]["spot_price"])
        except Exception:
            pass
        d -= timedelta(days=1)
    raise RuntimeError("No se pudo obtener el spot HRC")


def hrc_curve():
    """Curva de futuros HRC SHFE: [{contrato, vence, settle_cny, fecha}]."""
    today = date.today()
    curve = []
    for i in range(0, 10):
        y, m = divmod(today.month - 1 + i, 12)
        y += today.year
        m += 1
        code = f"HC{y % 100:02d}{m:02d}"
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception:
            continue
        if df is None or not len(df):
            continue
        row = df.iloc[-1]
        last = datetime.strptime(str(row["date"])[:10], "%Y-%m-%d").date()
        if (today - last).days > 7:
            continue  # contrato ya vencido o sin operación reciente
        curve.append({
            "contrato": code,
            "vence": date(y, m, 15).isoformat(),  # SHFE: vence ~15 del mes del contrato
            "settle_cny": float(row["settle"] or row["close"]),
            "volumen": int(row["volume"]),
            "fecha": last.isoformat(),
        })
    if len(curve) < 3:
        raise RuntimeError(f"Curva HRC incompleta: {curve}")
    return curve


def fred_last(series):
    txt = HTTP.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=30).text
    rows = [r for r in csv.reader(io.StringIO(txt))][1:]
    rows = [r for r in rows if len(r) > 1 and r[1] not in ("", ".")]
    return rows[-1][0], float(rows[-1][1])


def cny_por_usd():
    try:
        d = HTTP.get("https://open.er-api.com/v6/latest/USD", timeout=30).json()
        return float(d["rates"]["CNY"]), "open.er-api.com"
    except Exception:
        return fred_last("DEXCHUS")[1], "FRED DEXCHUS"


def dolar_observado():
    """Dólar observado: API del Banco Central si hay credenciales, si no mindicador.cl."""
    user, pwd = os.environ.get("BCCH_USER"), os.environ.get("BCCH_PASS")
    if user and pwd:
        try:
            r = HTTP.get(
                "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx",
                params={
                    "user": user, "pass": pwd, "function": "GetSeries",
                    "timeseries": "F073.TCO.PRE.Z.D",
                    "firstdate": (date.today() - timedelta(days=10)).isoformat(),
                    "lastdate": date.today().isoformat(),
                },
                timeout=30,
            ).json()
            obs = [o for o in r["Series"]["Obs"] if o.get("statusCode") == "OK"]
            o = obs[-1]
            f = datetime.strptime(o["indexDateString"], "%d-%m-%Y").date().isoformat()
            return f, float(o["value"]), "Banco Central de Chile"
        except Exception as e:
            print("BCCh falló, uso mindicador.cl:", e, file=sys.stderr)
    s = HTTP.get("https://mindicador.cl/api/dolar", timeout=30).json()["serie"][0]
    return s["fecha"][:10], float(s["valor"]), "mindicador.cl (BCCh)"


def tpm():
    s = HTTP.get("https://mindicador.cl/api/tpm", timeout=30).json()["serie"][0]
    return float(s["valor"])


# ---------------------------------------------------------------- cálculo

def interp_curve(spot_cny, curve, target):
    """Precio HRC (CNY) a una fecha objetivo, interpolando linealmente en la curva."""
    pts = [(date.today(), spot_cny)] + [
        (date.fromisoformat(c["vence"]), c["settle_cny"]) for c in curve
    ]
    pts.sort()
    if target >= pts[-1][0]:
        return pts[-1][1]
    for (d0, p0), (d1, p1) in zip(pts, pts[1:]):
        if d0 <= target <= d1:
            w = (target - d0).days / max((d1 - d0).days, 1)
            return p0 + w * (p1 - p0)
    return pts[0][1]


def forward_teorico(spot, tasa_clp, tasa_usd, dias):
    """Paridad cubierta de tasas, base 360 días."""
    return spot * (1 + tasa_clp / 100 * dias / 360) / (1 + tasa_usd / 100 * dias / 360)


def fob_usd(cny, cny_usd):
    """Precio interno chino (con IVA) -> aproximación FOB en USD/t."""
    return cny / (1 + CONFIG["iva_china_pct"] / 100) / cny_usd


def puesto_chile(fob):
    c = CONFIG["costos_chile"]
    return (fob + c["flete_usd_t"]) * (1 + c["internacion_pct"] / 100) * (1 + c["arancel_pct"] / 100)


def premio_galvanizado(history):
    """Premio USD/t del galvanizado sobre HRC FOB, según la última compra registrada."""
    compra = sorted(CONFIG["compras_galvanizado"], key=lambda c: c["mes"])[-1]
    fob = compra["precio_usd_t"]
    if compra["incoterm"].upper() in ("CFR", "CIF"):
        fob -= CONFIG["costos_chile"]["flete_usd_t"]
    ref = [float(r["hrc_fob_usd"]) for r in history
           if r["fecha"].startswith(compra["mes"]) and r.get("hrc_fob_usd")]
    if not ref:
        raise RuntimeError(f"No hay histórico HRC para {compra['mes']} para calibrar el galvanizado")
    hrc_ref = sum(ref) / len(ref)
    return fob - hrc_ref, {**compra, "fob_usd_t": round(fob, 1), "hrc_fob_ref_usd_t": round(hrc_ref, 1)}


def load_history():
    path = DATA / "history.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_history(rows):
    rows.sort(key=lambda r: r["fecha"])
    with (DATA / "history.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HISTORY_FIELDS})


def main():
    history = load_history()

    hrc_fecha, spot_cny = hrc_spot()
    curve = hrc_curve()
    cny_usd, cny_fuente = cny_por_usd()
    clp_fecha, usdclp, clp_fuente = dolar_observado()
    tasa_clp = tpm()
    tasas_usd = {"1m": fred_last("DGS1MO")[1], "3m": fred_last("DGS3MO")[1], "6m": fred_last("DGS6MO")[1]}

    premio, compra_ref = premio_galvanizado(history)

    horizontes = {"spot": 0, **CONFIG["horizontes_dias"]}
    proy = {}
    for h, dias in horizontes.items():
        target = date.today() + timedelta(days=dias)
        cny = spot_cny if dias == 0 else interp_curve(spot_cny, curve, target)
        fx = usdclp if dias == 0 else forward_teorico(usdclp, tasa_clp, tasas_usd[h], dias)
        hrc = fob_usd(cny, cny_usd)
        galv = hrc + premio
        proy[h] = {
            "fecha_objetivo": target.isoformat(),
            "usdclp": round(fx, 2),
            "hrc": {
                "cny_t": round(cny, 1),
                "fob_usd_t": round(hrc, 1),
                "fob_clp_t": round(hrc * fx),
                "puesto_usd_t": round(puesto_chile(hrc), 1),
                "puesto_clp_t": round(puesto_chile(hrc) * fx),
            },
            "galv": {
                "fob_usd_t": round(galv, 1),
                "fob_clp_t": round(galv * fx),
                "puesto_usd_t": round(puesto_chile(galv), 1),
                "puesto_clp_t": round(puesto_chile(galv) * fx),
            },
        }

    latest = {
        "actualizado": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "fechas": {"hrc": hrc_fecha, "usdclp": clp_fecha},
        "proyecciones": proy,
        "curva_hrc": curve,
        "parametros": {
            "cny_por_usd": round(cny_usd, 4),
            "tpm_pct": tasa_clp,
            "tasas_usd_pct": tasas_usd,
            "premio_galv_usd_t": round(premio, 1),
            "compra_referencia": compra_ref,
            **CONFIG["costos_chile"],
            "iva_china_pct": CONFIG["iva_china_pct"],
        },
        "fuentes": {
            "hrc_spot": "100ppi.com vía akshare (spot Shanghái)",
            "hrc_futuros": "SHFE vía Sina/akshare",
            "usdclp": clp_fuente,
            "cny": cny_fuente,
            "tasas_usd": "FRED (T-Bills 1/3/6 meses)",
            "tpm": "mindicador.cl (BCCh)",
        },
    }
    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    hoy = {
        "fecha": hrc_fecha,
        "hrc_spot_cny": spot_cny, "cny_por_usd": round(cny_usd, 4),
        "usdclp": usdclp, "tpm_pct": tasa_clp,
        "hrc_fob_usd": proy["spot"]["hrc"]["fob_usd_t"],
        "galv_fob_usd": proy["spot"]["galv"]["fob_usd_t"],
        **{f"hrc_fob_usd_{h}": proy[h]["hrc"]["fob_usd_t"] for h in ("1m", "3m", "6m")},
        **{f"usdclp_{h}": proy[h]["usdclp"] for h in ("1m", "3m", "6m")},
    }
    history = [r for r in history if r["fecha"] != hrc_fecha] + [hoy]
    save_history(history)
    print(json.dumps({h: (p["hrc"]["fob_usd_t"], p["galv"]["fob_usd_t"], p["usdclp"]) for h, p in proy.items()}))


if __name__ == "__main__":
    main()
