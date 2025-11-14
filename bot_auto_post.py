# bot_auto_post.py
"""
Bot de ofertas filtrado para componentes/periféricos:
- posta apenas itens relevantes (componentes e periféricos) em promoção
- mensagem reduzida: foto (se houver) + título limpo + preço + botão
- remove a palavra "oferta" do título
- tenta buscar título real quando o bloco tem título genérico
- roda webserver mínimo (health/stats) para deploy
"""

import os
import sqlite3
import asyncio
import hashlib
import re
import html
import unicodedata
from time import monotonic
from datetime import datetime, timezone
from typing import List, Dict, Optional

import feedparser
from aiohttp import ClientSession, web
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

load_dotenv()

# ----------------- Config -----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
DB_PATH = os.getenv("DB_PATH", "offers.db")
# posta a cada 20 minutos por padrão
POST_INTERVAL_SECONDS = int(os.getenv("POST_INTERVAL_SECONDS", 60 * 20))
AFF_AMAZON_TAG = os.getenv("AFF_AMAZON_TAG")
AFF_KABUM = os.getenv("AFF_KABUM")
AFF_PICHAU = os.getenv("AFF_PICHAU")

SOURCES = ["https://www.promodo.com.br/feed/"]

# ----------------- FILTER (peças/componentes + periféricos) -----------------
DEFAULT_FILTER_KEYWORDS = [
    "placa de video", "placa de vídeo", "placa mãe", "placa mae", "motherboard",
    "gpu", "rtx", "gtx", "radeon", "rx", "vga",
    "ssd", "nvme", "m.2", "m2", "hd", "hdd",
    "memoria ram", "memória ram", "ram", "ddr4", "ddr5",
    "processador", "cpu", "cooler", "dissipador", "heatsink",
    "fonte", "psu", "gabinete", "case", "ventoinha", "fan",
    "placa de som", "placa de rede", "ssd nvme", "ssd sata", "ssd m2", "m.2 ssd",
    # periféricos que normalmente fazem sentido em canal de hardware
    "teclado", "mouse", "monitor", "headset", "webcam", "mousepad", "gabinete",
    "fonte atx", "watercooler", "ssd externo"
]

# blacklist (itens que queremos evitar)
DEFAULT_FILTER_BLACKLIST = [
    "notebook", "laptop", "smartphone", "celular", "impressora",
    "televis", "tv", "geladeira", "airfryer", "console", "cadeira",
    "figurine", "funko", "roupa", "sapato", "tênis", "tenis",
    "power bank", "powerbank", "barra de proteina", "proteina", "suplemento"
]

env_kw = os.getenv("FILTER_KEYWORDS")
env_black = os.getenv("FILTER_BLACKLIST")
if env_kw:
    FILTER_KEYWORDS = [k.strip().lower() for k in env_kw.split(",") if k.strip()]
else:
    FILTER_KEYWORDS = DEFAULT_FILTER_KEYWORDS
if env_black:
    FILTER_BLACKLIST = [k.strip().lower() for k in env_black.split(",") if k.strip()]
else:
    FILTER_BLACKLIST = DEFAULT_FILTER_BLACKLIST

def _normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = " ".join(s.split())
    return s

