import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import io
import csv
import unicodedata
import feedparser
import urllib.parse
from datetime import datetime

# --- ページ設定 ---
st.set_page_config(
    page_title="もりかわ株管理APP", 
    page_icon="📈", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- ユーティリティ関数 ---

def normalize_text(text):
    if not isinstance(text, str): return str(text)
    return unicodedata.normalize('NFKC', text).upper()

def clean_currency(x):
    if isinstance(x, (int, float)): return float(x)
    if isinstance(x, str):
        clean_str = x.replace(',', '').replace('円', '').replace('USD', '').replace('%', '').strip()
        try:
            return float(clean_str)
        except ValueError:
            return 0.0
    return 0.0

def detect_encoding(bytes_data):
    encodings = ['cp932', 'shift_jis', 'utf-8', 'utf-16']
    for enc in encodings:
        try:
            text = bytes_data.decode(enc)
            if "銘柄" in text or "ファンド" in text or "評価" in text or "保有" in text:
                return text
        except:
            continue
    return bytes_data.decode('utf-8', errors='ignore')

def universal_parser(text_data):
    """万能CSVパーサー"""
    data_rows = []
    lines = text_data.splitlines()
    reader = csv.reader(lines)

    col_maps = {
        'name': ['銘柄', '銘柄名', '銘柄名称', 'ファンド名', '銘柄コード/銘柄名', '銘柄・ファンド名'],
        'value': ['評価額', '評価金額', '時価評価額', '時価評価額[円]', '金額'],
        'profit': ['評価損益', '含み損益', '評価損益[円]', '損益'],
        'account': ['口座', '口座区分', '預り区分', '詳細']
    }

    current_header_map = {}
    current_section = "不明"

    for row in reader:
        if not row: continue
        line_str = "".join(row)

        if ("株式" in line_str or "投資信託" in line_str or "NISA" in line_str) and \
           not any(k in line_str for k in ['銘柄', 'ファンド', '数量', '取得']):
             current_section = line_str.replace('"', '').replace(',', '').replace('合計', '').strip()

        is_header = False
        temp_map = {}
        for idx, col_val in enumerate(row):
            col_val_clean = col_val.replace('"', '').strip()
            temp_map[col_val_clean] = idx
            if any(k in col_val_clean for k in col_maps['name']):
                is_header = True
        
        if is_header:
            current_header_map = temp_map
            continue

        if not current_header_map: continue

        name_idx = next((current_header_map[k] for k in col_maps['name'] if k in current_header_map), None)
        val_idx = next((current_header_map[k] for k in col_maps['value'] if k in current_header_map), None)
        pl_idx = next((current_header_map[k] for k in col_maps['profit'] if k in current_header_map), None)
        acc_idx = next((current_header_map[k] for k in col_maps['account'] if k in current_header_map), None)

        if name_idx is not None and val_idx is not None:
            if len(row) > max(name_idx, val_idx):
                try:
                    name_val = row[name_idx].strip()
                    val_float = clean_currency(row[val_idx])
                    
                    if not name_val or (val_float == 0 and row[val_idx].strip() == ""):
                        continue
                    
                    acc_val = row[acc_idx] if acc_idx is not None and len(row) > acc_idx else current_section
                    
                    item = {
                        '銘柄名': name_val,
                        '評価額': val_float,
                        '評価損益': clean_currency(row[pl_idx]) if pl_idx is not None and len(row) > pl_idx else 0,
                        '口座区分_raw': acc_val,
                        '種別_raw': '投資信託' if '投資信託' in current_section or 'ファンド' in name_val else '株式'
                    }
                    data_rows.append(item)
                except:
                    continue

    df = pd.DataFrame(data_rows)
    if not df.empty:
        if any(k in current_header_map for k in ['銘柄コード・ティッカー', '時価評価額[円]']):
            df['証券会社'] = '楽天証券'
        else:
            df['証券会社'] = 'SBI証券'
            
    return df

def guess_attributes(df):
    """属性推測"""
    if df.empty: return df
    
    def get_country(row):
        name = normalize_text(row['銘柄名'])
        cat = normalize_text(row.get('種別_raw', ''))
        if any(x in name for x in ["日本", "TOPIX", "日経", "JAPAN"]) or "国内" in cat: return "日本"
        if any(x in name for x in ["米国", "S&P", "NASDAQ", "全米", "US", "AMERICA", "VYM", "SPYD"]): return "米国"
        if any(x in name for x in ["インド", "INDIA"]): return "インド"
        if any(x in name for x in ["全世界", "オール・カントリー", "オルカン", "GLOBAL"]): return "全世界"
        if "先進国" in name: return "先進国"
        if "新興国" in name: return "新興国"
        return "その他"
    
    def get_class(row):
        name = normalize_text(row['銘柄名'])
        cat = normalize_text(row.get('種別_raw', ''))
        if "投資信託" in cat or "ファンド" in name: return "投資信託"
        if "S&P500" in name or "全米" in name or "オルカン" in name: return "投資信託"
        return "個別株"

    def get_account(row):
        txt = normalize_text(row.get('口座区分_raw', ''))
        if "旧NISA" in txt: return "旧NISA"
        if "つみたて" in txt: return "新NISA(つみたて)"
        if "成長" in txt: return "新NISA(成長)"
        if "特定" in txt: return "特定口座"
        if "一般" in txt: return "一般口座"
        return "特定口座"

    def get_yield(row):
        name = normalize_text(row['銘柄名'])
        ac = get_class(row)
        if any(x in name for x in ["高配当", "VYM", "HDV", "SPYD"]): return 3.5
        if any(x in name for x in ["REIT", "リート"]): return 4.0
        if any(x in name for x in ["債券", "AGG", "BND"]): return 2.5
        if "インド" in name or "NASDAQ" in name: return 0.0
        if any(x in name for x in ["S&P500", "SP500", "全米", "VTI"]): return 1.3
        if any(x in name for x in ["全世界", "オルカン"]): return 1.5
        if ac == "個別株": return 2.0
        return 0.0

    def get_sector(row):
        name = normalize_text(row['銘柄名'])
        ac = get_class(row)
        if ac == "投資信託":
            if any(x in name for x in ["S&P500", "全米", "オルカン", "全世界", "TOPIX", "日経"]): return "インデックス投信"
            if "インド" in name: return "インド株投信"
            if "債券" in name: return "債券ファンド"
            if "ゴールド" in name or "金" in name: return "コモディティ"
            if "REIT" in name: return "REIT"
            return "その他投信"
        
        if any(x in name for x in ["銀行", "フィナンシャル", "FG", "バンク"]): return "銀行・金融"
        if any(x in name for x in ["商事", "物産", "伊藤忠", "丸紅", "双日"]): return "総合商社"
        if any(x in name for x in ["通信", "ソフトバンク", "KDDI", "NTT"]): return "情報通信"
        if any(x in name for x in ["自動車", "トヨタ", "ホンダ", "日産", "マツダ"]): return "自動車・輸送機"
        if any(x in name for x in ["製薬", "薬品", "ファーマ"]): return "医薬品"
        if any(x in name for x in ["鉄", "スチール"]): return "鉄鋼・素材"
        if any(x in name for x in ["船", "郵船", "商船"]): return "海運"
        if any(x in name for x in ["APPLE", "MICROSOFT", "NVIDIA", "AMAZON", "TESLA", "GOOGLE"]): return "米国ハイテク"
        if any(x in name for x in ["P&G", "COCA", "J&J", "MCDONALD"]): return "米国ディフェンシブ"
        return "その他事業"
    
    def get_custom_category(row):
        name = normalize_text(row['銘柄名'])
        ac = get_class(row)
        if ac == "個別株": return "個別株"
        if any(x in name for x in ["ゴールド", "金", "GOLD"]): return "ゴールド"
        if any(x in name for x in ["債券", "BND", "AGG"]): return "債券"
        if any(x in name for x in ["S&P500", "全米", "オルカン", "全世界", "TOPIX", "日経", "NASDAQ", "先進国", "VTI", "VOO"]): return "インデックス投信"
        return "その他投信"

    def get_div_months(row):
        country = get_country(row)
        if country == "日本": return [3, 9]
        if country == "米国": return [3, 6, 9, 12]
        return [6, 12]

    df['国・地域'] = df.apply(get_country, axis=1)
    df['資産クラス'] = df.apply(get_class, axis=1)
    df['口座区分'] = df.apply(get_account, axis=1)
    df['予想利回り(%)'] = df.apply(get_yield, axis=1)
    df['セクター'] = df.apply(get_sector, axis=1)
    df['配当月'] = df.apply(get_div_months, axis=1)
    df['詳細区分'] = df.apply(get_custom_category, axis=1)
    
    df['取得額'] = df['評価額'] - df['評価損益']
    def calc_after_tax(row):
        profit = row['評価損益']
        if profit <= 0: return profit
        if "NISA" in row['口座区分']: return profit
        return profit * 0.79685
    df['含み損益(税引後)'] = df.apply(calc_after_tax, axis=1)
    df['含み損益(税引前)'] = df['評価損益']

    return df

# --- ニュース取得関数 ---
@st.cache_data(ttl=3600)
def fetch_news_rss(query):
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"
    feed = feedparser.parse(url)
    return feed.entries[:5]

def analyze_sentiment(title):
    pos_words = ['最高益', '増益', '急騰', 'ストップ高', '続伸', '好調', '買われる', '上方修正', '増配', '自社株買い', '提携']
    neg_words = ['減益', '赤字', '急落', 'ストップ安', '続落', '不振', '売られる', '下方修正', '減配', '不正', '懸念', '提訴']
    score = 0
    if any(w in title for w in pos_words): score = 1
    if any(w in title for w in neg_words): score = -1
    return score

# --- ページ描画関数 ---

def render_dashboard(df_all):
    # --- 共通サイドバー設定 ---
    st.sidebar.markdown("---")
    st.sidebar.header("⚙️ 表示設定")
    metric_options = ['評価額', '取得額', '含み損益(税引前)', '含み損益(税引後)']
    selected_metric = st.sidebar.selectbox("分析する金額を選択:", metric_options, index=0)
    
    # --- 分析対象フィルター ---
    with st.expander("✅ 分析対象の選択 & セクター編集（クリックして開く）", expanded=False):
        if '分析対象' not in df_all.columns:
            df_all.insert(0, '分析対象', True)
        
        editor_cols = ['分析対象', '詳細区分', '銘柄名', 'セクター', '評価額', '評価損益', '予想利回り(%)']
        edited_df = st.data_editor(
            df_all[editor_cols],
            column_config={
                "分析対象": st.column_config.CheckboxColumn(default=True),
                "詳細区分": st.column_config.SelectboxColumn(options=["個別株", "インデックス投信", "ゴールド", "債券", "その他投信"]),
                "評価額": st.column_config.NumberColumn(format="%d"),
                "評価損益": st.column_config.NumberColumn(format="%d"),
                "予想利回り(%)": st.column_config.NumberColumn(format="%.1f %%"),
            },
            use_container_width=True,
            hide_index=True,
            height=300
        )
    
    df_filtered = df_all.iloc[edited_df.index].copy()
    df_filtered['分析対象'] = edited_df['分析対象']
    df_filtered['詳細区分'] = edited_df['詳細区分']
    df_filtered['セクター'] = edited_df['セクター']
    df_filtered['予想利回り(%)'] = edited_df['予想利回り(%)']
    df_filtered = df_filtered[df_filtered['分析対象'] == True]
    
    if df_filtered.empty:
        st.warning("分析対象がありません。")
        return

    # --- 集計 ---
    total_val = df_filtered[selected_metric].sum()
    total_profit = df_filtered['含み損益(税引前)'].sum()
    rakuten_val = df_filtered[df_filtered['証券会社'] == '楽天証券'][selected_metric].sum()
    sbi_val = df_filtered[df_filtered['証券会社'] == 'SBI証券'][selected_metric].sum()
    
    st.markdown("### 📈 Dashboard Overview")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"合計 {selected_metric}", f"{total_val:,.0f} 円")
    c2.metric("含み益 (税引前)", f"{total_profit:,.0f} 円", delta_color="normal")
    c3.metric(f"楽天証券 ({selected_metric})", f"{rakuten_val:,.0f} 円")
    c4.metric(f"SBI証券 ({selected_metric})", f"{sbi_val:,.0f} 円")

    st.markdown("---")

    # --- ツリーマップ ---
    st.subheader(f"🗺️ 資産ヒートマップ ({selected_metric})")
    df_filtered['損益率'] = df_filtered.apply(lambda x: (x['評価損益'] / (x['評価額'] - x['評価損益']) * 100) if (x['評価額'] - x['評価損益']) > 0 else 0, axis=1)
    
    fig_treemap = px.treemap(
        df_filtered, 
        path=[px.Constant("全資産"), '詳細区分', '銘柄名'], 
        values=selected_metric,
        color='損益率',
        color_continuous_scale='RdBu_r',
        color_continuous_midpoint=0,
    )
    fig_treemap.update_traces(textinfo="label+value+percent entry", hovertemplate='%{label}<br>金額: %{value:,.0f} 円<br>損益率: %{color:.2f}%')
    st.plotly_chart(fig_treemap, use_container_width=True)

    st.markdown("---")

    # --- 詳細分析タブ ---
    tab1, tab2, tab3 = st.tabs(["📊 セクター・国別", "🥧 資産クラス・口座", "💰 配当カレンダー"])
    
    def update_layout_common(fig):
        fig.update_layout(yaxis=dict(tickformat=","), xaxis=dict(tickformat=","))
        fig.update_traces(hovertemplate='%{label}: %{value:,.0f} 円')
        return fig

    with tab1:
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            fig_sec_pie = px.pie(df_filtered, values=selected_metric, names='セクター', title="セクター比率", hole=0.4)
            fig_sec_pie.update_traces(textinfo='percent+label', hovertemplate='%{label}: %{value:,.0f} 円')
            st.plotly_chart(fig_sec_pie, use_container_width=True)
        with col_s2:
            fig2 = px.pie(df_filtered, values=selected_metric, names='国・地域', title="国別比率", hole=0.4)
            fig2.update_traces(textinfo='percent+label', hovertemplate='%{label}: %{value:,.0f} 円')
            st.plotly_chart(fig2, use_container_width=True)

    with tab2:
        st.subheader("💡 資産内訳（詳細区分）")
        col_new1, col_new2 = st.columns([1, 1])
        
        with col_new1:
            fig_custom = px.pie(
                df_filtered, values=selected_metric, names='詳細区分', 
                title="資産内訳 (個別株・インデックス投信・ゴールド・債券)",
                hole=0.4,
                color='詳細区分',
                color_discrete_map={"個別株": "#1f77b4", "インデックス投信": "#2ca02c", "ゴールド": "#ff7f0e", "債券": "#d62728", "その他投信": "#7f7f7f"}
            )
            fig_custom.update_traces(textinfo='percent+label', hovertemplate='%{label}: %{value:,.0f} 円')
            st.plotly_chart(fig_custom, use_container_width=True)
            
        with col_new2:
            acc_grp = df_filtered.groupby('口座区分')[selected_metric].sum().reset_index()
            fig4 = px.bar(acc_grp, x='口座区分', y=selected_metric, color='口座区分', title="口座区分別残高")
            fig4 = update_layout_common(fig4)
            st.plotly_chart(fig4, use_container_width=True)

        st.markdown("---")
        col_a, col_b = st.columns(2)
        with col_a:
            fig3 = px.bar(df_filtered, x='資産クラス', y=selected_metric, color='国・地域', title="資産クラス内訳")
            fig3 = update_layout_common(fig3)
            st.plotly_chart(fig3, use_container_width=True)

    with tab3:
        st.subheader("月別配当金シミュレーション (予想)")
        monthly_div = {m: 0 for m in range(1, 13)}
        
        for _, row in df_filtered.iterrows():
            yearly_yield = row['予想利回り(%)'] / 100
            yearly_div_amount = row['評価額'] * yearly_yield
            payment_months = row['配当月']
            if not payment_months: continue
            amount_per_payment = yearly_div_amount / len(payment_months)
            for m in payment_months:
                monthly_div[m] += amount_per_payment

        df_monthly = pd.DataFrame(list(monthly_div.items()), columns=['月', '配当金額'])
        total_year_div = df_monthly['配当金額'].sum()
        total_assets_val = df_filtered['評価額'].sum()
        avg_yield_port = (total_year_div / total_assets_val * 100) if total_assets_val > 0 else 0

        c_d1, c_d2 = st.columns(2)
        c_d1.metric("年間受取配当（税引前・予想）", f"{total_year_div:,.0f} 円")
        c_d2.metric("平均利回り", f"{avg_yield_port:.2f} %")

        fig_div = px.bar(df_monthly, x='月', y='配当金額', title="月別配当金グラフ", text_auto='.2s')
        fig_div.update_xaxes(tickmode='linear', tick0=1, dtick=1)
        fig_div = update_layout_common(fig_div)
        st.plotly_chart(fig_div, use_container_width=True)

