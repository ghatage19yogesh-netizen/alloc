import pandas as pd
import streamlit as st

import engine as e

st.set_page_config(page_title="Allocation Planner", layout="wide")

# ---------- state ----------
if "wh" not in st.session_state:
    st.session_state.wh = e.default_warehouses()
    st.session_state.products = e.default_products()
    st.session_state.result = None
    st.session_state.approved = False

st.title("Allocation Planner")
st.caption("Demo on sample data · decides how many units of each product each warehouse should stock, "
           "at the lowest shipping cost, within space and business rules.")

tab_setup, tab_plan, tab_warn, tab_persona = st.tabs(
    ["1 · Set up & run", "2 · See the plan", "3 · Check warnings", "Who it's for"])

# ---------- 1. Set up & run ----------
with tab_setup:
    left, right = st.columns([2, 1], gap="large")
    with left:
        st.subheader("New monthly run")
        st.selectbox("Month", ["November 2026", "December 2026", "January 2027"], key="month")

        st.markdown("**Warehouse limits** — share of all units, and space by storage type")
        wh = st.data_editor(
            st.session_state.wh, key="wh_editor", hide_index=True, width="stretch",
            disabled=["Warehouse"],
            column_config={
                "Min %": st.column_config.NumberColumn(min_value=0, max_value=100, step=1),
                "Max %": st.column_config.NumberColumn(min_value=0, max_value=100, step=1),
                "FLA space": st.column_config.NumberColumn(min_value=0, step=100, help="Folded / flat storage"),
                "HFG space": st.column_config.NumberColumn(min_value=0, step=100, help="Hanging storage"),
            })

        with st.expander("Products and demand by zone"):
            st.caption("Velocity: high/medium go to the optimizer · low = slow seller (one warehouse) · "
                       "no demand = split evenly using Order qty")
            products = st.data_editor(
                st.session_state.products, key="prod_editor", hide_index=True, width="stretch",
                num_rows="dynamic",
                column_config={
                    "Velocity": st.column_config.SelectboxColumn(options=["high", "medium", "low", "no demand"], required=True),
                    "Storage": st.column_config.SelectboxColumn(options=e.STORAGE_TYPES, required=True),
                })

        penalty = st.number_input("Overflow penalty ($ per unit over space)", min_value=1.0, value=50.0, step=5.0,
                                  help="Should be well above normal shipping costs, so overflow is a last resort.")

    with right:
        st.subheader("Checks before running")
        products = products.dropna(subset=["Product"]).fillna(0)
        checks = e.validate(wh, products)
        for level, msg in checks:
            {"ok": st.success, "warn": st.warning, "error": st.error}[level](msg)
        blocked = any(level == "error" for level, _ in checks)

        if st.button("Run allocation", type="primary", width="stretch", disabled=blocked):
            with st.spinner("Placing special products, then solving the optimizer…"):
                st.session_state.result = e.run_allocation(wh, products, penalty)
                st.session_state.approved = False
            r = st.session_state.result
            if r.status == "Optimal":
                st.success("Done. Open **2 · See the plan**.")
            else:
                st.error(f"{r.status}: {r.message}")
        if blocked:
            st.caption("Fix the red checks to enable the run.")

# ---------- 2. See the plan ----------
with tab_plan:
    r = st.session_state.result
    if r is None:
        st.info("No plan yet. Go to **1 · Set up & run** and press **Run allocation**.")
    elif r.status != "Optimal":
        st.error(f"{r.status}: {r.message}")
    else:
        top = st.columns([3, 1, 1])
        top[0].subheader(f"Plan for {st.session_state.month}")
        top[1].download_button("Export CSV", r.allocations.to_csv(index=False).encode(),
                               file_name="allocation_plan.csv", mime="text/csv", width="stretch")
        if top[2].button("Approve", type="primary", width="stretch"):
            st.session_state.approved = True
        if st.session_state.approved:
            st.success("Plan approved.")
        else:
            st.success("Best plan found.")

        m = st.columns(4)
        m[0].metric("Total shipping cost", f"${r.total_cost:,.0f}")
        m[1].metric("Units placed", f"{r.allocations['Units'].sum():,.0f}")
        m[2].metric("Overflow units", f"{r.overflow_units:,.0f}")
        m[3].metric("Warnings", len(r.warnings))

        c1, c2 = st.columns([3, 2], gap="large")
        with c1:
            st.markdown("**By warehouse and storage type**")
            st.dataframe(
                r.summary, hide_index=True, width="stretch",
                column_config={
                    "Units": st.column_config.NumberColumn(format="%.0f"),
                    "Space": st.column_config.NumberColumn(format="%.0f"),
                    "Space used %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Overflow": st.column_config.NumberColumn(format="%.0f"),
                })
            shares = r.allocations.groupby("Warehouse")["Units"].sum()
            shares = (shares / shares.sum() * 100).round(1).rename("Share %").reset_index()
            st.markdown("**Share of all units per warehouse**")
            st.dataframe(shares, hide_index=True, width="stretch")
        with c2:
            st.markdown("**Shipping cost by warehouse**")
            cost = (r.allocations.dropna(subset=["Cost"])
                    .pivot_table(index="Warehouse", columns="Method", values="Cost", aggfunc="sum", fill_value=0))
            st.bar_chart(cost, stack=True)

        with st.expander("Full allocation (every warehouse → zone → product)"):
            st.dataframe(r.allocations, hide_index=True, width="stretch")

# ---------- 3. Check warnings ----------
with tab_warn:
    r = st.session_state.result
    if r is None or r.status != "Optimal":
        st.info("Run an allocation first to see warnings.")
    else:
        st.subheader(f"Things to check · {len(r.warnings)}")
        for level, title, detail in r.warnings:
            box = st.warning if level == "high" else st.info
            box(f"**{title}**  \n{detail}")
        if len(r.filled_costs):
            with st.expander("Missing cost lanes that were filled"):
                st.dataframe(r.filled_costs, hide_index=True, width="stretch")
        rules = r.allocations[r.allocations["Method"] != "Optimizer"]
        if len(rules):
            with st.expander("Products placed by business rules"):
                st.dataframe(rules, hide_index=True, width="stretch")

# ---------- Persona ----------
with tab_persona:
    a, b = st.columns([1, 2], gap="large")
    with a:
        st.subheader("Maria Lopez")
        st.caption("Allocation Planner · COM Supply Chain (illustrative persona)")
        st.markdown("- **Uses the tool:** once a month\n- **Comfortable with:** Excel, not Python")
        st.markdown("> “I just want to run the plan, see if it looks right, and know what to fix.”")
    with b:
        g, f = st.columns(2)
        g.markdown("**Goals**\n- Run the monthly plan on time\n- Keep every warehouse within space\n- Explain the plan to her manager")
        f.markdown("**Frustrations**\n- Needs a data scientist to run it\n- Results hidden in log files\n- Errors only appear after a crash")
        g.markdown("**Needs from the tool**\n- One button to run\n- A clear summary\n- A simple list of problems")
        f.markdown("**Her journey**\n1. Set up & run\n2. See the plan\n3. Check warnings\n4. Approve & export")
