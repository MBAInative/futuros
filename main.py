import asyncio
import io
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - handled at runtime in UI
    yf = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "futuros.db"
CACHE_DIR = BASE_DIR / "runtime_cache"
REFRESH_SECONDS = 60
SIGNAL_THRESHOLD = 5.5
DEFAULT_AUTO_BUY_THRESHOLD = 7.0
UNIVERSE = {
    "ES=F": {"name": "E-mini S&P 500", "multiplier": 50, "family": "indices"},
    "NQ=F": {"name": "E-mini Nasdaq 100", "multiplier": 20, "family": "indices"},
    "RTY=F": {"name": "E-mini Russell 2000", "multiplier": 50, "family": "indices"},
    "ZN=F": {"name": "10-Year T-Note", "multiplier": 1000, "family": "bonos"},
    "ZB=F": {"name": "U.S. Treasury Bond", "multiplier": 1000, "family": "bonos"},
    "CL=F": {"name": "WTI Crude Oil", "multiplier": 1000, "family": "energia"},
    "NG=F": {"name": "Natural Gas", "multiplier": 10000, "family": "energia"},
    "GC=F": {"name": "Gold", "multiplier": 100, "family": "metales"},
    "SI=F": {"name": "Silver", "multiplier": 5000, "family": "metales"},
    "6E=F": {"name": "Euro FX", "multiplier": 125000, "family": "divisas"},
    "DX-Y.NYB": {"name": "U.S. Dollar Index", "multiplier": 1000, "family": "divisas"},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR.mkdir(exist_ok=True)
if yf is not None:
    yf.set_tz_cache_location(str(CACHE_DIR))

app = FastAPI(title="Futuros Energy Monitor")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@dataclass
class Recommendation:
    ticker: str
    name: str
    action: str
    strength: float
    reason: str
    price: float
    volume: float
    return_1m: float
    return_5m: float
    volume_z: float
    return_z: float
    updated_at: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recommendations (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                strength REAL NOT NULL,
                reason TEXT NOT NULL,
                price REAL NOT NULL,
                volume REAL NOT NULL,
                return_1m REAL NOT NULL,
                return_5m REAL NOT NULL,
                volume_z REAL NOT NULL,
                return_z REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL NOT NULL,
                multiplier REAL NOT NULL,
                status TEXT NOT NULL,
                entry_strength REAL NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                realized_pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                equity REAL NOT NULL,
                open_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.commit()


def set_state(key: str, value: dict[str, Any]) -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO app_state(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, json.dumps(value)),
        )
        conn.commit()


def get_state(key: str, default: dict[str, Any]) -> dict[str, Any]:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def get_auto_buy_threshold() -> float:
    config = get_state("settings", {"auto_buy_threshold": DEFAULT_AUTO_BUY_THRESHOLD})
    value = config.get("auto_buy_threshold", DEFAULT_AUTO_BUY_THRESHOLD)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = DEFAULT_AUTO_BUY_THRESHOLD
    return min(10.0, max(0.0, round(numeric, 1)))


def set_auto_buy_threshold(value: float) -> float:
    threshold = min(10.0, max(0.0, round(float(value), 1)))
    set_state("settings", {"auto_buy_threshold": threshold})
    return threshold


def default_runtime_state(message: str = "Sin datos todavía.", status: str = "idle") -> dict[str, Any]:
    return {
        "last_refresh": None,
        "status": status,
        "message": message,
        "updated_symbols": [],
        "auto_buy_threshold": get_auto_buy_threshold(),
        "errors": [],
    }


def reset_test_data() -> dict[str, Any]:
    with closing(get_conn()) as conn:
        conn.executescript(
            """
            DELETE FROM recommendations;
            DELETE FROM paper_positions;
            DELETE FROM equity_snapshots;
            """
        )
        conn.commit()
    runtime = default_runtime_state(message="Modo prueba reiniciado. Base de datos limpiada.", status="idle")
    set_state("runtime", runtime)
    return runtime


def export_workbook_bytes() -> bytes:
    runtime = get_state("runtime", default_runtime_state())
    with closing(get_conn()) as conn:
        recommendations = pd.read_sql_query(
            "SELECT * FROM recommendations ORDER BY strength DESC, ticker ASC", conn
        )
        positions = pd.read_sql_query(
            "SELECT * FROM paper_positions ORDER BY opened_at DESC", conn
        )
        equity = pd.read_sql_query(
            "SELECT * FROM equity_snapshots ORDER BY captured_at ASC", conn
        )

    if not positions.empty:
        positions["nominal_usd"] = (
            positions["entry_price"] * positions["quantity"] * positions["multiplier"]
        ).round(2)
        positions["pnl_abierto"] = positions.apply(
            lambda row: calculate_position_pnl(
                side=row["side"],
                entry_price=row["entry_price"],
                current_price=row["current_price"],
                quantity=row["quantity"],
                multiplier=row["multiplier"],
            )
            if row["status"] == "open"
            else 0.0,
            axis=1,
        )
    else:
        positions = pd.DataFrame(
            columns=[
                "ticker",
                "name",
                "side",
                "quantity",
                "entry_price",
                "current_price",
                "multiplier",
                "status",
                "entry_strength",
                "opened_at",
                "updated_at",
                "closed_at",
                "realized_pnl",
                "nominal_usd",
                "pnl_abierto",
            ]
        )

    summary = pd.DataFrame(
        [
            {"Campo": "Estado", "Valor": runtime.get("status", "idle")},
            {"Campo": "Mensaje", "Valor": runtime.get("message", "")},
            {"Campo": "Ultima actualizacion", "Valor": runtime.get("last_refresh")},
            {"Campo": "Umbral de activacion", "Valor": get_auto_buy_threshold()},
            {"Campo": "Futuros actualizados", "Valor": ", ".join(runtime.get("updated_symbols", []))},
            {"Campo": "Errores parciales", "Valor": " | ".join(runtime.get("errors", []))},
            {"Campo": "Numero de recomendaciones", "Valor": len(recommendations)},
            {"Campo": "Numero de posiciones", "Valor": len(positions)},
        ]
    )

    config = pd.DataFrame(
        [
            {
                "ticker": ticker,
                "nombre": spec["name"],
                "familia": spec["family"],
                "multiplicador": spec["multiplier"],
            }
            for ticker, spec in UNIVERSE.items()
        ]
    )

    help_rows = pd.DataFrame(
        [
            {"Concepto": "Futuro", "Explicacion": "Contrato observado por la app, por ejemplo oro, gas, bonos o indices."},
            {"Concepto": "Accion", "Explicacion": "Direccion sugerida: buy si se apuesta por subida, sell si se apuesta por bajada, hold si no hay senal suficiente."},
            {"Concepto": "Fuerza", "Explicacion": "Puntuacion de 0 a 10 que resume la intensidad de la anomalia detectada."},
            {"Concepto": "Nominal", "Explicacion": "Tamano aproximado de la posicion simulada: precio de entrada x multiplicador x contratos."},
            {"Concepto": "PnL abierto", "Explicacion": "Ganancia o perdida de las posiciones aun abiertas segun el ultimo precio conocido."},
            {"Concepto": "PnL realizado", "Explicacion": "Ganancia o perdida ya consolidada por las posiciones cerradas."},
            {"Concepto": "Lado buy", "Explicacion": "Posicion larga: gana si el precio sube y pierde si baja."},
            {"Concepto": "Lado sell", "Explicacion": "Posicion corta: gana si el precio baja y pierde si sube."},
            {"Concepto": "Estado open", "Explicacion": "La operacion sigue abierta y su resultado puede cambiar."},
            {"Concepto": "Estado closed", "Explicacion": "La operacion ya se cerro y su resultado ya no cambia."},
            {"Concepto": "Motivo", "Explicacion": "Resumen textual de por que la app genero la senal: volumen, movimiento, tendencia o confirmacion."},
        ]
    )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, sheet_name="Resumen", index=False)
        recommendations.to_excel(writer, sheet_name="Recomendaciones", index=False)
        positions.to_excel(writer, sheet_name="Posiciones", index=False)
        equity.to_excel(writer, sheet_name="Equity", index=False)
        config.to_excel(writer, sheet_name="Configuracion", index=False)
        help_rows.to_excel(writer, sheet_name="Ayuda", index=False)
    output.seek(0)
    return output.getvalue()