def render_news(df_all):
    st.header("📰 保有銘柄に関するニュース")
    st.caption("※個別株のみを対象としています。")

    stock_df = df_all[df_all['資産クラス'] == '個別株']
    
    if stock_df.empty:
        st.info("個別株（株式）のデータが見つかりませんでした。")
        return

    top_stocks = stock_df.groupby('銘柄名')['評価額'].sum().sort_values(ascending=False).head(10).index.tolist()
    
    st.markdown(f"**評価額上位の個別株（{len(top_stocks)}銘柄）をチェック中...**")
    
    for stock in top_stocks:
        search_query = stock.replace('ホールディングス', 'HD').split(' ')[0] 
        query = f"{search_query} 株価 ニュース"
        entries = fetch_news_rss(query)
        
        if entries:
            with st.expander(f"📌 {stock}", expanded=True):
                found_count = 0
                for entry in entries:
                    sentiment = analyze_sentiment(entry.title)
                    icon = "📄"
                    style_prefix = ""
                    if sentiment == 1: 
                        icon = "📈"
                        style_prefix = ":green-background[好材料?]"
                    elif sentiment == -1: 
                        icon = "📉"
                        style_prefix = ":red-background[警戒]"
                    
                    published = entry.get('published', '')[:16]
                    st.markdown(f"{icon} {style_prefix} **[{entry.title}]({entry.link})**")
                    st.caption(f"{entry.source.title} | {published}")
                    found_count += 1
                if found_count == 0:
                    st.caption("最近の関連ニュースは見つかりませんでした。")
    st.markdown("---")
    st.info("💡 ニュースはGoogle News RSSを利用して取得しています。")

