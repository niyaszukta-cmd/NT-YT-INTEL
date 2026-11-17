"""YT Scope: YouTube niche and viral-content research for Indian language channels.

Uses the official YouTube Data API v3 (free key from Google Cloud Console).
Quota: search.list costs 100 units per call, videos.list and channels.list cost 1 unit per call
(up to 50 ids each). The default free quota is 10,000 units per day, so about 80 to 90 searches a day.
"""
import re
import math
import html
import random
import datetime as dt
from collections import Counter

import requests
import pandas as pd
import altair as alt
import streamlit as st

from i18n import t, LANGS

API = "https://www.googleapis.com/youtube/v3"
DEVA = re.compile(r"[ऀ-ॿ]")
WORD = re.compile(r"[\wऀ-ॿ]+")
STOP = set("the a an of to in is and or for on with how what kya hai ka ki ke ko se me mein aur ya hi to".split())
TEAL, AMBER, RED, BLUE, MUTED = "#1DD3A0", "#FFB020", "#FF4D6A", "#4C8DFF", "#8A96B2"

st.set_page_config(page_title="YT Scope | NYZTrade", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")


# ============================================================================= helpers
def h(s: str) -> str:
    """Collapse indented HTML so Streamlit markdown never treats it as a code block."""
    return " ".join(line.strip() for line in s.splitlines() if line.strip())


def iso_minutes(s: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    d, hh, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 1440 + hh * 60 + mi + se / 60


def call(endpoint: str, key: str, **params) -> dict:
    r = requests.get(f"{API}/{endpoint}", params={**params, "key": key}, timeout=25)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except Exception:
            msg = r.text
        raise RuntimeError(f"YouTube API error {r.status_code}: {msg}")
    return r.json()


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fmt(n):
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "-"
    n = float(n)
    for lim, suf in ((1e7, "Cr"), (1e5, "L"), (1e3, "K")):
        if n >= lim:
            return f"{n / lim:.1f}{suf}".replace(".0", "")
    return f"{int(n)}"


# ============================================================================= data layer
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def search_ids(_key, query, days, region, lang, order, duration, max_results):
    after = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ") if days else None
    ids, token = [], None
    while len(ids) < max_results:
        p = dict(part="snippet", type="video", q=query, maxResults=min(50, max_results - len(ids)),
                 order=order, regionCode=region, relevanceLanguage=lang, videoDuration=duration)
        if after:
            p["publishedAfter"] = after
        if token:
            p["pageToken"] = token
        j = call("search", _key, **p)
        ids += [i["id"]["videoId"] for i in j.get("items", []) if i.get("id", {}).get("videoId")]
        token = j.get("nextPageToken")
        if not token:
            break
    return ids[:max_results]


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def details(_key, ids: tuple):
    vids, ch_ids = [], set()
    for part in chunks(list(ids), 50):
        j = call("videos", _key, part="snippet,statistics,contentDetails", id=",".join(part))
        for it in j.get("items", []):
            vids.append(it)
            ch_ids.add(it["snippet"]["channelId"])
    subs = {}
    for part in chunks(sorted(ch_ids), 50):
        j = call("channels", _key, part="statistics", id=",".join(part))
        for it in j.get("items", []):
            s_ = it.get("statistics", {})
            subs[it["id"]] = None if s_.get("hiddenSubscriberCount") else int(s_.get("subscriberCount", 0))
    return vids, subs


def to_frame(vids, subs) -> pd.DataFrame:
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for v in vids:
        sn, stt, cd = v["snippet"], v.get("statistics", {}), v.get("contentDetails", {})
        pub = dt.datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        age = max((now - pub).total_seconds() / 86400, 0.25)
        views = int(stt.get("viewCount", 0))
        likes = int(stt.get("likeCount", 0))
        com = int(stt.get("commentCount", 0))
        sub = subs.get(sn["channelId"])
        title = sn["title"]
        rows.append(dict(
            vid=v["id"], title=title, channel=sn["channelTitle"], channel_id=sn["channelId"], published=pub.date(),
            age_days=round(age, 1), views=views, likes=likes, comments=com, subs=sub,
            minutes=round(iso_minutes(cd.get("duration", "")), 1),
            views_per_day=round(views / age), outlier=round(views / sub, 2) if sub else None,
            engagement_pct=round((likes + com) / views * 100, 2) if views else 0.0,
            title_len=len(title), deva_pct=round(len(DEVA.findall(title)) / max(len(title), 1) * 100),
            tags=sn.get("tags", []), url=f"https://www.youtube.com/watch?v={v['id']}"))
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600, show_spinner=False)
def autocomplete(q: str):
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search",
                         params={"client": "firefox", "ds": "yt", "q": q, "hl": "hi"}, timeout=10)
        return r.json()[1]
    except Exception:
        return []


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def trending(_key, region, category):
    j = call("videos", _key, part="snippet,statistics,contentDetails", chart="mostPopular",
             regionCode=region, videoCategoryId=category, maxResults=50)
    vids = j.get("items", [])
    ch = sorted({v["snippet"]["channelId"] for v in vids})
    subs = {}
    for part in chunks(ch, 50):
        for it in call("channels", _key, part="statistics", id=",".join(part)).get("items", []):
            s_ = it.get("statistics", {})
            subs[it["id"]] = None if s_.get("hiddenSubscriberCount") else int(s_.get("subscriberCount", 0))
    return vids, subs


