"""Synthetic fleet world for Saarthi AI. ALL DATA HERE IS SIMULATED (demo only)."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

CITIES = {
    "Chittorgarh": (24.8887, 74.6269), "Udaipur": (24.5854, 73.7125), "Jaipur": (26.9124, 75.7873),
    "Ahmedabad": (23.0225, 72.5714), "Delhi": (28.6139, 77.2090), "Mumbai": (19.0760, 72.8777),
    "Indore": (22.7196, 75.8577), "Surat": (21.1702, 72.8311), "Kota": (25.2138, 75.8648),
    "Bhilwara": (25.3463, 74.6364), "Vadodara": (22.3072, 73.1812), "Pune": (18.5204, 73.8567),
    "Ajmer": (26.4499, 74.6399), "Bhopal": (23.2599, 77.4126), "Nashik": (19.9975, 73.7898),
}

AGENTS = {
    "core": {"name": "Saarthi Core", "color": "#14b8a6"},
    "dispatch": {"name": "Dispatch Agent", "color": "#38bdf8"},
    "route": {"name": "Route Oracle", "color": "#a78bfa"},
    "recovery": {"name": "Recovery Agent", "color": "#f97316"},
    "freight": {"name": "Freight Matcher", "color": "#22c55e"},
    "compliance": {"name": "Compliance Agent", "color": "#eab308"},
    "customer": {"name": "Customer Desk", "color": "#ec4899"},
    "detention": {"name": "Detention Agent", "color": "#f43f5e"},
    "dash": {"name": "Saarthi Dash", "color": "#06b6d4"},
    "analyst": {"name": "Fleet Analyst", "color": "#60a5fa"},
    "sentinel": {"name": "CEO Sentinel", "color": "#f5c451"},
    "fastag": {"name": "FASTag Auditor", "color": "#c084fc"},
}

DIESEL_RS_PER_L = 90.0
KMPL = 3.6                 # loaded multi-axle truck
TOLL_RS_PER_KM = 5.2       # estimate, multi-axle, NH plazas (approx.)
DRIVER_ALLOWANCE_PER_DAY = 900


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def road_km(c1: str, c2: str) -> float:
    return round(haversine(CITIES[c1], CITIES[c2]) * 1.22, 1)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


class Fleet:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.reset()

    # ------------------------------------------------------------------ world
    def reset(self):
        r = self.rng
        today = date.today()
        drivers = ["Ramesh Gurjar", "Suresh Meena", "Mahendra Singh", "Iqbal Khan", "Bhanwar Lal", "Gopal Jat",
                   "Harpreet Singh", "Kailash Choudhary", "Raju Yadav", "Shankar Patel", "Mohan Rawat", "Dinesh Saini"]
        plan = [  # (status, origin, dest, progress)
            ("on_the_way", "Chittorgarh", "Ahmedabad", 0.42), ("on_the_way", "Jaipur", "Delhi", 0.63),
            ("on_the_way", "Udaipur", "Mumbai", 0.28), ("on_the_way", "Indore", "Surat", 0.55),
            ("loading", "Bhilwara", "Pune", 0.0), ("on_the_way", "Kota", "Vadodara", 0.71),
            ("empty", "Ahmedabad", None, 0.0), ("on_the_way", "Ajmer", "Indore", 0.35),
            ("idle", "Chittorgarh", None, 0.0), ("on_the_way", "Delhi", "Jaipur", 0.18),
            ("idle", "Udaipur", None, 0.0), ("waiting", "Surat", "Chittorgarh", 0.0),
        ]
        cargo = ["Cement bags", "Marble slabs", "Textile bales", "Zinc ingots", "FMCG cartons", "Steel coils",
                 "Soybean", "Auto parts", "Ceramic tiles", "Cotton yarn"]
        shippers = ["Mewar Cement Ltd", "Rajsamand Marble Co", "Bhilwara Textiles", "Hindustan Metals",
                    "Aravalli FMCG", "Western Steel", "Malwa Agro", "Pune Auto Components"]
        consignees = ["Gujarat BuildMart", "Delhi Distribution Hub", "Mumbai Port CFS", "Surat Weaving Park",
                      "Vadodara Infra", "Indore Mandi Traders", "Pune Assembly Plant", "Jaipur Retail DC"]
        self.trucks, self.orders = {}, {}
        for i, (status, org, dst, prog) in enumerate(plan):
            tid = f"RJ09GA{4501 + i * 7}"
            pos = CITIES[org] if not dst else lerp(CITIES[org], CITIES[dst], prog)
            exp = lambda lo, hi: (today + timedelta(days=r.randint(lo, hi))).isoformat()
            docs = {"Insurance": exp(40, 300), "Fitness": exp(30, 330), "National Permit": exp(60, 360),
                    "PUC": exp(20, 170)}
            if i == 2:
                docs["Insurance"] = (today + timedelta(days=6)).isoformat()
            if i == 5:
                docs["PUC"] = (today + timedelta(days=3)).isoformat()
            if i == 9:
                docs["Fitness"] = (today + timedelta(days=11)).isoformat()
            t = {"id": tid, "driver": drivers[i], "phone": f"+91 98{r.randint(10000000, 99999999)}",
                 "status": status, "lat": pos[0], "lng": pos[1], "city": org if not dst else None,
                 "fuel": r.randint(35, 92), "drive_hours": round(r.uniform(1, 8.5), 1),
                 "health": r.randint(78, 98), "docs": docs, "order": None, "home": "Chittorgarh",
                 "km_today": r.randint(120, 420)}
            if dst:
                oid = f"ORD-{2611 + i}"
                km = road_km(org, dst)
                rate = round(km * r.uniform(52, 64) / 100) * 100
                self.orders[oid] = {"id": oid, "truck": tid, "from": org, "to": dst, "km": km,
                                    "cargo": cargo[i % len(cargo)], "tons": r.choice([18, 21, 25, 28]),
                                    "shipper": shippers[i % len(shippers)],
                                    "consignee": consignees[i % len(consignees)],
                                    "freight": rate, "status": status if status != "waiting" else "loading",
                                    "progress": prog, "eta_h": round(km * (1 - prog) / 45, 1)}
                t["order"] = oid
            if status == "waiting":
                t["wait_since_h"] = 8.2
            self.trucks[tid] = t
        self.loads = [  # load board (simulated marketplace)
            {"id": "LB-501", "from": "Ahmedabad", "to": "Chittorgarh", "cargo": "Chemicals (non-haz)", "tons": 24, "rate": 41500},
            {"id": "LB-502", "from": "Ahmedabad", "to": "Udaipur", "cargo": "Packaged food", "tons": 20, "rate": 33800},
            {"id": "LB-503", "from": "Vadodara", "to": "Bhilwara", "cargo": "Polymer granules", "tons": 25, "rate": 46200},
            {"id": "LB-504", "from": "Surat", "to": "Jaipur", "cargo": "Synthetic fabric", "tons": 16, "rate": 52500},
            {"id": "LB-505", "from": "Delhi", "to": "Chittorgarh", "cargo": "Electricals", "tons": 14, "rate": 49000},
            {"id": "LB-506", "from": "Ahmedabad", "to": "Kota", "cargo": "Tiles", "tons": 27, "rate": 44800},
            {"id": "LB-507", "from": "Mumbai", "to": "Udaipur", "cargo": "Machinery parts", "tons": 18, "rate": 61000},
            {"id": "LB-508", "from": "Indore", "to": "Chittorgarh", "cargo": "Soymeal", "tons": 26, "rate": 34200},
        ]
        self.mechanics = [
            {"name": "Shree Balaji Truck Care", "city": "Himmatnagar", "lat": 23.598, "lng": 72.966, "rating": 4.6},
            {"name": "Rajdhani Diesel Works", "city": "Shamlaji", "lat": 23.689, "lng": 73.383, "rating": 4.3},
            {"name": "Mewar Auto Garage", "city": "Udaipur", "lat": 24.60, "lng": 73.73, "rating": 4.7},
            {"name": "Highway Tata Service", "city": "Kherwara", "lat": 23.98, "lng": 73.59, "rating": 4.4},
            {"name": "NH-48 Truck Point", "city": "Behror", "lat": 27.89, "lng": 76.28, "rating": 4.2},
        ]
        self.warehouses = {"Surat Weaving Park DC": {"city": "Surat", "contact": "Mr. Desai (Dock Manager)",
                                                     "free_hours": 4, "detention_rate": 600}}
        self.stats = {"revenue_recovered": 0, "empty_km_avoided": 0, "detention_billed": 0,
                      "incidents_resolved": 0, "on_time_pct": 91.4, "loss_prevented": 0}
        self.tasks: list[dict] = []
        self.next_order = 2700

    # ------------------------------------------------------------------ helpers
    def nearest_city(self, lat, lng):
        return min(CITIES, key=lambda c: haversine((lat, lng), CITIES[c]))

    def tick(self, dt_hours: float = 0.05):
        """Advance moving trucks a little along their lane."""
        for t in self.trucks.values():
            o = self.orders.get(t["order"]) if t["order"] else None
            if t["status"] == "on_the_way" and o:
                o["progress"] = min(1.0, o["progress"] + dt_hours * 45 / max(o["km"], 1))
                pos = lerp(CITIES[o["from"]], CITIES[o["to"]], o["progress"])
                t["lat"], t["lng"] = pos
                o["eta_h"] = round(o["km"] * (1 - o["progress"]) / 45, 1)
                t["drive_hours"] = round(t["drive_hours"] + dt_hours * 0.2, 2)

    def kpis(self):
        ts = list(self.trucks.values())
        return {
            "on_road": sum(t["status"] == "on_the_way" for t in ts),
            "loading": sum(t["status"] in ("loading", "waiting") for t in ts),
            "empty": sum(t["status"] in ("empty", "idle") for t in ts),
            "breakdown": sum(t["status"] == "breakdown" for t in ts),
            "fleet": len(ts), **self.stats,
        }

    def snapshot(self):
        return {"trucks": list(self.trucks.values()), "orders": list(self.orders.values()),
                "kpis": self.kpis(), "loads": self.loads, "tasks": self.tasks[-30:],
                "cities": CITIES, "agents": AGENTS, "now": datetime.now().isoformat(timespec="seconds")}

    def docs_report(self, within_days: int = 15):
        today = date.today()
        out = []
        for t in self.trucks.values():
            for name, d in t["docs"].items():
                left = (date.fromisoformat(d) - today).days
                if left <= within_days:
                    out.append({"truck": t["id"], "doc": name, "expires": d, "days_left": left})
        return sorted(out, key=lambda x: x["days_left"])
