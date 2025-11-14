# migrate_db_add_columns.py
import sqlite3

DB_PATH = "offers.db"

def column_exists(conn, table, column):
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]  # name is at index 1
    return column in cols

def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        if not column_exists(conn, "offers", "image_url"):
            print("Adicionando coluna image_url...")
            conn.execute("ALTER TABLE offers ADD COLUMN image_url TEXT")
        else:
            print("Coluna image_url já existe.")
        if not column_exists(conn, "offers", "coupon"):
            print("Adicionando coluna coupon...")
            conn.execute("ALTER TABLE offers ADD COLUMN coupon TEXT")
        else:
            print("Coluna coupon já existe.")
        conn.commit()
        print("Migração concluída.")
    except Exception as e:
        print("Erro na migração:", e)
    finally:
        conn.close()

if __name__ == '__main__':
    main()
