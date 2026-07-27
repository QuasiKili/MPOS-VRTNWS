import gc
import logging
import json
import time
import lvgl as lv
from mpos import Activity, Intent, DisplayMetrics, DownloadManager, TaskManager, add_focus_border

DEBUG = True

logger = logging.getLogger(__name__)

if DEBUG:
    logger.setLevel(logging.DEBUG)
    logger.info("vrtnws module loaded, DEBUG enabled")

VRT_FEED_URL = "https://www.vrt.be/vrtnws/nl.rss.articles.xml"
VRT_FRONTPAGE_URL = "https://www.vrt.be/vrtnws/nl/"

HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&#x27;": "'",
    "&#x2F;": "/",
    "&#x60;": "`",
    "&#x3D;": "=",
    "&nbsp;": " ",
    "&apos;": "'",
    "&ldquo;": '"',
    "&rdquo;": '"',
    "&lsquo;": "'",
    "&rsquo;": "'",
    "&hellip;": "...",
    "&mdash;": "--",
    "&ndash;": "-",
}


def replace_typographic_chars(text):
    text = text.replace("\u201c", '"')
    text = text.replace("\u201d", '"')
    text = text.replace("\u2018", "'")
    text = text.replace("\u2019", "'")
    return text


def strip_html(text):
    if not text:
        return ""
    result = []
    in_tag = False
    in_entity = False
    entity_buf = ""
    for char in text:
        if char == "<":
            in_tag = True
        elif char == ">":
            in_tag = False
        elif not in_tag:
            if char == "&":
                in_entity = True
                entity_buf = "&"
            elif in_entity:
                entity_buf += char
                if char == ";":
                    decoded = HTML_ENTITIES.get(entity_buf, entity_buf)
                    result.append(decoded)
                    in_entity = False
            else:
                result.append(char)
    return "".join(result)


def unescape_entities(text):
    if not text:
        return ""
    result = []
    in_entity = False
    entity_buf = ""
    for char in text:
        if char == "&":
            in_entity = True
            entity_buf = "&"
        elif in_entity:
            entity_buf += char
            if char == ";":
                decoded = HTML_ENTITIES.get(entity_buf, entity_buf)
                result.append(decoded)
                in_entity = False
        else:
            result.append(char)
    return "".join(result)


def get_tag_content(text, tag):
    start_tag = "<" + tag
    end_tag = "</" + tag + ">"
    start = text.find(start_tag)
    if start == -1:
        return None
    tag_end = text.find(">", start)
    if tag_end == -1:
        return None
    content_start = tag_end + 1
    end = text.find(end_tag, content_start)
    if end == -1:
        return None
    return text[content_start:end]


def extract_entries(xml_text):
    entries = []
    start = 0
    while True:
        entry_start = xml_text.find("<entry", start)
        if entry_start == -1:
            break
        tag_end = xml_text.find(">", entry_start)
        if tag_end == -1:
            break
        content_start = tag_end + 1
        entry_end = xml_text.find("</entry>", content_start)
        if entry_end == -1:
            break
        entries.append(xml_text[content_start:entry_end])
        start = entry_end + 8
    return entries


def get_atom_link(entry_text, rel_value):
    pos = 0
    rel_str = ' rel="' + rel_value + '"'
    while True:
        link_start = entry_text.find("<link", pos)
        if link_start == -1:
            break
        link_end = entry_text.find(">", link_start)
        if link_end == -1:
            break
        link_tag = entry_text[link_start : link_end + 1]
        if rel_str in link_tag:
            href_start = link_tag.find('href="')
            if href_start != -1:
                href_start += 6
                href_end = link_tag.find('"', href_start)
                if href_end != -1:
                    return link_tag[href_start:href_end]
        pos = link_end + 1
    return None


