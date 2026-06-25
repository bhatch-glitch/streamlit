from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from creatoriq.cohorts import (
    DEFAULT_COHORT_TITLE,
    active_sync_cohort_id,
    count_cohort_with_activity,
    load_cohort_by_id,
)
from creatoriq.meta import read_sync_meta
from creatoriq.import_csv import import_creatoriq_csv
from creatoriq.refresh_pipeline import fast_refresh, hourly_refresh
from creatoriq.storage import load_metrics_csv
from creatoriq.sync import session_is_saved

load_dotenv(Path(__file__).parent / ".env")

AUTO_SYNC_MINUTES = int(os.getenv("DASHBOARD_AUTO_SYNC_MINUTES", "60"))
REFRESH_UI_MINUTES = int(os.getenv("DASHBOARD_REFRESH_MINUTES", "5"))

LOOKER_PURPLE = "#9334e6"
LOOKER_BLACK = "#202124"
LOOKER_AXIS = "#5f6368"
LOOKER_GRID = "#dadce0"


@st.cache_data(ttl=60)
def get_data() -> pd.DataFrame:
    return prepare_metrics(load_metrics_csv())


def prepare_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dates for filtering (handles ISO+TZ, CSV dates, sidebar date_input)."""
    if df.empty:
        return df
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], utc=True, errors="coerce")
    out = out.dropna(subset=["date"])
    out["date"] = out["date"].dt.tz_localize(None).dt.normalize()
    for col in ("follower_count", "engagements", "gmv"):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    for col in ("post_count", "link_count"):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)
    return out


def filter_by_date_range(
    df: pd.DataFrame,
    start_date: date,
    end_date: date,
    *,
    creators: list[str] | None = None,
    campaigns: list[str] | None = None,
) -> pd.DataFrame:
    """Keep rows whose calendar date falls in [start_date, end_date] (inclusive)."""
    if df.empty:
        return df
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    mask = (df["date"] >= start) & (df["date"] <= end)
    if creators is not None:
        mask &= df["creator"].isin(creators)
    if campaigns is not None:
        mask &= df["campaign"].isin(campaigns)
    return df.loc[mask]


def aggregate_by_creator(df: pd.DataFrame) -> pd.DataFrame:
    """Sum revenue (GMV) and activity per creator for an already date-filtered frame."""
    if df.empty:
        return pd.DataFrame(
            columns=[
                "creator",
                "Follower count",
                "ER %",
                "# of posts",
                "# of links",
                "GMV $",
            ]
        )

    grouped = df.groupby("creator", as_index=False).agg(
        follower_count=("follower_count", "max"),
        engagements=("engagements", "sum"),
        posts=("post_count", "sum"),
        links=("link_count", "sum"),
        gmv=("gmv", "sum"),
    )
    grouped["er_pct"] = (
        grouped["engagements"] / grouped["follower_count"].clip(lower=1)
    ) * 100
    return grouped.rename(
        columns={
            "follower_count": "Follower count",
            "er_pct": "ER %",
            "posts": "# of posts",
            "links": "# of links",
            "gmv": "GMV $",
        }
    )[
        ["creator", "Follower count", "ER %", "# of posts", "# of links", "GMV $"]
    ].sort_values("GMV $", ascending=False)


def aggregate_gmv_per_post_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Daily GMV per post and rolling trends from an already filtered frame."""
    cols = [
        "date",
        "gmv",
        "post_count",
        "gmv_per_post",
        "gmv_per_post_7d",
        "gmv_per_post_14d",
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)

    daily = (
        df.groupby("date", as_index=False)
        .agg(gmv=("gmv", "sum"), post_count=("post_count", "sum"))
        .sort_values("date")
    )
    full_range = pd.date_range(daily["date"].min(), daily["date"].max(), freq="D")
    daily = (
        daily.set_index("date")
        .reindex(full_range)
        .rename_axis("date")
        .reset_index()
    )
    daily["gmv"] = daily["gmv"].fillna(0.0)
    daily["post_count"] = daily["post_count"].fillna(0).astype(int)
    daily["gmv_per_post"] = daily["gmv"] / daily["post_count"].replace(0, pd.NA)

    roll_gmv_7 = daily["gmv"].rolling(7, min_periods=1).sum()
    roll_posts_7 = daily["post_count"].rolling(7, min_periods=1).sum()
    roll_gmv_14 = daily["gmv"].rolling(14, min_periods=1).sum()
    roll_posts_14 = daily["post_count"].rolling(14, min_periods=1).sum()
    daily["gmv_per_post_7d"] = roll_gmv_7 / roll_posts_7.replace(0, pd.NA)
    daily["gmv_per_post_14d"] = roll_gmv_14 / roll_posts_14.replace(0, pd.NA)
    return daily[cols]


