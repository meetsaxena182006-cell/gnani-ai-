"""Deterministic trip costing. The LLM never does this arithmetic; it only explains the result.

All default rates are ESTIMATES for a demo and must be overridden with the owner's real
numbers (diesel price, actual tolls, salaries). Every output lists the assumptions used.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class VehicleProfile:
    label: str
    payload_tonnes: float
    kmpl_loaded: float
    kmpl_empty: float
    toll_per_km: float        # rough NH average for the axle class; override with real toll
    maintenance_per_km: float  # tyres + service + repairs reserve
    km_per_day: float
    fixed_per_day: float       # EMI/finance + insurance + permits + road tax + parking, spread per working day


PROFILES: dict[str, VehicleProfile] = {
    "tempo_lcv": VehicleProfile("Tempo / LCV (~2.5 t)", 2.5, 9.0, 11.0, 1.7, 2.5, 380, 900),
    "truck_6w": VehicleProfile("6-wheeler (~9 t)", 9.0, 5.0, 6.0, 3.6, 4.0, 350, 1800),
    "truck_10w": VehicleProfile("10-wheeler (~16 t)", 16.0, 3.5, 4.5, 3.9, 6.0, 350, 2800),
    "trailer": VehicleProfile("Trailer (~28 t)", 28.0, 2.8, 3.5, 5.8, 8.0, 330, 3800),
}


@dataclass
class CostInputs:
    distance_km: float
    vtype: str = "truck_10w"
    diesel_price: float = 90.0
    load_tonnes: float | None = None
    actual_tolls: float | None = None
    driver_salary_per_day: float = 800.0
    driver_bata_per_day: float = 400.0
    helper_per_day: float = 0.0
    misc_per_day: float = 300.0          # parking, loading tips, permits on road
    loading_unloading: float = 0.0
    empty_return_fraction: float = 0.0   # 0 = return load found; 1 = full empty return
    broker_commission_pct: float = 0.0
    target_margin_pct: float = 15.0
    offered_freight: float | None = None
    fixed_per_day: float | None = None    # owner's real EMI+insurance+permit per day; default from the profile


def _r(x: float) -> float:
    return round(x, 2)


def estimate(inp: CostInputs) -> dict:
    if inp.distance_km <= 0:
        raise ValueError("distance_km must be positive")
    if inp.vtype not in PROFILES:
        raise ValueError(f"unknown vtype {inp.vtype}; choose from {list(PROFILES)}")
    if not 0 <= inp.empty_return_fraction <= 1:
        raise ValueError("empty_return_fraction must be between 0 and 1")
    p = PROFILES[inp.vtype]
    if inp.load_tonnes is not None and inp.load_tonnes > p.payload_tonnes * 1.0001:
        overload = True
    else:
        overload = False

    empty_km = inp.distance_km * inp.empty_return_fraction
    total_km = inp.distance_km + empty_km
    days = max(1, math.ceil(total_km / p.km_per_day))

    diesel_l = inp.distance_km / p.kmpl_loaded + empty_km / p.kmpl_empty
    diesel = diesel_l * inp.diesel_price
    tolls = inp.actual_tolls if inp.actual_tolls is not None else total_km * p.toll_per_km
    crew = days * (inp.driver_salary_per_day + inp.driver_bata_per_day + inp.helper_per_day)
    maintenance = total_km * p.maintenance_per_km
    misc = days * inp.misc_per_day + inp.loading_unloading
    fixed = days * (inp.fixed_per_day if inp.fixed_per_day is not None else p.fixed_per_day)

    direct_cost = diesel + tolls + crew + maintenance + misc + fixed
    commission_rate = inp.broker_commission_pct / 100
    margin_rate = inp.target_margin_pct / 100
    if commission_rate + margin_rate >= 1:
        raise ValueError("commission + margin must be below 100%")

    # Freight F: F - commission*F - cost = margin*F  =>  F = cost / (1 - c - m)
    break_even = direct_cost / (1 - commission_rate)
    quote = direct_cost / (1 - commission_rate - margin_rate)

    out = {
        "vehicle": p.label,
        "distance_km": _r(inp.distance_km),
        "empty_return_km": _r(empty_km),
        "trip_days": days,
        "diesel_litres": _r(diesel_l),
        "cost_lines": {
            "diesel": _r(diesel),
            "tolls": _r(tolls),
            "driver_and_crew": _r(crew),
            "maintenance_tyres": _r(maintenance),
            "misc_loading": _r(misc),
            "fixed_emi_insurance_permit": _r(fixed),
        },
        "total_cost": _r(direct_cost),
        "cost_per_km": _r(direct_cost / inp.distance_km),
        "break_even_freight": _r(break_even),
        "recommended_quote": _r(math.ceil(quote / 100) * 100),
        "target_margin_pct": inp.target_margin_pct,
        "warnings": [],
        "assumptions": {
            **{k: v for k, v in asdict(inp).items() if k not in ("offered_freight",)},
            "kmpl_loaded": p.kmpl_loaded,
            "kmpl_empty": p.kmpl_empty,
            "toll_source": "actual" if inp.actual_tolls is not None else f"estimate ₹{p.toll_per_km}/km",
            "maintenance_per_km": p.maintenance_per_km,
            "km_per_day": p.km_per_day,
            "fixed_per_day": inp.fixed_per_day if inp.fixed_per_day is not None else p.fixed_per_day,
        },
    }
    if inp.load_tonnes:
        out["cost_per_tonne"] = _r(direct_cost / inp.load_tonnes)
    if overload:
        out["warnings"].append(
            f"Load {inp.load_tonnes} t exceeds {p.label} payload {p.payload_tonnes} t (overloading is illegal and unsafe)."
        )
    if inp.actual_tolls is None:
        out["warnings"].append("Tolls are an estimate; enter the real toll for an exact figure.")
    if inp.offered_freight is not None:
        commission = inp.offered_freight * commission_rate
        profit = inp.offered_freight - commission - direct_cost
        out["offered_freight"] = _r(inp.offered_freight)
        out["profit_at_offer"] = _r(profit)
        out["margin_at_offer_pct"] = _r(100 * profit / inp.offered_freight)
        if profit < 0:
            out["warnings"].append("Offered freight is BELOW cost: this trip loses money.")
    return out


def trip_pnl(freight: float, expenses_by_category: dict[str, float]) -> dict:
    total = sum(expenses_by_category.values())
    profit = freight - total
    return {
        "freight": _r(freight),
        "expenses": {k: _r(v) for k, v in expenses_by_category.items()},
        "total_expenses": _r(total),
        "profit": _r(profit),
        "margin_pct": _r(100 * profit / freight) if freight else None,
    }
