from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

DISPLAY_TZ = ZoneInfo("Europe/Madrid")


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
        return dt.astimezone(DISPLAY_TZ).strftime("%d/%m/%Y %H:%M:%S")
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
    st.caption("Horas mostradas en Europe/Madrid. 'Ultima actualizacion' es la hora del refresco de la app; 'Actualizado' en recomendaciones es la hora del ultimo dato recibido de Yahoo Finance.")


def render_help() -> None:
    with st.expander("Ayuda", expanded=False):
        st.markdown(
            """
            Esta app observa varios futuros liquidos, como indices bursatiles, bonos, materias primas y divisas.
            Cada minuto descarga los ultimos datos disponibles, compara lo que esta pasando ahora con el
            comportamiento reciente y decide si el movimiento parece normal o si resulta llamativamente distinto
            de lo habitual.

            La idea general es sencilla: si en un contrato aparece de repente mucho mas volumen del normal,
            o si el precio se mueve con una intensidad mayor de la esperada, la app lo considera un posible
            movimiento atipico. A partir de eso calcula una recomendacion. No pretende adivinar el futuro
            con certeza, sino detectar situaciones que merecen mas atencion que un movimiento corriente del mercado.

            Cuando la fuerza de la recomendacion supera el umbral elegido, la app abre una operacion simulada.
            Eso significa que no compra ni vende dinero real, pero si guarda la entrada, el precio, la direccion
            y la evolucion posterior para medir si esa senal habria sido util o no. De este modo se puede estudiar
            si seguir movimientos atipicos habria generado beneficios o perdidas.

            Debajo tienes la explicacion detallada de cada columna principal. Conviene leerlas en conjunto y no
            de forma aislada, porque la app no se basa en una sola cifra, sino en varias medidas que se combinan
            para formar la senal.

            **Futuro**

            Esta columna indica el contrato que se esta analizando. Un futuro es un acuerdo estandarizado que
            cotiza en mercado organizado y cuyo valor depende de un activo subyacente. Ese activo puede ser
            petroleo, gas, oro, bonos del Tesoro, euro o un indice como el S&P 500. Por eso cuando ves un nombre
            como E-mini S&P 500 no estas viendo una empresa concreta, sino un contrato que representa la evolucion
            esperada de ese indice.

            La utilidad de esta columna es situarte. No es lo mismo una anomalia en el petroleo que en los bonos
            o en el oro. Cada mercado tiene ritmos, horarios y motivos distintos para moverse. Ver el futuro
            concreto te ayuda a interpretar si la senal esta ligada a crecimiento economico, miedo en mercado,
            inflacion, materias primas o movimiento de divisas.

            **Accion**

            Es la conclusion operativa del sistema y puede tomar tres valores: `buy`, `sell` o `hold`.
            `buy` significa que, segun los datos de ese momento, la app interpreta que el movimiento atipico
            favorece una subida. `sell` significa que la lectura favorece una bajada. `hold` significa que,
            aunque el mercado se este moviendo, la senal no es lo bastante clara, intensa o consistente como
            para justificar una entrada.

            Es importante no interpretar esta columna como una orden infalible. La accion es un resumen.
            Detras hay volumen, retornos recientes, comparacion con el comportamiento habitual y, en algunos casos,
            coherencia con otros futuros. Por eso conviene mirar siempre tambien la fuerza y el motivo antes
            de sacar una conclusion.

            **Fuerza**

            Es una nota entre 0 y 10 que resume cuanta conviccion tiene el sistema en la recomendacion.
            No es una probabilidad matematica exacta de acierto, sino una puntuacion interna. Un valor bajo,
            como 1, 2 o 3, indica que el movimiento entra dentro de lo bastante normal. Un valor medio, como
            5 o 6, sugiere que ya hay algo llamativo. Un valor alto, por encima del umbral elegido, indica que
            la anomalia parece suficientemente clara como para abrir una operacion simulada automaticamente.

            En lenguaje simple, la fuerza intenta responder a esta pregunta: "de 0 a 10, hasta que punto parece
            especial lo que esta ocurriendo ahora?" Si sube mucho es porque coinciden varios factores a la vez,
            por ejemplo volumen fuerte, movimiento de precio brusco y cierta confirmacion en otros mercados relacionados.

            **Precio**

            Es el ultimo precio conocido del futuro en ese instante. Te dice donde esta ahora el mercado.
            Tambien es el valor que la app usa como referencia para una entrada simulada. Si el sistema abre
            una posicion y el precio despues cambia, la ganancia o perdida se calcula comparando el precio actual
            con ese precio de entrada.

            Esta columna es importante porque pone en contexto el resto. No basta con saber que hay una senal:
            tambien hay que saber a que nivel de mercado aparece. En una fase posterior, este dato permite estudiar
            si la senal surge cerca de maximos, minimos, zonas de ruptura o simplemente en mitad de un rango sin especial interes.

            **Vol. z**

            Significa z-score del volumen. La app compara el volumen negociado en ese instante con el volumen medio
            reciente del mismo contrato. Si el valor esta cerca de 0, significa que el volumen es parecido al normal.
            Si vale 1 o 2, el volumen ya esta por encima de lo habitual. Si sube a 3, 4 o mas, significa que en ese
            momento se esta negociando mucho mas de lo normal.

            Esto es util porque un movimiento de precio no tiene el mismo significado si ocurre con poco volumen o con
            volumen muy alto. Si el precio sube un poco pero casi nadie negocia, puede ser solo ruido. Si el precio sube
            o baja con una entrada fuerte de volumen, el movimiento parece mas serio. Por eso esta columna ayuda a distinguir
            una oscilacion pequena de un episodio en el que hay participacion inusual del mercado.

            **Ret. 5m**

            Es el retorno de los ultimos cinco minutos expresado en porcentaje. Si ves un `+0,30%`, quiere decir que
            el futuro vale ahora un `0,30%` mas que hace cinco minutos. Si ves un `-0,20%`, significa que vale un
            `0,20%` menos. Es una forma rapida de resumir la direccion reciente del precio sin quedarnos solo con lo
            ocurrido en un unico minuto.

            Se usa cinco minutos porque es un plazo corto pero algo mas estable. Un minuto puede ser muy ruidoso.
            Cinco minutos siguen siendo intradia, pero dan un poco mas de perspectiva. Esta columna sirve para ver
            si hay una tendencia reciente clara y en que sentido va. Cuando el retorno a cinco minutos y el volumen
            atipico apuntan en la misma direccion, la recomendacion suele ganar coherencia.

            **Motivo**

            Es la explicacion textual de por que la app propone esa accion. Aqui aparecen frases como volumen atipico,
            movimiento brusco, tendencia 5m o confirmacion con otros futuros. Esta columna es clave porque convierte
            el calculo interno en una razon legible. Asi no dependes solo de la palabra `buy` o `sell`, sino que ves
            que elementos han impulsado la senal.

            Por ejemplo, si el motivo menciona volumen atipico y tendencia 5m, eso quiere decir que no solo ha habido
            movimiento, sino que ese movimiento ha llegado con una actividad de mercado superior a la normal. Si ademas
            se indica confirmacion con otros futuros, la app esta diciendo que la senal no parece aislada, sino alineada
            con lo que sucede en contratos relacionados.

            **Ejemplos de lectura**

            Ejemplo 1: posible compra. Imagina que en `CL=F` ves `Accion = buy`, `Fuerza = 8.2`, `Vol. z = 3.6`,
            `Ret. 5m = +0.42%` y en `Motivo` aparece volumen atipico, tendencia 5m, confirmacion con otros futuros.

            Lectura: el petroleo no solo esta subiendo, sino que lo hace con mucho mas volumen del normal y con una
            subida sostenida en los ultimos minutos. Ademas, la app detecta coherencia con otros mercados relacionados.
            Eso no garantiza exito, pero si describe una situacion relativamente fuerte y poco corriente. Por eso la
            fuerza es alta y la app se inclina por comprar.

            Ejemplo 2: posible venta. Imagina que en `ES=F` ves `Accion = sell`, `Fuerza = 7.8`, `Vol. z = 2.9`,
            `Ret. 5m = -0.55%` y el `Motivo` habla de movimiento brusco y volumen atipico.

            Lectura: el futuro del S&P 500 esta cayendo con una intensidad superior a la habitual y, ademas, esa caida
            viene acompanada de un volumen elevado. Esa combinacion suele ser mas relevante que una simple bajada con
            poco negocio. La app interpreta que la presion vendedora domina en ese momento y por eso recomienda vender.

            Ejemplo 3: mejor esperar. Supongamos que en `GC=F` aparece `Accion = hold`, `Fuerza = 3.1`, `Vol. z = 0.4`,
            `Ret. 5m = +0.08%` y el `Motivo` dice mercado sin anomalia clara.

            Lectura: el oro puede estar moviendose algo, pero ese movimiento entra dentro de la normalidad. No hay mucho
            volumen extraordinario ni una aceleracion especialmente llamativa. La app prefiere no forzar una entrada solo
            porque el precio haya variado un poco. Aqui la informacion util es precisamente que todavia no hay una senal de calidad.

            Ejemplo 4: fuerza media, pero no suficiente. Piensa en `6E=F` con `Accion = hold`, `Fuerza = 5.9`,
            `Vol. z = 1.8` y `Ret. 5m = -0.18%`. El mercado empieza a ponerse interesante, pero aun no hay una combinacion
            tan clara como para que la app abra una operacion simulada.

            Lectura: este tipo de caso sirve para entender que la app no trabaja en blanco o negro. Puede detectar una
            situacion potencialmente relevante sin llegar todavia al nivel de conviccion exigido. Eso ayuda a seguir la
            evolucion del contrato y esperar si la senal se fortalece o se desinfla en los siguientes minutos.

            **Paper Trading y PnL**

            **Lado**

            Indica la direccion de la operacion simulada. `buy` significa compra: ganas si el precio sube.
            `sell` significa venta en corto: ganas si el precio baja. Por eso una operacion `sell` puede dar beneficio
            sin haber comprado el activo primero; en esta simulacion significa que apuestas a una bajada.

            **Entrada**

            Es el precio exacto al que la app abre la operacion simulada. Desde ese instante, todos los calculos de
            ganancia o perdida se hacen comparando el mercado actual con ese precio de entrada.

            **Actual**

            Es el ultimo precio conocido del futuro. La app lo actualiza cada refresco y vuelve a calcular el resultado
            potencial de la posicion abierta.

            **Cuanto se invierte**

            En la version actual la app simula siempre `1` contrato por operacion. No usa una cantidad variable de dinero
            ni una cartera proporcional. Usa el multiplicador propio de cada futuro.

            Eso significa que el nominal aproximado de una operacion se obtiene asi:
            `precio de entrada x multiplicador x numero de contratos`.
            Como el numero de contratos es `1`, basta con `precio x multiplicador`.

            Ejemplos: si `CL=F` entra en `92.820` y su multiplicador es `1000`, el nominal simulado es aproximadamente
            `92.820 x 1000 = 92.820 USD`. Si `NG=F` entra en `2.881` y su multiplicador es `10000`, el nominal simulado
            es aproximadamente `2.881 x 10000 = 28.810 USD`.

            **Como calcula la ganancia o la perdida**

            La app usa esta formula: `(precio actual - precio de entrada) x multiplicador x contratos`.
            Si el lado es `sell`, invierte el signo porque en una venta en corto ganas cuando el precio baja y pierdes cuando sube.

            En el ejemplo de `CL=F`, si la entrada es `92.820` y el precio actual es `92.920`, como es una posicion `sell`,
            esa subida te perjudica. La diferencia es `0.100`. Multiplicada por `1000` da `100 USD` de perdida.
            Por eso aparece `-100,00 USD`.

            En un ejemplo de `NG=F`, si la entrada y el actual son ambos `2.881`, la diferencia es `0`, asi que el
            resultado es `0,00 USD`.

            **PnL abierto**

            Es la suma de las ganancias y perdidas de las posiciones que siguen abiertas en este momento.
            Va cambiando cada minuto con el mercado.

            **PnL realizado**

            Es la suma de los resultados de las posiciones que ya se cerraron. Ese valor ya no cambia por el movimiento
            del mercado, porque la operacion esta terminada.

            **Estado**

            `open` significa que la posicion sigue viva y su PnL puede cambiar.
            `closed` significa que la posicion ya se cerro y su resultado pasa al PnL realizado.

            La mejor forma de leer la tabla es combinar todo: primero mirar que futuro es, despues ver la accion,
            luego fijarse en la fuerza y, finalmente, leer el motivo mientras se interpreta el precio, el volumen y
            el retorno de cinco minutos. Una fuerza alta con volumen anormal y motivo claro merece mas atencion que
            una accion aislada con fuerza baja.

            Importante: Yahoo Finance es suficiente para aprender, prototipar y hacer simulacion, pero no es una fuente
            profesional para trading real. La app esta pensada para estudio, seguimiento y mejora del modelo, no para
            ejecutar dinero real.
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