def render_gmv_per_post_chart(daily: pd.DataFrame) -> None:
    """Purple daily bars with 7d (steeper) and 14d (smoother) trend lines."""
    if daily.empty:
        st.info("No daily GMV per post data for these filters.")
        return
    if daily["post_count"].sum() == 0:
        st.info(
            "Post counts are not in the metrics export yet. "
            "GMV per post will populate once CreatorIQ includes post_count."
        )
        return

    plot = daily.copy()
    plot["date"] = pd.to_datetime(plot["date"])

    base = alt.Chart(plot).encode(x=alt.X("date:T", title="Date", axis=alt.Axis(format="%b %d", labelColor=LOOKER_AXIS)))

    bars = base.mark_bar(color=LOOKER_PURPLE, opacity=0.85).encode(
        y=alt.Y(
            "gmv_per_post:Q",
            title="GMV $ / posts",
            axis=alt.Axis(labelColor=LOOKER_AXIS, gridColor=LOOKER_GRID),
        ),
        tooltip=[
            alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"),
            alt.Tooltip("gmv_per_post:Q", title="GMV $ / post", format="$,.2f"),
            alt.Tooltip("gmv:Q", title="GMV $", format="$,.0f"),
            alt.Tooltip("post_count:Q", title="Posts", format=","),
        ],
    )

    line_14d = base.mark_line(color=LOOKER_BLACK, strokeWidth=2.5).encode(
        y=alt.Y("gmv_per_post_14d:Q", title="GMV $ / posts"),
        tooltip=[
            alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"),
            alt.Tooltip("gmv_per_post_14d:Q", title="14-day avg", format="$,.2f"),
        ],
    )

    line_7d = base.mark_line(color=LOOKER_PURPLE, strokeWidth=2).encode(
        y="gmv_per_post_7d:Q",
        tooltip=[
            alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"),
            alt.Tooltip("gmv_per_post_7d:Q", title="7-day avg", format="$,.2f"),
        ],
    )

    chart = (
        (bars + line_14d + line_7d)
        .properties(height=360, background="#ffffff")
        .configure_view(strokeWidth=0)
        .configure_axis(grid=True, domainColor=LOOKER_GRID, tickColor=LOOKER_GRID)
    )
    st.altair_chart(chart, use_container_width=True)