# ----------------- DB helpers -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        title TEXT,
        url TEXT UNIQUE,
        price TEXT,
        shop TEXT,
        image_url TEXT,
        coupon TEXT,
        posted INTEGER DEFAULT 0,
        hash TEXT UNIQUE,
        discovered_at DATETIME
      )
    """)
    conn.commit()
    conn.close()

def insert_offer(source: str, title: str, url: str, price: Optional[str]=None,
                 shop: Optional[str]=None, image_url: Optional[str]=None,
                 coupon: Optional[str]=None) -> bool:
    if not url:
        return False
    h = hashlib.sha256(url.encode('utf-8')).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
          INSERT INTO offers (source, title, url, price, shop, image_url, coupon, hash, discovered_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, title, url, price, shop, image_url, coupon, h, datetime.now(timezone.utc)))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_unposted_offers(limit=20) -> List[Dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
      SELECT id, title, url, price, shop, image_url, coupon FROM offers
      WHERE posted = 0
      ORDER BY discovered_at ASC
      LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "url": r[2], "price": r[3], "shop": r[4], "image_url": r[5], "coupon": r[6]} for r in rows]

def mark_as_posted(offer_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE offers SET posted = 1 WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()

def stats_counts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM offers")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM offers WHERE posted = 1")
    posted = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM offers WHERE posted = 0")
    unposted = c.fetchone()[0]
    conn.close()
    return total, posted, unposted

# ----------------- Helpers (price/coupon/promo) -----------------
PRICE_RE = re.compile(r"R\$\s?\d{1,3}(?:[\.\d{3}])*(?:,\d{2})?")
PRICE_RANGE_RE = re.compile(r"de\s*R\$\s?[\d\.\s,]+?\s*por\s*R\$\s?[\d\.\s,]+", re.IGNORECASE)
COUPON_RE = re.compile(r"(?:cupom|código|codigo|code|coupon)[:\s]*([A-Z0-9\-]{4,16})", re.IGNORECASE)

# promo detection: keywords or percent/off patterns
PROMO_WORDS = ["off", "%", "desconto", "promo", "promoção", "promoçao", "black friday", "oferta", "ofertas", "flash"]
PROMO_PERCENT_RE = re.compile(r"\d{1,3}\s?%")

async def fetch_text(url: str, session: ClientSession, timeout=20) -> Optional[str]:
    try:
        async with session.get(url, timeout=timeout, headers={"User-Agent": "OfertasBot/1.0 (+contato)"}) as resp:
            if resp.status != 200:
                print(f"fetch_text: {url} -> status {resp.status}")
                return None
            return await resp.text()
    except Exception as e:
        print("fetch_text error", url, e)
        return None

async def fetch_product_title(url: str, session: ClientSession, timeout=10) -> Optional[str]:
    """Tenta obter título 'real' da página do produto (og:title, meta title, h1, title)."""
    if not url:
        return None
    try:
        async with session.get(url, timeout=timeout, headers={"User-Agent": "OfertasBot/1.0 (+contato)"}) as resp:
            if resp.status != 200:
                return None
            html_text = await resp.text()
    except Exception:
        return None

    try:
        soup = BeautifulSoup(html_text, "html.parser")
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return clean_text(og.get("content"))
        mtitle = soup.find("meta", attrs={"name": "title"})
        if mtitle and mtitle.get("content"):
            return clean_text(mtitle.get("content"))
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return clean_text(h1.get_text(" ", strip=True))
        tit = soup.title
        if tit and tit.string:
            return clean_text(tit.string)
        h23 = soup.find(["h2", "h3"])
        if h23 and h23.get_text(strip=True):
            return clean_text(h23.get_text(" ", strip=True))
    except Exception:
        return None
    return None

GENERIC_TITLE_RE = re.compile(r"\b(oferta|promo(ca|ç)ao|promo|black friday|flash sale|ofertas?)\b", re.IGNORECASE)

def looks_like_generic_title(title: Optional[str]) -> bool:
    if not title:
        return True
    t = _normalize_text(title)
    if len(t) < 8:
        return True
    if GENERIC_TITLE_RE.search(t):
        return True
    return False

def clean_text(t: Optional[str]) -> Optional[str]:
    if not t:
        return None
    s = html.unescape(t)
    s = s.replace("\xa0", " ")
    s = " ".join(s.split())
    return s.strip() if s.strip() else None

def extract_price_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    txt = text.replace("\xa0", " ")
    m = PRICE_RE.search(txt)
    return m.group(0).replace("\xa0", " ").strip() if m else None

def extract_price_range(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    txt = text.replace("\xa0", " ")
    m = PRICE_RANGE_RE.search(txt)
    if m:
        pr = m.group(0)
        return " ".join(pr.split()).strip()
    return extract_price_from_text(txt)

def detect_coupon(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.replace("\xa0", " ")
    m = COUPON_RE.search(t)
    if m:
        return m.group(1).strip().upper()
    return None

def is_promotional(title: Optional[str], extra_text: Optional[str]) -> bool:
    """
    Verifica se o bloco/título contém indícios de promoção:
    - palavras-chave (promo, desconto, off, black friday...)
    - percentuais (e.g. '49%')
    - 'de ... por ...' pattern (PRICE_RANGE_RE)
    """
    combined = " ".join(filter(None, [title or "", extra_text or ""]))
    plain = _normalize_text(combined)
    # percent
    if PROMO_PERCENT_RE.search(plain):
        return True
    # price range pattern
    if PRICE_RANGE_RE.search(combined):
        return True
    # promo words
    for w in PROMO_WORDS:
        if w in plain:
            return True
    return False

def apply_affiliate(link: str, shop: Optional[str]) -> str:
    if not link or not shop:
        return link
    try:
        if "amazon.com.br" in link and AFF_AMAZON_TAG:
            if "tag=" not in link:
                sep = "&" if "?" in link else "?"
                return link + f"{sep}tag={AFF_AMAZON_TAG}"
        if "kabum.com.br" in link and AFF_KABUM:
            if "aff" not in link:
                sep = "&" if "?" in link else "?"
                return link + f"{sep}aff={AFF_KABUM}"
        if "pichau.com.br" in link and AFF_PICHAU:
            if "aff" not in link:
                sep = "&" if "?" in link else "?"
                return link + f"{sep}aff={AFF_PICHAU}"
    except Exception:
        pass
    return link

# ----------------- Extraction helpers -----------------
BAD_TITLES = {
    "ir para o conteúdo principal", "ir para o conteúdo", "ir para o contéudo principal",
    "skip to main content", "skip to content", "ir para o conteúdo principal »"
}

def find_link_title_image(elem, base_url: Optional[str] = None):
    title = None
    link = None
    image = None

    for a in elem.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().endswith("#main-content"):
            continue
        link = href
        title = a.get("title") or a.get("aria-label") or a.get("data-title") or a.get_text(strip=True)
        if not title:
            img = a.find("img")
            if img and img.get("alt"):
                title = img.get("alt")
        img = a.find("img")
        if img and img.get("src"):
            image = img.get("src")
        break

    if not title:
        for attr in ("data-title", "data-name", "aria-label", "title"):
            val = elem.get(attr)
            if val:
                title = val
                break

    if not title:
        h = elem.find(["h2", "h3", "h4"])
        if h:
            title = h.get_text(strip=True)

    if not title:
        txt = elem.get_text(" ", strip=True)
        if txt:
            title = txt.split("\n")[0].strip()

    title = clean_text(title) if title else None
    if title and title.lower() in BAD_TITLES:
        title = None

    if link and base_url:
        from urllib.parse import urljoin
        link = urljoin(base_url, link)
    if image and base_url:
        from urllib.parse import urljoin
        image = urljoin(base_url, image)

    return title, link, image

# ----------------- Collectors (filtragem por promo + relevância) -----------------
async def collect_kabum(session: ClientSession):
    urls = [
        "https://www.kabum.com.br/ofertas/ofertaskabum",
        "https://www.kabum.com.br/lojas/ofertas-do-dia",
        "https://www.kabum.com.br/promocao/OFERTAFLASH"
    ]
    for page in urls:
        html = await fetch_text(page, session)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for tag in soup.find_all(text=PRICE_RE):
            block = tag.parent
            for _ in range(4):
                if block is None:
                    break
                if block.find("a", href=True):
                    candidates.append(block)
                    break
                block = block.parent
        seen = set()
        for block in candidates:
            title, link, image = find_link_title_image(block, base_url=page)
            if not link:
                continue
            # tenta buscar título real se genérico
            if looks_like_generic_title(title):
                real_title = await fetch_product_title(link, session)
                if real_title:
                    title = real_title
                    print("Kabum: título genérico substituído ->", title)
            if link in seen:
                continue
            seen.add(link)

            extra_text = block.get_text(" ", strip=True) if block else ""
            price = extract_price_from_text(extra_text) or ""
            coupon = detect_coupon(extra_text) or ""

            # exige que seja promoção + item relevante + tenha preço
            if not price:
                print("Kabum: sem preço detectado, ignorando:", link)
                continue
            if not is_promotional(title, extra_text):
                print("Kabum: não parece promoção, ignorando:", title, link)
                continue
            if not is_relevant_offer(title, extra_text, link):
                print("Kabum: não é componente/periférico relevante, ignorando:", title, link)
                continue

            # limpa título e insere
            cleaned_title = clean_text(title) or ""
            if insert_offer("kabum", cleaned_title, link, price=price, shop="KaBuM", image_url=image, coupon=coupon):
                print("Kabum -> inserida:", cleaned_title, price, link)

async def collect_pichau(session: ClientSession):
    urls = [
        "https://www.pichau.com.br/promocao/",
        "https://www.pichau.com.br/ofertas/",
        "https://www.pichau.com.br/"
    ]
    for page in urls:
        html = await fetch_text(page, session)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for tag in soup.find_all(text=PRICE_RE):
            block = tag.parent
            for _ in range(4):
                if block is None:
                    break
                if block.find("a", href=True):
                    candidates.append(block)
                    break
                block = block.parent
        seen = set()
        for block in candidates:
            title, link, image = find_link_title_image(block, base_url=page)
            if not link:
                continue
            if looks_like_generic_title(title):
                real_title = await fetch_product_title(link, session)
                if real_title:
                    title = real_title
                    print("Pichau: título genérico substituído ->", title)
            if link in seen:
                continue
            seen.add(link)

            extra_text = block.get_text(" ", strip=True) if block else ""
            price = extract_price_from_text(extra_text) or ""
            coupon = detect_coupon(extra_text) or ""

            if not price:
                print("Pichau: sem preço detectado, ignorando:", link)
                continue
            if not is_promotional(title, extra_text):
                print("Pichau: não parece promoção, ignorando:", title, link)
                continue
            if not is_relevant_offer(title, extra_text, link):
                print("Pichau: não é componente/periférico relevante, ignorando:", title, link)
                continue

            cleaned_title = clean_text(title) or ""
            if insert_offer("pichau", cleaned_title, link, price=price, shop="Pichau", image_url=image, coupon=coupon):
                print("Pichau -> inserida:", cleaned_title, price, link)

async def collect_amazon(session: ClientSession):
    pages = [
        "https://www.amazon.com.br/deals",
        "https://www.amazon.com.br/gp/goldbox"
    ]
    for page in pages:
        html = await fetch_text(page, session)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/dp/" in href or "/gp/product/" in href:
                block = a.parent
                for _ in range(4):
                    if block is None:
                        break
                    if PRICE_RE.search(block.get_text(" ", strip=True) or ""):
                        candidates.append((a, block))
                        break
                    block = block.parent
        seen = set()
        for a, block in candidates:
            link = a.get("href")
            from urllib.parse import urljoin
            link = urljoin(page, link)
            if link in seen:
                continue

            title = a.get_text(strip=True) or (a.find("img") and a.find("img").get("alt")) or None
            title = clean_text(title) if title else None
            if looks_like_generic_title(title):
                real_title = await fetch_product_title(link, session)
                if real_title:
                    title = real_title
                    print("Amazon: título genérico substituído ->", title)

            image = None
            img = a.find("img")
            if img and img.get("src"):
                image = urljoin(page, img.get("src"))
            extra_text = block.get_text(" ", strip=True) if block else ""
            price = extract_price_from_text(extra_text) or ""
            coupon = detect_coupon(extra_text) or ""

            if not price:
                print("Amazon: sem preço detectado, ignorando:", link)
                continue
            if not is_promotional(title, extra_text):
                print("Amazon: não parece promoção, ignorando:", title, link)
                continue
            if not is_relevant_offer(title, extra_text, link):
                print("Amazon: não é componente/periférico relevante, ignorando:", title, link)
                continue

            cleaned_title = clean_text(title) or ""
            if insert_offer("amazon", cleaned_title, link, price=price, shop="Amazon", image_url=image, coupon=coupon):
                print("Amazon -> inserida:", cleaned_title, price, link)
            seen.add(link)

# ----------------- collector job -----------------
async def collector_job():
    async with ClientSession(headers={"User-Agent": "OfertasBot/1.0 (+contato)"}) as session:
        start = monotonic()
        try:
            await collect_kabum(session)
            await asyncio.sleep(1)
            await collect_pichau(session)
            await asyncio.sleep(1)
            await collect_amazon(session)
        except Exception as e:
            print("collector_job error:", e)
        elapsed = monotonic() - start
        print(f"collector_job finalizado em {elapsed:.1f}s")

# ----------------- Posting (photo + title + price + button) -----------------
def make_offer_keyboard(original_url: str, shop: Optional[str]):
    link = apply_affiliate(original_url, shop)
    keyboard = [
        [InlineKeyboardButton("Ver oferta 🛍️", url=link)]
    ]
    return InlineKeyboardMarkup(keyboard)

# title cleaning: remove 'oferta' / promo words from title display
_REMOVE_PROMO_TOKENS_RE = re.compile(r"\b(oferta|ofertas|promo(ca|ç)ao|promo|black friday|flash sale|-\s*\d{1,3}%|\\\d+%|[0-9]{1,3}\s?%|off)\b", re.IGNORECASE)

def clean_title_for_display(raw_title: Optional[str]) -> str:
    t = (raw_title or "").strip()
    if not t:
        return "Produto"
    t = html.unescape(t)
    t = t.replace("\xa0", " ")
    t = t.replace("\r", " ").replace("\n", " ")
    t = " ".join(t.split())
    # remove promo tokens like "oferta", "black friday", "49% off"
    t = _REMOVE_PROMO_TOKENS_RE.sub("", t)
    # remove price fragments like "De: R$ ... Por: R$ ..."
    t = re.sub(r"\bDe:\s*R\$\s?[\d\.\s,]+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bPor:\s*R\$\s?[\d\.\s,]+", "", t, flags=re.IGNORECASE)
    # collapse spaces/punctuation
    t = " ".join(t.split()).strip()
    t = t.strip(" -–—:;/")
    return t if t else (raw_title or "Produto")

async def post_offers_loop(bot: Bot):
    """
    Post format:
    [photo if available]
    🔥 Title (cleaned)
    💸 Price: R$...
    [button]
    """
    while True:
        try:
            to_post = get_unposted_offers(limit=10)
            if not to_post:
                await asyncio.sleep(10)
                continue

            for offer in to_post:
                title = (offer.get("title") or "").strip()
                url = offer.get("url") or ""
                # skip test offers
                if title and ("teste" in _normalize_text(title) or "insert_test" in _normalize_text(title)):
                    mark_as_posted(offer["id"])
                    print("Ignorado: oferta de teste:", title, url)
                    continue

                # ensure price exists and offer is promotional (should already be true at insertion)
                price_text = offer.get("price") or ""
                if not price_text:
                    print("Ignorado no post: sem preço:", title, url)
                    mark_as_posted(offer["id"])
                    continue

                # final safety: relevant + promo (double check)
                if not is_relevant_offer(title, "", url):
                    print("Ignorado no post: não relevante:", title, url)
                    mark_as_posted(offer["id"])
                    continue
                if not is_promotional(title, ""):
                    # try using price text for promotion detection
                    if not is_promotional(title, price_text):
                        print("Ignorado no post: não promocional:", title, url)
                        mark_as_posted(offer["id"])
                        continue

                # clean title for display (remove promo words)
                display_title = clean_title_for_display(title)
                display_title = display_title.replace("`", "").replace("[", "").replace("]", "").replace("*", "").strip()
                # assemble message: title + price only
                message = f"*{display_title}*\n💸 *Preço:* {price_text}"

                keyboard = make_offer_keyboard(url, offer.get("shop"))

                try:
                    if offer.get("image_url"):
                        try:
                            await bot.send_photo(chat_id=TARGET_CHAT_ID, photo=offer["image_url"], caption=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                        except TelegramError as te_photo:
                            print("send_photo falhou, fallback para send_message:", te_photo)
                            await bot.send_message(chat_id=TARGET_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                    else:
                        await bot.send_message(chat_id=TARGET_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
                    mark_as_posted(offer["id"])
                    print("Postado:", display_title, price_text, url)
                    await asyncio.sleep(2)
                except TelegramError as te:
                    print("Erro ao postar oferta:", te)
                    await asyncio.sleep(5)

            await asyncio.sleep(POST_INTERVAL_SECONDS)
        except Exception as e:
            print("Erro no post_offers_loop:", e)
            await asyncio.sleep(10)

# ----------------- Webserver (health + stats) -----------------
async def handle_health(request):
    return web.Response(text="OK")

async def handle_stats(request):
    total, posted, unposted = stats_counts()
    data = {"total": total, "posted": posted, "unposted": unposted}
    return web.json_response(data)

async def start_webserver(port: int):
    aio_app = web.Application()
    aio_app.router.add_get("/", handle_health)
    aio_app.router.add_get("/health", handle_health)
    aio_app.router.add_get("/stats", handle_stats)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Webserver rodando em 0.0.0.0:{port}")

# ----------------- Main -----------------
async def main():
    if not TELEGRAM_TOKEN or not TARGET_CHAT_ID:
        raise SystemExit("Configure TELEGRAM_TOKEN e TARGET_CHAT_ID no .env")
    init_db()
    bot = Bot(token=TELEGRAM_TOKEN)

    async def periodic_collect():
        while True:
            try:
                print("Coletando fontes...")
                await collector_job()
            except Exception as e:
                print("Erro coletor:", e)
            await asyncio.sleep(60 * 10)

    port = int(os.environ.get("PORT", "10000"))

    await asyncio.gather(
        start_webserver(port),
        periodic_collect(),
        post_offers_loop(bot)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrompido pelo usuário")
