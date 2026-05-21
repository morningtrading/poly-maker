"""
SQLite adapter — primary config / market store for the bot.

Objective: Provide a worksheet/get_all_records-style API over SQLite tables
so the rest of the codebase reads markets and hyperparameters from polymb.db.
Rational: A local DB removes API auth, quota, and network failure modes.
Dependencies: sqlite3 (stdlib), pandas
Expected output: Adapter object with worksheet(name).get_all_records() interface
Test: see test_sqlite_integration.py
"""

import sqlite3
import os
import pandas as pd
from contextlib import contextmanager


class SQLiteAdapter:
    """Top-level handle on the SQLite config store."""
    
    def __init__(self, db_path='polymb.db'):
        """
        Initialize adapter with database path.
        
        Args:
            db_path: Path to SQLite database file (created if missing)
        """
        self.db_path = db_path
        self.title = f"SQLite:{db_path}"
        
        # Create/connect DB on init
        self._ensure_db()
    
    def _ensure_db(self):
        """Initialize database and schema if not present."""
        is_new = not os.path.exists(self.db_path)
        
        with self._get_conn() as conn:
            if is_new:
                self._create_schema(conn)
                print(f"[sqlite_adapter] Created new database: {self.db_path}")
            else:
                print(f"[sqlite_adapter] Connected to existing database: {self.db_path}")
    
    @contextmanager
    def _get_conn(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    @staticmethod
    def _create_schema(conn):
        """Create base tables for poly-maker workflow.

        Table names use the underscore form (selected_markets, all_markets, ...)
        because that is what SQLiteAdapter.worksheet()'s name_map resolves
        human-readable names to. Keep both in sync.

        Columns are kept permissive (the data-fetch path in
        update_markets.py writes a wide set of columns including time-window
        volatility metrics like "1_hour" .. "30_day"); the schema declares
        each one explicitly so pandas to_sql(if_exists='append') can match.
        """
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS selected_markets (
                id INTEGER PRIMARY KEY,
                question TEXT NOT NULL UNIQUE,
                answer1 TEXT,
                answer2 TEXT,
                token1 TEXT,
                token2 TEXT,
                condition_id TEXT,
                neg_risk TEXT,
                trade_size REAL,
                max_size REAL,
                min_size REAL,
                tick_size REAL,
                max_spread REAL,
                best_bid REAL,
                best_ask REAL,
                volatility_price REAL,
                param_type TEXT,
                multiplier TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS all_markets (
                id INTEGER PRIMARY KEY,
                question TEXT,
                answer1 TEXT,
                answer2 TEXT,
                token1 TEXT,
                token2 TEXT,
                condition_id TEXT,
                neg_risk TEXT,
                spread REAL,
                rewards_daily_rate REAL,
                gm_reward_per_100 REAL,
                sm_reward_per_100 REAL,
                bid_reward_per_100 REAL,
                ask_reward_per_100 REAL,
                volatility_sum REAL,
                "volatilty/reward" TEXT,
                min_size REAL,
                "1_hour" REAL,
                "3_hour" REAL,
                "6_hour" REAL,
                "12_hour" REAL,
                "24_hour" REAL,
                "7_day" REAL,
                "30_day" REAL,
                best_bid REAL,
                best_ask REAL,
                volatility_price REAL,
                max_spread REAL,
                tick_size REAL,
                market_slug TEXT,
                volume_24hr REAL,
                end_date_iso TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hyperparameters (
                id INTEGER PRIMARY KEY,
                type TEXT,
                param TEXT,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS summary (
                id INTEGER PRIMARY KEY,
                question TEXT,
                answer TEXT,
                order_size REAL,
                position_size REAL,
                marketInSelected INTEGER,
                earnings REAL,
                earning_percentage TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS full_markets (
                id INTEGER PRIMARY KEY,
                question TEXT,
                answer1 TEXT,
                answer2 TEXT,
                neg_risk TEXT,
                spread REAL,
                best_bid REAL,
                best_ask REAL,
                rewards_daily_rate REAL,
                bid_reward_per_100 REAL,
                ask_reward_per_100 REAL,
                gm_reward_per_100 REAL,
                sm_reward_per_100 REAL,
                min_size REAL,
                max_spread REAL,
                tick_size REAL,
                market_slug TEXT,
                token1 TEXT,
                token2 TEXT,
                condition_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS volatility_markets (
                id INTEGER PRIMARY KEY,
                question TEXT,
                answer1 TEXT,
                answer2 TEXT,
                spread REAL,
                rewards_daily_rate REAL,
                gm_reward_per_100 REAL,
                sm_reward_per_100 REAL,
                bid_reward_per_100 REAL,
                ask_reward_per_100 REAL,
                volatility_sum REAL,
                "volatilty/reward" TEXT,
                min_size REAL,
                "1_hour" REAL,
                "3_hour" REAL,
                "6_hour" REAL,
                "12_hour" REAL,
                "24_hour" REAL,
                "7_day" REAL,
                "30_day" REAL,
                best_bid REAL,
                best_ask REAL,
                volatility_price REAL,
                max_spread REAL,
                tick_size REAL,
                neg_risk TEXT,
                market_slug TEXT,
                token1 TEXT,
                token2 TEXT,
                condition_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("[sqlite_adapter] Database schema created")
    
    def worksheet(self, name):
        """
        Get worksheet-like object for a table.
        
        Args:
            name: Table name (e.g., 'Selected Markets')
            
        Returns:
            SQLiteWorksheet object
            
        Raises:
            ValueError: If table does not exist
        """
        # Normalize human-readable table names to actual SQLite identifiers.
        name_map = {
            'Selected Markets': 'selected_markets',
            'selected_markets': 'selected_markets',
            'All Markets': 'all_markets',
            'all_markets': 'all_markets',
            'Hyperparameters': 'hyperparameters',
            'hyperparameters': 'hyperparameters',
            'Summary': 'summary',
            'summary': 'summary',
            'Full Markets': 'full_markets',
            'full_markets': 'full_markets',
            'Volatility Markets': 'volatility_markets',
            'volatility_markets': 'volatility_markets',
        }
        
        # Get the actual table name
        actual_name = name_map.get(name, name.lower().replace(' ', '_'))
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (actual_name,)
            )
            if not cursor.fetchone():
                raise ValueError(f"Table '{name}' (mapped to '{actual_name}') does not exist")
        
        return SQLiteWorksheet(self.db_path, actual_name)


class SQLiteWorksheet:
    """Worksheet-like object wrapping a SQLite table."""
    
    def __init__(self, db_path, table_name):
        """
        Initialize worksheet for a table.
        
        Args:
            db_path: Path to database file
            table_name: Name of table to wrap
        """
        self.db_path = db_path
        self.table_name = table_name
    
    def get_all_records(self):
        """
        Return all rows as a list of dicts (one dict per row).
        """
        with self._get_conn() as conn:
            df = pd.read_sql_query(
                f"SELECT * FROM [{self.table_name}] ORDER BY id",
                conn
            )

        records = df.to_dict('records')
        print(f"[sqlite_adapter] {self.table_name}: fetched {len(records)} records")
        return records
    
    def get_all_values(self):
        """
        Return all rows as a list of lists, with the column headers as the first row.
        """
        with self._get_conn() as conn:
            df = pd.read_sql_query(
                f"SELECT * FROM [{self.table_name}] ORDER BY id",
                conn
            )
        
        # Return as list of lists with headers
        result = [df.columns.tolist()] + df.values.tolist()
        return result
    
    def clear(self):
        """Delete all rows from table."""
        with self._get_conn() as conn:
            conn.execute(f"DELETE FROM [{self.table_name}]")
        print(f"[sqlite_adapter] {self.table_name}: cleared")
    
    def update(self, df):
        """
        Replace entire table with DataFrame contents.
        
        Args:
            df: pandas DataFrame to insert
        """
        with self._get_conn() as conn:
            # Clear existing data
            conn.execute(f"DELETE FROM [{self.table_name}]")
            
            # Insert new data
            df.to_sql(
                self.table_name,
                conn,
                if_exists='append',
                index=False
            )
        
        print(f"[sqlite_adapter] {self.table_name}: updated with {len(df)} rows")
    
    @contextmanager
    def _get_conn(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