def demo_frame(query: str, n: int) -> pd.DataFrame:
    rnd = random.Random(sum(map(ord, query)))
    stems = ["{q} kaise nikale", "{q} kya hai", "{q} explained in Hindi", "{q} for beginners", "{q} ki 5 galtiyan",
             "{q} samjho aasan bhasha me", "{q} real example", "{q} Hindi me"]
    pool = ["stock valuation", "dcf", "intrinsic value", "fundamental analysis", "share market hindi", "pe ratio",
            "investing", "value investing", "reverse dcf", "stock market for beginners", "fair value", "screener"]
    now = dt.datetime.now(dt.timezone.utc)
    vids, subs = [], {}
    tag = sum(map(ord, query)) % 9999
    for i in range(n):
        cid = f"C{rnd.randint(1, 18)}"
        subs.setdefault(cid, int(10 ** rnd.uniform(3.2, 6.8)))
        age = rnd.uniform(2, 400)
        views = int(subs[cid] * rnd.lognormvariate(-0.5, 1.2))
        vids.append({"id": f"demo{tag}_{i}", "snippet": {
            "title": rnd.choice(stems).format(q=query.title()), "channelId": cid, "channelTitle": f"Channel {cid[1:]}",
            "publishedAt": (now - dt.timedelta(days=age)).isoformat(), "tags": rnd.sample(pool, rnd.randint(4, 9))},
            "statistics": {"viewCount": views, "likeCount": int(views * rnd.uniform(.01, .05)),
                           "commentCount": int(views * rnd.uniform(.0005, .004))},
            "contentDetails": {"duration": f"PT{rnd.randint(1, 25)}M{rnd.randint(0, 59)}S"}})
    return to_frame(vids, subs)


# ============================================================================= analysis
def title_words(df: pd.DataFrame, top=25) -> pd.DataFrame:
    c = Counter(w for t in df["title"] for w in set(WORD.findall(t.lower())) if len(w) > 2 and w not in STOP)
    return pd.DataFrame(c.most_common(top), columns=["word", "videos"])


def bigrams(df: pd.DataFrame, top=20) -> pd.DataFrame:
    c = Counter()
    for t in df["title"]:
        w = WORD.findall(t.lower())
        c.update(set(zip(w, w[1:])))
    return pd.DataFrame([(" ".join(k), v) for k, v in c.most_common(top) if v > 1], columns=["phrase", "videos"])


def tag_table(df: pd.DataFrame, top=40) -> pd.DataFrame:
    c = Counter(t.lower().strip() for tags in df["tags"] for t in set(tags))
    total = max(len(df), 1)
    out = pd.DataFrame(c.most_common(top), columns=["tag", "videos"])
    out["share_pct"] = (out["videos"] / total * 100).round(0)
    return out


def tag_string(tt: pd.DataFrame, limit=480) -> str:
    out, size = [], 0
    for t in tt["tag"]:
        if size + len(t) + 1 > limit:
            break
        out.append(t)
        size += len(t) + 1
    return ", ".join(out)


def opportunity(df: pd.DataFrame, small=50_000) -> dict:
    d = df.dropna(subset=["subs"])
    if d.empty:
        return dict(videos=len(df), median_views=int(df["views"].median()) if len(df) else 0,
                    small_share=None, breakout=None, score=None)
    sm = d[d["subs"] <= small]
    br = sm[sm["outlier"] >= 1]
    share = len(sm) / len(d)
    brk = len(br) / max(len(sm), 1)
    return dict(videos=len(df), median_views=int(df["views"].median()), small_share=round(share * 100),
                breakout=round(brk * 100), score=round(100 * (0.5 * share + 0.5 * brk)))


