"""
Standalone helper to (re)create the database schema without starting
the bot or the forward engine:

    python -m database.schema
"""

from database.db import init_db

if __name__ == "__main__":
    init_db()
    print("Database schema created/verified successfully.")