def fetch_history(ticker: str) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance no está instalado.")

    history = yf.Ticker(ticker).history(period="7d", interval="1m", auto_adjust=False, prepost=False)
    if history.empty:
        raise RuntimeError(f"Yahoo Finance no devolvió datos para {ticker}.")

    frame = history.reset_index()
    frame.columns = [str(col).lower().replace(" ", "_") for col in frame.columns]
    if "datetime" not in frame.columns:
        raise RuntimeError(f"La serie descargada para {ticker} no incluye timestamps válidos.")
    frame = frame.rename(columns={"datetime": "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp", "close"])
    frame["volume"] = frame.get("volume", 0).fillna(0.0)
    return frame.tail(400).copy()


def compute_recommendation(
    ticker: str,
    frame: pd.DataFrame,
    peer_returns: dict[str, float],
    action_threshold: float,
) -> Recommendation:
    spec = UNIVERSE[ticker]
    df = frame.copy()
    df["ret_1m"] = df["close"].pct_change(1)
    df["ret_5m"] = df["close"].pct_change(5)
    df["rolling_vol_mean"] = df["volume"].rolling(60, min_periods=20).mean()
    df["rolling_vol_std"] = df["volume"].rolling(60, min_periods=20).std(ddof=0)
    df["rolling_ret_std"] = df["ret_1m"].rolling(60, min_periods=20).std(ddof=0)

    latest = df.iloc[-1]
    volume_z = 0.0
    if pd.notna(latest["rolling_vol_std"]) and latest["rolling_vol_std"] > 0:
        volume_z = float((latest["volume"] - latest["rolling_vol_mean"]) / latest["rolling_vol_std"])

    return_z = 0.0
    if pd.notna(latest["rolling_ret_std"]) and latest["rolling_ret_std"] > 0:
        return_z = float(latest["ret_1m"] / latest["rolling_ret_std"])

    ret_1m = float(latest["ret_1m"]) if pd.notna(latest["ret_1m"]) else 0.0
    ret_5m = float(latest["ret_5m"]) if pd.notna(latest["ret_5m"]) else 0.0
    direction_raw = (ret_1m * 0.55) + (ret_5m * 0.45)
    direction = "hold"
    if direction_raw > 0:
        direction = "buy"
    elif direction_raw < 0:
        direction = "sell"

    peer_alignment = 0.0
    peer_signals = [v for k, v in peer_returns.items() if k != ticker]
    if peer_signals:
        same_direction = sum(np.sign(ret_5m) == np.sign(v) for v in peer_signals if v != 0)
        peer_alignment = same_direction / len(peer_signals)

    anomaly_score = max(volume_z, 0) * 0.45 + abs(return_z) * 0.4 + abs(ret_5m) * 1000 * 0.15
    signed_score = anomaly_score if direction == "buy" else -anomaly_score if direction == "sell" else 0.0
    strength = min(10.0, round(abs(signed_score) * (1 + peer_alignment) * 1.15, 2))
    if strength < action_threshold:
        direction = "hold"

    reasons = []
    if volume_z > 1.5:
        reasons.append(f"volumen atípico (z={volume_z:.2f})")
    if abs(return_z) > 1.5:
        reasons.append(f"movimiento brusco (z={return_z:.2f})")
    if abs(ret_5m) > 0.002:
        reasons.append(f"tendencia 5m {ret_5m*100:.2f}%")
    if peer_alignment >= 0.66:
        reasons.append("confirmación con otros futuros relacionados")
    if not reasons:
        reasons.append("mercado sin anomalía clara")

    return Recommendation(
        ticker=ticker,
        name=spec["name"],
        action=direction,
        strength=strength,
        reason=", ".join(reasons),
        price=float(latest["close"]),
        volume=float(latest["volume"]),
        return_1m=ret_1m,
        return_5m=ret_5m,
        volume_z=volume_z,
        return_z=return_z,
        updated_at=latest["timestamp"].isoformat(),
    )


