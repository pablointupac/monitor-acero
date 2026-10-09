"""Construye el histórico inicial (data/history.csv) a partir de un CSV de spot HRC.

Uso único:  python scripts/backfill.py hrc_spot_backfill.csv
El CSV de entrada tiene columnas date (YYYYMMDD) y spot_price (CNY/t), obtenido con
akshare.futures_spot_price_daily(..., vars_list=["HC"]).
"""
import csv
import io
import sys

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "scripts")
import update as u  # noqa: E402


def serie_fred(series):
    txt = u.HTTP.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=30).text
    return {r[0]: float(r[1]) for r in list(csv.reader(io.StringIO(txt)))[1:]
            if len(r) > 1 and r[1] not in ("", ".")}


def serie_mindicador(ind, years):
    out = {}
    for y in years:
        for s in u.HTTP.get(f"https://mindicador.cl/api/{ind}/{y}", timeout=30).json()["serie"]:
            out[s["fecha"][:10]] = float(s["valor"])
    return out


def ultimo(serie, fecha):
    """Último valor disponible a una fecha (rellena fines de semana y feriados)."""
    previas = [k for k in serie if k <= fecha]
    return serie[max(previas)] if previas else None


def main(path):
    with open(path, encoding="utf-8") as f:
        spot = {f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:8]}": float(r["spot_price"])
                for r in csv.DictReader(f)}
    years = sorted({d[:4] for d in spot})
    cny = serie_fred("DEXCHUS")
    dolar = serie_mindicador("dolar", years)
    tasa = serie_mindicador("tpm", years)

    rows = {r["fecha"]: r for r in u.load_history()}
    for fecha, cny_t in sorted(spot.items()):
        fx_cny, clp = ultimo(cny, fecha), ultimo(dolar, fecha)
        if fx_cny is None or clp is None:
            continue
        rows.setdefault(fecha, {}).update({
            "fecha": fecha, "hrc_spot_cny": cny_t, "cny_por_usd": fx_cny,
            "usdclp": clp, "tpm_pct": ultimo(tasa, fecha),
            "hrc_fob_usd": round(u.fob_usd(cny_t, fx_cny), 1),
        })
    hist = list(rows.values())
    premio, _ = u.premio_galvanizado(hist)
    for r in hist:
        if not r.get("galv_fob_usd"):
            r["galv_fob_usd"] = round(float(r["hrc_fob_usd"]) + premio, 1)
    u.save_history(hist)
    print(f"{len(hist)} filas; premio galvanizado {premio:.1f} USD/t")


if __name__ == "__main__":
    main(sys.argv[1])
