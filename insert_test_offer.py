# insert_test_offer.py
import sqlite3, hashlib
from datetime import datetime
DB_PATH = "offers.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, title TEXT, url TEXT UNIQUE, price TEXT, shop TEXT, posted INTEGER DEFAULT 0,
    hash TEXT UNIQUE, discovered_at DATETIME
)""")
now = datetime.utcnow()
url = "https://exemplo.com/oferta-teste-" + now.strftime("%Y%m%d%H%M%S")
h = hashlib.sha256(url.encode()).hexdigest()
try:
    c.execute("INSERT INTO offers (source, title, url, price, shop, hash, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ("test", "Oferta de teste automática", url, "R$ 1,00", "Loja Teste", h, now))
    conn.commit()
    print("Oferta de teste inserida.")
except Exception as e:
    print("Erro ao inserir (talvez já exista):", e)
conn.close()
