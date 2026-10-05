#!/usr/bin/env python3
"""Ai بالعربي - news collector. Runs hourly on GitHub Actions. No extra libraries needed.

Reads sources.json, pulls every feed, translates new items to Arabic for free,
and writes data/news.json (read by the app) and data/status.json (which sources worked).
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
NEWS = os.path.join(DATA, "news.json")
STATUS = os.path.join(DATA, "status.json")
CHANNELS = os.path.join(DATA, "youtube_channels.json")

PER_FEED = 8          # newest items taken from each source per run
MAX_AGE_DAYS = 7      # older items are dropped
VIDEO_AGE_DAYS = 14   # videos stay longer, channels post less often
SCAN = 40             # how deep to look in feeds that are filtered by topic
PER_SECTION = 150     # items kept per section
MAX_NEW = 350         # translation budget per run
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, */*", "Accept-Language": "en-US,en;q=0.8"}

KEYS = {
    "laptops": r"\b(laptops?|notebooks?|macbook|thinkpad|zenbook|vivobook|xps \d+|chromebook|surface (laptop|pro)|ultrabook|legion|elitebook|ideapad|zephyrus|copilot\+ pcs?|snapdragon x|core ultra|ryzen ai)\b|لابتوب|حاسوب محمول|حواسيب محمولة|ماك ?بوك",
    "phones": r"\b(iphone|galaxy (s|z|a|m)\s?\d*|pixel \d+|smartphones?|phones?|oneplus|xiaomi|redmi|poco|oppo|vivo|realme|honor|foldables?|one ui|ios \d+|android \d+)\b|هاتف|هواتف|آيفون|ايفون|جالاكسي|شاومي",
    "ai": r"\b(a\.?i\.?|artificial intelligence|openai|chatgpt|gpt-?\d\w*|anthropic|claude|gemini|llms?|copilot|deepmind|machine learning|neural|chatbots?|generative|agentic|mistral|llama|grok|sora)\b|ذكاء اصطناعي|الذكاء الاصطناعي|شات جي بي تي|تعلم الآلة|روبوت محادثة",
}
ORDER = ["laptops", "phones", "ai"]


def matches(section, text):
    return re.search(KEYS[section], text, re.I) is not None


def classify(text):
    for sec in ORDER:
        if matches(sec, text):
            return sec
    return "tech"


def get(url, params=None, extra=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=dict(UA, **(extra or {})))
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def clean(text, limit):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def clean_video_description(text):
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"https?://\S+", "", line).strip()
        if len(line) < 3 or re.match(r"^[#@\d:\-–•*]", line):
            continue
        lines.append(line)
    return clean("\n".join(lines), 900)


def local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return 0
    try:
        if re.match(r"\d{4}-\d\d-\d\d", text):
            d = datetime.fromisoformat(re.sub(r"\.\d+", "", text.replace("Z", "+00:00")))
        else:
            d = parsedate_to_datetime(text)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except Exception:  # noqa: BLE001
        return 0


def is_image(node):
    url = node.get("url") or ""
    kind = node.get("type") or ""
    return bool(url) and (node.get("medium") == "image" or kind.startswith("image") or re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I) is not None)


def parse(data):
    """RSS and Atom -> list of {title, link, summary, ts, img, vid}."""
    root = ET.fromstring(data.lstrip())
    out = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        e = {"title": "", "link": "", "summary": "", "ts": 0, "img": "", "vid": ""}
        body = updated = media_text = ""
        for ch in node:
            t, text = local(ch.tag), (ch.text or "").strip()
            if t == "title":
                e["title"] = e["title"] or text
            elif t == "link":
                href = ch.get("href")
                if href and ch.get("rel", "alternate") == "alternate":
                    e["link"] = e["link"] or href
                elif href and ch.get("rel") == "enclosure" and (ch.get("type") or "").startswith("image"):
                    e["img"] = e["img"] or href
                elif text:
                    e["link"] = e["link"] or text
            elif t in ("description", "summary"):
                e["summary"] = e["summary"] or text
            elif t in ("encoded", "content") and text:
                body = body or text
            elif t == "enclosure" and (ch.get("type") or "").startswith("image"):
                e["img"] = e["img"] or ch.get("url", "")
            elif t in ("pubdate", "published", "date"):
                e["ts"] = e["ts"] or parse_date(text)
            elif t == "updated":
                updated = text
        for ch in node.iter():
            t = local(ch.tag)
            if t == "thumbnail" and ch.get("url"):
                e["img"] = e["img"] or ch.get("url")
            elif t == "content" and is_image(ch):
                e["img"] = e["img"] or ch.get("url")
            elif t == "videoid":
                e["vid"] = (ch.text or "").strip()
            elif t == "description" and ch not in list(node):
                media_text = media_text or (ch.text or "").strip()
        e["summary"] = e["summary"] or body or media_text
        e["ts"] = e["ts"] or parse_date(updated)
        if not e["img"]:
            m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)', e["summary"] + body)
            e["img"] = html.unescape(m.group(1)) if m else ""
        out.append(e)
    return out


def fetch(url):
    return parse(get(url))


def fetch_channel(cid):
    """YouTube feeds fail at random, so try twice and then the uploads playlist feed."""
    base = "https://www.youtube.com/feeds/videos.xml?"
    last = None
    for url in (base + "channel_id=" + cid, base + "channel_id=" + cid, base + "playlist_id=UU" + cid[2:]):
        try:
            return fetch(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise last


def google(text):
    raw = get("https://translate.googleapis.com/translate_a/single", {"client": "gtx", "sl": "auto", "tl": "ar", "dt": "t", "q": text})
    return "".join(part[0] for part in json.loads(raw)[0] if part and part[0])


def mymemory(text):
    raw = get("https://api.mymemory.translated.net/get", {"q": text[:480], "langpair": "en|ar"})
    out = json.loads(raw)["responseData"]["translatedText"]
    if not out or "MYMEMORY WARNING" in out:
        raise RuntimeError("mymemory limit")
    return html.unescape(out)


def translate(text):
    """Returns Arabic text, or None when every free translator failed."""
    if not text:
        return ""
    for fn in (google, google, mymemory):
        try:
            out = fn(text).strip()
            if out:
                time.sleep(0.25)
                return out
        except Exception as e:  # noqa: BLE001
            print("  translate retry:", type(e).__name__, str(e)[:80])
            time.sleep(2)
    return None


def channel_id(handle, cache):
    if handle in cache:
        return cache[handle]
    page = get("https://www.youtube.com/" + handle, extra={"Cookie": "CONSENT=YES+1; SOCS=CAI"}).decode("utf-8", "replace")
    m = (re.search(r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'property="og:url" content="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page))
    if not m:
        raise RuntimeError("channel id not found")
    cache[handle] = m.group(1)
    return cache[handle]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def main():
    os.makedirs(DATA, exist_ok=True)
    cfg = load(os.path.join(ROOT, "sources.json"), None)
    if not cfg:
        sys.exit("sources.json is missing or broken")
    old = {it["id"]: it for it in load(NEWS, {}).get("items", [])}
    channels = load(CHANNELS, {})
    now = int(time.time())
    oldest = now - MAX_AGE_DAYS * 86400
    status, fresh = [], []

    jobs = [dict(s, video=False) for s in cfg.get("feeds", [])] + [dict(s, video=True) for s in cfg.get("youtube", [])]
    for src in jobs:
        try:
            if src["video"]:
                entries = fetch_channel(src.get("id") or channel_id(src["handle"], channels))
            else:
                entries = fetch(src["url"])
            if not entries:
                raise RuntimeError("no items in feed")
            taken = 0
            limit = now - (VIDEO_AGE_DAYS if src["video"] else MAX_AGE_DAYS) * 86400
            for e in entries[:SCAN if src.get("only_matching") else PER_FEED]:
                if taken >= PER_FEED:
                    break
                link, title = e["link"].strip(), clean(e["title"], 300)
                if not link.startswith("http") or not title:
                    continue
                ts = e["ts"] or now
                if ts < limit:
                    continue
                if src["video"] and "/shorts/" in link:
                    continue
                summary = clean_video_description(e["summary"]) if src["video"] else clean(e["summary"], 700)
                text = title + " " + summary[:300]
                sec = src["section"]
                if src.get("only_matching") and not matches(sec, text):
                    continue
                if src.get("laptops_too") and matches("laptops", title):
                    sec = "laptops"
                if sec == "auto":
                    sec = classify(text)
                fresh.append({
                    "id": hashlib.sha1(link.encode()).hexdigest()[:12], "sec": sec, "title": title, "sum": summary,
                    "src": src["name"], "url": link, "ts": min(ts, now), "video": src["video"], "lang": src.get("lang", "en"),
                    "img": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % e["vid"] if e["vid"] else e["img"],
                })
                taken += 1
            status.append({"name": src["name"], "ok": True, "items": taken, "seen": len(entries),
                           "newest_days": round((now - max(e["ts"] for e in entries)) / 86400, 1) if any(e["ts"] for e in entries) else None})
            print("ok  ", src["name"], taken)
        except Exception as e:  # noqa: BLE001
            status.append({"name": src["name"], "ok": False, "error": (type(e).__name__ + ": " + str(e))[:160]})
            print("FAIL", src["name"], type(e).__name__, str(e)[:120])

    new = sorted((it for it in fresh if it["id"] not in old), key=lambda it: -it["ts"])[:MAX_NEW]
    added = failed = 0
    for it in new:
        if it.pop("lang") != "ar":
            title = translate(it["title"])
            summary = translate(it["sum"]) if title is not None else None
            if title is None or summary is None:
                failed += 1
                if failed >= 15:
                    print("translation keeps failing, stopping for this run")
                    break
                continue
            it["title"], it["sum"] = title, summary
        old[it["id"]] = it
        added += 1

    items, counts = [], {}
    for it in sorted(old.values(), key=lambda it: -it["ts"]):
        if it["ts"] < (now - VIDEO_AGE_DAYS * 86400 if it.get("video") else oldest):
            continue
        counts[it["sec"]] = counts.get(it["sec"], 0) + 1
        if counts[it["sec"]] <= PER_SECTION:
            items.append(it)

    save(NEWS, {"updated": now, "items": items})
    save(STATUS, {"updated": now, "added": added, "untranslated": failed, "sources": status})
    save(CHANNELS, channels)
    bad = [s["name"] for s in status if not s["ok"]]
    print("\nadded %d, total %d, failed sources %d: %s" % (added, len(items), len(bad), ", ".join(bad)))


if __name__ == "__main__":
    main()
