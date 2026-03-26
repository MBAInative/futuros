from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from main import (
    REFRESH_SECONDS,
    collect_market_snapshot,
    default_runtime_state,
    export_workbook_bytes,
    get_auto_buy_threshold,
    get_dashboard_state,
    init_db,
    reset_test_data,
    set_auto_buy_threshold,
)


st.set_page_config(
    page_title="Monitor de Futuros Liquidos",
    page_icon=":bar_chart:",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def inject_styles() -> None:
    st.markdown(
        """
        <style>
            .stApp {
                background:
                    radial-gradient(circle at top right, rgba(217, 119, 6, 0.10), transparent 30%),
                    linear-gradient(180deg, #f7f1e8 0%, #f2eadc 100%);
            }
            .block-container {
                padding-top: 1.4rem;
                padding-bottom: 2rem;
            }
            .hero {
                background: linear-gradient(135deg, #17324d 0%, #254966 60%, #8f5a2a 100%);
                border-radius: 18px;
                padding: 1.2rem 1.4rem;
                color: #f7f1e8;
                box-shadow: 0 14px 34px rgba(23, 50, 77, 0.15);
                margin-bottom: 1rem;
            }
            .hero h1 {
                margin: 0;
                font-size: 1.8rem;
                line-height: 1.2;
            }
            .hero p {
                margin: 0.35rem 0 0 0;
                color: #e6ddcf;
                font-size: 0.98rem;
            }
            .status-card {
                background: rgba(255, 250, 243, 0.92);
                border: 1px solid rgba(37, 73, 102, 0.14);
                border-radius: 16px;
                padding: 1rem 1rem 0.75rem 1rem;
                box-shadow: 0 10px 24px rgba(42, 55, 68, 0.06);
            }
            .metric-strip {
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 0.8rem;
                margin-bottom: 0.8rem;
            }
            .metric-box {
                background: #fffaf3;
                border: 1px solid rgba(37, 73, 102, 0.10);
                border-radius: 12px;
                padding: 0.75rem 0.9rem;
            }
            .metric-label {
                color: #6b7280;
                font-size: 0.82rem;
                text-transform: uppercase;
                letter-spacing: 0.04em;
            }
            .metric-value {
                color: #17324d;
                font-size: 1.08rem;
                font-weight: 700;
                margin-top: 0.2rem;
            }
            .metric-value.ok { color: #176b44; }
            .metric-value.warning { color: #9a6700; }
            .metric-value.error { color: #9f2f26; }
            .pnl-positive { color: #176b44; font-weight: 700; }
            .pnl-negative { color: #9f2f26; font-weight: 700; }
            .pnl-flat { color: #6b7280; font-weight: 700; }
            .section-title {
                color: #17324d;
                font-size: 1.1rem;
                font-weight: 700;
                margin: 1.1rem 0 0.6rem 0;
            }
            .small-note {
                color: #6b7280;
                font-size: 0.9rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def inject_autorefresh() -> None:
    milliseconds = REFRESH_SECONDS * 1000
    components.html(
        f"""
        <script>
        const now = Date.now();
        const last = window.localStorage.getItem("futuros_streamlit_reload");
        if (!last || now - Number(last) > {milliseconds - 5000}) {{
            window.localStorage.setItem("futuros_streamlit_reload", String(now));
            window.setTimeout(function() {{
                window.parent.location.reload();
            }}, {milliseconds});
        }}
        </script>
        """,
        height=0,
    )


def format_money(value: float) -> str:
    return f"{value:,.2f} US$"


def format_price(value: float) -> str:
    return f"{value:,.3f}"


def format_pct(value: float) -> str:
    return f"{value * 100:,.2f}%"


def format_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return value


def pnl_class(value: float) -> str:
    if value > 0:
        return "pnl-positive"
    if value < 0:
        return "pnl-negative"
    return "pnl-flat"


def status_class(value: str) -> str:
    return value if value in {"ok", "warning", "error"} else ""


def ensure_runtime_state(force: bool = False) -> dict:
    state = get_dashboard_state()
    runtime = state["runtime"]
    needs_refresh = force or not runtime.get("last_refresh")
    if not needs_refresh and runtime.get("last_refresh"):
        try:
            last = datetime.fromisoformat(runtime["last_refresh"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
            needs_refresh = age >= REFRESH_SECONDS
        except ValueError:
            needs_refresh = True
    if needs_refresh:
        try:
            collect_market_snapshot()
        except Exception:
            return get_dashboard_state()
        return get_dashboard_state()
    return state


def build_recommendations_df(state: dict) -> pd.DataFrame:
    rows = []
    for rec in state["recommendations"]:
        family = state["config"]["universe"].get(rec["ticker"], {}).get("family", "")
        rows.append(
            {
                "Futuro": rec["ticker"],
                "Nombre": rec["name"],
                "Familia": family,
                "Accion": rec["action"],
                "Fuerza": round(rec["strength"], 2),
                "Precio": format_price(rec["price"]),
                "Vol. z": round(rec["volume_z"], 2),
                "Ret. 5m": format_pct(rec["return_5m"]),
                "Motivo": rec["reason"],
                "Actualizado": format_timestamp(rec["updated_at"]),
            }
        )
    return pd.DataFrame(rows)


def build_positions_df(state: dict) -> tuple[pd.DataFrame, float, float]:
    rows = []
    open_pnl = 0.0
    realized_pnl = 0.0
    for pos in state["positions"]:
        family = state["config"]["universe"].get(pos["ticker"], {}).get("family", "")
        pnl_value = pos["unrealized_pnl"] if pos["status"] == "open" else float(pos["realized_pnl"])
        if pos["status"] == "open":
            open_pnl += pnl_value
        else:
            realized_pnl += pnl_value
        rows.append(
            {
                "Futuro": pos["ticker"],
                "Nombre": pos["name"],
                "Familia": family,
                "Lado": pos["side"],
                "Nominal": format_money(float(pos["nominal_usd"])),
                "Entrada": format_price(float(pos["entry_price"])),
                "Actual": format_price(float(pos["current_price"])),
                "PnL": pnl_value,
                "Estado": pos["status"],
                "Abierta": format_timestamp(pos["opened_at"]),
                "Cerrada": format_timestamp(pos["closed_at"]),
            }
        )
    return pd.DataFrame(rows), round(open_pnl, 2), round(realized_pnl, 2)


def build_equity_df(state: dict) -> pd.DataFrame:
    rows = []
    for item in state["equity"]:
        rows.append(
            {
                "Captura": format_timestamp(item["captured_at"]),
                "Equity": float(item["equity"]),
                "PnL abierto": float(item["open_pnl"]),
                "PnL realizado": float(item["realized_pnl"]),
            }
        )
    return pd.DataFrame(rows)


def style_pnl_table(df: pd.DataFrame, pnl_column: str) -> "pd.io.formats.style.Styler":
    def colorize(value: float) -> str:
        if value > 0:
            return "color: #176b44; font-weight: 700;"
        if value < 0:
            return "color: #9f2f26; font-weight: 700;"
        return "color: #6b7280; font-weight: 700;"

    return df.style.map(colorize, subset=[pnl_column]).format({pnl_column: format_money})


def render_header() -> None:
    st.markdown(
        """
        <div class="hero">
            <h1>Monitor de movimientos atipicos en futuros liquidos</h1>
            <p>La app vigila cada minuto futuros de indices, bonos, materias primas y divisas. Detecta anomalias, genera senales y simula operaciones cuando la fuerza es alta.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_panel(state: dict) -> None:
    runtime = state["runtime"]
    current_threshold = float(state["config"]["auto_buy_threshold"])

    st.markdown('<div class="status-card">', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="metric-strip">
            <div class="metric-box">
                <div class="metric-label">Estado</div>
                <div class="metric-value {status_class(runtime.get('status', 'idle'))}">{runtime.get('status', 'idle')}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Ultima actualizacion</div>
                <div class="metric-value">{format_timestamp(runtime.get('last_refresh'))}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Mensaje</div>
                <div class="metric-value">{runtime.get('message', '-')}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    slider_col, refresh_col, reset_col, download_col = st.columns([3, 1, 1, 1])
    with slider_col:
        threshold_value = st.slider(
            "Fuerza para activar compras/ventas",
            min_value=0.0,
            max_value=10.0,
            value=current_threshold,
            step=0.5,
            key="threshold_slider",
        )
        if threshold_value != current_threshold:
            set_auto_buy_threshold(threshold_value)
            st.rerun()
    with refresh_col:
        st.write("")
        if st.button("Actualizar ahora", use_container_width=True, type="primary"):
            st.session_state["force_refresh"] = True
            st.rerun()
    with reset_col:
        st.write("")
        if st.button("Reset", use_container_width=True):
            reset_test_data()
            st.session_state["force_refresh"] = False
            st.rerun()
    with download_col:
        st.write("")
        st.download_button(
            "Descargar Excel",
            data=export_workbook_bytes(),
            file_name=f"futuros_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def render_help() -> None:
    with st.expander("Ayuda", expanded=False):
        st.markdown(
            """
            La app mira varios futuros cada minuto y trata de detectar movimientos poco normales.
            Si encuentra una anomalia suficiente, genera una senal y, si la fuerza supera el umbral elegido,
            abre una operacion simulada.

            Conceptos principales:

            - Futuro: contrato que estamos observando. Puede ser de indices, bonos, energia, metales o divisas.
            - Accion: recomendacion actual. `buy` significa apostar a subida. `sell` significa apostar a bajada. `hold` significa no actuar.
            - Fuerza: nota de 0 a 10. Cuanto mas alta, mas intensa es la senal.
            - Precio: ultimo precio usado para calcular la recomendacion.
            - Vol. z: compara el volumen de ahora con el volumen normal reciente. Un valor alto indica actividad rara.
            - Ret. 5m: cambio porcentual del precio en los ultimos 5 minutos.
            - Motivo: frase corta que resume por que la app penso en comprar, vender o esperar.

            Paper trading:

            - Lado buy: posicion larga. Gana si el precio sube y pierde si baja.
            - Lado sell: posicion corta. Gana si el precio baja y pierde si sube.
            - Nominal: tamano aproximado de la posicion simulada.
            - Entrada: precio al que se abrio la operacion.
            - Actual: ultimo precio conocido o precio de cierre.
            - PnL: ganancia o perdida. Verde si gana, rojo si pierde.
            - Estado open: la operacion sigue viva.
            - Estado closed: la operacion ya se cerro y ese resultado queda fijado.

            Como leer un ejemplo:

            - Si ves `buy`, fuerza `8.2` y `Ret. 5m` positivo, la app cree que el movimiento alcista es fuerte.
            - Si ves `sell`, fuerza `7.8` y el precio cae, esa posicion gana porque apostaba a la bajada.
            - Si ves `hold`, la app no ve una anomalia suficiente para abrir operacion.

            Excel:

            - El boton `Descargar Excel` baja un archivo con resumen, recomendaciones, posiciones, equity, configuracion y ayuda.
            - El boton `Reset` limpia la base de datos de prueba para empezar desde cero.
            """
        )


def render_recommendations(state: dict) -> None:
    st.markdown('<div class="section-title">Recomendaciones</div>', unsafe_allow_html=True)
    st.caption(f"Refresco automatico: cada {REFRESH_SECONDS} segundos")
    recommendations_df = build_recommendations_df(state)
    if recommendations_df.empty:
        st.info("Todavia no hay recomendaciones guardadas.")
        return
    st.dataframe(recommendations_df, use_container_width=True, hide_index=True)


def render_positions(state: dict) -> None:
    positions_df, open_pnl, realized_pnl = build_positions_df(state)
    st.markdown('<div class="section-title">Paper Trading</div>', unsafe_allow_html=True)
    pnl_col_1, pnl_col_2 = st.columns(2)
    with pnl_col_1:
        st.markdown(
            f'<div class="{pnl_class(open_pnl)}">PnL abierto: {format_money(open_pnl)}</div>',
            unsafe_allow_html=True,
        )
    with pnl_col_2:
        st.markdown(
            f'<div class="{pnl_class(realized_pnl)}">PnL realizado: {format_money(realized_pnl)}</div>',
            unsafe_allow_html=True,
        )
    if positions_df.empty:
        st.info("No hay posiciones simuladas.")
        return
    st.dataframe(style_pnl_table(positions_df, "PnL"), use_container_width=True, hide_index=True)


def render_equity(state: dict) -> None:
    equity_df = build_equity_df(state)
    st.markdown('<div class="section-title">Curva de Equity</div>', unsafe_allow_html=True)
    if equity_df.empty:
        st.info("Todavia no hay historial de equity.")
        return
    chart_df = equity_df.copy()
    chart_df["Captura"] = pd.to_datetime(chart_df["Captura"], dayfirst=True, errors="coerce")
    chart_df = chart_df.dropna(subset=["Captura"]).set_index("Captura")
    if not chart_df.empty:
        st.line_chart(chart_df[["Equity", "PnL abierto", "PnL realizado"]], use_container_width=True)
    st.dataframe(
        style_pnl_table(equity_df, "Equity")
        .map(
            lambda v: "color: #176b44; font-weight: 700;" if v > 0 else "color: #9f2f26; font-weight: 700;" if v < 0 else "color: #6b7280; font-weight: 700;",
            subset=["PnL abierto", "PnL realizado"],
        )
        .format({"PnL abierto": format_money, "PnL realizado": format_money}),
        use_container_width=True,
        hide_index=True,
    )


def main() -> None:
    init_db()
    inject_styles()
    inject_autorefresh()
    render_header()

    force_refresh = st.session_state.pop("force_refresh", False)
    state = ensure_runtime_state(force=force_refresh)

    render_status_panel(state)
    render_help()
    render_recommendations(state)
    render_positions(state)
    render_equity(state)

    errors = state["runtime"].get("errors", [])
    if errors:
        st.markdown('<div class="section-title">Errores parciales</div>', unsafe_allow_html=True)
        for item in errors:
            st.warning(item)
    st.caption("Fuente de datos: Yahoo Finance API via yfinance. Uso orientado a observacion y paper trading.")


if __name__ == "__main__":
    main()