def render_ai_advice(df_all):
    st.header("🤖 AIポートフォリオ診断 (Pro)")
    st.info("あなたの資産状況と最新のニュースを照らし合わせ、プロのアナリスト視点で具体的なアドバイスを行います。")

    # データ準備
    total_assets = df_all['評価額'].sum()
    stock_df = df_all[df_all['資産クラス'] == '個別株']
    top_stocks = stock_df.groupby('銘柄名')['評価額'].sum().sort_values(ascending=False).head(3) # 上位3銘柄を重点分析
    
    # --- 1. 健康診断レポート (Good / Bad) ---
    st.subheader("1. 資産健康診断")
    c_good, c_bad = st.columns(2)

    with c_good:
        st.success("##### 👍 良い点 (Strengths)")
        # 配当利回りチェック
        div_yield = (df_all['評価額'] * df_all['予想利回り(%)']).sum() / total_assets
        if div_yield > 2.5:
            st.markdown(f"- **インカムゲインが太い**: 平均利回りが {div_yield:.1f}% あり、配当再投資による複利効果が期待できます。")
        else:
            st.markdown("- **キャピタルゲイン重視**: 配当よりも値上がり益を狙える構成です。資産拡大期に適しています。")
        
        # NISA活用度チェック
        nisa_assets = df_all[df_all['口座区分'].str.contains('NISA')]['評価額'].sum()
        nisa_ratio = (nisa_assets / total_assets) * 100
        if nisa_ratio > 40:
             st.markdown(f"- **NISA活用が優秀**: 資産の {nisa_ratio:.1f}% が非課税口座にあり、税制メリットを最大限享受できています。")
    
    with c_bad:
        st.error("##### 👎 懸念点 (Weaknesses)")
        # 集中投資チェック
        if not top_stocks.empty:
            top_stock_ratio = (top_stocks.iloc[0] / total_assets) * 100
            if top_stock_ratio > 20:
                st.markdown(f"- **銘柄への過度な依存**: 「{top_stocks.index[0]}」1銘柄で資産の {top_stock_ratio:.1f}% を占めています。この銘柄が急落した際のダメージが甚大です。")
            else:
                st.markdown("- **大きな懸念なし**: 特定銘柄への過度な集中は見られません。")
        
        # 資産クラスの偏り
        bond_gold_ratio = df_all[df_all['詳細区分'].isin(['債券', 'ゴールド'])]['評価額'].sum() / total_assets * 100
        if bond_gold_ratio < 5:
            st.markdown("- **守りが手薄**: 暴落時にクッションとなる「債券」や「ゴールド」がほとんどありません。市場全体の暴落時に資産が大きく目減りするリスクがあります。")

    st.markdown("---")

    # --- 2. ニュース連動型・緊急アドバイス ---
    st.subheader("2. 警戒すべきニュースと具体的対策")
    st.caption("主力銘柄に関する直近のネガティブニュースを検知し、立ち回りを提案します。")

    if top_stocks.empty:
        st.write("個別株の保有がないため、この分析はスキップします。")
    else:
        found_alert = False
        for stock_name, val in top_stocks.items():
            search_query = stock_name.replace('ホールディングス', 'HD').split(' ')[0]
            query = f"{search_query} 株価 ニュース"
            entries = fetch_news_rss(query)
            
            # センチメント分析とアドバイス生成
            neg_news = []
            for entry in entries:
                if analyze_sentiment(entry.title) == -1:
                    neg_news.append(entry)
            
            if neg_news:
                found_alert = True
                with st.expander(f"⚠️ **緊急: {stock_name} に警戒シグナル**", expanded=True):
                    for news in neg_news:
                        st.markdown(f"- 📰 [{news.title}]({news.link})")
                    
                    st.markdown("""
                    **【プロのアドバイス】**
                    ネガティブなニュースが出ています。以下の基準で冷静に対処してください：
                    
                    1.  **「ストーリー」は崩れたか？**:
                        * 単なる「地合いの悪化」や「一時的な減益」なら、**ホールド（または押し目買い）**が正解の可能性が高いです。狼狽売りは厳禁です。
                        * もし「粉飾決算」「強力な競合の出現」「ビジネスモデルの崩壊」なら、**含み損があっても即座に売却（損切り）**することを推奨します。
                    2.  **損切りラインの徹底**:
                        * まだ迷う場合は、「買値から-10%」または「直近安値を割ったら」など、**逆指値（ストップロス）**を必ず設定してください。
                    """)
        
        if not found_alert:
            st.info("✅ 現在、主力銘柄に関して、直ちに売却を迫るような致命的なニュースは見当たりません。上昇トレンドであれば利益を伸ばし（トレーリングストップ活用）、静観するのが賢明です。")

    st.markdown("---")

    # --- 3. 今後の処方箋 (Next Action) ---
    st.subheader("3. 今後のアクションプラン")
    
    # 現金比率がないので、アセットアロケーションに基づく提案
    with st.chat_message("assistant", avatar="🧑‍💼"):
        st.write("ポートフォリオの安定感を高めるために、次の買い付けでは以下を検討してください：")
        
        recommendations = []
        if bond_gold_ratio < 10:
            recommendations.append("**ゴールド（金）または債券ETF（AGG/BND）**: 株式との相関が低い資産を10%程度まで増やすと、資産全体の変動率（リスク）を下げられます。")
        
        if div_yield < 1.5:
             recommendations.append("**高配当株（VYM/HDVなど）**: 下落相場でも配当が心の支えになります。少しインカムゲインを強化しても良いでしょう。")
        
        if not recommendations:
            recommendations.append("**現状維持（オルカン/S&P500積立継続）**: 非常にバランスが良い状態です。このまま積立を継続し、余計な売買をしないことが最高のリターンを生みます。")
            
        for rec in recommendations:
            st.markdown(f"- {rec}")

