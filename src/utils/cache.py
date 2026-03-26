"""Cache management for translations using SQLite"""
import sqlite3
import json
import time
from pathlib import Path
from datetime import datetime, timedelta


class TranslationCache:
    """SQLite-based cache for translations"""

    def __init__(self, db_path="cache.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database schema"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_text TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    translation_mode TEXT NOT NULL DEFAULT 'default',
                    translated_text TEXT NOT NULL,
                    confidence REAL,
                    cloud_refined TEXT,
                    timestamp REAL NOT NULL,
                    UNIQUE(source_text, source_lang, target_lang, translation_mode)
                )
                """
            )
            cursor.execute("PRAGMA table_info(translations)")
            columns = {row[1] for row in cursor.fetchall()}
            if "translation_mode" not in columns:
                cursor.execute(
                    "ALTER TABLE translations ADD COLUMN translation_mode TEXT NOT NULL DEFAULT 'default'"
                )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_langs
                ON translations(source_lang, target_lang, translation_mode)
                """
            )
            conn.commit()

    def get(self, source_text, source_lang, target_lang, translation_mode="default"):
        """
        Get cached translation

        Returns:
            dict with 'translated_text', 'confidence', 'cloud_refined' or None
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT translated_text, confidence, cloud_refined
                FROM translations
                WHERE source_text = ? AND source_lang = ? AND target_lang = ? AND translation_mode = ?
                """,
                (source_text, source_lang, target_lang, translation_mode),
            )
            result = cursor.fetchone()

            if result:
                return {
                    "translated_text": result[0],
                    "confidence": result[1],
                    "cloud_refined": result[2],
                }
            return None

    def get_best(self, source_text, source_lang, target_lang, translation_mode="default"):
        """Get the best cached translation, preferring cloud-refined text when present."""
        cached = self.get(source_text, source_lang, target_lang, translation_mode=translation_mode)
        if not cached:
            return None

        best_text = cached["cloud_refined"] or cached["translated_text"]
        return {
            "translated_text": best_text,
            "confidence": cached["confidence"],
            "cloud_refined": cached["cloud_refined"],
            "base_translation": cached["translated_text"],
        }

    def set(
        self,
        source_text,
        source_lang,
        target_lang,
        translated_text,
        confidence=0.95,
        translation_mode="default",
    ):
        """Cache a translation"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO translations
                (source_text, source_lang, target_lang, translation_mode, translated_text, confidence, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_text,
                    source_lang,
                    target_lang,
                    translation_mode,
                    translated_text,
                    confidence,
                    time.time(),
                ),
            )
            conn.commit()

    def set_cloud_refinement(
        self,
        source_text,
        source_lang,
        target_lang,
        cloud_refined,
        translation_mode="default",
    ):
        """Update cache with cloud-refined translation"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE translations
                SET cloud_refined = ?
                WHERE source_text = ? AND source_lang = ? AND target_lang = ? AND translation_mode = ?
                """,
                (cloud_refined, source_text, source_lang, target_lang, translation_mode),
            )
            conn.commit()

    def get_recent(self, limit=10):
        """Get recent translations"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT source_text, source_lang, target_lang, translated_text, timestamp
                FROM translations
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            return cursor.fetchall()

    def clear_old(self, days=7):
        """Clear cache entries older than specified days"""
        cutoff_time = time.time() - (days * 24 * 3600)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM translations WHERE timestamp < ?", (cutoff_time,))
            conn.commit()
            return cursor.rowcount

    def clear(self):
        """Clear entire cache"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM translations")
            conn.commit()

    def get_size(self):
        """Get cache size in MB"""
        return round(self.db_path.stat().st_size / (1024 * 1024), 2)

    def get_stats(self):
        """Get cache statistics"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM translations")
            count = cursor.fetchone()[0]

            cursor.execute(
                "SELECT DISTINCT source_lang, target_lang, translation_mode FROM translations ORDER BY source_lang"
            )
            language_pairs = cursor.fetchall()

        return {
            "total_entries": count,
            "size_mb": self.get_size(),
            "language_pairs": [
                {"source": pair[0], "target": pair[1], "mode": pair[2]}
                for pair in language_pairs
            ],
        }