def parse_atom_feed(xml_text, max_items=50):
    if not xml_text:
        return []
    feed_end = xml_text.find("</feed>")
    if feed_end == -1:
        return []
    entries = extract_entries(xml_text)
    articles = []
    for entry_text in entries[:max_items]:
        article = {}
        title = get_tag_content(entry_text, "title")
        if title:
            title = title.strip()
            title = unescape_entities(title)
            title = replace_typographic_chars(title)
            if title:
                article["title"] = title
        summary = get_tag_content(entry_text, "summary")
        if summary:
            summary = strip_html(summary)
            summary = unescape_entities(summary)
            summary = replace_typographic_chars(summary)
            summary = summary.strip()
            article["summary"] = summary
        published = get_tag_content(entry_text, "published")
        if published:
            article["published"] = published
        self_link = get_atom_link(entry_text, "self")
        if self_link:
            article["self_link"] = self_link
        link = get_atom_link(entry_text, "alternate")
        if link:
            article["link"] = link
        if "title" not in article:
            continue
        articles.append(article)
    return articles


def get_article_content(xml_text):
    if not xml_text:
        return ""
    content = get_tag_content(xml_text, "content")
    if not content:
        return ""
    text = unescape_entities(content)
    text = strip_html(text)
    text = replace_typographic_chars(text)
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    text = "\n".join(lines)
    if len(text) > 10000:
        text = text[:10000] + "..."
    return text


def extract_next_data(html_text):
    start_marker = '<script id="__NEXT_DATA__" type="application/json">'
    end_marker = "</script>"
    start = html_text.find(start_marker)
    if start == -1:
        return None
    content_start = start + len(start_marker)
    end = html_text.find(end_marker, content_start)
    if end == -1:
        return None
    return html_text[content_start:end]


def extract_article_info(json_text):
    try:
        data = json.loads(json_text)
    except Exception as e:
        logger.warning("Failed to parse __NEXT_DATA__ JSON: %s", e)
        return []
    try:
        compositions = data["props"]["pageProps"]["data"]["compositions"]
    except (KeyError, TypeError) as e:
        logger.warning("Unexpected JSON structure: %s", e)
        return []
    articles = []

    def walk(items):
        for item in items:
            if isinstance(item, dict):
                actions = item.get("actions", [])
                title = item.get("title", {})
                if actions and isinstance(actions, list) and title and isinstance(title, dict):
                    uri = actions[0].get("uri", "") if len(actions) > 0 else ""
                    text = title.get("text", "")
                    if uri and text:
                        pub = item.get("publication", {}) or {}
                        ts = pub.get("timestamp", 0) if isinstance(pub, dict) else 0
                        articles.append({
                            "url": uri,
                            "title": text,
                            "timestamp": ts,
                        })
                for key in ("compositions", "items", "children"):
                    sub = item.get(key, [])
                    if isinstance(sub, list):
                        walk(sub)

    walk(compositions)
    return articles


def format_timestamp(ts_ms):
    if not ts_ms:
        return ""
    ts_s = ts_ms // 1000
    t = time.localtime(ts_s)
    return "%04d-%02d-%02d %02d:%02d" % (t[0], t[1], t[2], t[3], t[4])


def is_valid_article_url(url):
    excl = ["/liveblog/", "/kijk/", "sporza.be", "/audio/"]
    for e in excl:
        if e in url:
            logger.debug("Excluded by '%s': %s", e, url)
            return False
    if "vrt.be" not in url:
        logger.debug("Excluded (not vrt.be): %s", url)
        return False
    idx = url.find("/vrtnws/nl/")
    if idx == -1:
        logger.debug("Excluded (no /vrtnws/nl/): %s", url)
        return False
    after = url[idx + len("/vrtnws/nl/"):]
    if len(after) < 11:
        logger.debug("Excluded (after too short: %s): %s", after, url)
        return False
    if after[4] != "/" or after[7] != "/":
        logger.debug("Excluded (bad date separators: %s): %s", after, url)
        return False
    if not (after[:4].isdigit() and after[5:7].isdigit() and after[8:10].isdigit()):
        logger.debug("Excluded (non-digit in date: %s): %s", after, url)
        return False
    rest = after[10:]
    slug = rest.lstrip("/")
    if not slug:
        logger.debug("Excluded (no slug): %s", url)
        return False
    logger.debug("Accepted: %s", url)
    return True


