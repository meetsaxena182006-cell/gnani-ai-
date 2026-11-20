"""Fleet Analyst: natural-language questions over fleet history.
Runs on BigQuery (dataset saarthi_fleet) when BIGQUERY_ENABLED=1, otherwise on a local SQLite mirror with the
same schema (used while Cloud billing is pending). Data is SYNTHETIC: 90 days of simulated trips."""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from datetime import date, timedelta

from . import gemini
from .fleet import CITIES, DIESEL_RS_PER_L, TOLL_RS_PER_KM, road_km

SCHEMA = """Table trips (one row per completed trip, last 90 days):
  trip_id TEXT, trip_date DATE (YYYY-MM-DD), truck_id TEXT, driver TEXT, origin TEXT, destination TEXT,
  loaded_km REAL, empty_km REAL, freight_rs INTEGER, diesel_litres REAL, diesel_rs INTEGER, toll_rs INTEGER,
  driver_rs INTEGER, detention_hours REAL, on_time INTEGER (1/0), breakdown INTEGER (1/0)
Derived: profit_rs = freight_rs - diesel_rs - toll_rs - driver_rs ; mileage_kmpl = (loaded_km+empty_km)/diesel_litres"""

PRESETS = {
    "profit": ("Profit by truck (last 30 days)",
               "SELECT truck_id, SUM(freight_rs-diesel_rs-toll_rs-driver_rs) AS profit_rs FROM trips "
               "WHERE trip_date >= DATE('now','-30 day') GROUP BY truck_id ORDER BY profit_rs ASC", "bar"),
    "diesel": ("Mileage by truck (km per litre)",
               "SELECT truck_id, ROUND(SUM(loaded_km+empty_km)/SUM(diesel_litres),2) AS kmpl FROM trips "
               "GROUP BY truck_id ORDER BY kmpl ASC", "bar"),
    "empty": ("Empty kilometres by truck",
              "SELECT truck_id, ROUND(SUM(empty_km)) AS empty_km FROM trips GROUP BY truck_id ORDER BY empty_km DESC", "bar"),
    "ontime": ("On-time delivery % by route",
               "SELECT origin||'-'||destination AS route, ROUND(100.0*AVG(on_time),1) AS on_time_pct FROM trips "
               "GROUP BY route HAVING COUNT(*)>=8 ORDER BY on_time_pct ASC LIMIT 10", "bar"),
    "detention": ("Detention hours by destination",
                  "SELECT destination, ROUND(SUM(detention_hours),1) AS detention_h FROM trips GROUP BY destination "
                  "ORDER BY detention_h DESC", "bar"),
    "trend": ("Weekly profit trend",
              "SELECT strftime('%Y-W%W', trip_date) AS week, SUM(freight_rs-diesel_rs-toll_rs-driver_rs) AS profit_rs "
              "FROM trips GROUP BY week ORDER BY week", "line"),
    "breakdown": ("Breakdowns by truck",
                  "SELECT truck_id, SUM(breakdown) AS breakdowns FROM trips GROUP BY truck_id ORDER BY breakdowns DESC", "bar"),
}
KEYWORDS = [("nuksaan|loss|profit|munafa|kamai|earning", "profit"), ("diesel|mileage|fuel|chori|theft", "diesel"),
            ("empty|khaali|khali", "empty"), ("on.?time|late|delay", "ontime"), ("detention|wait|ruk", "detention"),
            ("trend|week|hafta", "trend"), ("breakdown|kharab|repair", "breakdown")]


