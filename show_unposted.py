import sqlite3

DB_PATH = "offers.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
SELECT id, title, url, price, shop, posted, discovered_at
FROM offers
ORDER BY discovered_at DESC LIMIT 30
""")

rows = c.fetchall()

for r in rows:
    print(r)

conn.close()