def insights(allv: pd.DataFrame, frames: dict, small: int) -> list:
    out = []
    top = allv.sort_values("views_per_day", ascending=False).head(max(10, len(allv) // 4))
    out.append((TEAL, t("ins1", m=top["minutes"].median(), c=top["title_len"].median())))
    tt = tag_table(top, 5)
    if len(tt):
        out.append((BLUE, t("ins2", t=html.escape(", ".join(tt["tag"].head(4))))))
    gaps = sorted(((k, opportunity(f, small)["score"]) for k, f in frames.items()),
                  key=lambda x: (x[1] is None, -(x[1] or 0)))
    if gaps and gaps[0][1] is not None:
        out.append((AMBER, t("ins3", k=html.escape(gaps[0][0]), s=gaps[0][1])))
    br = allv.dropna(subset=["outlier"])
    br = br[(br["outlier"] >= 1) & (br["subs"] <= small)]
    out.append((RED, t("ins4", n=len(br))))
    return out


RISKY = {"buy", "sell", "target", "targets", "guaranteed", "guarantee", "sure", "shot", "multibagger", "multibaggers",
         "undervalued", "jackpot", "tips", "tip", "profit", "profits", "double", "triple", "crorepati", "rich", "free"}


def safe_word(w: str) -> bool:
    return not (set(WORD.findall(w.lower())) & RISKY)


def video_plan(allv: pd.DataFrame, frames: dict, small: int) -> dict:
    scores = [(k, opportunity(f, small)["score"]) for k, f in frames.items()]
    scored = [x for x in scores if x[1] is not None]
    kw, sc = max(scored, key=lambda x: x[1]) if scored else (list(frames)[0], None)
    pool = frames[kw]
    top = pool.sort_values("views_per_day", ascending=False).head(max(10, len(pool) // 4))
    kw_words = set(WORD.findall(kw.lower()))
    mods = [w for w in title_words(top, 40)["word"] if w not in kw_words and len(w) > 3 and safe_word(w)]
    m = (mods + ["basics", "example", "method", "steps"])[:4]
    base = kw.strip().title()
    drafts = [f"{base}: {m[0].title()} {m[1].title()} Explained",
              f"{base} for Beginners | {m[0].title()}",
              f"{base} Step by Step with a Real Example",
              f"{base}: Common Mistakes ({m[2].title()})"]
    limit = max(int(top["title_len"].median()) + 15, 70)
    drafts = [d if len(d) <= limit else d[:limit].rsplit(" ", 1)[0] for d in drafts]
    tt = tag_table(top, 60)
    tt = tt[tt["tag"].map(safe_word)]
    br = pool.dropna(subset=["outlier"])
    br = br[br["subs"] <= small].sort_values("outlier", ascending=False)
    model = br.iloc[0] if len(br) else top.iloc[0]
    return dict(keyword=kw, score=sc, drafts=drafts, tags=tag_string(tt), length=float(top["minutes"].median()),
                tlen=float(top["title_len"].median()), model=model)


# ============================================================================= styling
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Malayalam:wght@400;700&family=Noto+Sans+Devanagari:wght@400;700&display=swap');
:root{--teal:#1DD3A0;--amber:#FFB020;--red:#FF4D6A;--blue:#4C8DFF;--bg:#0A1020;--card:#111A2E;--line:#1F2B47;--muted:#8A96B2}
html,body,.stApp,.stMarkdown,button,input,textarea,[data-baseweb]{font-family:"Source Sans Pro","Source Sans 3","Noto Sans Malayalam","Noto Sans Devanagari",sans-serif}
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"]{visibility:hidden;height:0}
[data-testid="stHeader"]{background:transparent}
.block-container{padding-top:2.2rem;padding-bottom:3rem;max-width:1280px}
.stApp{background:radial-gradient(1200px 500px at 85% -10%,rgba(29,211,160,.10),transparent 60%),var(--bg)}
[data-testid="stSidebar"]{background:#0D1426;border-right:1px solid var(--line)}
h1,h2,h3{letter-spacing:-.3px}
.brand{display:flex;align-items:center;gap:10px;margin-top:6px}
.brand .dot{width:14px;height:14px;border-radius:4px;background:var(--teal)}
.brand .name{font-weight:800;font-size:20px}
.brand .sub{color:var(--muted);font-size:13px;margin-left:6px}
div[role="radiogroup"][aria-label="Language"],div[data-testid="stRadio"] div[role="radiogroup"]{gap:6px}
.hero{border:1px solid var(--line);border-radius:22px;padding:56px 52px;margin:8px 0 26px;position:relative;overflow:hidden;
  background:linear-gradient(135deg,rgba(29,211,160,.10),rgba(76,141,255,.07) 55%,rgba(17,26,46,.2))}
.hero:after{content:"";position:absolute;right:-60px;top:-60px;width:380px;height:380px;border-radius:50%;
  background:radial-gradient(circle,rgba(29,211,160,.22),transparent 65%)}
.pill{display:inline-block;padding:6px 14px;border-radius:99px;font-size:12.5px;font-weight:700;letter-spacing:.4px;
  background:rgba(29,211,160,.12);color:var(--teal);border:1px solid rgba(29,211,160,.35)}
.hero h1{font-size:46px;line-height:1.2;margin:18px 0 14px;font-weight:800;max-width:780px;padding:0}
.hero p{font-size:18px;color:#B9C4DD;max-width:680px;line-height:1.6;margin:0}
.candles{position:absolute;right:56px;bottom:34px;display:flex;align-items:flex-end;gap:10px;opacity:.9;z-index:1}
.candles i{display:block;width:16px;border-radius:4px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:8px 0 8px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:8px 0}
.fcard{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 20px;height:100%}
.fcard .ic{font-size:26px;margin-bottom:10px}
.fcard b{display:block;font-size:16.5px;margin-bottom:6px}
.fcard span{color:#A9B5CF;font-size:14px;line-height:1.55}
.step{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 20px}
.step .n{width:34px;height:34px;border-radius:50%;background:var(--teal);color:#06231B;font-weight:800;
  display:flex;align-items:center;justify-content:center;margin-bottom:12px}
.step b{display:block;font-size:16.5px;margin-bottom:6px}
.step span{color:#A9B5CF;font-size:14px;line-height:1.55}
.sec{font-size:13px;letter-spacing:1.2px;color:var(--muted);font-weight:700;text-transform:uppercase;margin:34px 0 10px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:6px 0 18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;border-left:4px solid var(--teal)}
.kpi .l{color:var(--muted);font-size:12.5px;font-weight:600;letter-spacing:.3px}
.kpi .v{font-size:30px;font-weight:800;margin-top:4px}
.ins{display:flex;gap:12px;align-items:flex-start;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:12px 16px;margin-bottom:10px;font-size:15px}
.ins .bar{width:4px;align-self:stretch;border-radius:4px}
.plan{background:linear-gradient(135deg,rgba(29,211,160,.10),rgba(17,26,46,.9));border:1px solid rgba(29,211,160,.4);border-radius:16px;padding:8px 22px;margin-bottom:12px}
.prow{display:flex;gap:18px;padding:14px 0;border-bottom:1px solid var(--line)}
.prow:last-child{border-bottom:0}
.pl{min-width:110px;color:var(--teal);font-weight:800;font-size:12.5px;letter-spacing:.8px;padding-top:3px}
.prow ul{margin:0 0 6px 18px;padding:0}.prow li{margin-bottom:4px}
.pm{display:block;color:var(--muted);font-size:12.5px;margin-top:3px}
.prow a{color:#E8EEF9!important;font-weight:700}
.vgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:10px 0 18px}
.vcard{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;text-decoration:none!important;color:inherit!important;display:block}
.vcard:hover{border-color:var(--teal);transform:translateY(-2px);transition:.15s}
.vcard .th{height:150px;background:linear-gradient(135deg,#16233f,#0e1a33);background-size:cover;background-position:center;position:relative}
.vcard .th .tag{position:absolute;left:10px;top:10px;background:rgba(10,16,32,.85);padding:3px 9px;border-radius:99px;font-size:12px;font-weight:700;color:var(--teal)}
.vcard .bd{padding:12px 14px 14px}
.vcard .t{font-weight:700;font-size:14.5px;line-height:1.35;height:39px;overflow:hidden}
.vcard .m{color:var(--muted);font-size:12.5px;margin-top:8px;display:flex;justify-content:space-between}
.stTabs [data-baseweb="tab-list"]{gap:6px;border-bottom:1px solid var(--line)}
.stTabs [data-baseweb="tab"]{height:44px;padding:0 16px;border-radius:10px 10px 0 0;color:#B9C4DD;font-weight:600}
.stTabs [aria-selected="true"]{color:var(--teal)!important}
button[kind="primary"]{background:var(--teal)!important;color:#06231B!important;font-weight:800!important;border:0!important;border-radius:10px!important}
button[kind="primary"]:hover{filter:brightness(1.08)}
button[kind="secondary"]{border-radius:10px!important;border:1px solid var(--line)!important}
[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:12px;overflow:hidden}
[data-testid="stExpander"]{border:1px solid var(--line);border-radius:12px;background:var(--card)}
.foot{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px;line-height:1.7}
@media (max-width:900px){.grid4,.kpis{grid-template-columns:repeat(2,1fr)}.grid3,.vgrid{grid-template-columns:1fr}
 .hero{padding:34px 24px}.hero h1{font-size:32px}.candles{display:none}}
</style>
"""


def kpi(label, value, color=TEAL):
    return f'<div class="kpi" style="border-left-color:{color}"><div class="l">{label}</div><div class="v">{value}</div></div>'


def video_cards(df: pd.DataFrame, n=6):
    cards = []
    for _, r in df.head(n).iterrows():
        thumb = "" if str(r["vid"]).startswith("demo") else f"background-image:url('https://i.ytimg.com/vi/{r['vid']}/mqdefault.jpg')"
        x = f"{r['outlier']:.1f}" if pd.notna(r["outlier"]) else "-"
        cards.append(h(f"""
        <a class="vcard" href="{r['url']}" target="_blank">
          <div class="th" style="{thumb}"><span class="tag">{t('badge', x=x)}</span></div>
          <div class="bd"><div class="t">{html.escape(r['title'])}</div>
          <div class="m"><span>{html.escape(str(r['channel']))[:22]}</span><span>{t('views_sfx', v=fmt(r['views']))}</span></div></div>
        </a>"""))
    st.markdown('<div class="vgrid">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def plan_box(p: dict):
    sc = t("pl_score", s=f"{p['score']}/100") if p["score"] is not None else ""
    drafts = "".join(f"<li>{html.escape(d)}</li>" for d in p["drafts"])
    m = p["model"]
    x = m["outlier"] if pd.notna(m["outlier"]) else "-"
    st.markdown(f'<div class="sec">{t("sec_plan")}</div>', unsafe_allow_html=True)
    st.markdown(h(f"""
    <div class="plan">
      <div class="prow"><span class="pl">{t('pl1')}</span><div><b>{html.escape(p['keyword'])}</b>
        <span class="pm">{sc}</span></div></div>
      <div class="prow"><span class="pl">{t('pl2')}</span><div><ul>{drafts}</ul>
        <span class="pm">{t('pl_titlenote', n=f"{p['tlen']:.0f}")}</span></div></div>
      <div class="prow"><span class="pl">{t('pl3')}</span><div><b>{t('pl_len', n=f"{p['length']:.0f}")}</b>
        <span class="pm">{t('pl_lennote')}</span></div></div>
      <div class="prow"><span class="pl">{t('pl4')}</span><div><a href="{m['url']}" target="_blank">{html.escape(m['title'])}</a>
        <span class="pm">{t('pl_model', c=html.escape(str(m['channel'])), v=fmt(m['views']), x=x)}</span></div></div>
    </div>"""), unsafe_allow_html=True)
    st.text_area(t("pl_tags"), p["tags"], height=80)
    st.caption(t("pl_cap"))


def go(page: str):
    st.session_state["page"] = page


def start_demo():
    st.session_state["demo"] = True
    st.session_state["page"] = "app"


# ============================================================================= top bar (language first)
st.markdown(CSS, unsafe_allow_html=True)
st.session_state.setdefault("page", "home")
st.session_state.setdefault("ui_lang", "English")
if st.session_state["ui_lang"] != "English":
    st.markdown("<style>.sec,.pl,.kpi .l,.pill,.hero h1{letter-spacing:0!important;text-transform:none!important}</style>",
                unsafe_allow_html=True)

top_l, top_r = st.columns([3, 2])
with top_r:
    st.radio("Language", list(LANGS), horizontal=True, key="ui_lang", label_visibility="collapsed")
with top_l:
    sub = t("tagline") if st.session_state["page"] == "home" else t("workspace")
    st.markdown(f'<div class="brand"><div class="dot"></div><div class="name">YT Scope</div><div class="sub">{sub}</div></div>',
                unsafe_allow_html=True)

# ============================================================================= sidebar
with st.sidebar:
    st.markdown('<div class="brand"><div class="dot"></div><div class="name">YT Scope</div></div>'
                f'<div style="color:#8A96B2;font-size:13px;margin-bottom:14px">{t("by")}</div>', unsafe_allow_html=True)
    key = ""
    try:
        key = st.secrets.get("YOUTUBE_API_KEY", "")
    except Exception:
        pass
    key = st.text_input(t("api_key"), value=key, type="password", help=t("api_help"))
    st.session_state.setdefault("demo", not bool(key))
    demo = st.toggle(t("demo"), key="demo", help=t("demo_help"))
    st.divider()
    region = st.selectbox(t("region"), ["IN", "US", "GB", "AE", "SG"], index=0)
    lang = st.selectbox(t("slang"), ["hi", "ml", "en", "ta", "te", "bn", "mr"], index=0)
    days = st.select_slider(t("days"), [7, 30, 90, 180, 365, 0], value=180,
                            format_func=lambda x: t("any_time") if x == 0 else str(x))
    order = st.selectbox(t("order"), ["viewCount", "relevance", "date", "rating"], index=0)
    duration = st.selectbox(t("length"), ["any", "short", "medium", "long"], index=0, help=t("length_help"))
    max_results = st.slider(t("per_kw"), 10, 100, 50, step=10)
    small_ch = st.number_input(t("small"), 1000, 1_000_000, 50_000, step=5000)
    st.caption(t("quota"))
    if st.session_state["page"] == "app":
        st.button(t("back"), on_click=go, args=("home",), width="stretch")


# ============================================================================= landing page
def landing():
    st.markdown(h(f"""
    <div class="hero">
      <span class="pill">{t('pill')}</span>
      <h1 style="color:#E8EEF9">{t('h1a')}<span style="color:#1DD3A0">{t('h1b')}</span></h1>
      <p>{t('hero_p')}</p>
      <div class="candles">
        <i style="height:46px;background:#FF4D6A"></i><i style="height:70px;background:#1DD3A0"></i>
        <i style="height:54px;background:#FF4D6A"></i><i style="height:96px;background:#1DD3A0"></i>
        <i style="height:78px;background:#1DD3A0"></i><i style="height:128px;background:#1DD3A0"></i>
      </div>
    </div>"""), unsafe_allow_html=True)

    c1, c2, _ = st.columns([1.3, 1.3, 3])
    c1.button(t("btn_demo"), type="primary", on_click=start_demo, width="stretch")
    c2.button(t("btn_key"), on_click=go, args=("app",), width="stretch")

    st.markdown(f'<div class="sec">{t("sec_get")}</div>', unsafe_allow_html=True)
    st.markdown(h(f"""
    <div class="grid4">
      <div class="fcard"><div class="ic">🚀</div><b>{t('f1t')}</b><span>{t('f1d')}</span></div>
      <div class="fcard"><div class="ic">🏷️</div><b>{t('f2t')}</b><span>{t('f2d')}</span></div>
      <div class="fcard"><div class="ic">🎯</div><b>{t('f3t')}</b><span>{t('f3d')}</span></div>
      <div class="fcard"><div class="ic">💡</div><b>{t('f4t')}</b><span>{t('f4d')}</span></div>
    </div>"""), unsafe_allow_html=True)

    st.markdown(f'<div class="sec">{t("sec_how")}</div>', unsafe_allow_html=True)
    st.markdown(h(f"""
    <div class="grid3">
      <div class="step"><div class="n">1</div><b>{t('s1t')}</b><span>{t('s1d')}</span></div>
      <div class="step"><div class="n">2</div><b>{t('s2t')}</b><span>{t('s2d')}</span></div>
      <div class="step"><div class="n">3</div><b>{t('s3t')}</b><span>{t('s3d')}</span></div>
    </div>"""), unsafe_allow_html=True)

    st.markdown(f'<div class="sec">{t("sec_faq")}</div>', unsafe_allow_html=True)
    with st.expander(t("faq1q")):
        st.markdown(t("faq1a"))
    with st.expander(t("faq2q")):
        st.write(t("faq2a"))
    with st.expander(t("faq3q")):
        st.write(t("faq3a"))


# ============================================================================= analysis page
def app_page():
    if demo or not key:
        st.info(t("demo_info"))

    tabs = st.tabs([t("tab1"), t("tab2"), t("tab3"), t("tab4"), t("tab5")])
    tab1, tab2, tab3, tab4, tab5 = tabs

    with tab1:
        kws = st.text_area(t("kw_label"), "reverse dcf hindi\nintrinsic value kaise nikale\nstock valuation hindi", height=110)
        run = st.button(t("analyse"), type="primary")

        if run:
            keywords = [k.strip() for k in kws.splitlines() if k.strip()][:10]
            frames = {}
            prog = st.progress(0.0, text=t("p_start"))
            try:
                for i, k in enumerate(keywords):
                    prog.progress(i / max(len(keywords), 1), text=t("p_search", k=k))
                    if demo or not key:
                        frames[k] = demo_frame(k, max_results)
                    else:
                        ids = search_ids(key, k, days, region, lang, order, duration, max_results)
                        vids, subs = details(key, tuple(ids))
                        frames[k] = to_frame(vids, subs)
                prog.empty()
                st.session_state["frames"] = frames
            except Exception as e:
                prog.empty()
                st.error(str(e))
                st.stop()

        if "frames" in st.session_state:
            frames = st.session_state["frames"]
            allv = pd.concat([f.assign(keyword=k) for k, f in frames.items()], ignore_index=True).drop_duplicates(subset=["url"])
            st.session_state["all"] = allv

            st.markdown('<div class="kpis">' + kpi(t("k_videos"), fmt(len(allv)))
                        + kpi(t("k_views"), fmt(allv["views"].median()), BLUE)
                        + kpi(t("k_len"), t("min", n=f"{allv['minutes'].median():.0f}"), AMBER)
                        + kpi(t("k_ch"), fmt(allv["channel_id"].nunique()), RED) + "</div>", unsafe_allow_html=True)

            st.markdown(f'<div class="sec">{t("sec_take")}</div>', unsafe_allow_html=True)
            for color, text in insights(allv, frames, small_ch):
                st.markdown(f'<div class="ins"><div class="bar" style="background:{color}"></div><div>{text}</div></div>',
                            unsafe_allow_html=True)

            plan_box(video_plan(allv, frames, small_ch))

            br = allv.dropna(subset=["outlier"])
            br = br[br["subs"] <= small_ch].sort_values("outlier", ascending=False)
            if len(br):
                st.markdown(f'<div class="sec">{t("sec_break")}</div>', unsafe_allow_html=True)
                video_cards(br, 6)

            st.markdown(f'<div class="sec">{t("sec_all")}</div>', unsafe_allow_html=True)
            view = st.radio(t("sort"), ["views_per_day", "outlier", "views", "engagement_pct"], horizontal=True,
                            help=t("sort_help"))
            show = allv.sort_values(view, ascending=False, na_position="last")
            st.dataframe(
                show[["keyword", "title", "channel", "subs", "views", "views_per_day", "outlier", "engagement_pct",
                      "minutes", "age_days", "published", "url"]],
                column_config={"url": st.column_config.LinkColumn(t("link"), display_text=t("open")),
                               "subs": st.column_config.NumberColumn("subs", format="%d"),
                               "views": st.column_config.NumberColumn("views", format="%d")},
                hide_index=True, height=420)
            st.download_button(t("dl"), show.drop(columns=["tags"]).to_csv(index=False).encode(),
                               "yt_scope_videos.csv", "text/csv")

            plot = allv.dropna(subset=["subs"]).copy()
            if len(plot):
                diag = pd.DataFrame({"x": [plot["subs"].min(), plot["subs"].max()]}).assign(y=lambda d: d["x"])
                pts = alt.Chart(plot).mark_circle(opacity=0.7).encode(
                    x=alt.X("subs:Q", scale=alt.Scale(type="log"), title=t("ax_subs")),
                    y=alt.Y("views:Q", scale=alt.Scale(type="log"), title=t("ax_views")),
                    size=alt.Size("views_per_day:Q", legend=None),
                    color=alt.Color("keyword:N", legend=alt.Legend(orient="bottom", title=None)),
                    tooltip=["title", "channel", "subs", "views", "outlier"])
                line = alt.Chart(diag).mark_line(color=MUTED, strokeDash=[5, 5]).encode(x="x:Q", y="y:Q")
                st.markdown(f'<div class="sec">{t("sec_chart")}</div>', unsafe_allow_html=True)
                st.altair_chart((pts + line).properties(height=380), theme=None)
                st.caption(t("chart_cap"))
        else:
            st.markdown(f'<div class="ins"><div class="bar" style="background:#1DD3A0"></div><div>{t("empty")}</div></div>',
                        unsafe_allow_html=True)

    with tab2:
        allv = st.session_state.get("all")
        if allv is None:
            st.info(t("need_run"))
        else:
            top_n = st.slider(t("topn"), 10, max(len(allv), 11), min(40, len(allv)))
            top = allv.sort_values("views_per_day", ascending=False).head(top_n)
            a, b = st.columns(2)
            with a:
                st.markdown(f'<div class="sec">{t("sec_tags")}</div>', unsafe_allow_html=True)
                tt = tag_table(top)
                if tt.empty:
                    st.write(t("no_tags"))
                else:
                    st.altair_chart(alt.Chart(tt.head(20)).mark_bar(color=TEAL, cornerRadiusEnd=4).encode(
                        x=alt.X("videos:Q", title=t("ax_tagvideos")), y=alt.Y("tag:N", sort="-x", title=None),
                        tooltip=["tag", "videos", "share_pct"]).properties(height=430), theme=None)
                    st.text_area(t("tagstr"), tag_string(tt), height=90)
            with b:
                st.markdown(f'<div class="sec">{t("sec_words")}</div>', unsafe_allow_html=True)
                st.altair_chart(alt.Chart(title_words(top).head(20)).mark_bar(color=BLUE, cornerRadiusEnd=4).encode(
                    x=alt.X("videos:Q", title=t("ax_titles")), y=alt.Y("word:N", sort="-x", title=None)).properties(height=430), theme=None)
                bg = bigrams(top)
                if len(bg):
                    st.dataframe(bg, hide_index=True)
            st.markdown(f'<div class="sec">{t("sec_prof")}</div>', unsafe_allow_html=True)
            st.markdown('<div class="kpis" style="grid-template-columns:repeat(3,1fr)">'
                        + kpi(t("k_tlen"), t("chars", n=f"{top['title_len'].median():.0f}"))
                        + kpi(t("k_vlen"), t("min", n=f"{top['minutes'].median():.1f}"), AMBER)
                        + kpi(t("k_deva"), f"{(top['deva_pct'] > 20).mean() * 100:.0f}%", BLUE) + "</div>",
                        unsafe_allow_html=True)
            st.caption(t("tag_note"))

    with tab3:
        frames = st.session_state.get("frames")
        if not frames:
            st.info(t("need_run"))
        else:
            rows = [dict(keyword=k, **opportunity(f, small_ch)) for k, f in frames.items()]
            gap = pd.DataFrame(rows).sort_values("score", ascending=False, na_position="last")
            best = gap.iloc[0]
            if pd.notna(best["score"]):
                st.markdown(f'<div class="ins"><div class="bar" style="background:{AMBER}"></div><div>'
                            + t("best_opp", k=html.escape(str(best["keyword"])), s=int(best["score"])) + "</div></div>",
                            unsafe_allow_html=True)
            st.dataframe(gap, hide_index=True,
                         column_config={"keyword": t("c_keyword"), "videos": t("c_videos"),
                                        "small_share": t("c_small"), "breakout": t("c_break"),
                                        "median_views": st.column_config.NumberColumn(t("c_median"), format="%d"),
                                        "score": st.column_config.ProgressColumn(t("c_opp"), min_value=0, max_value=100)})
            st.markdown(t("howread"))

    with tab4:
        seed = st.text_input(t("seed"), "reverse dcf")
        if st.button(t("get_sug"), type="primary"):
            sug = []
            for suffix in ["", " kya hai", " kaise", " hindi", " vs", " example", " galti"]:
                sug += autocomplete(seed + suffix)
            sug = list(dict.fromkeys(sug))
            if sug:
                st.dataframe(pd.DataFrame({t("sug_col"): sug}), hide_index=True)
                st.caption(t("sug_cap"))
            else:
                st.warning(t("sug_none"))

    with tab5:
        cats = ["27", "25", "22", "24", "28"]
        cat = st.selectbox(t("category"), cats, format_func=lambda c: t(f"cat{c}"))
        if st.button(t("load_tr"), type="primary"):
            if demo or not key:
                st.info(t("tr_need"))
            else:
                try:
                    vids, subs = trending(key, region, cat)
                    tr = to_frame(vids, subs)
                    st.dataframe(tr[["title", "channel", "subs", "views", "views_per_day", "minutes", "age_days", "url"]],
                                 column_config={"url": st.column_config.LinkColumn(t("link"), display_text=t("open"))},
                                 hide_index=True)
                except Exception as e:
                    st.error(str(e))


if st.session_state["page"] == "home":
    landing()
else:
    app_page()

st.markdown(f'<div class="foot">{t("footer")}</div>', unsafe_allow_html=True)
