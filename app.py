import streamlit as st
import pandas as pd
import requests
import plotly.express as px
from pymongo import MongoClient

# ---------- 基本設定 ----------
DB_NAME = "Portfolio"
COLLECTION_NAME = "us_stocks"
EPS = 1e-9  # 浮點數比較用的容差

st.set_page_config(
    page_title="美股投資儀表板",
    page_icon="📈",
    layout="centered",  # 置中版面在手機上閱讀體驗較佳
)

# ---------- 資料來源連線 ----------
@st.cache_resource
def get_collection():
    """建立 MongoDB 連線（cache_resource 確保整個 session 只連一次）"""
    client = MongoClient(st.secrets["MONGO_URI"])
    return client[DB_NAME][COLLECTION_NAME]


@st.cache_data(ttl=300)
def load_transactions():
    """從 MongoDB 撈取所有交易紀錄，回傳依日期排序的 DataFrame"""
    collection = get_collection()
    docs = list(collection.find({}, {"_id": 0}))
    if not docs:
        return pd.DataFrame(columns=["date", "ticker", "action", "shares", "price"])
    df = pd.DataFrame(docs)
    df["date"] = pd.to_datetime(df["date"])
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=60)
def get_quote(ticker: str):
    """透過 Finnhub 取得即時報價，回傳目前價格；失敗則回傳 None"""
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": st.secrets["FINNHUB_API_KEY"]},
            timeout=10,
        )
        resp.raise_for_status()
        price = resp.json().get("c")  # c = current price（目前價格）
        return float(price) if price else None
    except Exception:
        return None


# ---------- 核心計算 ----------
def compute_portfolio(df: pd.DataFrame):
    """依時間順序重播每一筆交易，計算現金、持股、平均成本、已實現損益、總本金"""
    cash = 0.0          # 帳戶現金
    deposits = 0.0      # 累計本金（DEPOSIT）
    realized = 0.0      # 已實現損益
    positions = {}      # ticker -> {"shares": 股數, "cost": 總成本}

    for _, r in df.iterrows():
        action, ticker = r["action"], r["ticker"]
        shares, price = r["shares"], r["price"]

        if action == "DEPOSIT":
            cash += price
            deposits += price
        elif action == "DIVIDEND":
            cash += price
        elif action == "OTHER":
            # 預設視為現金調整。手續費／提款請在資料庫以「負數」price 紀錄
            cash += price
        elif action == "BUY":
            cost = shares * price
            cash -= cost
            p = positions.setdefault(ticker, {"shares": 0.0, "cost": 0.0})
            p["shares"] += shares
            p["cost"] += cost
        elif action == "SELL":
            cash += shares * price
            p = positions.setdefault(ticker, {"shares": 0.0, "cost": 0.0})
            avg = p["cost"] / p["shares"] if p["shares"] > EPS else 0.0
            realized += (price - avg) * shares     # 移動平均法計算已實現損益
            p["cost"] -= avg * shares
            p["shares"] -= shares

    return cash, deposits, realized, positions


def build_holdings(positions: dict):
    """組裝目前仍持有（股數 > 0）的標的明細，並抓取即時報價"""
    rows = []
    for ticker, p in positions.items():
        if p["shares"] <= EPS:        # 持股為 0 不顯示
            continue
        avg_cost = p["cost"] / p["shares"]
        price = get_quote(ticker)
        # 若報價失敗，市值暫以成本估算，避免整體當機
        market_price = price if price is not None else avg_cost
        market_value = p["shares"] * market_price
        pnl = market_value - p["cost"]
        pnl_pct = (pnl / p["cost"] * 100) if p["cost"] > EPS else 0.0
        rows.append({
            "代號": ticker,
            "股數": round(p["shares"], 4),
            "均價": round(avg_cost, 2),
            "現價": round(market_price, 2) if price is not None else None,
            "市值": round(market_value, 2),
            "損益": round(pnl, 2),
            "報酬率": round(pnl_pct, 2),
        })
    return pd.DataFrame(rows)


# ---------- 介面 ----------
st.title("📈 我的美股儀表板")

_, col_btn = st.columns([3, 1])
with col_btn:
    if st.button("🔄 更新報價", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

df = load_transactions()

if df.empty:
    st.info("資料庫目前沒有任何交易紀錄。")
    st.stop()

cash, deposits, realized, positions = compute_portfolio(df)
holdings = build_holdings(positions)

stock_value = holdings["市值"].sum() if not holdings.empty else 0.0
total_assets = cash + stock_value
unrealized = holdings["損益"].sum() if not holdings.empty else 0.0
total_pnl = total_assets - deposits
cost_basis = stock_value - unrealized
unreal_pct = (unrealized / cost_basis * 100) if cost_basis > EPS else 0.0

# --- 頂部 KPI ---
k1, k2, k3 = st.columns(3)
k1.metric("總本金", f"${deposits:,.0f}")
k2.metric("總資產", f"${total_assets:,.0f}", f"{total_pnl:+,.0f}")
k3.metric("未實現損益", f"${unrealized:+,.0f}", f"{unreal_pct:+.1f}%")

st.caption(f"💵 帳戶現金 ${cash:,.2f}　|　✅ 已實現損益 ${realized:+,.2f}")

st.divider()

# --- 中間：持股明細 ---
st.subheader("持股明細")
if holdings.empty:
    st.write("目前沒有持股。")
else:
    st.dataframe(
        holdings,
        hide_index=True,
        use_container_width=True,
        column_config={
            "均價": st.column_config.NumberColumn(format="$%.2f"),
            "現價": st.column_config.NumberColumn(format="$%.2f"),
            "市值": st.column_config.NumberColumn(format="$%.0f"),
            "損益": st.column_config.NumberColumn(format="$%.0f"),
            "報酬率": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

st.divider()

# --- 底部：資產配置圓餅圖 ---
st.subheader("資產配置")
alloc = []
if not holdings.empty:
    for _, row in holdings.iterrows():
        alloc.append({"資產": row["代號"], "金額": row["市值"]})
if cash > EPS:
    alloc.append({"資產": "現金", "金額": cash})

if alloc:
    alloc_df = pd.DataFrame(alloc)
    fig = px.pie(alloc_df, names="資產", values="金額", hole=0.4)
    fig.update_traces(textposition="inside", textinfo="percent+label")
    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), showlegend=True)
    st.plotly_chart(fig, use_container_width=True)
