# insert_test_offer_with_image.py
import sqlite3, hashlib
from datetime import datetime, timezone
DB_PATH = "offers.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, title TEXT, url TEXT UNIQUE, price TEXT, shop TEXT, image_url TEXT, coupon TEXT, posted INTEGER DEFAULT 0,
    hash TEXT UNIQUE, discovered_at DATETIME
)""")
now = datetime.now(timezone.utc)
url = "https://exemplo.com/oferta-imagem-" + now.strftime("%Y%m%d%H%M%S")
h = hashlib.sha256(url.encode()).hexdigest()
try:
    c.execute("INSERT INTO offers (source, title, url, price, shop, image_url, coupon, hash, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              ("test", "Oferta TEST imagem + cupom", url, "R$ 9,99", "Loja Teste", "https://via.placeholder.com/400x300.png?text=Oferta", "TEST10", h, now))
    conn.commit()
    print("Oferta de teste com imagem inserida.")
except Exception as e:
    print("Erro ao inserir:", e)
conn.close()