def upsert_recommendation(rec: Recommendation) -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO recommendations(
                ticker, name, action, strength, reason, price, volume, return_1m, return_5m, volume_z, return_z, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name=excluded.name,
                action=excluded.action,
                strength=excluded.strength,
                reason=excluded.reason,
                price=excluded.price,
                volume=excluded.volume,
                return_1m=excluded.return_1m,
                return_5m=excluded.return_5m,
                volume_z=excluded.volume_z,
                return_z=excluded.return_z,
                updated_at=excluded.updated_at
            """,
            (
                rec.ticker,
                rec.name,
                rec.action,
                rec.strength,
                rec.reason,
                rec.price,
                rec.volume,
                rec.return_1m,
                rec.return_5m,
                rec.volume_z,
                rec.return_z,
                rec.updated_at,
            ),
        )
        conn.commit()


def prune_stale_recommendations() -> None:
    placeholders = ",".join("?" for _ in UNIVERSE)
    with closing(get_conn()) as conn:
        conn.execute(
            f"DELETE FROM recommendations WHERE ticker NOT IN ({placeholders})",
            tuple(UNIVERSE.keys()),
        )
        conn.commit()


def sync_positions(price_map: dict[str, float], recommendations: list[Recommendation], auto_buy_threshold: float) -> None:
    timestamp = utc_now_iso()
    by_ticker = {rec.ticker: rec for rec in recommendations}
    with closing(get_conn()) as conn:
        open_positions = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open' ORDER BY opened_at ASC"
        ).fetchall()
        for position in open_positions:
            last_price = price_map.get(position["ticker"], position["current_price"])
            conn.execute(
                "UPDATE paper_positions SET current_price = ?, updated_at = ? WHERE id = ?",
                (last_price, timestamp, position["id"]),
            )

            rec = by_ticker.get(position["ticker"])
            if rec and rec.action not in ("hold", position["side"]) and rec.strength >= auto_buy_threshold:
                realized_pnl = calculate_position_pnl(
                    side=position["side"],
                    entry_price=position["entry_price"],
                    current_price=last_price,
                    quantity=position["quantity"],
                    multiplier=position["multiplier"],
                )
                conn.execute(
                    """
                    UPDATE paper_positions
                    SET status = 'closed', current_price = ?, updated_at = ?, closed_at = ?, realized_pnl = ?
                    WHERE id = ?
                    """,
                    (last_price, timestamp, timestamp, realized_pnl, position["id"]),
                )

        existing_open = {
            row["ticker"]: row["side"]
            for row in conn.execute("SELECT ticker, side FROM paper_positions WHERE status = 'open'")
        }

        for rec in recommendations:
            if rec.action == "hold" or rec.strength < auto_buy_threshold or rec.ticker in existing_open:
                continue
            spec = UNIVERSE[rec.ticker]
            conn.execute(
                """
                INSERT INTO paper_positions(
                    ticker, name, side, quantity, entry_price, current_price, multiplier, status,
                    entry_strength, opened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    rec.ticker,
                    rec.name,
                    rec.action,
                    1,
                    rec.price,
                    rec.price,
                    spec["multiplier"],
                    rec.strength,
                    timestamp,
                    timestamp,
                ),
            )
        conn.commit()


