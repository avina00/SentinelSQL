from psycopg_pool import AsyncConnectionPool

EXCLUDED_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)

async def seed_database(pool: AsyncConnectionPool) -> None:
    """Creates sample e-commerce tables and seeds Indian market data if empty."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    customer_id SERIAL PRIMARY KEY,
                    full_name VARCHAR(100) NOT NULL,
                    email VARCHAR(120) UNIQUE NOT NULL,
                    city VARCHAR(50) NOT NULL,
                    tier VARCHAR(20) DEFAULT 'Standard'
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id SERIAL PRIMARY KEY,
                    customer_id INT REFERENCES customers(customer_id) ON DELETE CASCADE,
                    category VARCHAR(50) NOT NULL,
                    amount_inr NUMERIC(12, 2) NOT NULL,
                    payment_mode VARCHAR(30) NOT NULL,
                    order_status VARCHAR(30) DEFAULT 'Delivered',
                    order_date DATE DEFAULT CURRENT_DATE
                );
            """)

            await cur.execute("SELECT COUNT(*) AS cnt FROM customers;")
            row = await cur.fetchone()
            if row and row["cnt"] == 0:
                await cur.executemany(
                    "INSERT INTO customers (full_name, email, city, tier) VALUES (%s, %s, %s, %s)",
                    [
                        ("Aarav Sharma", "aarav@example.in", "Bengaluru", "Platinum"),
                        ("Diya Patel", "diya@example.in", "Mumbai", "Gold"),
                        ("Rohan Nair", "rohan@example.in", "Bengaluru", "Standard"),
                        ("Sneha Gupta", "sneha@example.in", "Delhi", "Gold"),
                        ("Vikram Reddy", "vikram@example.in", "Hyderabad", "Platinum"),
                    ],
                )
                await cur.executemany(
                    """INSERT INTO orders 
                       (customer_id, category, amount_inr, payment_mode, order_status, order_date) 
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    [
                        (1, "Electronics", 68500.00, "UPI", "Delivered", "2026-09-12"),
                        (1, "Accessories", 4200.00, "Credit Card", "Delivered", "2026-10-01"),
                        (2, "Apparel", 8900.00, "UPI", "Delivered", "2026-10-04"),
                        (3, "Electronics", 112000.00, "NetBanking", "Processing", "2026-10-15"),
                        (4, "Groceries", 3450.00, "UPI", "Cancelled", "2026-10-16"),
                        (5, "Electronics", 54000.00, "Credit Card", "Delivered", "2026-10-18"),
                    ],
                )

async def get_live_schema(pool: AsyncConnectionPool) -> str:
    """Introspects PostgreSQL information_schema to build a live DDL summary for the LLM."""
    query = """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name NOT IN ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations')
        ORDER BY table_name, ordinal_position;
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query)
            rows = await cur.fetchall()

    tables: dict[str, list[str]] = {}
    for r in rows:
        t_name = r["table_name"]
        col_def = f"{r['column_name']} ({r['data_type']}, nullable={r['is_nullable']})"
        tables.setdefault(t_name, []).append(col_def)

    schema_lines = []
    for table, cols in tables.items():
        schema_lines.append(f"Table `{table}`:\n  - " + "\n  - ".join(cols))
    return "\n\n".join(schema_lines)