def apply_looker_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600&display=swap');
        .stApp { background-color: #f8f9fa; }
        [data-testid="stSidebar"] {
            background-color: #ffffff;
            border-right: 1px solid #dadce0;
        }
        .looker-header {
            font-family: 'Google Sans', 'Segoe UI', sans-serif;
            font-size: 1.35rem;
            font-weight: 500;
            color: #202124;
            margin-bottom: 0.15rem;
        }
        .looker-subtitle {
            font-family: 'Google Sans', 'Segoe UI', sans-serif;
            font-size: 0.85rem;
            color: #5f6368;
            margin-bottom: 1.25rem;
        }
        .looker-section-title {
            font-size: 0.95rem;
            font-weight: 500;
            color: #202124;
            margin: 1.5rem 0 0.65rem 0;
            padding-bottom: 0.35rem;
            border-bottom: 2px solid #1a73e8;
            display: inline-block;
        }
        .sync-pill {
            display: inline-block;
            background: #e8f0fe;
            color: #1967d2;
            border-radius: 999px;
            padding: 0.2rem 0.65rem;
            font-size: 0.78rem;
            margin-bottom: 0.75rem;
        }
        div[data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #dadce0;
            border-radius: 8px;
            padding: 0.75rem 1rem;
            box-shadow: 0 1px 2px rgba(60,64,67,.12);
        }
        div[data-testid="stMetric"] label {
            color: #5f6368 !important;
            font-size: 0.75rem !important;
            text-transform: uppercase;
        }
        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            color: #1a73e8 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def format_kpi(value: float, kind: str) -> str:
    if kind == "followers":
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return f"{value:,.0f}"
    if kind == "er":
        return f"{value:.2f}%"
    if kind == "money":
        return f"${value:,.0f}"
    return f"{value:,.0f}"


def maybe_auto_sync() -> None:
    if not st.session_state.get("auto_sync_enabled", True):
        return
    last = st.session_state.get("last_auto_sync")
    if last and (pd.Timestamp.now() - last) < timedelta(minutes=AUTO_SYNC_MINUTES):
        return
    with st.spinner("Refreshing dashboard data…"):
        result = hourly_refresh()
    st.session_state["last_auto_sync"] = pd.Timestamp.now()
    st.session_state["last_sync_message"] = result.message
    get_data.clear()


@st.fragment(run_every=timedelta(minutes=REFRESH_UI_MINUTES))
def refresh_dashboard_data() -> None:
    get_data.clear()


def render_sync_sidebar() -> None:
    meta = read_sync_meta()
    synced_at = meta.get("synced_at", "never")
    source = meta.get("source", "unknown")
    row_count = meta.get("row_count", 0)
    if synced_at != "never":
        synced_display = str(synced_at)[:19].replace("T", " ")
        st.caption(f"Last sync: {synced_display} UTC · {row_count} creators · source: {source}")
    else:
        st.caption("Last sync: never — use Refresh now or drop CSVs in data/incoming/")

    if not session_is_saved():
        st.caption(
            "Optional: save a browser session for legacy export, or use CSV files only."
        )

    st.session_state.setdefault("auto_sync_enabled", True)
    st.checkbox(f"Auto-refresh every {AUTO_SYNC_MINUTES} min", key="auto_sync_enabled")

    if st.button("Refresh now", type="primary", use_container_width=True):
        with st.spinner("Importing latest CSV exports…"):
            result = fast_refresh()
        st.session_state["last_sync_message"] = result.message
        st.session_state["last_auto_sync"] = pd.Timestamp.now()
        get_data.clear()
        if result.ok:
            st.success(result.message)
        else:
            st.warning(result.message)

    st.caption(
        "Fast refresh uses CSV in `data/incoming/` or Downloads. "
        "Double-click **Refresh Data Now.bat** for the same sync without opening this app."
    )
    st.markdown("**Or upload a CreatorIQ payouts export**")
    uploaded = st.file_uploader("CSV / Excel from campaign payouts", type=["csv", "xlsx", "xls"])
    if uploaded is not None:
        incoming = Path(__file__).parent / "data" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        dest = incoming / uploaded.name
        dest.write_bytes(uploaded.getvalue())
        from creatoriq.watch_export import import_latest_export

        ok, message, _ = import_latest_export()
        get_data.clear()
        if ok:
            st.success(message)
        else:
            st.error(message)

    if msg := st.session_state.get("last_sync_message"):
        st.caption(msg)


def main() -> None:
    st.set_page_config(
        page_title="Brand Ambassador: Test I",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_looker_theme()
    refresh_dashboard_data()
    maybe_auto_sync()

    raw = get_data()
    if raw.empty:
        st.markdown(
            f'<p class="looker-header">{DEFAULT_COHORT_TITLE}</p>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="looker-subtitle">Waiting for CreatorIQ cohort data</p>',
            unsafe_allow_html=True,
        )
        with st.sidebar:
            st.markdown("### CreatorIQ")
            render_sync_sidebar()
        st.stop()

    campaigns = sorted({c for c in raw.get("campaign", pd.Series(dtype=str)).fillna("") if c})
    cohort = load_cohort_by_id(active_sync_cohort_id())
    program_view = cohort.title if cohort else DEFAULT_COHORT_TITLE

    st.markdown(
        f'<p class="looker-header">{program_view}</p>',
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown("### CreatorIQ")
        render_sync_sidebar()
        if cohort and not raw.empty:
            matched, total, _ = count_cohort_with_activity(raw, cohort)
            st.caption(f"{matched}/{total} cohort creators with GMV or posts in range")
        elif cohort:
            st.caption(f"0/{cohort.size} cohort creators in metrics")

        view_min = raw["date"].min().to_pydatetime().date() if not raw.empty else date.today()
        view_max = raw["date"].max().to_pydatetime().date() if not raw.empty else date.today()
        st.markdown("### Filters")
        date_range = st.date_input(
            "Date range",
            value=(view_min, view_max),
            min_value=view_min,
            max_value=view_max,
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start_date, end_date = date_range
        else:
            start_date = end_date = date_range

        all_creators = sorted(raw["creator"].unique()) if not raw.empty else []
        selected_creators = st.multiselect(
            "Creators",
            all_creators,
            default=all_creators,
        )
        selected_campaigns = (
            st.multiselect("Campaigns", campaigns, default=campaigns) if campaigns else []
        )

    if raw.empty:
        st.info("No metrics for this cohort. Run **Refresh now** to import cohort data.")
        st.stop()

    subtitle = f"{program_view} · Campaign payouts (sales) by creator · Auto-syncs hourly from CreatorIQ"
    st.markdown(
        f'<p class="looker-subtitle">{subtitle}</p>',
        unsafe_allow_html=True,
    )
    meta = read_sync_meta()
    if meta.get("synced_at"):
        src = meta.get("source", "")
        src_note = f" · {src}" if src else ""
        st.markdown(
            f'<span class="sync-pill">Live · updated {meta["synced_at"][:19].replace("T", " ")} UTC{src_note}</span>',
            unsafe_allow_html=True,
        )

    if not selected_creators:
        st.warning("Select at least one creator.")
        st.stop()

    filtered = filter_by_date_range(
        raw,
        start_date,
        end_date,
        creators=selected_creators,
        campaigns=selected_campaigns if campaigns and selected_campaigns else None,
    )
    if filtered.empty:
        st.info("No data for these filters.")
        st.stop()

    summary = aggregate_by_creator(filtered)
    total_followers = int(summary["Follower count"].sum())
    total_engagements = float(filtered["engagements"].sum())
    total_posts = int(filtered["post_count"].sum())
    total_links = int(filtered["link_count"].sum())
    total_gmv = float(filtered["gmv"].sum())
    weighted_er = (
        total_engagements / total_followers * 100 if total_followers else 0.0
    )

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Follower count", format_kpi(total_followers, "followers"))
    k2.metric("ER", format_kpi(weighted_er, "er"))
    k3.metric("# of posts", format_kpi(total_posts, "count"))
    k4.metric("# of links", format_kpi(total_links, "count"))
    k5.metric("GMV $", format_kpi(total_gmv, "money"))

    daily_gmv = aggregate_gmv_per_post_daily(filtered)
    st.markdown(
        '<p class="looker-section-title">GMV per post over time</p>',
        unsafe_allow_html=True,
    )
    st.caption("Purple bars = daily GMV ÷ posts · Black = 14-day rolling · Purple line = 7-day rolling")
    render_gmv_per_post_chart(daily_gmv)

    st.markdown('<p class="looker-section-title">By creator</p>', unsafe_allow_html=True)
    display = summary.rename(columns={"creator": "Creator"}).copy()
    display["Follower count"] = display["Follower count"].map(lambda x: f"{x:,.0f}")
    display["ER %"] = display["ER %"].map(lambda x: f"{x:.2f}%")
    display["# of posts"] = display["# of posts"].map(lambda x: f"{x:,.0f}")
    display["# of links"] = display["# of links"].map(lambda x: f"{x:,.0f}")
    display["GMV $"] = display["GMV $"].map(lambda x: f"${x:,.2f}")
    st.dataframe(display, use_container_width=True, hide_index=True)

    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown('<p class="looker-section-title">GMV by creator</p>', unsafe_allow_html=True)
        st.bar_chart(summary.set_index("creator")[["GMV $"]], color="#1a73e8", height=320)
    with c2:
        st.markdown('<p class="looker-section-title">Engagement rate</p>', unsafe_allow_html=True)
        st.bar_chart(summary.set_index("creator")[["ER %"]], color="#34a853", height=320)


if __name__ == "__main__":
    main()