def article_url_to_rss(url):
    qpos = url.find("?")
    if qpos != -1:
        url = url[:qpos]
    fpos = url.find("#")
    if fpos != -1:
        url = url[:fpos]
    if url.endswith("/"):
        url = url[:-1]
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://www.vrt.be" + url
    return url + ".rss.xml"


class VrtNewsActivity(Activity):
    def onCreate(self):
        if DEBUG:
            logger.info("VrtNewsActivity.onCreate started")
        self.articles = []
        self._loading = False
        self._article_loading = False
        self._saved_scroll_y = None
        self._saved_status = ""
        self._mode = "top"
        w = DisplayMetrics.width()
        h = DisplayMetrics.height()
        bar_h = 50
        if DEBUG:
            logger.info("VrtNewsActivity screen dims: %dx%d", w, h)

        screen = lv.obj()
        if DEBUG:
            logger.info("VrtNewsActivity screen obj created")

        bar = lv.obj(screen)
        bar.set_pos(0, 0)
        bar.set_size(w, bar_h)
        bar.set_style_border_width(0, 0)
        bar.set_style_pad_hor(10, 0)
        bar.set_flex_flow(lv.FLEX_FLOW.ROW)
        bar.set_style_flex_cross_place(lv.FLEX_ALIGN.CENTER, 0)
        bar.remove_flag(lv.obj.FLAG.SCROLLABLE)
        if DEBUG:
            logger.info("VrtNewsActivity bar created")

        title = lv.label(bar)
        title.set_text("VRT NWS")
        title.set_style_text_font(lv.font_montserrat_24, 0)
        title.set_flex_grow(1)

        self.mode_btn = lv.button(bar)
        self.mode_label = lv.label(self.mode_btn)
        self.mode_label.set_text("Toon laatste")
        self.mode_label.center()
        self.mode_btn.add_event_cb(lambda e: self._toggle_mode(), lv.EVENT.CLICKED, None)

        self.refresh_btn = lv.button(bar)
        refresh_label = lv.label(self.refresh_btn)
        refresh_label.set_text(lv.SYMBOL.REFRESH)
        refresh_label.center()
        self.refresh_btn.add_event_cb(lambda e: self.load_articles(), lv.EVENT.CLICKED, None)

        self.list_container = lv.obj(screen)
        self.list_container.set_pos(0, bar_h)
        self.list_container.set_size(w, h - bar_h - 30)
        self.list_container.add_flag(lv.obj.FLAG.SCROLLABLE)
        self.list_container.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        self.list_container.set_style_pad_all(5, 0)
        if DEBUG:
            logger.info("VrtNewsActivity list_container created")

        self.status_label = lv.label(screen)
        self.status_label.set_pos(10, h - 25)
        self.status_label.set_text("")
        if DEBUG:
            logger.info("VrtNewsActivity status_label created")

        self.setContentView(screen)
        if DEBUG:
            logger.info("VrtNewsActivity.onCreate done, calling load_articles")
        self.load_articles()

    def onResume(self, screen):
        if DEBUG:
            logger.info("VrtNewsActivity.onResume")
        if self._saved_scroll_y is not None:
            self.list_container.scroll_to_y(self._saved_scroll_y, False)
            self._saved_scroll_y = None

    def _toggle_mode(self):
        if DEBUG:
            logger.info("VrtNewsActivity._toggle_mode: %s -> %s", self._mode, "top" if self._mode == "latest" else "latest")
        self._mode = "top" if self._mode == "latest" else "latest"
        self.mode_label.set_text("Toon laatste" if self._mode == "top" else "Toon top")
        self.load_articles()

    def load_articles(self):
        if self._loading:
            if DEBUG:
                logger.info("VrtNewsActivity.load_articles skipped (already loading)")
            return
        if DEBUG:
            logger.info("VrtNewsActivity.load_articles mode=%s", self._mode)
        self._loading = True
        self._article_loading = True
        self.mode_btn.add_state(lv.STATE.DISABLED)
        self.refresh_btn.add_state(lv.STATE.DISABLED)
        if self._mode == "latest":
            self.status_label.set_text("Laatste artikels laden...")
        else:
            self.status_label.set_text("Top artikels ophalen...")
        if self._mode == "latest":
            TaskManager.create_task(self._do_fetch_latest())
        else:
            TaskManager.create_task(self._do_fetch_top())

    async def _do_fetch_latest(self):
        if DEBUG:
            logger.info("_do_fetch_latest: downloading %s", VRT_FEED_URL)
        gc.collect()
        if DEBUG:
            logger.info("_do_fetch_latest: free mem %d bytes", gc.mem_free())
        try:
            data = await DownloadManager.download_url(VRT_FEED_URL)
            if DEBUG:
                logger.info("_do_fetch_latest: got %d bytes", len(data) if data else 0)
            if not data:
                self.status_label.set_text("Leeg antwoord van VRT NWS")
                return
            xml_text = data.decode("utf-8")
            data = None
            if not xml_text:
                self.status_label.set_text("Leeg antwoord van VRT NWS")
                return
            if DEBUG:
                logger.info("_do_fetch_latest: decoded, len=%d", len(xml_text))
            self.articles = parse_atom_feed(xml_text)
            if DEBUG:
                logger.info("_do_fetch_latest: parsed %d articles", len(self.articles))
            before = len(self.articles)
            self.articles = [a for a in self.articles if is_valid_article_url(a.get("self_link", ""))]
            if DEBUG and len(self.articles) != before:
                logger.info("_do_fetch_latest: filtered %d -> %d articles", before, len(self.articles))
            if not self.articles:
                self.status_label.set_text("Geen artikels gevonden")
            else:
                self.status_label.set_text("Laatste artikels - %d artikels" % len(self.articles))
            self._build_article_list(self.articles)
        except Exception as e:
            logger.error("Failed to load feed: %s", e)
            self.status_label.set_text("Fout: %s" % e)
        finally:
            self._loading = False
            self._article_loading = False
            self.mode_btn.remove_state(lv.STATE.DISABLED)
            self.refresh_btn.remove_state(lv.STATE.DISABLED)
            if DEBUG:
                logger.info("_do_fetch_latest: done")

    async def _do_fetch_top(self):
        if DEBUG:
            logger.info("_do_fetch_top: downloading %s", VRT_FRONTPAGE_URL)
        gc.collect()
        if DEBUG:
            logger.info("_do_fetch_top: free mem %d bytes", gc.mem_free())
        try:
            logger.info("Fetching VRT front page...")
            data = await DownloadManager.download_url(VRT_FRONTPAGE_URL)
            if DEBUG:
                logger.info("_do_fetch_top: got %d bytes", len(data) if data else 0)
            if not data:
                self.status_label.set_text("Kon voorpagina niet laden")
                return
            html_text = data.decode("utf-8")
            data = None
            logger.info("Front page response: %d bytes", len(html_text))

            json_text = extract_next_data(html_text)
            if DEBUG:
                logger.info("_do_fetch_top: __NEXT_DATA__ found=%s", json_text is not None)
            if not json_text:
                self.status_label.set_text("Geen data gevonden op voorpagina")
                return
            html_text = None

            infos = extract_article_info(json_text)
            json_text = None
            logger.info("Extracted %d items from __NEXT_DATA__", len(infos))

            if infos:
                logger.info("First 5 items:")
                for info in infos[:5]:
                    logger.info("  [%s] %s", info["url"], info["title"])

            if not infos:
                self.status_label.set_text("Geen items gevonden")
                return

            valid = [info for info in infos if is_valid_article_url(info["url"])]
            logger.info("Valid articles after URL filtering: %d", len(valid))

            if not valid:
                self.status_label.set_text("Geen artikels gevonden op voorpagina")
                return

            articles = []
            for info in valid[:30]:
                rss_url = article_url_to_rss(info["url"])
                if DEBUG:
                    logger.info("RSS URL: %s", rss_url)
                title = info["title"]
                title = unescape_entities(title)
                title = replace_typographic_chars(title)
                article = {
                    "title": title,
                    "self_link": rss_url,
                    "link": info["url"],
                    "published": format_timestamp(info.get("timestamp", 0)),
                }
                articles.append(article)
            if DEBUG:
                logger.info("_do_fetch_top: built %d articles", len(articles))

            self.articles = articles
            self.status_label.set_text("Top artikels - %d artikels" % len(self.articles))
            self._build_article_list(self.articles)
            logger.info("Top article list built with %d items", len(articles))
        except Exception as e:
            logger.error("Failed to load top articles: %s", e)
            self.status_label.set_text("Fout: %s" % e)
        finally:
            self._loading = False
            self._article_loading = False
            self.mode_btn.remove_state(lv.STATE.DISABLED)
            self.refresh_btn.remove_state(lv.STATE.DISABLED)
            if DEBUG:
                logger.info("_do_fetch_top: done")

    def _build_article_list(self, articles):
        if DEBUG:
            logger.info("_build_article_list: %d articles", len(articles))
        self.list_container.clean()
        if DEBUG:
            logger.info("_build_article_list: container cleaned")
        if not articles:
            empty = lv.label(self.list_container)
            empty.set_text("Geen artikels")
            empty.set_width(lv.pct(100))
            empty.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
            if DEBUG:
                logger.info("_build_article_list: empty label shown")
            return

        for i, article in enumerate(articles):
            title = article.get("title", "Geen titel")
            pubdate = article.get("published", "")
            if DEBUG and i < 3:
                logger.info("_build_article_list article %d: title=%s", i, title)

            btn = lv.button(self.list_container)
            btn.set_width(lv.pct(100))
            btn.set_height(lv.SIZE_CONTENT)
            btn.set_style_pad_all(8, 0)
            btn.set_style_pad_hor(10, 0)
            btn.set_style_border_width(0, 0)
            btn.set_style_radius(4, 0)
            btn.set_style_bg_opa(lv.OPA._10, 0)
            btn.set_style_bg_color(lv.palette_main(lv.PALETTE.GREY), 0)
            btn.set_flex_flow(lv.FLEX_FLOW.COLUMN)

            title_label = lv.label(btn)
            title_label.set_text(title)
            title_label.set_width(lv.pct(100))
            title_label.set_height(50)
            title_label.set_long_mode(lv.label.LONG_MODE.WRAP)
            title_label.set_style_text_color(lv.color_hex(0x000000), 0)

            if pubdate:
                date_label = lv.label(btn)
                date_label.set_text(pubdate)
                date_label.set_style_text_color(lv.palette_main(lv.PALETTE.GREY), 0)

            btn.add_event_cb(lambda e, a=article: self._open_article(a), lv.EVENT.CLICKED, None)

            if i < len(articles) - 1:
                sep = lv.obj(self.list_container)
                sep.set_width(lv.pct(100))
                sep.set_height(1)
                sep.set_style_border_width(0, 0)
                sep.set_style_pad_all(0, 0)
                sep.set_style_radius(0, 0)
        if DEBUG:
            logger.info("_build_article_list: done, %d buttons created", len(articles))

    def _open_article(self, article):
        if self._article_loading:
            if DEBUG:
                logger.info("_open_article: skipped (already loading article)")
            return
        if DEBUG:
            logger.info("_open_article: title=%s", article.get("title", ""))
        self._saved_scroll_y = self.list_container.get_scroll_y()
        self._saved_status = self.status_label.get_text()
        self._article_loading = True
        self.status_label.set_text("Artikel laden...")
        TaskManager.create_task(self._fetch_and_show(article))

    async def _fetch_and_show(self, article):
        if DEBUG:
            logger.info("_fetch_and_show: starting for title=%s", article.get("title", ""))
        try:
            content = article.get("content", "")
            if not content:
                content = article.get("summary", "")
                rss_url = article.get("self_link", "")
                if DEBUG:
                    logger.info("_fetch_and_show: no content, fetching rss=%s", rss_url)
                if rss_url:
                    try:
                        data = await DownloadManager.download_url(rss_url)
                        if DEBUG:
                            logger.info("_fetch_and_show: rss response %d bytes", len(data) if data else 0)
                        if data:
                            if DEBUG:
                                t0 = time.ticks_ms()
                            xml_text = data.decode("utf-8")
                            if DEBUG:
                                logger.info("_fetch_and_show: decode took %dms, len=%d", time.ticks_diff(time.ticks_ms(), t0), len(xml_text))
                                t0 = time.ticks_ms()
                            entries = extract_entries(xml_text)
                            if DEBUG:
                                logger.info("_fetch_and_show: extract_entries took %dms, count=%d", time.ticks_diff(time.ticks_ms(), t0), len(entries))
                                t0 = time.ticks_ms()
                            if entries:
                                entry = entries[0]
                                pub = get_tag_content(entry, "published")
                                if pub:
                                    article["published"] = pub
                            if DEBUG:
                                logger.info("_fetch_and_show: get_tag_content took %dms", time.ticks_diff(time.ticks_ms(), t0))
                                t0 = time.ticks_ms()
                            fetched = get_article_content(xml_text)
                            if DEBUG:
                                logger.info("_fetch_and_show: get_article_content took %dms, fetched=%d", time.ticks_diff(time.ticks_ms(), t0), len(fetched) if fetched else 0)
                            if fetched:
                                content = fetched
                    except Exception as e:
                        logger.error("Failed to fetch article content: %s", e)
            intent = Intent(activity_class=VrtArticleActivity)
            intent.putExtra("title", article.get("title", ""))
            intent.putExtra("content", content)
            intent.putExtra("link", article.get("link", ""))
            intent.putExtra("pubDate", article.get("published", ""))
            self.startActivity(intent)
        finally:
            self._article_loading = False
            self.status_label.set_text(self._saved_status)