class Analyst:
    def __init__(self, trucks: dict):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.bq = os.getenv("BIGQUERY_ENABLED", "").lower() in ("1", "true")
        self._seed(trucks)

    def _seed(self, trucks):
        rng = random.Random(42)
        self.db.execute("""CREATE TABLE trips (trip_id TEXT, trip_date DATE, truck_id TEXT, driver TEXT, origin TEXT,
            destination TEXT, loaded_km REAL, empty_km REAL, freight_rs INTEGER, diesel_litres REAL, diesel_rs INTEGER,
            toll_rs INTEGER, driver_rs INTEGER, detention_hours REAL, on_time INTEGER, breakdown INTEGER)""")
        lanes = [("Chittorgarh", "Ahmedabad"), ("Jaipur", "Delhi"), ("Udaipur", "Mumbai"), ("Indore", "Surat"),
                 ("Bhilwara", "Pune"), ("Kota", "Vadodara"), ("Ajmer", "Indore"), ("Chittorgarh", "Jaipur"),
                 ("Surat", "Chittorgarh"), ("Ahmedabad", "Udaipur"), ("Delhi", "Jaipur"), ("Vadodara", "Bhilwara")]
        rows, n = [], 0
        ids = list(trucks)
        for d in range(90, 0, -1):
            day = (date.today() - timedelta(days=d)).isoformat()
            for i, tid in enumerate(ids):
                if rng.random() > 0.62:
                    continue
                o, de = rng.choice(lanes)
                if rng.random() < 0.5:
                    o, de = de, o
                km = road_km(o, de)
                empty = km * (rng.uniform(0.35, 0.8) if i == 6 else rng.uniform(0.0, 0.35))
                kmpl = rng.uniform(3.4, 3.9)
                if i == 2:            # anomalous diesel use (leak or pilferage signal)
                    kmpl *= 0.78
                litres = (km + empty) / kmpl
                freight = int(km * rng.uniform(50, 66))
                det = max(0.0, rng.gauss(2.5 if de in ("Surat", "Mumbai") else 0.8, 1.5))
                n += 1
                rows.append((f"T{n:05d}", day, tid, trucks[tid]["driver"], o, de, km, round(empty, 1), freight,
                             round(litres, 1), int(litres * DIESEL_RS_PER_L), int(km * TOLL_RS_PER_KM),
                             int(max(1, km / 450) * 900), round(det, 1),
                             int(rng.random() > (0.28 if de in ("Delhi", "Mumbai") else 0.1)),
                             int(rng.random() < (0.12 if i in (0, 3) else 0.03))))
        self.db.executemany("INSERT INTO trips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def export_csv(self, path: str):
        import csv
        cur = self.db.execute("SELECT * FROM trips")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([c[0] for c in cur.description])
            w.writerows(cur.fetchall())

    def _run(self, sql: str):
        if self.bq:
            from google.cloud import bigquery  # optional dependency
            ds = os.getenv("BQ_DATASET", "saarthi_fleet")
            client = bigquery.Client()
            bq_sql = re.sub(r"\btrips\b", f"`{client.project}.{ds}.trips`", sql)
            res = client.query(bq_sql).result()
            cols = [f.name for f in res.schema]
            return cols, [list(r.values()) for r in res][:60]
        cur = self.db.execute(sql)
        cols = [c[0] for c in cur.description]
        return cols, [list(r) for r in cur.fetchall()[:60]]

    @staticmethod
    def _safe(sql: str):
        s = sql.strip().rstrip(";")
        if ";" in s or not re.match(r"(?is)^\s*(select|with)\b", s):
            return None
        if re.search(r"(?i)\b(insert|update|delete|drop|alter|attach|pragma|create)\b", s):
            return None
        return s

    async def ask(self, question: str):
        plan = None
        if gemini.ready():
            raw = await gemini.generate(
                f"{SCHEMA}\nDialect: SQLite. Today is {date.today()}. Write ONE read-only SQL query answering: "
                f"\"{question}\"\nReturn JSON: {{\"sql\": str, \"title\": str, \"chart\": \"bar\"|\"line\"|\"none\"}}. "
                "First column = label, second column = numeric value when a chart makes sense. Max 15 rows.",
                system="You are a precise analytics engineer.", model="fast", timeout=12, json_mode=True)
            try:
                plan = json.loads(raw) if raw else None
            except Exception:
                plan = None
        source = "Gemini text-to-SQL"
        if plan and self._safe(plan.get("sql", "")):
            title, sql, chart = plan.get("title") or question, self._safe(plan["sql"]), plan.get("chart", "bar")
            try:
                cols, rows = self._run(sql)
            except Exception:
                plan = None
        if not plan or not self._safe(plan.get("sql", "")):
            key = next((k for pat, k in KEYWORDS if re.search(pat, question, re.I)), "profit")
            title, sql, chart = PRESETS[key]
            cols, rows = self._run(sql)
            source = "preset query"
        answer = await gemini.generate(
            f"Question: {question}\nResult columns: {cols}\nRows: {rows[:15]}\n"
            "Answer the fleet owner in 2 short sentences with the key numbers and one action to take. "
            "Rupees as Rs. Simple English, a little Hinglish is fine.", model="fast", timeout=10)
        if not answer:
            if rows:
                v = rows[0][1]
                val = f"Rs {int(v):,}" if isinstance(v, (int, float)) and abs(v) >= 1000 else str(v)
                low = re.search(r"loss|nuksaan|least|lowest|sabse kam", question.lower()) and "truck" in title.lower()
                answer = (f"{rows[0][0]} earned the least: {val} profit in 30 days, lowest in the fleet. "
                          f"Inspect its diesel use and empty kilometres first." if low else
                          f"{title}: {rows[0][0]} stands out at {val}. The chart shows the full picture.")
            else:
                answer = "No data for that question."
        return {"question": question, "title": title, "sql": sql, "columns": cols, "rows": rows, "chart": chart,
                "answer": answer, "engine": "BigQuery" if self.bq else "local mirror (BigQuery schema)", "via": source}