def calculate_position_pnl(side: str, entry_price: float, current_price: float, quantity: int, multiplier: float) -> float:
    delta = current_price - entry_price
    if side == "sell":
        delta = -delta
    return round(delta * quantity * multiplier, 2)


def snapshot_equity() -> None:
    with closing(get_conn()) as conn:
        open_positions = conn.execute("SELECT * FROM paper_positions WHERE status = 'open'").fetchall()
        closed_positions = conn.execute("SELECT realized_pnl FROM paper_positions WHERE status = 'closed'").fetchall()
        open_pnl = sum(
            calculate_position_pnl(
                side=row["side"],
                entry_price=row["entry_price"],
                current_price=row["current_price"],
                quantity=row["quantity"],
                multiplier=row["multiplier"],
            )
            for row in open_positions
        )
        realized_pnl = round(sum(row["realized_pnl"] for row in closed_positions), 2)
        equity = round(open_pnl + realized_pnl, 2)
        conn.execute(
            "INSERT INTO equity_snapshots(captured_at, equity, open_pnl, realized_pnl) VALUES (?, ?, ?, ?)",
            (utc_now_iso(), equity, round(open_pnl, 2), realized_pnl),
        )
        conn.commit()


def get_dashboard_state() -> dict[str, Any]:
    runtime = get_state("runtime", default_runtime_state())
    with closing(get_conn()) as conn:
        recommendations = [
            dict(row)
            for row in conn.execute("SELECT * FROM recommendations ORDER BY strength DESC, ticker ASC")
        ]
        positions = [
            dict(row)
            for row in conn.execute("SELECT * FROM paper_positions ORDER BY status ASC, opened_at DESC")
        ]
        equity = [
            dict(row)
            for row in conn.execute("SELECT * FROM equity_snapshots ORDER BY captured_at DESC LIMIT 50")
        ]

    for position in positions:
        position["nominal_usd"] = round(
            position["entry_price"] * position["quantity"] * position["multiplier"], 2
        )
        if position["status"] == "open":
            position["unrealized_pnl"] = calculate_position_pnl(
                side=position["side"],
                entry_price=position["entry_price"],
                current_price=position["current_price"],
                quantity=position["quantity"],
                multiplier=position["multiplier"],
            )
        else:
            position["unrealized_pnl"] = 0.0

    return {
        "runtime": runtime,
        "recommendations": recommendations,
        "positions": positions,
        "equity": list(reversed(equity)),
        "config": {
            "refresh_seconds": REFRESH_SECONDS,
            "signal_threshold": SIGNAL_THRESHOLD,
            "auto_buy_threshold": get_auto_buy_threshold(),
            "universe": UNIVERSE,
            "uses_yahoo_finance": True,
        },
    }


