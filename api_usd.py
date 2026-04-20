# api_usd.py

import asyncio
import aiohttp
import omni.usd

from .ui_helpers import set_status


async def send_impulse_to_api(ext, node_id):
    if ext._sim_mode:
        return

    headers = {"X-API-KEY": ext.api_key, "accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "NodeName": node_id,
                "Value": "true",
                "user": "admin",
                "apiKey": ext.api_key
            }
            async with session.post(
                ext.api_url_set,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    ext._log(f"Impulse API true: {node_id}", "ok")

            await asyncio.sleep(0.3)

            params["Value"] = "false"
            async with session.post(
                ext.api_url_set,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    ext._log(f"Impulse API reset: {node_id}", "log")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        ext._log(f"Impulse API Fehler: {node_id} | {e}", "error")


async def send_api_update(ext, node_id, value):
    if ext._sim_mode:
        ext.node_values[node_id] = value
        ext._set_node_display(node_id, value)
        ext._apply_usd_for_node(node_id, value)
        return

    headers = {"X-API-KEY": ext.api_key, "accept": "application/json"}
    params = {
        "NodeName": node_id,
        "Value": str(value).lower(),
        "user": "admin",
        "apiKey": ext.api_key
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ext.api_url_set,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    ext.node_values[node_id] = value
                    ext._set_node_display(node_id, value)
                    ext._apply_usd_for_node(node_id, value)
                    ext._log(f"Set OK: {node_id} = {value}", "ok")
                else:
                    ext._log(f"Set Fehler {resp.status}: {node_id}", "error")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        ext._log(f"Set Exception: {node_id} | {e}", "error")


async def auto_update_loop(ext):
    if ext._sim_mode:
        return

    session = None
    try:
        session = aiohttp.ClientSession()
        set_status(ext, True)

        while ext._is_running and not ext._sim_mode:
            try:
                await poll_all_nodes(ext, session)
                ext._poll_count += 1
                if hasattr(ext, "_poll_label"):
                    ext._poll_label.text = f"Polls: {ext._poll_count}"
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                raise
            except aiohttp.ClientError:
                set_status(ext, False)
                if ext._is_running and not ext._sim_mode:
                    ext._log("HTTP Fehler, Retry 2s", "error")
                    await asyncio.sleep(2)
            except Exception as e:
                if ext._is_running:
                    ext._log(f"Loop Fehler: {e}", "error")
                    await asyncio.sleep(1)

    except asyncio.CancelledError:
        pass
    finally:
        if session and not session.closed:
            await session.close()
            await asyncio.sleep(0.05)
        if not ext._sim_mode:
            set_status(ext, False)


async def poll_all_nodes(ext, session):
    if not ext._is_running or ext._sim_mode:
        return

    headers = {"X-API-KEY": ext.api_key, "accept": "application/json"}

    for node in ext.nodes:
        if not ext._is_running or ext._sim_mode:
            break

        node_id = node.get("node_id")
        if not ext.node_labels.get(node_id):
            continue

        mode = node.get("mode", "toggle")
        params = {
            "NodeName": node_id,
            "useHistoricalData": "false",
            "user": "admin",
            "apiKey": ext.api_key,
        }

        try:
            async with session.get(
                ext.api_url_get,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=3),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    continue

                data = await resp.text()
                val_str = data.replace('"', "").strip().lower()
                val = val_str in ("true", "1")

                old_val = ext.node_values.get(node_id)
                ext.node_values[node_id] = val

                if mode == "impulse":
                    ext._handle_step_impulse_poll(node_id, node, old_val, val)
                elif mode == "velocity_impulse":
                    ext._handle_velocity_impulse_poll(node_id, node, old_val, val)
                else:
                    if old_val != val:
                        ext._set_node_display(node_id, val)
                        ext._apply_usd_for_node(node_id, val)
                        ext._log(f"{node_id} = {val}", "ok")

        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            if ext._is_running:
                ext._log(f"Poll Fehler ({node_id}): {e}", "error")


def apply_usd_for_node(ext, node_id, val):
    stage = omni.usd.get_context().get_stage()
    if not stage:
        return

    for node in ext.nodes:
        if node.get("node_id") == node_id:
            target_val = float(node.get("target_value", 1.0))
            new_val = target_val if val else 0.0
            set_usd_attr(ext, stage, node, new_val)
            return


def set_usd_attr(ext, stage, node, value):
    p_path = node.get("prim_path")
    if not p_path:
        return

    prim = stage.GetPrimAtPath(p_path)
    if not prim or not prim.IsValid():
        return

    attr_name = node.get("attribute", "drive:angular:physics:targetPosition")
    attr = prim.GetAttribute(attr_name)

    if not attr or not attr.IsValid():
        alt_name = attr_name.replace(":physics:", ":")
        attr = prim.GetAttribute(alt_name)
        if attr and attr.IsValid():
            attr_name = alt_name

    if not attr or not attr.IsValid():
        return

    try:
        attr.Set(value)
        layer = stage.GetEditTarget().GetLayer()
        prim_spec = layer.GetPrimAtPath(p_path)
        if prim_spec:
            sdf_attr = prim_spec.attributes.get(attr_name)
            if sdf_attr:
                sdf_attr.default = value
    except Exception as e:
        ext._log(f"USD Fehler: {p_path} | {e}", "error")