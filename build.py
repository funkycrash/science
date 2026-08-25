#!/usr/bin/env python3
"""Build the static Brief.science reference archive.

Reads the SiteSucker dump and writes a static site into ./site.

Usage:
    python3 build.py [--src /path/to/us.sitesucker.mac.sitesucker]

Requires: beautifulsoup4, pillow.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import unicodedata
from collections import Counter, defaultdict
from copy import copy
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site"
ASSETS_SRC = ROOT / "assets"

MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
]
SITE_NAME = "Brief.science"
ORIGIN = "https://app.brief.science"

SLUG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d+)-(.+)$")
HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")

REMOVE_SELECTORS = [
    "script", "style", "svg", "form", "button", "noscript",
    ".Sharing", ".v1_sharing", ".v1_article__overlay", "#pdf-container",
    "[id^=htmx-]", ".v1_Issue__buttons", ".v2_Issue__buttons", ".Player",
    "#player", ".v1_Issue__intro__mobile-header", ".u-hidden-desktop",
    ".v1_NewsDots", ".NewsRelance", "#definition-banner", "#InfographyModal",
    ".v1_title_icon", ".u-screen-reader-text", ".Editions-nav", "#content_relance",
]
DROP_ATTRS_PREFIX = ("hx-", "data-", "on")
DROP_ATTRS = {"style", "tabindex", "id", "width", "height", "loading", "role", "aria-hidden"}
KEEP_EMPTY = {"img", "iframe", "video", "audio", "br", "hr", "source"}

TOP_BLOCKS = {
    "v1_focus__intro", "v1_focus_talk", "v1_etonnant", "v1_image", "v1_video",
    "v1_retrospective", "v1_postcard", "v1_radar", "v1_NewsDose", "v1_Issue__end",
    "v2_NewsOutro",
}
V1_ARTICLE_BLOCKS = {
    "v1_focus__intro", "v1_focus_talk", "v1_etonnant", "v1_image", "v1_video",
    "v1_retrospective", "v1_postcard",
}

stats = Counter()
unknown_links = Counter()


# --------------------------------------------------------------------------- data

@dataclass
class Article:
    slug: str
    id: int
    date: date
    title: str
    kind: str | None
    summary: str
    body_html: str
    cover_src: str | None        # local /img/... path or None
    themes: list[str] = field(default_factory=list)
    edition: str | None = None   # edition slug
    source: str = ""             # "page" or "edition"

    @property
    def url(self) -> str:
        return f"/article/{self.slug}/"


@dataclass
class Edition:
    slug: str
    id: int
    date: date
    title: str
    intro: str
    body_html: str
    articles: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/edition/{self.slug}/"


# ------------------------------------------------------------------------ helpers

def parse_slug(slug: str) -> tuple[date, int]:
    m = SLUG_RE.match(slug)
    if not m:
        raise ValueError(slug)
    y, mo, d, n, _ = m.groups()
    return date(int(y), int(mo), int(d)), int(n)


def fr_date(d: date) -> str:
    day = "1er" if d.day == 1 else str(d.day)
    return f"{day} {MONTHS[d.month - 1]} {d.year}"


def clean_text(s: str) -> str:
    s = s.replace("\xa0", " ").replace("‌", "")
    return re.sub(r"\s+", " ", s).strip()


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def read_soup(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")


def page_html_files(d: Path) -> list[Path]:
    files = sorted(d.glob("*.html"))
    files.sort(key=lambda p: (p.name != "index.html", p.name))
    return files


def main_article(soup: BeautifulSoup) -> Tag | None:
    art = soup.select_one("main article")
    if art is None or not art.get_text(strip=True):
        return None
    return art


def truncate(s: str, n: int) -> str:
    s = clean_text(s)
    if len(s) <= n:
        return s
    cut = s[: n].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "…"


def normalize_kind(k: str) -> str:
    k = clean_text(k)
    if k.startswith("C’était il y a") or k.startswith("C'était il y a"):
        return "C’était il y a…"
    if k.lower().startswith("sea, science"):
        return "Sea, science & sun"
    return k


# ------------------------------------------------------------------------ builder

class Builder:
    def __init__(self, src: Path):
        self.src = src
        self.app = src / "app.brief.science"
        self.www = src / "www.brief.science"
        self.articles: dict[str, Article] = {}
        self.editions: dict[str, Edition] = {}
        self.article_slugs: set[str] = set()
        self.edition_slugs: set[str] = set()
        self.themes: dict[str, str] = {}                # slug -> name
        self.theme_articles: dict[str, list[str]] = defaultdict(list)
        self.article_themes: dict[str, list[str]] = defaultdict(list)
        self.copied: set[str] = set()
        self.thumb_failed: set[str] = set()
        self.image_checked: dict[str, bool] = {}

    # ---------------------------------------------------------------- discovery

    def discover(self) -> None:
        for d in (self.app / "article").iterdir():
            if d.is_dir() and SLUG_RE.match(d.name):
                self.article_slugs.add(d.name)
        for d in (self.app / "edition").iterdir():
            if d.is_dir() and SLUG_RE.match(d.name):
                for f in page_html_files(d):
                    if main_article(read_soup(f)) is not None:
                        self.edition_slugs.add(d.name)
                        break

        # Article slugs referenced only inside editions (recovered later).
        for slug in self.edition_slugs:
            for f in page_html_files(self.app / "edition" / slug):
                soup = read_soup(f)
                art = main_article(soup)
                if art is None:
                    continue
                for a in art.select(".v1_Issue__news h1 a[href]"):
                    s = self.article_slug_from_href(a["href"])
                    if s:
                        self.article_slugs.add(s)
                break

    def article_slug_from_href(self, href: str) -> str | None:
        href = unquote(href)
        m = re.search(r"article/(\d{4}-\d{2}-\d{2}-\d+-[^/?#﹖]+)", href)
        if m:
            return m.group(1)
        return None

    # ----------------------------------------------------------------- themes

    def load_themes(self) -> None:
        idx = read_soup(self.app / "thematiques" / "index.html")
        for a in idx.select("a.ThematiquesPage__thematique[href]"):
            m = re.search(r"thematique/([a-z-]+)/", a["href"])
            if m:
                self.themes[m.group(1)] = clean_text(a.get_text())
        for tdir in sorted((self.app / "thematique").iterdir()):
            if not tdir.is_dir():
                continue
            t = tdir.name
            self.themes.setdefault(t, t.capitalize())
            seen: set[str] = set()
            for f in sorted(tdir.glob("*.html")):
                soup = read_soup(f)
                for a in soup.select("a.NewsPreview[href]"):
                    s = self.article_slug_from_href(a["href"])
                    if s and s not in seen:
                        seen.add(s)
                        self.theme_articles[t].append(s)
                        self.article_themes[s].append(t)

    # ------------------------------------------------------------- URL rewriting

    def rewrite_href(self, href: str, base: str) -> str | None:
        """Return the new href, or None to unwrap the link."""
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return href or None
        absolute = urljoin(base, unquote(href))
        parts = urlsplit(absolute)
        path = parts.path
        if parts.netloc == "app.brief.science":
            segs = [s for s in path.split("/") if s]
            # SiteSucker relativised some external links below the app host.
            if segs and HOST_RE.match(segs[0]) and segs[0] != "app.brief.science":
                return self.external_or_local_file(segs)
            if not segs:
                return "/"
            head = segs[0]
            if head == "article" and len(segs) >= 2:
                slug = segs[1]
                if slug in self.article_slugs:
                    return f"/article/{slug}/"
                unknown_links["article:" + slug] += 1
                return f"{ORIGIN}/article/{slug}/"
            if head == "edition" and len(segs) >= 2:
                slug = segs[1]
                if slug in self.edition_slugs:
                    return f"/edition/{slug}/"
                # Empty shell in the dump: send to the editions index.
                return "/editions/"
            if head == "thematique" and len(segs) >= 2 and segs[1] in self.themes:
                return f"/thematique/{segs[1]}/"
            if head == "thematiques":
                return "/thematiques/"
            if head in ("a-la-une", "index.html"):
                return "/"
            if head == "recherche":
                return "/recherche/"
            # definitions, connexion, favoris, downloads, etc.
            stats["links unwrapped"] += 1
            return None
        if parts.scheme in ("http", "https"):
            return absolute
        return absolute

    def external_or_local_file(self, segs: list[str]) -> str:
        host, rest = segs[0], segs[1:]
        if host == "www.brief.science":
            local = self.www.joinpath(*rest)
            if local.is_file():
                target = OUT / "files" / local.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(local, target)
                stats["files copied"] += 1
                return f"/files/{local.name}"
        return "https://" + "/".join(segs)

    def image_ok(self, path: Path) -> bool:
        key = str(path)
        if key in self.image_checked:
            return self.image_checked[key]
        ok = True
        if path.suffix.lower() in (".webp", ".jpeg", ".jpg", ".png", ".gif"):
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            try:
                with Image.open(path) as im:
                    im.load()
            except Exception:  # noqa: BLE001
                ok = False
            finally:
                ImageFile.LOAD_TRUNCATED_IMAGES = True
        self.image_checked[key] = ok
        return ok

    def rewrite_img(self, src: str, base: str) -> str | None:
        absolute = urljoin(base, unquote(src.strip()))
        parts = urlsplit(absolute)
        segs = [s for s in parts.path.split("/") if s]
        if parts.netloc == "app.brief.science" and segs and HOST_RE.match(segs[0]):
            host, rest = segs[0], segs[1:]
        else:
            host, rest = parts.netloc, segs
        if host == "www.brief.science" and rest[:3] == ["editorial", "news", "img"] and len(rest) >= 5:
            news_id = rest[3]
            tail = [p for p in rest[4:] if p != "file"]
            fname = tail[-1]
            ext = fname.rsplit(".", 1)[-1].lower()
            stem = "-".join(tail[:-1]) if len(tail) > 1 else fname.rsplit(".", 1)[0]
            if not stem or stem == "index":
                stem = "image"
            local = self.www.joinpath(*rest)
            if local.is_file() and not self.image_ok(local):
                stats["images truncated, linked remotely"] += 1
                return "https://www.brief.science/" + "/".join(rest)
            if local.is_file():
                rel = f"/img/{news_id}/{stem}.{ext}"
                target = OUT / rel.lstrip("/")
                if rel not in self.copied:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local, target)
                    self.copied.add(rel)
                    stats["images copied"] += 1
                return rel
            stats["images missing locally"] += 1
            return "https://www.brief.science/" + "/".join(rest)
        if "icon-arrow" in absolute or "img-decoration" in absolute:
            return None
        if parts.scheme in ("http", "https") and host not in ("app.brief.science",):
            return absolute
        stats["images unknown"] += 1
        return None

    # ------------------------------------------------------------ DOM transform

    def transform(self, root: Tag, base: str, context: str) -> Tag:
        """Clean a copied <article> tree in place. context is 'article' or 'edition'."""
        for sel in REMOVE_SELECTORS:
            for el in root.select(sel):
                el.decompose()
        for img in list(root.find_all("img")):
            src = img.get("src") or ""
            new = self.rewrite_img(src, base)
            if new is None:
                img.decompose()
            else:
                img["src"] = new
                img["loading"] = "lazy"
                if not img.get("alt"):
                    img["alt"] = ""
        for a in list(root.find_all("a")):
            href = a.get("href")
            if href is None:
                a.unwrap()
                continue
            new = self.rewrite_href(href, base)
            if new is None:
                a.unwrap()
                continue
            a["href"] = new
            if new.startswith("http"):
                a["target"] = "_blank"
                a["rel"] = "noopener"
            else:
                for k in ("target", "rel"):
                    if k in a.attrs:
                        del a[k]
        for el in root.find_all(True):
            for attr in list(el.attrs):
                if attr in DROP_ATTRS and not (attr == "loading" and el.name == "img"):
                    del el[attr]
                elif attr.startswith(DROP_ATTRS_PREFIX):
                    del el[attr]
        for dfn in root.find_all("dfn"):
            dfn.attrs = {}
        for comment in root.find_all(string=lambda s: isinstance(s, NavigableString) and s.parent is not None and s.__class__.__name__ == "Comment"):
            comment.extract()

        if context == "article":
            for sel in (".v2_NewsCover", ".v2_NewsDate", ".v2_NewsTitle", ".v2_ArticleTitle", "p.v1_article_date", "h1"):
                for el in root.select(sel):
                    el.decompose()
            first_kicker = root.select_one("h2.v1_align-icon")
            if first_kicker is not None and "Pour aller plus loin" not in first_kicker.get_text():
                first_kicker.decompose()
        else:
            for sel in (".v2_IssueIntro", ".v1_Issue__intro", "p.v1_article_date", ".v2_Issue__date", ".v2_NewsDate"):
                for el in root.select(sel):
                    el.decompose()
            for h1 in root.find_all("h1"):
                h1.name = "h2"
                h1["class"] = ["edition-article-title"]

        # Section kickers become plain labels.
        for el in root.select("h2.v1_align-icon, .v1_focus_talk h3, h3.v2_NewsMain__beyond_title, "
                              "span.v1_NewsKicker, span.v1_RadarKicker, div.v2_NewsKicker"):
            text = clean_text(" ".join(t for t in el.find_all(string=True, recursive=True)))
            el.clear()
            el.append(text)
            el.name = "p"
            el["class"] = ["label"]

        self.prune_empty(root)
        self.mark_sections(root)
        return root

    def prune_empty(self, root: Tag) -> None:
        changed = True
        while changed:
            changed = False
            for el in list(root.find_all(["p", "div", "h2", "h3", "h4", "span", "figure", "ul", "li", "section", "aside"])):
                if el.decomposed if hasattr(el, "decomposed") else False:
                    continue
                if el.get_text(strip=True):
                    continue
                if el.find(list(KEEP_EMPTY)):
                    continue
                el.decompose()
                changed = True

    def mark_sections(self, root: Tag) -> None:
        for el in root.find_all(True):
            classes = set(el.get("class", []))
            if classes & TOP_BLOCKS:
                if "v2_News" in classes and el.find(class_="v2_News"):
                    continue
                el["class"] = el.get("class", []) + ["section"]
            elif "v2_News" in classes and not el.find(class_="v2_News"):
                el["class"] = el.get("class", []) + ["section"]

    # -------------------------------------------------------------- articles

    def extract_article(self, art: Tag, slug: str, base: str, source: str) -> Article | None:
        d, n = parse_slug(slug)
        raw = copy(art)
        # Metadata from the untouched copy.
        h1 = raw.select_one(".v2_NewsTitle h1") or raw.find("h1") or raw.select_one(".v2_ArticleTitle")
        title = clean_text(h1.get_text()) if h1 else slug
        kind = None
        k = raw.select_one("h2.v1_align-icon")
        if k is not None:
            for p in k.find_all("p"):
                p.decompose()
            kind = normalize_kind(k.get_text()) or None
        summary = ""
        summ = raw.select_one(".v2_NewsSummary")
        if summ is not None:
            summary = clean_text(" ".join(p.get_text() for p in summ.find_all("p")))
        else:
            lede = raw.select_one(".v1_focus_frame")
            if lede is not None:
                summary = clean_text(lede.get_text())
        issue = None
        link = raw.select_one(".v1_issue_link a[href]")
        if link is not None:
            m = re.search(r"edition/(\d{4}-\d{2}-\d{2}-\d+-[^/?#]+)", unquote(link["href"]))
            if m:
                issue = m.group(1)

        cover = None
        cimg = raw.select_one(".v2_NewsCover img[src]")
        if cimg is not None:
            new = self.rewrite_img(cimg["src"], base)
            if new and new.startswith("/img/"):
                cover = new

        body = self.transform(art, base, "article")
        if not summary:
            for p in body.find_all("p"):
                t = clean_text(p.get_text())
                if len(t) > 80:
                    summary = t
                    break
        if cover is None:
            img = body.find("img")
            if img is not None and img.get("src", "").startswith("/img/"):
                cover = img["src"]
        inner = body.decode_contents()
        return Article(slug=slug, id=n, date=d, title=title, kind=kind, summary=summary,
                       body_html=inner, cover_src=cover, source=source, edition=issue)

    def load_articles(self) -> None:
        for slug in sorted(self.article_slugs):
            d = self.app / "article" / slug
            if not d.is_dir():
                continue
            art = None
            for f in page_html_files(d):
                soup = read_soup(f)
                art = main_article(soup)
                if art is not None:
                    break
            if art is None:
                stats["article pages empty"] += 1
                continue
            base = f"{ORIGIN}/article/{slug}/index.html"
            a = self.extract_article(art, slug, base, "page")
            if a:
                self.articles[slug] = a
                stats["articles from pages"] += 1

    # -------------------------------------------------------------- editions

    def load_editions(self) -> None:
        for slug in sorted(self.edition_slugs):
            d = self.app / "edition" / slug
            art = None
            for f in page_html_files(d):
                soup = read_soup(f)
                art = main_article(soup)
                if art is not None:
                    break
            if art is None:
                continue
            base = f"{ORIGIN}/edition/{slug}/index.html"
            dt, n = parse_slug(slug)
            raw = copy(art)
            title, intro = "", ""
            v2 = raw.select_one(".v2_IssueIntro")
            if v2 is not None:
                h1 = v2.find("h1")
                title = clean_text(h1.get_text()) if h1 else ""
                p = v2.find("p")
                intro = clean_text(p.get_text()) if p else ""
            else:
                p = raw.select_one(".v1_Intro p")
                intro = clean_text(p.get_text()) if p else ""
            if not title:
                title = f"Édition du {fr_date(dt)}"

            members: list[str] = []
            self.recover_articles_from_edition(art, slug, base, members)

            body = self.transform(copy(art), base, "edition")
            ed = Edition(slug=slug, id=n, date=dt, title=title, intro=intro,
                         body_html=body.decode_contents(), articles=members)
            self.editions[slug] = ed

    def recover_articles_from_edition(self, art: Tag, ed_slug: str, base: str, members: list[str]) -> None:
        news = art.select_one(".v1_Issue__news")
        if news is None:
            # v2 edition: one article, match by title.
            h1 = art.select_one(".v2_IssueIntro h1")
            if h1 is not None:
                t = clean_text(h1.get_text())
                for a in self.articles.values():
                    if a.title == t:
                        members.append(a.slug)
                        a.edition = a.edition or ed_slug
            return
        groups: list[list[Tag]] = []
        for child in news.children:
            if not isinstance(child, Tag):
                continue
            classes = set(child.get("class", []))
            if not classes & V1_ARTICLE_BLOCKS:
                continue
            if "v1_focus_talk" in classes and groups and "v1_focus__intro" in set(groups[-1][0].get("class", [])):
                groups[-1].append(child)
            else:
                groups.append([child])
        for group in groups:
            h1a = group[0].select_one("h1 a[href]")
            slug = self.article_slug_from_href(h1a["href"]) if h1a else None
            if not slug:
                continue
            members.append(slug)
            if slug in self.articles:
                a = self.articles[slug]
                a.edition = a.edition or ed_slug
                continue
            wrapper = BeautifulSoup("<article class='v1_article'></article>", "html.parser").article
            for block in group:
                wrapper.append(copy(block))
            a = self.extract_article(wrapper, slug, base, "edition")
            if a:
                a.edition = ed_slug
                self.articles[slug] = a
                stats["articles recovered from editions"] += 1

    # ----------------------------------------------------------- thumbnails

    def thumbnail(self, a: Article) -> str | None:
        if not a.cover_src:
            return None
        src = OUT / a.cover_src.lstrip("/")
        if not src.is_file():
            return None
        rel = f"/img/{a.id}/thumb.jpg"
        target = OUT / rel.lstrip("/")
        if target.exists():
            return rel
        if rel in self.thumb_failed:
            return None
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im = ImageOps.fit(im, (720, 480), Image.LANCZOS, centering=(0.5, 0.35))
                im.save(target, "JPEG", quality=80, optimize=True, progressive=True)
            stats["thumbnails"] += 1
            return rel
        except Exception as e:  # noqa: BLE001
            self.thumb_failed.add(rel)
            print("thumb failed", src, e, file=sys.stderr)
            return None

    # ---------------------------------------------------------------- pages

    def nav(self, current: str) -> str:
        items = [("/articles/", "Articles"), ("/editions/", "Éditions"),
                 ("/thematiques/", "Thématiques"), ("/recherche/", "Rechercher")]
        links = "".join(
            f'<a href="{u}"{" aria-current=\"page\"" if current == u else ""}>{n}</a>' for u, n in items
        )
        return (
            '<header class="site-header"><div class="site-header__inner">'
            f'<a class="wordmark" href="/">{SITE_NAME}<span>archive</span></a>'
            f'<nav class="site-nav" aria-label="Navigation principale">{links}</nav>'
            "</div></header>"
        )

    def shell(self, title: str, content: str, current: str = "", strip: str = "",
              description: str = "", extra_head: str = "") -> str:
        full = f"{title} · {SITE_NAME}" if title else SITE_NAME
        desc = f'<meta name="description" content="{esc(truncate(description, 160))}">' if description else ""
        return (
            "<!doctype html>\n<html lang=\"fr\">\n<head>\n<meta charset=\"utf-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(full)}</title>\n{desc}\n"
            '<link rel="icon" href="/assets/favicon.png">\n'
            '<link rel="stylesheet" href="/assets/css/fonts.css">\n'
            '<link rel="stylesheet" href="/assets/css/style.css">\n'
            f"{extra_head}</head>\n<body>\n{self.nav(current)}\n{strip}\n"
            f'<main class="page">\n{content}\n</main>\n'
            f'<footer class="site-footer small"><div class="site-footer__inner">'
            f"<span>{SITE_NAME}, copie de référence</span>"
            f"<span>{len(self.articles)} articles · {len(self.editions)} éditions · {len([t for t in self.themes if self.theme_count(t)])} thématiques</span>"
            "</div></footer>\n</body>\n</html>\n"
        )

    def write(self, rel: str, content: str) -> None:
        target = OUT / rel.lstrip("/")
        if rel.endswith("/"):
            target = target / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        stats["pages written"] += 1

    def card(self, a: Article) -> str:
        thumb = self.thumbnail(a)
        img = f'<img src="{thumb}" alt="" loading="lazy" width="720" height="480">' if thumb else '<div class="card__blank"></div>'
        meta = fr_date(a.date) + (f" · {esc(a.kind)}" if a.kind else "")
        return (
            f'<a class="card" href="{a.url}">{img}<div>'
            f'<time class="small" datetime="{a.date.isoformat()}">{meta}</time>'
            f"<h2>{esc(a.title)}</h2><p>{esc(truncate(a.summary, 170))}</p></div></a>"
        )

    def row(self, a: Article) -> str:
        kind = f'<span class="row__kind small">{esc(a.kind)}</span>' if a.kind else ""
        return (
            f'<li class="row"><time class="small" datetime="{a.date.isoformat()}">{fr_date(a.date)}</time>'
            f'<a href="{a.url}">{esc(a.title)}</a>{kind}</li>'
        )

    def strip(self, prev: tuple[str, str] | None, nxt: tuple[str, str] | None, label: str = "") -> str:
        left = f'<a href="{prev[0]}">← {esc(prev[1])}</a>' if prev else "<span></span>"
        right = f'<a href="{nxt[0]}">{esc(nxt[1])} →</a>' if nxt else "<span></span>"
        mid = f'<span class="strip__label">{esc(label)}</span>' if label else ""
        return f'<nav class="edition-strip small" aria-label="Navigation">{left}{mid}{right}</nav>'

    # ---- article page

    def render_article(self, a: Article, ordered: list[Article], idx: int) -> str:
        prev = ordered[idx - 1] if idx > 0 else None
        nxt = ordered[idx + 1] if idx + 1 < len(ordered) else None
        strip = self.strip(
            (prev.url, fr_date(prev.date)) if prev else None,
            (nxt.url, fr_date(nxt.date)) if nxt else None,
        )
        meta = [f'<time datetime="{a.date.isoformat()}">{fr_date(a.date)}</time>']
        if a.kind:
            meta.append(f"<span>{esc(a.kind)}</span>")
        for t in a.themes:
            meta.append(f'<a href="/thematique/{t}/">{esc(self.themes[t])}</a>')
        if a.edition and a.edition in self.editions:
            meta.append(f'<a href="{self.editions[a.edition].url}">Édition du {fr_date(self.editions[a.edition].date)}</a>')
        cover = ""
        # v2 covers are photos shown above the title; v1 first images stay in the body.
        if a.cover_src and "/cover." in a.cover_src:
            cover = f'<img class="cover" src="{a.cover_src}" alt="">'
        head = (
            f'<header class="head">{cover}<div class="meta small">{"".join(meta)}</div>'
            f"<h1>{esc(a.title)}</h1></header>"
        )
        related = self.related(a)
        rel_html = ""
        if related:
            label = f"À lire aussi dans la thématique {esc(self.themes[a.themes[0]])}" if a.themes else "À lire aussi"
            rel_html = (
                f'<aside class="related"><div class="label">{label}</div>'
                f'<div class="related__grid">{"".join(self.card(r) for r in related)}</div></aside>'
            )
        pn = []
        if prev:
            pn.append(f'<a class="pn pn--prev" href="{prev.url}"><span class="small">← Article précédent</span><span>{esc(prev.title)}</span></a>')
        if nxt:
            pn.append(f'<a class="pn pn--next" href="{nxt.url}"><span class="small">Article suivant →</span><span>{esc(nxt.title)}</span></a>')
        pn_html = f'<nav class="prevnext" aria-label="Articles voisins">{"".join(pn)}</nav>' if pn else ""
        body = f'<article class="article-body">{head}{a.body_html}</article>'
        return self.shell(a.title, body + rel_html + pn_html, "/articles/", strip, a.summary)

    def related(self, a: Article, n: int = 4) -> list[Article]:
        pool: list[Article] = []
        if a.themes:
            pool = [self.articles[s] for s in self.theme_articles[a.themes[0]] if s in self.articles and s != a.slug]
        if len(pool) < 2:
            pool = [x for x in self.articles.values() if x.slug != a.slug]
        pool.sort(key=lambda x: abs((x.date - a.date).days))
        return pool[:n]

    # ---- edition page

    def render_edition(self, e: Edition, ordered: list[Edition], idx: int) -> str:
        prev = ordered[idx - 1] if idx > 0 else None
        nxt = ordered[idx + 1] if idx + 1 < len(ordered) else None
        strip = self.strip(
            (prev.url, f"Édition du {fr_date(prev.date)}") if prev else None,
            (nxt.url, f"Édition du {fr_date(nxt.date)}") if nxt else None,
        )
        intro = f'<p class="lede">{esc(e.intro)}</p>' if e.intro else ""
        toc = ""
        members = [self.articles[s] for s in e.articles if s in self.articles]
        if members:
            toc = '<ul class="toc">' + "".join(
                f'<li><a href="{m.url}">{esc(m.title)}</a>{f"<span class=\"small\"> · {esc(m.kind)}</span>" if m.kind else ""}</li>'
                for m in members
            ) + "</ul>"
        meta = "" if e.title.startswith("Édition du") else (
            f'<div class="meta small"><time datetime="{e.date.isoformat()}">Édition du {fr_date(e.date)}</time></div>'
        )
        head = f'<header class="head">{meta}<h1>{esc(e.title)}</h1>{intro}{toc}</header>'
        body = f'<article class="article-body edition-body">{head}{e.body_html}</article>'
        return self.shell(e.title, body, "/editions/", strip, e.intro)

    # ---- index pages

    def render_home(self, ordered: list[Article]) -> str:
        latest = list(reversed(ordered))[:12]
        cards = "".join(self.card(a) for a in latest)
        themes = "".join(
            f'<li><a href="/thematique/{t}/">{esc(self.themes[t])}</a> <span class="small">{self.theme_count(t)}</span></li>'
            for t in sorted(self.themes, key=lambda t: self.themes[t]) if self.theme_count(t)
        )
        eds = sorted(self.editions.values(), key=lambda e: e.date, reverse=True)[:8]
        ed_rows = "".join(
            f'<li class="row"><time class="small" datetime="{e.date.isoformat()}">{fr_date(e.date)}</time><a href="{e.url}">{esc(e.title)}</a></li>'
            for e in eds
        )
        content = (
            '<section class="section section--first"><div class="label">Derniers articles</div>'
            f'<div class="related__grid">{cards}</div>'
            '<p class="more"><a href="/articles/">Tous les articles</a></p></section>'
            f'<section class="section"><div class="label">Thématiques</div><ul class="themes">{themes}</ul></section>'
            f'<section class="section"><div class="label">Dernières éditions</div><ul class="rows">{ed_rows}</ul>'
            '<p class="more"><a href="/editions/">Toutes les éditions</a></p></section>'
        )
        return self.shell("", content, "/")

    def render_articles_index(self, ordered: list[Article]) -> str:
        by_year: dict[int, list[Article]] = defaultdict(list)
        for a in reversed(ordered):
            by_year[a.date.year].append(a)
        parts = ['<header class="head"><h1>Articles</h1>'
                 f'<p class="lede">{len(ordered)} articles, du {fr_date(ordered[0].date)} au {fr_date(ordered[-1].date)}.</p></header>']
        for y in sorted(by_year, reverse=True):
            rows = "".join(self.row(a) for a in by_year[y])
            parts.append(f'<section class="section"><h2 class="year">{y}</h2><ul class="rows">{rows}</ul></section>')
        return self.shell("Articles", "".join(parts), "/articles/")

    def render_editions_index(self) -> str:
        eds = sorted(self.editions.values(), key=lambda e: e.date, reverse=True)
        by_year: dict[int, list[Edition]] = defaultdict(list)
        for e in eds:
            by_year[e.date.year].append(e)
        parts = ['<header class="head"><h1>Éditions</h1>'
                 f'<p class="lede">{len(eds)} éditions hebdomadaires. Chaque édition rassemble les articles de la semaine.</p></header>']
        for y in sorted(by_year, reverse=True):
            items = []
            for e in by_year[y]:
                members = [self.articles[s] for s in e.articles if s in self.articles]
                sub = ""
                if e.intro and e.title.startswith("Édition du"):
                    sub = f'<p class="small">{esc(truncate(e.intro, 220))}</p>'
                elif members:
                    sub = '<p class="small">' + " · ".join(esc(m.title) for m in members[:4]) + "</p>"
                items.append(
                    f'<li class="row row--stack"><time class="small" datetime="{e.date.isoformat()}">{fr_date(e.date)}</time>'
                    f'<div><a href="{e.url}">{esc(e.title)}</a>{sub}</div></li>'
                )
            parts.append(f'<section class="section"><h2 class="year">{y}</h2><ul class="rows">{"".join(items)}</ul></section>')
        return self.shell("Éditions", "".join(parts), "/editions/")

    def theme_count(self, t: str) -> int:
        return len([s for s in self.theme_articles[t] if s in self.articles])

    def render_themes_index(self) -> str:
        items = []
        for t in sorted(self.themes, key=lambda t: self.themes[t]):
            n = self.theme_count(t)
            if not n:
                continue
            items.append(f'<li><a href="/thematique/{t}/">{esc(self.themes[t])}</a><span class="small">{n} article{"s" if n > 1 else ""}</span></li>')
        content = ('<header class="head"><h1>Thématiques</h1></header>'
                   f'<section class="section"><ul class="theme-list">{"".join(items)}</ul></section>')
        return self.shell("Thématiques", content, "/thematiques/")

    def render_theme(self, t: str) -> str:
        arts = sorted((self.articles[s] for s in self.theme_articles[t] if s in self.articles),
                      key=lambda a: a.date, reverse=True)
        cards = "".join(self.card(a) for a in arts)
        content = (
            f'<header class="head"><div class="meta small"><a href="/thematiques/">Thématiques</a></div><h1>{esc(self.themes[t])}</h1>'
            f'<p class="lede">{len(arts)} article{"s" if len(arts) > 1 else ""}.</p></header>'
            f'<section class="section"><div class="related__grid">{cards}</div></section>'
        )
        return self.shell(self.themes[t], content, "/thematiques/")

    def render_search(self) -> str:
        content = (
            '<header class="head"><h1>Rechercher</h1></header>'
            '<section class="section"><form class="search" role="search" onsubmit="return false">'
            '<label for="q" class="label">Titre, résumé ou thématique</label>'
            '<input id="q" type="search" autocomplete="off" autofocus placeholder="Par exemple : trous noirs">'
            '</form><p id="count" class="small"></p><ul id="results" class="rows"></ul>'
            '<noscript><p>La recherche a besoin de JavaScript. <a href="/articles/">Voir la liste des articles.</a></p></noscript>'
            '</section>'
        )
        return self.shell("Rechercher", content, "/recherche/", extra_head='<script defer src="/assets/js/search.js"></script>\n')

    def render_404(self) -> str:
        content = ('<header class="head"><h1>Page introuvable</h1>'
                   '<p class="lede">Cette page n’existe pas dans l’archive. <a href="/articles/">Voir tous les articles</a> ou <a href="/recherche/">rechercher</a>.</p></header>')
        return self.shell("Page introuvable", content)

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir()
        shutil.copytree(ASSETS_SRC, OUT / "assets")

        self.discover()
        self.load_themes()
        self.load_articles()
        self.load_editions()
        for a in self.articles.values():
            a.themes = [t for t in self.article_themes.get(a.slug, []) if t in self.themes]

        ordered = sorted(self.articles.values(), key=lambda a: (a.date, a.id))
        for i, a in enumerate(ordered):
            self.write(a.url, self.render_article(a, ordered, i))
        eds = sorted(self.editions.values(), key=lambda e: (e.date, e.id))
        for i, e in enumerate(eds):
            self.write(e.url, self.render_edition(e, eds, i))
        self.write("/", self.render_home(ordered))
        self.write("/articles/", self.render_articles_index(ordered))
        self.write("/editions/", self.render_editions_index())
        self.write("/thematiques/", self.render_themes_index())
        for t in self.themes:
            if self.theme_count(t):
                self.write(f"/thematique/{t}/", self.render_theme(t))
        self.write("/recherche/", self.render_search())
        self.write("/404.html", self.render_404())

        index = [
            {
                "t": a.title, "s": truncate(a.summary, 240), "u": a.url, "d": a.date.isoformat(),
                "f": fr_date(a.date), "k": a.kind or "", "th": [self.themes[t] for t in a.themes],
            }
            for a in reversed(ordered)
        ]
        (OUT / "search.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        print(f"articles: {len(self.articles)}  editions: {len(self.editions)}  themes: {len(self.themes)}")
        for k, v in sorted(stats.items()):
            print(f"  {k}: {v}")
        if unknown_links:
            print(f"  links to articles not in the dump: {sum(unknown_links.values())} ({len(unknown_links)} distinct)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(Path.home() / "Downloads" / "us.sitesucker.mac.sitesucker"))
    args = ap.parse_args()
    src = Path(args.src)
    if not (src / "app.brief.science").is_dir():
        sys.exit(f"source dump not found at {src}")
    Builder(src).run()


if __name__ == "__main__":
    main()