class VrtArticleActivity(Activity):
    def onCreate(self):
        if DEBUG:
            logger.info("VrtArticleActivity.onCreate started")
        intent = self.getIntent()
        title = intent.extras.get("title", "")
        content_text = intent.extras.get("content", "")
        pubdate = intent.extras.get("pubDate", "")
        if DEBUG:
            logger.info("VrtArticleActivity intent: title=%s, pubdate=%s, content_len=%d", title, pubdate, len(content_text))

        screen = lv.obj()
        if DEBUG:
            logger.info("VrtArticleActivity screen obj created")
        screen.add_flag(lv.obj.FLAG.SCROLLABLE)
        screen.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        screen.set_style_pad_all(10, 0)

        back_btn = lv.button(screen)
        back_label = lv.label(back_btn)
        back_label.set_text(lv.SYMBOL.LEFT)
        back_label.center()
        back_btn.add_event_cb(lambda e: self.finish(), lv.EVENT.CLICKED, None)

        title_label = lv.label(screen)
        title_label.set_text(title)
        title_label.set_width(lv.pct(100))
        title_label.set_long_mode(lv.label.LONG_MODE.WRAP)
        title_label.set_style_text_font(lv.font_montserrat_24, 0)

        if pubdate:
            date_label = lv.label(screen)
            date_label.set_text(pubdate)
            date_label.set_width(lv.pct(100))
            date_label.set_style_text_color(lv.palette_main(lv.PALETTE.GREY), 0)

        if content_text:
            if DEBUG:
                logger.info("VrtArticleActivity: creating %d paragraphs", len(content_text.split("\n")))
            paragraphs = content_text.split("\n")
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                label = lv.label(screen)
                label.set_text(para)
                label.set_width(lv.pct(100))
                label.set_long_mode(lv.label.LONG_MODE.WRAP)
                label.set_style_margin_bottom(4, 0)
                add_focus_border(label)
        else:
            if DEBUG:
                logger.info("VrtArticleActivity: no content to display")

        self.setContentView(screen)
        if DEBUG:
            logger.info("VrtArticleActivity.onCreate done")
