"""
Simplified allocation engine for the Allocation Planner demo.

Mirrors the structure of the production pipeline on synthetic data:
  1. sort products into groups (ok / low velocity / no demand)
  2. place special products by rules (deterministic, no random tie-break)
  3. reduce warehouse space
  4. solve an LP for everything else: minimise shipping cost + overflow penalty
     subject to demand, warehouse share (min/max) and storage capacity
Uses scipy's HiGHS solver instead of Gurobi, so no licence is needed.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linprog

WAREHOUSES = ["WH-East", "WH-Central", "WH-West"]
ZONES = ["East", "Central", "West"]
STORAGE_TYPES = ["FLA", "HFG"]

# Base shipping cost per unit (warehouse -> zone)
BASE_COST = pd.DataFrame(
    [[2.0, 3.5, 5.0],
     [3.5, 2.0, 3.5],
     [5.0, 3.5, 2.0]],
    index=WAREHOUSES, columns=ZONES,
)
STORAGE_COST_FACTOR = {"FLA": 1.0, "HFG": 1.3}

# One lane deliberately has no cost data, to show the missing-cost warning
MISSING_LANES = {("WH-West", "East", "Coats")}


def default_warehouses() -> pd.DataFrame:
    return pd.DataFrame({
        "Warehouse": WAREHOUSES,
        "Min %": [20, 20, 20],
        "Max %": [50, 50, 50],
        "FLA space": [6000, 5000, 5000],
        "HFG space": [2000, 1500, 2000],
    })


def default_products() -> pd.DataFrame:
    return pd.DataFrame({
        "Product": ["Shirts", "Jeans", "Dresses", "Coats", "Towels",
                    "Scarves", "Ties", "New-brand socks"],
        "Velocity": ["high", "high", "medium", "medium", "high",
                     "low", "low", "no demand"],
        "Storage": ["FLA", "FLA", "HFG", "HFG", "FLA", "FLA", "HFG", "FLA"],
        "East": [2500, 1800, 900, 600, 1200, 80, 40, 0],
        "Central": [1500, 1200, 500, 400, 900, 50, 30, 0],
        "West": [2000, 1600, 800, 500, 1100, 60, 40, 0],
        "Order qty": [0, 0, 0, 0, 0, 0, 0, 300],
    })


def build_costs(products: pd.DataFrame):
    """Cost per (warehouse, zone, product). Missing lanes are filled with the
    warehouse's average cost (not the minimum, which would make them the
    cheapest option)."""
    costs, filled = {}, []
    for _, p in products.iterrows():
        f = STORAGE_COST_FACTOR.get(p["Storage"], 1.0)
        for w in WAREHOUSES:
            for z in ZONES:
                if (w, z, p["Product"]) in MISSING_LANES:
                    costs[(w, z, p["Product"])] = np.nan
                else:
                    costs[(w, z, p["Product"])] = round(BASE_COST.loc[w, z] * f, 2)
    for key, val in list(costs.items()):
        if np.isnan(val):
            w = key[0]
            avg = np.nanmean([v for k, v in costs.items() if k[0] == w])
            costs[key] = round(float(avg), 2)
            filled.append({"Warehouse": key[0], "Zone": key[1], "Product": key[2],
                           "Filled cost": costs[key]})
    return costs, pd.DataFrame(filled)


def validate(wh: pd.DataFrame, products: pd.DataFrame):
    """Pre-run checks. Returns list of (level, message); level 'error' blocks the run."""
    checks = []
    bad = wh[wh["Min %"] > wh["Max %"]]
    if len(bad):
        checks.append(("error", f"Min % is above Max % for: {', '.join(bad['Warehouse'])}"))
    if wh["Max %"].sum() < 100:
        checks.append(("error", f"Max shares add up to {wh['Max %'].sum():.0f}% (need at least 100%)"))
    if wh["Min %"].sum() > 100:
        checks.append(("error", f"Min shares add up to {wh['Min %'].sum():.0f}% (must be 100% or less)"))
    if (products[ZONES] < 0).any().any() or (products["Order qty"] < 0).any():
        checks.append(("error", "Demand and order quantities can't be negative"))
    unknown = set(products["Storage"]) - set(STORAGE_TYPES)
    if unknown:
        checks.append(("error", f"Unknown storage type(s): {', '.join(sorted(unknown))}"))
    if MISSING_LANES:
        checks.append(("warn", f"{len(MISSING_LANES)} shipping cost lane(s) missing — will be filled with the warehouse average"))
    for s in STORAGE_TYPES:
        need = products.loc[products["Storage"] == s, ZONES].to_numpy().sum() + \
            products.loc[products["Storage"] == s, "Order qty"].sum()
        have = wh[f"{s} space"].sum()
        if need > have:
            checks.append(("warn", f"{s}: demand {need:,.0f} is above total space {have:,.0f} — some overflow is unavoidable"))
    if not any(lvl == "error" for lvl, _ in checks):
        checks.insert(0, ("ok", "Warehouse limits are valid"))
    return checks


@dataclass
class RunResult:
    status: str                      # "Optimal", "Infeasible", "Error"
    message: str
    allocations: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    warnings: list = field(default_factory=list)
    filled_costs: pd.DataFrame = field(default_factory=pd.DataFrame)
    total_cost: float = 0.0
    overflow_units: float = 0.0


def run_allocation(wh: pd.DataFrame, products: pd.DataFrame, penalty: float = 50.0) -> RunResult:
    wh = wh.set_index("Warehouse").loc[WAREHOUSES]
    products = products.copy()
    costs, filled = build_costs(products)
    space = {(w, s): float(wh.loc[w, f"{s} space"]) for w in WAREHOUSES for s in STORAGE_TYPES}
    rows = []

    # 1. Sort products into groups
    low = products[products["Velocity"] == "low"]
    no_dem = products[products["Velocity"] == "no demand"]
    ok = products[products["Velocity"].isin(["high", "medium"])]

    # 2a. Slow sellers -> ONE warehouse: the cheapest one that has space (deterministic)
    for _, p in low.iterrows():
        demand = p[ZONES].astype(float)
        ranked = sorted(WAREHOUSES, key=lambda w: sum(costs[(w, z, p["Product"])] * demand[z] for z in ZONES))
        chosen = next((w for w in ranked if space[(w, p["Storage"])] >= demand.sum()), ranked[0])
        for z in ZONES:
            if demand[z] > 0:
                rows.append(dict(Warehouse=chosen, Zone=z, Product=p["Product"], Storage=p["Storage"],
                                 Units=demand[z], **{"Unit cost": costs[(chosen, z, p["Product"])]},
                                 Method="Rule: slow seller"))
        space[(chosen, p["Storage"])] -= demand.sum()

    # 2b. No demand history -> split evenly across warehouses with space
    for _, p in no_dem.iterrows():
        targets = [w for w in WAREHOUSES if space[(w, p["Storage"])] > 0] or WAREHOUSES
        share = float(p["Order qty"]) / len(targets)
        for w in targets:
            rows.append(dict(Warehouse=w, Zone="(not assigned)", Product=p["Product"], Storage=p["Storage"],
                             Units=share, **{"Unit cost": np.nan}, Method="Rule: no demand, even split"))
            space[(w, p["Storage"])] -= share

    rule_units = {w: sum(r["Units"] for r in rows if r["Warehouse"] == w) for w in WAREHOUSES}

    # 3. LP for the remaining products
    P = list(ok["Product"])
    ok = ok.set_index("Product")
    nW, nZ, nP, nS = len(WAREHOUSES), len(ZONES), len(P), len(STORAGE_TYPES)
    nx = nW * nZ * nP
    xi = lambda w, z, p: (w * nZ + z) * nP + p
    oi = lambda w, s: nx + w * nS + s
    nvar = nx + nW * nS

    c = np.zeros(nvar)
    for w, wn in enumerate(WAREHOUSES):
        for z, zn in enumerate(ZONES):
            for p, pn in enumerate(P):
                c[xi(w, z, p)] = costs[(wn, zn, pn)]
    c[nx:] = penalty

    # Demand: every zone gets exactly what it needs
    A_eq, b_eq = [], []
    for z, zn in enumerate(ZONES):
        for p, pn in enumerate(P):
            row = np.zeros(nvar)
            for w in range(nW):
                row[xi(w, z, p)] = 1
            A_eq.append(row); b_eq.append(float(ok.loc[pn, zn]))

    # Share limits include rule-based stock, so the % matches the warehouse's real total
    total_units = float(ok[ZONES].to_numpy().sum()) + sum(rule_units.values())
    A_ub, b_ub = [], []
    for w, wn in enumerate(WAREHOUSES):
        row = np.zeros(nvar)
        for z in range(nZ):
            for p in range(nP):
                row[xi(w, z, p)] = 1
        A_ub.append(row); b_ub.append(wh.loc[wn, "Max %"] / 100 * total_units - rule_units[wn])
        A_ub.append(-row); b_ub.append(-(wh.loc[wn, "Min %"] / 100 * total_units - rule_units[wn]))

    # Storage space: soft limit everywhere, overflow pays the penalty
    for w, wn in enumerate(WAREHOUSES):
        for s, sn in enumerate(STORAGE_TYPES):
            row = np.zeros(nvar)
            for p, pn in enumerate(P):
                if ok.loc[pn, "Storage"] == sn:
                    for z in range(nZ):
                        row[xi(w, z, p)] = 1
            row[oi(w, s)] = -1
            A_ub.append(row); b_ub.append(max(space[(wn, sn)], 0.0))

    # Medium velocity: only the 2 cheapest warehouses for that product
    bounds = [(0, None)] * nvar
    allowed = {}
    for p, pn in enumerate(P):
        if ok.loc[pn, "Velocity"] == "medium":
            d = ok.loc[pn, ZONES].astype(float)
            ranked = sorted(WAREHOUSES, key=lambda w: sum(costs[(w, z, pn)] * d[z] for z in ZONES))
            allowed[pn] = ranked[:2]
            for w, wn in enumerate(WAREHOUSES):
                if wn not in allowed[pn]:
                    for z in range(nZ):
                        bounds[xi(w, z, p)] = (0, 0)

    res = linprog(c, A_ub=np.array(A_ub), b_ub=np.array(b_ub), A_eq=np.array(A_eq),
                  b_eq=np.array(b_eq), bounds=bounds, method="highs")

    if res.status == 2:
        return RunResult("Infeasible", "No plan fits all the rules. Most likely the warehouse min/max "
                         "shares can't be met — try widening them.", filled_costs=filled)
    if res.status != 0:
        return RunResult("Error", f"Solver stopped: {res.message}", filled_costs=filled)

    x = res.x
    for w, wn in enumerate(WAREHOUSES):
        for z, zn in enumerate(ZONES):
            for p, pn in enumerate(P):
                u = x[xi(w, z, p)]
                if u > 1e-6:
                    rows.append(dict(Warehouse=wn, Zone=zn, Product=pn, Storage=ok.loc[pn, "Storage"],
                                     Units=round(u, 2), **{"Unit cost": costs[(wn, zn, pn)]},
                                     Method="Optimizer"))

    alloc = pd.DataFrame(rows)
    alloc["Cost"] = (alloc["Units"] * alloc["Unit cost"]).round(2)

    summary = (alloc.groupby(["Warehouse", "Storage"], as_index=False)["Units"].sum())
    summary["Space"] = [float(wh.loc[r.Warehouse, f"{r.Storage} space"]) for r in summary.itertuples()]
    summary["Space used %"] = (summary["Units"] / summary["Space"] * 100).round(1)
    summary["Overflow"] = (summary["Units"] - summary["Space"]).clip(lower=0).round(0)

    warnings = []
    overflow = float(x[nx:].sum())
    for r in summary[summary["Overflow"] > 0].itertuples():
        warnings.append(("high", f"{r.Warehouse} is over its {r.Storage} space by {r.Overflow:,.0f} units",
                         "Demand for this storage type was higher than the space left. Each extra unit costs the penalty."))
    if len(filled):
        warnings.append(("high", f"{len(filled)} shipping cost lane(s) were missing",
                         "Filled with the warehouse's average cost. Check the source data."))
    n_rule = alloc[alloc["Method"] != "Optimizer"]["Product"].nunique()
    if n_rule:
        warnings.append(("info", f"{n_rule} product(s) were placed by business rules",
                         "Slow sellers go to one warehouse; products without demand are split evenly."))
    for pn, ws in allowed.items():
        warnings.append(("info", f"{pn} (medium velocity) limited to {ws[0]} and {ws[1]}",
                         "Medium-velocity products may use at most 2 warehouses."))

    total = float(alloc["Cost"].sum(skipna=True))
    return RunResult("Optimal", "Best plan found.", alloc, summary, warnings, filled,
                     total_cost=total, overflow_units=overflow)
