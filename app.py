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

from i18n import t, LANGS, idx

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
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ") if days else None
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
            tags=sn.get("tags", []), desc=sn.get("description", ""), caption=(cd.get("caption") == "true"),
            published_at=sn["publishedAt"], url=f"https://www.youtube.com/watch?v={v['id']}"))
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


INDIC = re.compile(r"[ऀ-෿]")
FORMAT_WORDS = set("explained explain guide beginner beginners complete basics basic introduction course full step steps "
                   "simple easy best live video tutorial using with from what that this your you why how the and for "
                   "real example examples trading trade".split())
PATTERNS = {
    "how": r"\bhow\b|kaise|कैसे|എങ്ങനെ",
    "explained": r"explain|samjh|समझ|what is|kya hai|क्या है|എന്താണ്",
    "beginner": r"beginner|basics?|intro|introduction|course|\bep\.?\s?\d|complete guide|शुरुआत|बेसिक|തുടക്കക്കാർ|അടിസ്ഥാന",
    "mistakes": r"mistake|galti|गलती|गलतियाँ|avoid|wrong|myth|मिथक|fail|തെറ്റ്|തെറ്റുകൾ",
    "vs": r"\bvs\.?\b|versus|difference|compared|बनाम|फ़र्क|फर्क|വ്യത്യാസം",
    "number": r"\b\d+\b",
    "question": r"\?",
    "secret": r"hidden|secret|truth|reality|\bwhy\b|छिपी|छिपा|क्यों|മറഞ്ഞിരിക്കുന്ന|എന്തുകൊണ്ട്",
    "example": r"example|case study|\blive\b|demo|\breal\b|उदाहरण|ഉദാഹരണം",
}
TEMPLATES = {
    "how": ("How to Use {T} on {A}: Step by Step", "How {T} Works: A Simple Step by Step Guide"),
    "explained": ("{T} Explained with {A} Examples", "{T} Explained in Simple Words"),
    "beginner": ("{T} for Beginners: {A} Examples Included", "{T} for Beginners: Complete Basics"),
    "mistakes": ("{T}: 5 Common Mistakes to Avoid", "{T}: 5 Common Mistakes to Avoid"),
    "vs": ("{T} vs {M}: What is the Difference?", "{T} vs {M}: What is the Difference?"),
    "number": ("5 Things to Know About {T} for {A}", "5 Things to Know About {T}"),
    "question": ("What is {T}? A Clear Answer with {A} Examples", "What is {T}? A Clear Answer"),
    "secret": ("The Hidden Role of {T} in {A}", "The Hidden Force Behind {T}"),
    "example": ("{T} Real Example with {A} Data", "{T} Real Example, Step by Step"),
}
TEMPLATES_L = {
    "hi": {
        "how": ("{A} पर {T} का उपयोग कैसे करें: स्टेप बाय स्टेप", "{T} कैसे काम करता है: आसान स्टेप बाय स्टेप गाइड"),
        "explained": ("{T} क्या है? {A} के उदाहरण के साथ समझें", "{T} क्या है? आसान भाषा में समझें"),
        "beginner": ("शुरुआती लोगों के लिए {T}: {A} के उदाहरण सहित", "शुरुआती लोगों के लिए {T}: पूरी बेसिक्स"),
        "mistakes": ("{T} में होने वाली 5 आम गलतियाँ", "{T} में होने वाली 5 आम गलतियाँ"),
        "vs": ("{T} बनाम {M}: फ़र्क क्या है?", "{T} बनाम {M}: फ़र्क क्या है?"),
        "number": ("{T} के बारे में 5 ज़रूरी बातें ({A})", "{T} के बारे में 5 ज़रूरी बातें"),
        "question": ("{T} क्या है? {A} के उदाहरण के साथ साफ़ जवाब", "{T} क्या है? साफ़ और सरल जवाब"),
        "secret": ("{A} में {T} की छिपी भूमिका", "{T} के पीछे की छिपी ताकत"),
        "example": ("{T} का असली उदाहरण: {A} डेटा के साथ", "{T} का असली उदाहरण, स्टेप बाय स्टेप"),
    },
    "ml": {
        "how": ("{A}-ൽ {T} എങ്ങനെ ഉപയോഗിക്കാം: സ്റ്റെപ്പ് ബൈ സ്റ്റെപ്പ്", "{T} എങ്ങനെ പ്രവർത്തിക്കുന്നു: ലളിതമായ ഗൈഡ്"),
        "explained": ("{T} എന്താണ്? {A} ഉദാഹരണങ്ങളോടെ മനസ്സിലാക്കാം", "{T} എന്താണ്? ലളിതമായി മനസ്സിലാക്കാം"),
        "beginner": ("തുടക്കക്കാർക്കായി {T}: {A} ഉദാഹരണങ്ങളോടെ", "തുടക്കക്കാർക്കായി {T}: അടിസ്ഥാനങ്ങൾ മുഴുവൻ"),
        "mistakes": ("{T}: ഒഴിവാക്കേണ്ട 5 സാധാരണ തെറ്റുകൾ", "{T}: ഒഴിവാക്കേണ്ട 5 സാധാരണ തെറ്റുകൾ"),
        "vs": ("{T} vs {M}: എന്താണ് വ്യത്യാസം?", "{T} vs {M}: എന്താണ് വ്യത്യാസം?"),
        "number": ("{T} അറിയേണ്ട 5 കാര്യങ്ങൾ ({A})", "{T} അറിയേണ്ട 5 കാര്യങ്ങൾ"),
        "question": ("{T} എന്താണ്? {A} ഉദാഹരണത്തോടെ വ്യക്തമായ ഉത്തരം", "{T} എന്താണ്? വ്യക്തവും ലളിതവുമായ ഉത്തരം"),
        "secret": ("{A}-ൽ {T}-ന്റെ മറഞ്ഞിരിക്കുന്ന പങ്ക്", "{T}-ന് പിന്നിലെ മറഞ്ഞിരിക്കുന്ന ശക്തി"),
        "example": ("{T} യഥാർത്ഥ ഉദാഹരണം: {A} ഡാറ്റയോടെ", "{T} യഥാർത്ഥ ഉദാഹരണം, സ്റ്റെപ്പ് ബൈ സ്റ്റെപ്പ്"),
    },
}
NATIVE_SUFFIX = {"hi": ["क्या है", "कैसे", "हिंदी में", "समझें", "शुरुआती के लिए"],
                 "ml": ["എന്താണ്", "മലയാളത്തിൽ", "ലളിതമായി", "തുടക്കക്കാർക്ക്"]}
UNTAPPED_ORDER = ["mistakes", "vs", "example", "beginner", "how", "explained", "secret", "question", "number"]


