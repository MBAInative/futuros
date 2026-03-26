# Monitor de Futuros Liquidos

App de observacion y paper trading sobre futuros liquidos usando Yahoo Finance.

La aplicacion:

- vigila varios futuros cada minuto
- detecta anomalias de precio y volumen
- genera recomendaciones `buy`, `sell` o `hold`
- asigna una fuerza de `0` a `10`
- abre operaciones simuladas cuando la fuerza supera el umbral elegido
- calcula `PnL` abierto y realizado
- permite `reset` de la base de prueba
- exporta todo a Excel

## Stack

- `streamlit`
- `pandas`
- `numpy`
- `yfinance`
- `xlsxwriter`

La logica principal vive en `main.py` y la interfaz Streamlit en `app.py`.

## Arranque local

```powershell
cd C:\Dev\futuros
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

URL local:

- `http://127.0.0.1:8501`

## Archivos clave

- `app.py`: interfaz Streamlit
- `main.py`: logica de recomendaciones, SQLite, exportacion Excel y paper trading
- `.streamlit/config.toml`: tema y configuracion de Streamlit
- `requirements.txt`: dependencias

## Despliegue en Streamlit Community Cloud

1. Subir este proyecto a un repositorio de GitHub.
2. Entrar en Streamlit Community Cloud.
3. Crear una nueva app desde tu repo.
4. Seleccionar:
   - rama: la principal que uses
   - main file path: `app.py`
5. Desplegar.

## Recomendacion de Git

No subir:

- `futuros.db`
- logs
- caches

Eso ya queda cubierto por `.gitignore`.
