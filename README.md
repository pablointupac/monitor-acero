# Monitor acero China y USD/CLP

Dashboard con el spot y la proyección a 1, 3 y 6 meses de:

- **HRC China**: spot de Shanghái y curva de futuros SHFE.
- **Galvanizado Z180 0,80 mm**: HRC más un premio calibrado con compras reales.
- **USD/CLP**: dólar observado y forward teórico por diferencial de tasas.

GitHub Actions lo actualiza de lunes a viernes a las 08:00 (hora de Chile) y lo publica en GitHub Pages.

## Ajustes (`config.json`)

| Campo | Qué es |
|---|---|
| `costos_chile.flete_usd_t` | Flete marítimo China–Chile, en USD/t |
| `costos_chile.internacion_pct` | Gastos de internación, en % sobre CFR |
| `costos_chile.arancel_pct` | Arancel, en % |
| `iva_china_pct` | IVA chino que se descuenta para aproximar el precio FOB |
| `compras_galvanizado` | Sus compras de galvanizado. Se usa la más reciente para calibrar el premio sobre HRC. |

Para registrar una compra nueva, agregue una entrada a `compras_galvanizado` con `mes` (AAAA-MM),
`precio_usd_t`, `incoterm` (`FOB`, `CFR` o `CIF`) y `producto`. Se puede editar directamente en github.com.

## Ejecución manual

```bash
pip install -r requirements.txt
python scripts/update.py
```

## Secrets opcionales

- `BCCH_USER` y `BCCH_PASS`: credenciales de la API del Banco Central. Sin ellas, el dólar se toma de mindicador.cl.