def pattern_stats(df: pd.DataFrame) -> pd.DataFrame:
    med = df["views_per_day"].median()
    rows = []
    for k, rx in PATTERNS.items():
        m = df["title"].str.contains(rx, case=False, regex=True)
        n = int(m.sum())
        if n == 0:
            rows.append(dict(pattern=k, videos=0, median_vpd=None, lift=None))
        else:
            mv = df.loc[m, "views_per_day"].median()
            rows.append(dict(pattern=k, videos=n, median_vpd=int(mv), lift=round(mv / med, 2) if med else None))
    return pd.DataFrame(rows)


def subtopics(df: pd.DataFrame, exclude: set, top=15) -> pd.DataFrame:
    med = df["views_per_day"].median()
    bag = {}
    for _, r in df.iterrows():
        w = [x for x in WORD.findall(r["title"].lower())
             if len(x) > 3 and x not in STOP and x not in FORMAT_WORDS and x not in exclude and safe_word(x)]
        for g in set(w) | {" ".join(p) for p in zip(w, w[1:])}:
            bag.setdefault(g, []).append(r["views_per_day"])
    rows = []
    cap = max(4, len(df) // 10)
    for g, v in bag.items():
        if 2 <= len(v) <= cap:
            mv = float(pd.Series(v).median())
            if med and mv >= med:
                rows.append(dict(subtopic=g, videos=len(v), median_vpd=int(mv), demand=round(mv / med, 1),
                                 _score=mv / med / len(v)))
    out = pd.DataFrame(rows, columns=["subtopic", "videos", "median_vpd", "demand", "_score"])
    return out.sort_values("_score", ascending=False).drop(columns="_score").head(top)


def related_term(df: pd.DataFrame, kw: str):
    kws = set(WORD.findall(kw.lower()))
    for w in title_words(df, 40)["word"]:
        if w not in kws and w not in FORMAT_WORDS and len(w) > 3 and safe_word(w):
            return w.title()
    return None


def ui_code() -> str:
    return ("en", "ml", "hi")[idx()]


def suggest_titles(allv: pd.DataFrame, kw: str, angle: str, lang="en", topic="", n_proven=3, n_untapped=2) -> list:
    stats = pattern_stats(allv)
    a_terms = [x.strip() for x in (angle or "").split(",") if x.strip()]
    A = a_terms[0].upper() if a_terms and len(a_terms[0]) <= 12 else (a_terms[0].title() if a_terms else "")
    T = (topic or "").strip() or kw.strip().title()
    tpls = TEMPLATES if lang == "en" else TEMPLATES_L[lang]
    M = related_term(allv, kw)
    proven = stats[(stats["videos"] >= 2) & stats["lift"].notna()].sort_values("lift", ascending=False)
    if proven.empty:
        proven = stats[(stats["videos"] >= 1) & stats["lift"].notna()].sort_values("lift", ascending=False)
    picks = [(r["pattern"], "proven", int(r["videos"]), float(r["lift"])) for _, r in proven.iterrows()]
    out, used = [], set()

    def make(pat, kind, n, lift):
        if pat == "vs" and not M:
            return
        tpl = tpls[pat][0 if A else 1]
        title = tpl.format(T=T, A=A, M=M or "")
        if title not in used:
            used.add(title)
            out.append(dict(title=title, pattern=pat, kind=kind, n=n, lift=lift))

    for pat, kind, n, lift in picks:
        if len([o for o in out if o["kind"] == "proven"]) >= n_proven:
            break
        make(pat, kind, n, lift)
    missing = [p for p in UNTAPPED_ORDER if int(stats.loc[stats["pattern"] == p, "videos"].iloc[0]) == 0]
    for p in missing:
        if len([o for o in out if o["kind"] == "untapped"]) >= n_untapped:
            break
        make(p, "untapped", 0, 0.0)
    return out


def smart_tags(allv: pd.DataFrame, keywords: list, top_n=40) -> pd.DataFrame:
    kwset = {w for k in keywords for w in WORD.findall(k.lower()) if len(w) > 2 and w not in STOP}
    best = allv.sort_values("views_per_day", ascending=False).head(max(10, len(allv) // 4))
    rel = kwset | set(title_words(best, 30)["word"])
    med = allv["views_per_day"].median()
    norm = [[x.lower().strip() for x in ts] for ts in allv["tags"]]
    pool = Counter(t_ for ts in norm for t_ in set(ts))
    rows = []
    for tag, n in pool.items():
        if n < 2 or not (set(WORD.findall(tag)) & rel) or not safe_word(tag):
            continue
        mask = [tag in ts for ts in norm]
        mv = allv.loc[mask, "views_per_day"].median()
        rows.append(dict(tag=tag, videos=n, share_pct=round(n / len(allv) * 100), lift=round(mv / med, 2) if med else None))
    out = pd.DataFrame(rows, columns=["tag", "videos", "share_pct", "lift"])
    if out.empty:
        return out
    out["_s"] = out["videos"] ** 0.5 * out["lift"].fillna(1)
    return out.sort_values("_s", ascending=False).drop(columns="_s").head(top_n)


def native_names(keys) -> dict:
    return {k: st.session_state.get(f"tn_{k}", "") for k in keys}


def best_keyword(frames: dict, small: int) -> str:
    return max(frames, key=lambda k: (opportunity(frames[k], small)["score"] or 0))


def build_tags(allv: pd.DataFrame, keywords: list, angle: str, lang="en", native=None, focus=None):
    tg = smart_tags(allv, keywords)
    seed = [k.lower().strip() for k in keywords] + [x.strip().lower() for x in (angle or "").split(",") if x.strip()]
    seed = [x for x in seed if safe_word(x)]
    nat = []
    if lang != "en" and focus:
        base = ((native or {}).get(focus) or "").strip() or focus.strip()
        nat = [base] + [f"{base} {s_}" for s_ in NATIVE_SUFFIX[lang]]
    merged = pd.DataFrame({"tag": list(dict.fromkeys(nat + seed + list(tg["tag"])))})
    return tg, tag_string(merged)


def video_plan(allv: pd.DataFrame, frames: dict, small: int) -> dict:
    scores = [(k, opportunity(f, small)["score"]) for k, f in frames.items()]
    scored = [x for x in scores if x[1] is not None]
    kw, sc = max(scored, key=lambda x: x[1]) if scored else (list(frames)[0], None)
    pool = frames[kw]
    top = pool.sort_values("views_per_day", ascending=False).head(max(10, len(pool) // 4))
    angle = st.session_state.get("angle", "")
    lc = ui_code()
    en_t = [x["title"] for x in suggest_titles(allv, kw, angle)]
    if lc != "en":
        nat_t = [x["title"] for x in suggest_titles(allv, kw, angle, lc, st.session_state.get(f"tn_{kw}", ""))]
        drafts = nat_t[:3] + en_t[:2]
    else:
        drafts = en_t[:4]
    _, tagstr = build_tags(allv, list(frames), angle, lc, native_names(frames), kw)
    br = pool.dropna(subset=["outlier"])
    br = br[br["subs"] <= small].sort_values("outlier", ascending=False)
    model = br.iloc[0] if len(br) else top.iloc[0]
    return dict(keyword=kw, score=sc, drafts=drafts, tags=tagstr, length=float(top["minutes"].median()),
                tlen=float(top["title_len"].median()), model=model)


def excel_report(allv, frames, small, angle) -> bytes:
    from io import BytesIO
    kw = max(((k, opportunity(f, small)["score"] or 0) for k, f in frames.items()), key=lambda x: x[1])[0]
    tg, tagstr = build_tags(allv, list(frames), angle, ui_code(), native_names(frames), kw)
    gaps = pd.DataFrame([dict(keyword=k, **opportunity(f, small)) for k, f in frames.items()])
    lc = ui_code()
    titles = pd.DataFrame([dict(language="en", **x) for x in suggest_titles(allv, kw, angle)]
                          + ([dict(language=lc, **x) for x in suggest_titles(allv, kw, angle, lc, st.session_state.get(f"tn_{kw}", ""))]
                             if lc != "en" else []))
    sub = subtopics(allv, {w for k in frames for w in WORD.findall(k.lower())})
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        allv.drop(columns=["tags", "desc"]).to_excel(xw, sheet_name="Videos", index=False)
        pattern_stats(allv).to_excel(xw, sheet_name="Title formats", index=False)
        titles.to_excel(xw, sheet_name="Suggested titles", index=False)
        sub.to_excel(xw, sheet_name="Under-served subtopics", index=False)
        tg.to_excel(xw, sheet_name="Tags", index=False)
        pd.DataFrame({"tag_string": [tagstr]}).to_excel(xw, sheet_name="Tag string", index=False)
        gaps.to_excel(xw, sheet_name="Keyword gaps", index=False)
        ideas = [dict(video_id=v, question=q) for v, qs in st.session_state.get("ideas", {}).items() for q in qs]
        if ideas:
            pd.DataFrame(ideas).to_excel(xw, sheet_name="Viewer questions", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                w = max(len(str(c.value)) if c.value is not None else 0 for c in col[:60])
                ws.column_dimensions[col[0].column_letter].width = min(max(10, w + 2), 70)
    return buf.getvalue()



# ============================================================================= video and channel tools
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
Q_WORDS = re.compile(r"\?|\b(how|what|why|can you|could you|please|which|when|kaise|kya|kyun|kyu|kab|kaun)\b|"
                     r"कैसे|क्या|क्यों|कब|कृपया|എങ്ങനെ|എന്താണ്|എന്തുകൊണ്ട്|ഒന്ന് പറയാമോ", re.I)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def video_comments(_key, vid, n=100):
    try:
        j = call("commentThreads", _key, part="snippet", videoId=vid, maxResults=min(n, 100),
                 order="relevance", textFormat="plainText")
    except RuntimeError:
        return []
    out = []
    for it in j.get("items", []):
        sn = it["snippet"]["topLevelComment"]["snippet"]
        out.append(dict(text=sn.get("textDisplay", ""), likes=int(sn.get("likeCount", 0))))
    return out


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def channel_overview(_key, cid):
    it = call("channels", _key, part="snippet,statistics,contentDetails", id=cid)["items"][0]
    up = it["contentDetails"]["relatedPlaylists"]["uploads"]
    pl = call("playlistItems", _key, part="contentDetails", playlistId=up, maxResults=30)
    ids = [x["contentDetails"]["videoId"] for x in pl.get("items", [])]
    vids, subs = details(_key, tuple(ids))
    return it, to_frame(vids, subs)


def demo_comments(vid):
    rnd = random.Random(sum(map(ord, str(vid))))
    pool = ["Please explain this with a NIFTY example?", "Great video, very clear", "How do you read this on the option chain?",
            "Kya yeh intraday me kaam karta hai?", "Can you make a full course on this?", "Thanks sir, subscribed",
            "Why does the level change every day?", "Which tool do you use for this chart?", "Please do one in Hindi also",
            "Kaise calculate karte hain?", "Nice explanation", "What is the difference between this and open interest?"]
    return [dict(text=rnd.choice(pool), likes=rnd.randint(0, 120)) for _ in range(40)]


def demo_channel(cid, subs, n=20):
    rnd = random.Random(sum(map(ord, str(cid))))
    now = dt.datetime.now(dt.timezone.utc)
    vids = []
    for i in range(n):
        age = i * rnd.uniform(5, 14) + 2
        views = int((subs or 20000) * rnd.lognormvariate(-0.8, 0.9))
        vids.append({"id": f"democh{cid}_{i}", "snippet": {
            "title": f"Episode {n - i}: " + rnd.choice(["Basics", "Explained", "Real example", "Common mistakes", "Q and A"]),
            "channelId": cid, "channelTitle": f"Channel {str(cid)[1:]}",
            "publishedAt": (now - dt.timedelta(days=age)).isoformat(), "tags": []},
            "statistics": {"viewCount": views, "likeCount": int(views * .03), "commentCount": int(views * .002)},
            "contentDetails": {"duration": f"PT{rnd.randint(5, 20)}M0S"}})
    return to_frame(vids, {cid: subs or 20000})


def get_channel(is_demo, key, cid, subs):
    if is_demo or not key:
        f = demo_channel(cid, subs)
        return dict(title=f["channel"].iloc[0], subs=subs, videos=len(f) + 40, created="", country="IN"), f
    it, f = channel_overview(key, cid)
    s_ = it.get("statistics", {})
    return dict(title=it["snippet"]["title"], subs=None if s_.get("hiddenSubscriberCount") else int(s_.get("subscriberCount", 0)),
                videos=int(s_.get("videoCount", 0)), created=it["snippet"].get("publishedAt", "")[:10],
                country=it["snippet"].get("country", "")), f


def channel_stats(f: pd.DataFrame) -> dict:
    n = len(f)
    span = max(f["age_days"].max() - f["age_days"].min(), 1)
    med = float(f["views"].median())
    best = f.sort_values("views", ascending=False).iloc[0]
    consistent = bool(med) and float(f["views"].quantile(.25)) / med >= 0.5
    return dict(n=n, per_month=round(n / span * 30, 1), median=int(med), best=best, consistent=consistent)


def comment_questions(comments: list, top=10) -> list:
    seen, out = set(), []
    for c in sorted(comments, key=lambda x: -x["likes"]):
        txt = " ".join(c["text"].split())
        key_ = txt.lower()[:60]
        if 12 <= len(txt) <= 300 and Q_WORDS.search(txt) and key_ not in seen:
            seen.add(key_)
            out.append(dict(text=txt, likes=c["likes"]))
    return out[:top]


def cohort(allv: pd.DataFrame) -> pd.DataFrame:
    return allv.sort_values("views_per_day", ascending=False).head(max(10, len(allv) // 4))


def risky_in(text: str) -> list:
    return sorted(set(WORD.findall(text.lower())) & RISKY)


def packaging_checklist(r, allv: pd.DataFrame) -> list:
    top = cohort(allv)
    lo, hi = int(top["title_len"].quantile(.25)), int(top["title_len"].quantile(.75))
    mlo, mhi = float(top["minutes"].quantile(.25)), float(top["minutes"].quantile(.75))
    kws = [w for w in WORD.findall(str(r.get("keyword", "")).lower()) if w not in STOP and len(w) > 2]
    desc = r.get("desc", "") or ""
    first = " ".join(desc.splitlines()[:2]).lower()
    have_desc = bool(desc.strip())
    risk = risky_in(r["title"] + " " + " ".join(r["tags"]))
    return [
        ("ck_title_len", lo <= r["title_len"] <= hi, dict(a=lo, b=hi)),
        ("ck_tags", len(r["tags"]) >= 5, {}),
        ("ck_kw_title", (any(w in r["title"].lower() for w in kws) if kws else None), {}),
        ("ck_kw_desc", (any(w in first for w in kws) if (kws and have_desc) else None), {}),
        ("ck_chapters", (bool(re.search(r"(?m)^\s*\(?\d{1,2}:\d{2}", desc)) if have_desc else None), {}),
        ("ck_hash", (1 <= len(re.findall(r"#\w+", desc)) <= 3 if have_desc else None), {}),
        ("ck_len", mlo * 0.8 <= r["minutes"] <= mhi * 1.2, dict(a=f"{mlo:.0f}", b=f"{mhi:.0f}")),
        ("ck_caps", bool(r.get("caption", False)), {}),
        ("ck_risk", not risk, dict(w=(": " + ", ".join(risk)) if risk else "")),
    ]


def title_report(title: str, allv: pd.DataFrame, kw: str):
    top = cohort(allv)
    lo, hi = int(top["title_len"].quantile(.25)), int(top["title_len"].quantile(.75))
    kws = [w for w in WORD.findall(kw.lower()) if w not in STOP and len(w) > 2]
    ps = pattern_stats(allv)
    good = set(ps[(ps["videos"] >= 2) & (ps["lift"] > 1)]["pattern"])
    matched = [p for p, rx in PATTERNS.items() if re.search(rx, title, re.I)]
    risk = risky_in(title)
    return [
        ("tc_len", lo <= len(title) <= hi, dict(L=len(title), lo=lo, hi=hi)),
        ("tc_kw", (any(w in title.lower() for w in kws) if kws else None), {}),
        ("tc_fmt", (bool(set(matched) & good) if good else None), {}),
        ("ck_risk", not risk, dict(w=(": " + ", ".join(risk)) if risk else "")),
    ], matched


def timing_table(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    ts = pd.to_datetime(d["published_at"], utc=True).dt.tz_convert(IST)
    d["day"] = ts.dt.strftime("%a")
    d["block"] = (ts.dt.hour // 4) * 4
    top = d[d["views_per_day"] >= d["views_per_day"].quantile(.6)]
    g = top.groupby(["day", "block"]).size().reset_index(name="videos")
    g["slot"] = g["block"].map(lambda b: f"{b:02d}-{b + 4:02d}")
    return g


def render_checks(items: list):
    rows = []
    for key_, ok, kw in items:
        ok = None if ok is None else bool(ok)
        icon, color = ("✅", TEAL) if ok is True else (("❌", RED) if ok is False else ("➖", MUTED))
        rows.append(f'<div class="ins"><div class="bar" style="background:{color}"></div><div>{icon}  {t(key_, **kw)}</div></div>')
    st.markdown("".join(rows), unsafe_allow_html=True)
    scored = [bool(ok) for _, ok, _ in items if ok is not None]
    return round(sum(scored) / len(scored) * 100) if scored else 0



# ============================================================================= buzz
STOCKS = {
    "Reliance Industries": ["reliance", "ril", "रिलायंस"], "HDFC Bank": ["hdfc bank", "hdfcbank", "एचडीएफसी"],
    "ICICI Bank": ["icici bank", "icicibank", "icici"], "Infosys": ["infosys", "infy", "इंफोसिस"],
    "TCS": ["tcs", "tata consultancy", "टीसीएस"], "ITC": ["itc"], "Larsen & Toubro": ["larsen", "l&t", "larsen & toubro"],
    "Bharti Airtel": ["airtel", "bharti airtel"], "State Bank of India": ["sbi", "state bank of india", "एसबीआई"],
    "Kotak Mahindra Bank": ["kotak bank", "kotak mahindra"], "Axis Bank": ["axis bank"],
    "Hindustan Unilever": ["hindustan unilever", "hul"], "Bajaj Finance": ["bajaj finance"], "Bajaj Finserv": ["bajaj finserv"],
    "Bajaj Auto": ["bajaj auto"], "Asian Paints": ["asian paints"], "Maruti Suzuki": ["maruti"], "Sun Pharma": ["sun pharma"],
    "Titan": ["titan"], "UltraTech Cement": ["ultratech"], "Nestle India": ["nestle"], "Tata Motors": ["tata motors"],
    "Tata Steel": ["tata steel"], "Tata Power": ["tata power"], "Tata Consumer": ["tata consumer"], "Tata Elxsi": ["tata elxsi"],
    "Tata Technologies": ["tata technologies"], "Power Grid": ["power grid", "powergrid"], "NTPC": ["ntpc"], "ONGC": ["ongc"],
    "Coal India": ["coal india"], "Wipro": ["wipro"], "HCL Tech": ["hcl tech", "hcltech", "hcl technologies"],
    "Tech Mahindra": ["tech mahindra"], "Mahindra & Mahindra": ["m&m", "mahindra & mahindra"],
    "Hero MotoCorp": ["hero motocorp"], "Eicher Motors": ["eicher", "royal enfield"], "TVS Motor": ["tvs motor"],
    "Adani Enterprises": ["adani enterprises"], "Adani Ports": ["adani ports"], "Adani Power": ["adani power"],
    "Adani Green": ["adani green"], "Adani Group": ["adani", "अदानी"], "Grasim": ["grasim"], "JSW Steel": ["jsw steel"],
    "Hindalco": ["hindalco"], "Britannia": ["britannia"], "Cipla": ["cipla"], "Dr Reddys": ["dr reddy", "dr. reddy"],
    "Divis Labs": ["divi's", "divis lab"], "Apollo Hospitals": ["apollo hospitals"], "Shriram Finance": ["shriram finance"],
    "IndusInd Bank": ["indusind"], "SBI Life": ["sbi life"], "HDFC Life": ["hdfc life"], "BPCL": ["bpcl"], "Trent": ["trent"],
    "Zomato (Eternal)": ["zomato", "eternal ltd"], "Swiggy": ["swiggy"], "Jio Financial": ["jio financial"],
    "IndiGo (InterGlobe)": ["indigo", "interglobe"], "Bharat Electronics": ["bharat electronics"],
    "HAL": ["hindustan aeronautics", "hal share"], "Vedanta": ["vedanta"], "Paytm": ["paytm", "one97"], "Nykaa": ["nykaa"],
    "LIC": ["lic share", "lic ipo", "life insurance corporation"], "Yes Bank": ["yes bank"], "IRCTC": ["irctc"],
    "IRFC": ["irfc"], "RVNL": ["rvnl"], "IREDA": ["ireda"], "Suzlon": ["suzlon"], "Vodafone Idea": ["vodafone idea"],
    "Bank of Baroda": ["bank of baroda"], "Punjab National Bank": ["punjab national bank", "pnb"], "Canara Bank": ["canara bank"],
    "IDFC First Bank": ["idfc first"], "Federal Bank": ["federal bank"], "Pidilite": ["pidilite"], "Havells": ["havells"],
    "Siemens": ["siemens"], "DLF": ["dlf"], "Godrej Properties": ["godrej properties"], "Lodha": ["lodha", "macrotech"],
    "Zydus": ["zydus"], "Lupin": ["lupin"], "Aurobindo Pharma": ["aurobindo"], "Biocon": ["biocon"],
    "Persistent Systems": ["persistent systems"], "Coforge": ["coforge"], "LTIMindtree": ["ltimindtree"],
    "Dixon Technologies": ["dixon"], "Polycab": ["polycab"], "Voltas": ["voltas"], "DMart (Avenue Supermarts)": ["dmart", "avenue supermarts"],
    "Berger Paints": ["berger paints"], "Dabur": ["dabur"], "Marico": ["marico"], "Varun Beverages": ["varun beverages"],
    "Ambuja Cement": ["ambuja"], "Shree Cement": ["shree cement"], "SRF": ["srf"], "UPL": ["upl"], "MRF": ["mrf"],
    "Ashok Leyland": ["ashok leyland"], "Muthoot Finance": ["muthoot"], "Cholamandalam": ["cholamandalam", "chola finance"],
    "Max Healthcare": ["max healthcare"], "Jindal Steel": ["jindal steel"], "SAIL": ["sail share", "steel authority"],
    "NMDC": ["nmdc"], "Hindustan Zinc": ["hindustan zinc"], "GAIL": ["gail"], "Indian Oil": ["indian oil", "ioc"],
    "Mazagon Dock": ["mazagon"], "Cochin Shipyard": ["cochin shipyard"], "Bharat Dynamics": ["bharat dynamics"],
    "Ola Electric": ["ola electric"], "Waaree Energies": ["waaree"], "Hyundai Motor India": ["hyundai motor india"],
    "Tesla": ["tesla"], "Nvidia": ["nvidia"], "Apple": ["apple stock", "aapl"], "Microsoft": ["microsoft"], "Amazon": ["amazon"],
}
TERMS = {
    "Nifty 50": ["nifty 50", "nifty50", "nifty"], "Bank Nifty": ["bank nifty", "banknifty"], "Sensex": ["sensex"],
    "FinNifty": ["finnifty", "fin nifty"], "Midcap and Smallcap": ["midcap", "smallcap", "small cap", "mid cap"],
    "Gamma exposure (GEX)": ["gamma exposure", "gex", "gamma flip", "call wall", "put wall"],
    "Options and option chain": ["options", "option chain", "option selling", "option buying"],
    "Valuation (DCF, PE)": ["valuation", "dcf", "intrinsic value", "pe ratio", "fair value"],
    "IPO": ["ipo", "gmp"], "Results and earnings": ["results", "earnings", "q1", "q2", "q3", "q4"],
    "Dividend": ["dividend"], "Mutual funds and SIP": ["mutual fund", "sip"], "Gold": ["gold"], "Silver": ["silver"],
    "Crypto": ["bitcoin", "crypto", "ethereum"], "Budget": ["budget"], "RBI and interest rates": ["rbi", "repo rate", "interest rate"],
    "US Fed": ["fed ", "federal reserve", "fomc"], "Inflation": ["inflation"], "Market crash or rally": ["crash", "rally", "correction"],
    "Personal finance": ["personal finance", "tax saving", "emergency fund", "insurance"],
}


def compile_defs(defs: dict) -> dict:
    return {n: re.compile(r"(?<![\w])(?:" + "|".join(re.escape(a) for a in sorted(al, key=len, reverse=True)) + r")(?![\w])", re.I)
            for n, al in defs.items()}


STOCK_RX, TERM_RX = compile_defs(STOCKS), compile_defs(TERMS)


def video_text(df: pd.DataFrame) -> pd.Series:
    return (df["title"] + " " + df["tags"].apply(lambda ts: " ".join(ts))).str.lower()


def entity_stats(df: pd.DataFrame, rx_map: dict) -> pd.DataFrame:
    text = video_text(df)
    rows = []
    for name, rx in rx_map.items():
        m = text.apply(lambda s_: bool(rx.search(s_)))
        if m.any():
            sub = df[m]
            rows.append(dict(name=name, videos=len(sub), channels=int(sub["channel_id"].nunique()), total_views=int(sub["views"].sum()),
                             median_vpd=int(sub["views_per_day"].median()), newest_h=int(sub["age_days"].min() * 24)))
    out = pd.DataFrame(rows, columns=["name", "videos", "channels", "total_views", "median_vpd", "newest_h"])
    return out.sort_values(["videos", "total_views"], ascending=False).reset_index(drop=True)


def window_filter(df: pd.DataFrame, mode: str, day=None) -> pd.DataFrame:
    if mode == "all":
        return df
    if mode == "day":
        d_ = pd.to_datetime(df["published_at"], utc=True).dt.tz_convert(IST).dt.date
        return df[d_ == day]
    lim = {"d1": 1, "d2": 2, "d7": 7, "d30": 30}[mode]
    return df[df["age_days"] <= lim]


TOPIC_Q = {"stock_market": "stock market today", "finance": "finance", "economics": "economics", "personal_finance": "personal finance",
           "mutual_funds": "mutual funds sip", "investing": "investing for beginners", "options": "options trading",
           "ipo": "ipo", "crypto": "crypto bitcoin", "gold": "gold price", "banking": "bank loan interest rate",
           "tax": "income tax", "insurance": "insurance", "realestate": "real estate"}
CHART_CATS = ["27", "25", "22", "24", "28"]


# ============================================================================= styling
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Malayalam:wght@400;700&family=Noto+Sans+Devanagari:wght@400;700&display=swap');
:root{--teal:#1DD3A0;--amber:#FFB020;--red:#FF4D6A;--blue:#4C8DFF;--bg:#0A1020;--card:#111A2E;--line:#1F2B47;--muted:#8A96B2}
html,body,.stApp,.stMarkdown,button,input,textarea,[data-baseweb]{font-family:"Source Sans Pro","Source Sans 3","Noto Sans Malayalam","Noto Sans Devanagari",sans-serif}
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stToolbarActions"], [data-testid="stAppDeployButton"], [data-testid="stMainMenu"]{visibility:hidden}
[data-testid="stExpandSidebarButton"],[data-testid="stSidebarCollapsedControl"],[data-testid="collapsedControl"]{visibility:visible!important;display:flex!important;background:#111A2E;border:1px solid #1F2B47;border-radius:8px}
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
.chip{display:inline-block;padding:3px 11px;margin:3px 4px 3px 0;border-radius:99px;background:#16233f;border:1px solid var(--line);font-size:12.5px}
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
    days = st.select_slider(t("days"), [1, 2, 7, 30, 90, 180, 365, 0], value=180,
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

    tabs = st.tabs([t("tab1"), t("tab9"), t("tab6"), t("tab7"), t("tab8"), t("tab2"), t("tab3"), t("tab4"), t("tab5")])
    tab1, tab9, tab6, tab7, tab8, tab2, tab3, tab4, tab5 = tabs

    with tab1:
        kws = st.text_area(t("kw_label"), "reverse dcf hindi\nintrinsic value kaise nikale\nstock valuation hindi", height=110)
        st.text_input(t("angle_label"), key="angle", help=t("angle_help"), placeholder="nifty, banknifty")
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
            st.download_button(t("dl"), show.drop(columns=["tags", "desc"]).to_csv(index=False).encode(),
                               "yt_scope_videos.csv", "text/csv")
            try:
                st.download_button(t("dl_xlsx"), excel_report(allv, frames, small_ch, st.session_state.get("angle", "")),
                                   "yt_scope_report.xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            except Exception:
                pass

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

    with tab9:
        allv = st.session_state.get("all")
        if allv is None:
            st.info(t("need_run"))
        else:
            modes = ["all", "d1", "d2", "d7", "d30", "day"]
            mode = st.radio(t("bz_window"), modes, horizontal=True, format_func=lambda m_: t("bz_" + m_), index=0)
            day_pick = st.date_input(t("bz_pick_day"), dt.datetime.now(IST).date()) if mode == "day" else None
            sub = window_filter(allv, mode, day_pick)
            if sub.empty:
                st.warning(t("bz_none"))
            else:
                st.markdown('<div class="kpis" style="grid-template-columns:repeat(3,1fr)">' + kpi(t("k_videos"), fmt(len(sub)))
                            + kpi(t("k_ch"), fmt(sub["channel_id"].nunique()), BLUE) + kpi(t("c_totviews"), fmt(sub["views"].sum()), AMBER) + "</div>",
                            unsafe_allow_html=True)
                custom = st.text_input(t("bz_custom"), placeholder="Suzlon, Waaree, Tata Power")
                crx = compile_defs({c_.strip().title(): [c_.strip()] for c_ in custom.split(",") if c_.strip()}) if custom.strip() else {}
                stk = entity_stats(sub, {**crx, **STOCK_RX})
                trm = entity_stats(sub, TERM_RX)
                cfg = {"name": t("bz_col_name"), "videos": t("c_videos"), "channels": t("c_channels"),
                       "total_views": st.column_config.NumberColumn(t("c_totviews"), format="%d"),
                       "median_vpd": t("c_vpd"), "newest_h": t("c_newest")}
                for title_key, tbl, color in [("bz_stocks", stk, TEAL), ("bz_terms", trm, BLUE)]:
                    st.markdown(f'<div class="sec">{t(title_key)}</div>', unsafe_allow_html=True)
                    if tbl.empty:
                        st.write(t("bz_nomatch"))
                        continue
                    st.altair_chart(alt.Chart(tbl.head(12)).mark_bar(color=color, cornerRadiusEnd=4).encode(
                        x=alt.X("videos:Q", title=t("c_videos")), y=alt.Y("name:N", sort="-x", title=None),
                        tooltip=["name", "videos", "channels", "total_views"]).properties(height=max(120, 30 * min(len(tbl), 12))), theme=None)
                    st.dataframe(tbl, hide_index=True, column_config=cfg, height=min(360, 40 + 36 * len(tbl)))

                st.markdown(f'<div class="sec">{t("bz_phr")}</div>', unsafe_allow_html=True)
                pa, pb = st.columns(2)
                pa.dataframe(title_words(sub, 20), hide_index=True)
                bgm = bigrams(sub, 15)
                if len(bgm):
                    pb.dataframe(bgm, hide_index=True)

                names = list(stk["name"]) + list(trm["name"])
                if names:
                    st.markdown(f'<div class="sec">{t("bz_pick")}</div>', unsafe_allow_html=True)
                    pick = st.selectbox(t("bz_pick"), names, label_visibility="collapsed")
                    rx = {**crx, **STOCK_RX, **TERM_RX}[pick]
                    hit = sub[video_text(sub).apply(lambda s_: bool(rx.search(s_)))].sort_values("views_per_day", ascending=False)
                    st.markdown(f'<div class="sec">{t("bz_top")}</div>', unsafe_allow_html=True)
                    video_cards(hit, 6)
                    st.link_button(t("bz_trends"), "https://trends.google.com/trends/explore?date=now%201-d&geo=IN&gprop=youtube&q="
                                   + requests.utils.quote(pick))
                    st.markdown(f'<div class="sec">{t("bz_ac")}</div>', unsafe_allow_html=True)
                    sug = list(dict.fromkeys(sum([autocomplete(pick + sfx) for sfx in ["", " share", " today", " news", " analysis"]], [])))
                    if sug:
                        st.markdown("".join(f'<span class="chip">{html.escape(x_)}</span>' for x_ in sug[:20]), unsafe_allow_html=True)
                    else:
                        st.write(t("sug_none"))
                st.caption(t("bz_note"))

    with tab6:
        frames = st.session_state.get("frames")
        allv = st.session_state.get("all")
        if not frames or allv is None:
            st.info(t("need_run"))
        else:
            angle = st.session_state.get("angle", "")
            sc = {k: (opportunity(f, small_ch)["score"] or 0) for k, f in frames.items()}
            kws_list = sorted(frames, key=lambda k: -sc[k])
            kw = st.selectbox(t("tl_pick"), kws_list)

            lc = ui_code()
            tn = ""
            if lc != "en":
                tn = st.text_input(t("tn_label"), key=f"tn_{kw}", help=t("tn_help"))
                heads = [(lc, t("sec_titles_native")), ("en", t("sec_titles_en"))]
            else:
                heads = [("en", t("sec_titles"))]
            for code, head in heads:
                st.markdown(f'<div class="sec">{head}</div>', unsafe_allow_html=True)
                for sgt in suggest_titles(allv, kw, angle, code, tn if code != "en" else ""):
                    why = (t("tl_proven", n=sgt["n"], x=f"{sgt['lift']:.1f}") if sgt["kind"] == "proven" else t("tl_untapped"))
                    color = TEAL if sgt["kind"] == "proven" else AMBER
                    st.markdown(f'<div class="ins"><div class="bar" style="background:{color}"></div><div><b>{html.escape(sgt["title"])}</b>'
                                f'<span class="pm">{t("pat_" + sgt["pattern"])}  ·  {why}</span></div></div>', unsafe_allow_html=True)
            st.caption(t("tl_note"))

            st.markdown(f'<div class="sec">{t("sec_pat")}</div>', unsafe_allow_html=True)
            ps = pattern_stats(allv)
            ps["format"] = ps["pattern"].map(lambda p: t("pat_" + p))
            have = ps[ps["videos"] > 0].sort_values("lift", ascending=False)
            if len(have):
                st.altair_chart(alt.Chart(have).mark_bar(color=TEAL, cornerRadiusEnd=4).encode(
                    x=alt.X("lift:Q", title=t("c_lift")), y=alt.Y("format:N", sort="-x", title=None),
                    tooltip=["format", "videos", "median_vpd", "lift"]).properties(height=max(120, 34 * len(have))), theme=None)
            st.dataframe(ps[["format", "videos", "median_vpd", "lift"]], hide_index=True,
                         column_config={"format": t("c_pattern"), "videos": t("c_videos"),
                                        "median_vpd": t("c_vpd"), "lift": t("c_lift")})
            st.caption(t("pat_cap"))

            st.markdown(f'<div class="sec">{t("sec_gap")}</div>', unsafe_allow_html=True)
            missing = [t("pat_" + p) for p in ps.loc[ps["videos"] == 0, "pattern"]]
            line = t("g_fmt", f=html.escape(", ".join(missing))) if missing else t("g_fmt_none")
            st.markdown(f'<div class="ins"><div class="bar" style="background:{AMBER}"></div><div>{line}</div></div>', unsafe_allow_html=True)

            terms = [x.strip().lower() for x in angle.split(",") if x.strip()]
            if terms:
                hit = allv.apply(lambda r: any(x in r["title"].lower() or any(x in tg.lower() for tg in r["tags"]) for x in terms), axis=1)
                n_hit, tot = int(hit.sum()), len(allv)
                pct = round(n_hit / tot * 100)
                verdict = t("g_angle_gap") if pct < 15 else t("g_angle_busy")
                line = t("g_angle", n=n_hit, tot=tot, p=pct, a=html.escape(", ".join(terms)), v=verdict)
            else:
                line = t("g_angle_empty")
            st.markdown(f'<div class="ins"><div class="bar" style="background:{BLUE}"></div><div>{line}</div></div>', unsafe_allow_html=True)

            native = round(allv["title"].apply(lambda x: bool(INDIC.search(x))).mean() * 100)
            verdict = t("g_lang_gap") if native < 20 else t("g_lang_busy")
            st.markdown(f'<div class="ins"><div class="bar" style="background:{RED}"></div><div>{t("g_lang", p=native, v=verdict)}</div></div>',
                        unsafe_allow_html=True)

            st.markdown(f'<div class="sec">{t("g_under")}</div>', unsafe_allow_html=True)
            sub = subtopics(allv, {w for k in frames for w in WORD.findall(k.lower())})
            if sub.empty:
                st.write(t("g_under_none"))
            else:
                st.dataframe(sub, hide_index=True,
                             column_config={"subtopic": t("c_sub"), "videos": t("c_videos"),
                                            "median_vpd": t("c_vpd"), "demand": t("c_demand")})

    with tab7:
        allv = st.session_state.get("all")
        if allv is None:
            st.info(t("need_run"))
        else:
            cand = allv.sort_values("views_per_day", ascending=False).head(60).reset_index(drop=True)
            i_ = st.selectbox(t("vd_pick"), list(range(len(cand))),
                              format_func=lambda i: f"{cand.loc[i, 'title'][:72]}  |  {cand.loc[i, 'channel']}")
            r = cand.loc[i_]
            is_demo_vid = str(r["vid"]).startswith("demo")
            c1, c2 = st.columns([1, 2])
            with c1:
                if is_demo_vid:
                    st.markdown('<div class="vcard"><div class="th"></div></div>', unsafe_allow_html=True)
                else:
                    st.image(f"https://i.ytimg.com/vi/{r['vid']}/hqdefault.jpg")
            with c2:
                st.markdown(f'<a href="{r["url"]}" target="_blank" style="font-size:20px;font-weight:800;color:#E8EEF9;'
                            f'text-decoration:none">{html.escape(r["title"])}</a>'
                            f'<div style="color:#8A96B2;margin-top:4px">{html.escape(str(r["channel"]))}  ·  {r["published"]}</div>',
                            unsafe_allow_html=True)
                x = f"{r['outlier']:.1f}x" if pd.notna(r["outlier"]) else "-"
                st.markdown('<div class="kpis" style="grid-template-columns:repeat(4,1fr)">'
                            + kpi(t("k_views"), fmt(r["views"]), BLUE) + kpi(t("c_vpd"), fmt(r["views_per_day"]))
                            + kpi(t("c_outlier"), x, AMBER) + kpi(t("c_eng"), f"{r['engagement_pct']}%", RED) + "</div>",
                            unsafe_allow_html=True)

            st.markdown(f'<div class="sec">{t("vd_tags")}</div>', unsafe_allow_html=True)
            if r["tags"]:
                st.markdown("".join(f'<span class="chip">{html.escape(x_)}</span>' for x_ in r["tags"]), unsafe_allow_html=True)
            else:
                st.write(t("vd_notags"))

            st.markdown(f'<div class="sec">{t("vd_pack")}</div>', unsafe_allow_html=True)
            score = render_checks(packaging_checklist(r, allv))
            st.caption(t("vd_score", s=score))
            if (r["desc"] or "").strip():
                with st.expander(t("vd_desc")):
                    st.text(r["desc"][:1500])

            st.markdown(f'<div class="sec">{t("vd_questions")}</div>', unsafe_allow_html=True)
            ck = f"cm_{r['vid']}"
            if st.button(t("vd_load_comments"), key="b_" + ck):
                try:
                    st.session_state[ck] = demo_comments(r["vid"]) if (demo or not key or is_demo_vid) else video_comments(key, r["vid"])
                except Exception as e:
                    st.error(str(e))
            if ck in st.session_state:
                cm = st.session_state[ck]
                qs = comment_questions(cm)
                st.session_state.setdefault("ideas", {})[r["vid"]] = [q["text"] for q in qs]
                st.caption(t("vd_ncomm", n=len(cm)))
                if qs:
                    for q in qs:
                        st.markdown(f'<div class="ins"><div class="bar" style="background:{AMBER}"></div>'
                                    f'<div>{html.escape(q["text"])}<span class="pm">👍 {q["likes"]}</span></div></div>', unsafe_allow_html=True)
                else:
                    st.write(t("vd_nocomments"))

            st.markdown(f'<div class="sec">{t("vd_channel")}</div>', unsafe_allow_html=True)
            ck2 = f"ch_{r['channel_id']}"
            if st.button(t("vd_load_channel"), key="b_" + ck2):
                try:
                    st.session_state[ck2] = get_channel(demo or is_demo_vid, key, r["channel_id"], r["subs"])
                except Exception as e:
                    st.error(str(e))
            if ck2 in st.session_state:
                info, cf = st.session_state[ck2]
                cs = channel_stats(cf)
                st.markdown('<div class="kpis">' + kpi(t("ch_subs"), fmt(info["subs"]))
                            + kpi(t("ch_videos"), fmt(info["videos"]), BLUE) + kpi(t("ch_median"), fmt(cs["median"]), AMBER)
                            + kpi(t("ch_rate"), str(cs["per_month"]), RED) + "</div>", unsafe_allow_html=True)
                msg = t("ch_consistent") if cs["consistent"] else t("ch_spiky")
                st.markdown(f'<div class="ins"><div class="bar" style="background:{TEAL if cs["consistent"] else AMBER}"></div><div>{msg}</div></div>',
                            unsafe_allow_html=True)
                b_ = cs["best"]
                st.markdown(f'<div class="ins"><div class="bar" style="background:{BLUE}"></div><div>{t("ch_best")}: '
                            f'<a href="{b_["url"]}" target="_blank">{html.escape(b_["title"])}</a> ({fmt(b_["views"])})</div></div>',
                            unsafe_allow_html=True)
                st.markdown(f'<div class="sec">{t("ch_recent")}</div>', unsafe_allow_html=True)
                st.dataframe(cf[["title", "views", "views_per_day", "minutes", "age_days", "url"]].head(15),
                             column_config={"url": st.column_config.LinkColumn(t("link"), display_text=t("open"))}, hide_index=True)

    with tab8:
        allv = st.session_state.get("all")
        if allv is None:
            st.info(t("need_run"))
        else:
            st.markdown(f'<div class="sec">{t("pk_check")}</div>', unsafe_allow_html=True)
            kwl = list(st.session_state.get("frames", {}))
            ca, cb = st.columns([3, 2])
            draft = ca.text_input(t("pk_check_in"), "")
            tkw = cb.selectbox(t("tl_pick"), kwl, key="pk_kw")
            if draft.strip():
                items, matched = title_report(draft.strip(), allv, tkw)
                render_checks(items)
                st.caption(t("pk_matched", f=", ".join(t("pat_" + p) for p in matched) if matched else t("pk_none")))

            st.markdown(f'<div class="sec">{t("pk_timing")}</div>', unsafe_allow_html=True)
            tt_ = timing_table(allv)
            if len(tt_):
                day_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                st.altair_chart(alt.Chart(tt_).mark_rect(cornerRadius=4).encode(
                    x=alt.X("slot:N", title=None, sort=["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"]),
                    y=alt.Y("day:N", sort=day_order, title=None),
                    color=alt.Color("videos:Q", scale=alt.Scale(scheme="tealblues"), legend=None),
                    tooltip=["day", "slot", "videos"]).properties(height=260), theme=None)
                bt = tt_.sort_values("videos", ascending=False).iloc[0]
                st.markdown(f'<div class="ins"><div class="bar" style="background:{TEAL}"></div><div>{t("pk_timing_best", d=bt["day"], b=bt["slot"])}</div></div>',
                            unsafe_allow_html=True)
                st.caption(t("pk_timing_cap"))

            st.markdown(f'<div class="sec">{t("pk_gallery")}</div>', unsafe_allow_html=True)
            video_cards(allv.sort_values("views_per_day", ascending=False), 12)
            st.caption(t("pk_gallery_cap"))

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
                    _fr = st.session_state.get("frames", {})
                    st.text_area(t("tagstr") if ui_code() == "en" else t("tagstr_native"),
                                 build_tags(allv, list(_fr), st.session_state.get("angle", ""), ui_code(), native_names(_fr), best_keyword(_fr, small_ch))[1], height=100)
            with b:
                st.markdown(f'<div class="sec">{t("sec_words")}</div>', unsafe_allow_html=True)
                st.altair_chart(alt.Chart(title_words(top).head(20)).mark_bar(color=BLUE, cornerRadiusEnd=4).encode(
                    x=alt.X("videos:Q", title=t("ax_titles")), y=alt.Y("word:N", sort="-x", title=None)).properties(height=430), theme=None)
                bg = bigrams(top)
                if len(bg):
                    st.dataframe(bg, hide_index=True)
            st.markdown(f'<div class="sec">{t("sec_taglift")}</div>', unsafe_allow_html=True)
            stg = smart_tags(allv, list(st.session_state.get("frames", {})))
            if len(stg):
                st.dataframe(stg, hide_index=True, column_config={"tag": t("c_tag"), "videos": t("c_videos"),
                             "share_pct": "%", "lift": t("c_lift")})
            st.caption(t("tag_cap2"))
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
        chart_names = [t(f"cat{c}") for c in CHART_CATS]
        topic_names = ["🔎 " + t("tp_" + k) for k in TOPIC_Q]
        custom_label = "✏️ " + t("tp_custom")
        all_names = chart_names + topic_names + [custom_label]
        choice = st.selectbox(t("category"), all_names)
        ci = all_names.index(choice)
        is_chart = ci < len(CHART_CATS)
        if not is_chart:
            c1_, c2_ = st.columns(2)
            win = c1_.selectbox(t("days"), [1, 2, 7, 30], index=2)
            ord_ = c2_.selectbox(t("order"), ["viewCount", "date"])
            if choice == custom_label:
                q_ = st.text_input(t("tp_custom"), "stock market india")
            else:
                q_ = TOPIC_Q[list(TOPIC_Q)[ci - len(CHART_CATS)]]
            st.caption(t("tr_note"))
        if st.button(t("load_tr"), type="primary"):
            try:
                if is_chart:
                    if demo or not key:
                        st.info(t("tr_need"))
                        tr = None
                    else:
                        vids, subs = trending(key, region, CHART_CATS[ci])
                        tr = to_frame(vids, subs)
                else:
                    if demo or not key:
                        tr = demo_frame(q_, 50)
                    else:
                        ids = search_ids(key, q_, win, region, lang, ord_, "any", 50)
                        vids, subs = details(key, tuple(ids))
                        tr = to_frame(vids, subs)
                st.session_state["tr_frame"] = None if tr is None else (choice, tr)
            except Exception as e:
                st.error(str(e))
        if st.session_state.get("tr_frame"):
            lab_, tr = st.session_state["tr_frame"]
            st.dataframe(tr[["title", "channel", "subs", "views", "views_per_day", "outlier", "minutes", "age_days", "url"]],
                         column_config={"url": st.column_config.LinkColumn(t("link"), display_text=t("open"))}, hide_index=True)
            if st.button(t("tr_add")):
                st.session_state.setdefault("frames", {})[lab_.replace("🔎 ", "").replace("✏️ ", "")] = tr
                st.success(t("tr_added"))


if st.session_state["page"] == "home":
    landing()
else:
    app_page()

st.markdown(f'<div class="foot">{t("footer")}<br>{t("tos")}</div>', unsafe_allow_html=True)