def collect_market_snapshot() -> dict[str, Any]:
    auto_buy_threshold = get_auto_buy_threshold()
    peer_returns: dict[str, float] = {}
    histories: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for ticker in UNIVERSE:
        try:
            history = fetch_history(ticker)
            histories[ticker] = history
            ret_5m = history["close"].pct_change(5).iloc[-1]
            peer_returns[ticker] = float(ret_5m) if pd.notna(ret_5m) else 0.0
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")

    if not histories:
        raise RuntimeError("No se pudo descargar datos válidos de Yahoo Finance para ningún futuro.")

    recommendations = [
        compute_recommendation(ticker, histories[ticker], peer_returns, auto_buy_threshold)
        for ticker in histories
    ]
    prune_stale_recommendations()
    for rec in recommendations:
        upsert_recommendation(rec)

    price_map = {rec.ticker: rec.price for rec in recommendations}
    sync_positions(price_map, recommendations, auto_buy_threshold)
    snapshot_equity()

    status = "warning" if errors else "ok"
    message = "Datos actualizados correctamente."
    if errors:
        message = "Actualización parcial. Algunos futuros no se pudieron refrescar."

    summary = {
        "last_refresh": utc_now_iso(),
        "status": status,
        "message": message,
        "updated_symbols": list(histories.keys()),
        "auto_buy_threshold": auto_buy_threshold,
        "errors": errors,
    }
    set_state("runtime", summary)
    return summary


async def refresh_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(collect_market_snapshot)
        except Exception as exc:  # pragma: no cover - runtime protection
            logger.exception("Error al refrescar mercado")
            set_state(
                "runtime",
                {
                    "last_refresh": utc_now_iso(),
                    "status": "error",
                    "message": str(exc),
                    "updated_symbols": [],
                },
            )
        await asyncio.sleep(REFRESH_SECONDS)


@app.on_event("startup")
async def startup_event() -> None:
    init_db()
    set_auto_buy_threshold(get_auto_buy_threshold())
    set_state("runtime", default_runtime_state(message="Inicializando monitor.", status="starting"))
    asyncio.create_task(refresh_loop())


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    return get_dashboard_state()


@app.post("/api/refresh")
async def api_refresh() -> dict[str, Any]:
    try:
        summary = await asyncio.to_thread(collect_market_snapshot)
        return {"ok": True, "summary": summary}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/settings/auto-buy-threshold/{value}")
async def api_set_auto_buy_threshold(value: float) -> dict[str, Any]:
    threshold = set_auto_buy_threshold(value)
    runtime = get_state("runtime", default_runtime_state())
    runtime["auto_buy_threshold"] = threshold
    runtime["message"] = f"Umbral de activacion actualizado a {threshold:.1f}."
    set_state("runtime", runtime)
    return {"ok": True, "auto_buy_threshold": threshold}


@app.post("/api/reset")
async def api_reset() -> dict[str, Any]:
    runtime = reset_test_data()
    return {"ok": True, "runtime": runtime}


@app.get("/api/export.xlsx")
async def api_export_xlsx() -> StreamingResponse:
    content = export_workbook_bytes()
    filename = f"futuros_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )

