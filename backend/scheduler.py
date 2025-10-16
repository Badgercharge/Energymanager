# ... oben unverändert (Importe/Helper)

async def control_loop(app, lat: float, lon: float, base_limit_kw: float):
    tzname = os.getenv("LOCAL_TZ", "Europe/Berlin")
    battery_kwh = float(os.getenv("BATTERY_KWH", "60"))
    efficiency  = float(os.getenv("EFFICIENCY", "0.92"))

    if not hasattr(app.state, "pricing"):
        app.state.pricing = {"as_of": None, "current_ct_per_kwh": None, "median_ct_per_kwh": None, "below_or_equal_median": None}

    prices_cache = {"ts": None, "series": [], "median": None, "cur": None}

    while True:
        try:
            # ... Preise + Eco wie zuvor (deine aktuelle Version)
            # cur_price & prices_cache["median"] setzen

            for cp_id, st in STATE.items():
                # Ziel-Leistung für den Modus (Manual hat Vorrang)
                if st.mode == "manual":
                    target = st.target_kw
                elif st.mode == "off":
                    target = MIN_KW
                elif st.mode == "max":
                    target = MAX_KW
                elif st.mode == "price":
                    if prices_cache["median"] is not None and cur_price is not None:
                        target = MAX_KW if cur_price <= prices_cache["median"] else MIN_KW
                    else:
                        target = MIN_KW
                    # Deadline 07:00 berücksichtigen (wie bisher)
                    # ... (deine vorhandene Berechnung)
                else:  # eco
                    # ... (deine Eco + Boost Berechnung)
                    target = clamp_kw( # bereits befüllt
                        # eco_kw ggf. durch req_kw erhöhen
                    )

                target = clamp_kw(target)
                st.target_kw = round(target, 2)

                # Prognose Endezeit berechnen (falls SoC vorhanden und Session aktiv)
                st.session_est_end_at = None
                if st.tx_active and st.current_soc is not None:
                    # Ziel-SoC: im Eco-Boost der boost_target_soc, sonst 100%
                    target_soc = st.boost_target_soc if (st.mode == "eco" and st.boost_enabled) else 100
                    need_soc = max(0.0, target_soc - float(st.current_soc))
                    if need_soc > 0 and target > 0:
                        need_kwh = (need_soc / 100.0) * battery_kwh
                        eff = max(0.5, min(1.0, efficiency))
                        hours = need_kwh / (target * eff)
                        from datetime import datetime as dt
                        st.session_est_end_at = dt.now().astimezone(ZoneInfo(tzname)) + timedelta(hours=hours)

                cp = app.state.cps.get(cp_id)
                if cp:
                    await cp.push_charging_profile(st.target_kw)

            await asyncio.sleep(900)
        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(30)