# --- メイン処理 ---
st.sidebar.title("もりかわ株管理APP")
page = st.sidebar.radio("メニュー切り替え", ["📊 保有資産内訳", "📰 保有銘柄に関するニュース", "🤖 AIポートフォリオ診断"], index=0)
st.sidebar.markdown("---")
st.sidebar.header("📂 データ取り込み")
st.sidebar.caption("対応：楽天証券、SBI証券")
uploaded_files = st.sidebar.file_uploader("CSVをアップロード", type=['csv'], accept_multiple_files=True)

if uploaded_files:
    df_list = []
    for file in uploaded_files:
        content = file.getvalue()
        text = detect_encoding(content)
        df_temp = universal_parser(text)
        if not df_temp.empty:
            df_list.append(df_temp)
    
    if df_list:
        df_all = pd.concat(df_list, ignore_index=True)
        df_all = guess_attributes(df_all)
        
        if page == "📊 保有資産内訳":
            render_dashboard(df_all)
        elif page == "📰 保有銘柄に関するニュース":
            render_news(df_all)
        elif page == "🤖 AIポートフォリオ診断":
            render_ai_advice(df_all)
    else:
        st.error("CSVを読み込めませんでした。")
else:
    st.info("👈 左側のサイドバーからCSVファイルをアップロードしてください。")
