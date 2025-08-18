import os
from APIConnect.APIConnect import APIConnect
import redis
import orjson
import importlib.util, sys, pathlib, traceback
import time
import logging
from .order_class import Orders
from constants.exchange import ExchangeEnum
from constants.action import ActionEnum
from constants.order_type import OrderTypeEnum
from constants.product_code import ProductCodeENum
import threading
from concurrent.futures import ThreadPoolExecutor ,as_completed

# module logger
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class Stratergy1:
    def __init__(self, paramsid) -> None:
        self.r = redis.Redis(host="localhost", port=6379, db=0)
        self.order = Orders()
        # lock used when updating shared per-user templates/qtys from worker threads
        self.templates_lock = threading.Lock()

        # load params and basic state
        self.params_key = f"stratergies:stratergy_1_{paramsid}"
        raw_params = self.r.get(self.params_key)
        if raw_params is None:
            raise RuntimeError(f"params key missing in redis: {self.params_key}")
        self.params = orjson.loads(raw_params.decode())
        self.executed = False

        # cached mapping used to translate streaming symbol -> trading symbol
        try:
            raw_map = self.r.get("option_mapper")
            if raw_map is None:
                raise RuntimeError("option_mapper key missing in redis")
            self.option_mapper = orjson.loads(raw_map.decode())
        except Exception:
            logger.exception("failed to load option_mapper from redis")
            raise

        self.completed_straddle = False
        self.completed_exit = False
        self.entry_qty = 0
        self.exit_qty = 0

        # start background thread to watch params updates
        self.params_update_thread = threading.Thread(target=self.live_params_update_thread, daemon=True)
        self.params_update_thread.start()

        # initialise legs/templates
        self._init_legs_and_orders()
        # main logic is started externally when desired

    def live_params_update_thread(self):
        while True:
            try:
                params = orjson.loads(self.r.get(self.params_key).decode())
                if self.params != params:
                    self.params = params
                    print("Live params updated:", self.params)
                    self._init_legs_and_orders()
            except Exception as e:
                logger.exception("failed to update live params")
                time.sleep(1)

    # --- initialization helpers -------------------------------------------------
    def _depth_from_redis(self, streaming_symbol: str):
        """Load depth JSON from redis and return parsed object."""
        try:
            raw = self.r.get(streaming_symbol)
            if raw is None:
                logger.debug("redis key not found: %s", streaming_symbol)
                return None
            return orjson.loads(raw.decode())
        except orjson.JSONDecodeError:
            logger.exception("invalid JSON for redis key %s", streaming_symbol)
            return None
        except redis.RedisError:
            logger.exception("redis error while fetching %s", streaming_symbol)
            return None
        except Exception:
            logger.exception("unexpected error reading %s from redis", streaming_symbol)
            return None

    def _init_legs_and_orders(self):
        # choose which leg is base vs other depending on requested base_leg
        sym = self.params["symbol"].upper()
        if self.params["base_leg"].upper() == "CE":
            self.base_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['call_strike']}.0_CE-{self.params['expiry']}"
            )
            self.other_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['put_strike']}.0_PE-{self.params['expiry']}"
            )
        else:
            self.base_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['put_strike']}.0_PE-{self.params['expiry']}"
            )
            self.other_leg = self._depth_from_redis(
                f"depth:{sym}_{self.params['call_strike']}.0_CE-{self.params['expiry']}"
            )

        # determine exchange from the streaming symbol
        if self.base_leg is None:
            logger.error("base_leg depth not found for params=%s", self.params)
            raise RuntimeError("base_leg depth missing in redis")
        symbol_text = self.base_leg["response"]["data"]["symbol"]
        if "BFO" in symbol_text:
            self.exchange = ExchangeEnum.BFO
        elif "NFO" in symbol_text:
            self.exchange = ExchangeEnum.NFO
        elif "NSE" in symbol_text:
            self.exchange = ExchangeEnum.NSE
        else:
            self.exchange = ExchangeEnum.BSE


        # create per-user order templates used during runtime
        # params may contain a list of user ids; normalize to list
        uids = self.params.get("user_ids") or self.params.get("user_ids")
        if isinstance(uids, (int, str)):
            uids = [uids]
        if not isinstance(uids, list):
            uids = list(uids) if uids is not None else []

        # dictionaries keyed by user_id -> template
        self.order_templates = {}
        self.order_templates_base_leg = {}
        self.exit_order_templates = {}
        self.exit_order_templates_base_leg = {}

        # save normalized user list and create per-user tracking structures
        self.uids = uids
        self.entry_qtys = {uid: 0 for uid in uids}
        self.exit_qtys = {uid: 0 for uid in uids}
        self.completed_straddles = {uid: False for uid in uids}
        self.completed_exits = {uid: False for uid in uids}

        if self.params['action'].upper() == "BUY":
            for uid in uids:
                self.order_templates[uid] = self._make_order_template(self.other_leg, buy_if="BUY", user_id=uid)
                self.order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="BUY", user_id=uid)
                self.exit_order_templates[uid] = self._make_order_template(self.other_leg, buy_if="SELL", quantity=0, user_id=uid)
                self.exit_order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="SELL", quantity=0, user_id=uid)
        else:
            for uid in uids:
                self.order_templates[uid] = self._make_order_template(self.other_leg, buy_if="SELL", user_id=uid)
                self.order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="SELL", user_id=uid)
                self.exit_order_templates[uid] = self._make_order_template(self.other_leg, buy_if="BUY", quantity=0, user_id=uid)
                self.exit_order_templates_base_leg[uid] = self._make_order_template(self.base_leg, buy_if="BUY", quantity=0, user_id=uid)

        # keep single-template attributes for backward compatibility (use first user if present)
        first_uid = uids[0] if uids else None
        if first_uid is not None:
            self.order_details = self.order_templates[first_uid]
            self.order_details_base_leg = self.order_templates_base_leg[first_uid]
            self.exit_order_details = self.exit_order_templates[first_uid]
            self.exit_order_details_base_leg = self.exit_order_templates_base_leg[first_uid]
        else:
            # fallback to single templates without user_id
            self.order_details = self._make_order_template(self.other_leg, buy_if=("BUY" if self.params['action'].upper() == "BUY" else "SELL"))
            self.order_details_base_leg = self._make_order_template(self.base_leg, buy_if=("BUY" if self.params['action'].upper() == "BUY" else "SELL"))
            self.exit_order_details = self._make_order_template(self.other_leg, buy_if=("SELL" if self.params['action'].upper() == "BUY" else "BUY"), quantity=0)
            self.exit_order_details_base_leg = self._make_order_template(self.base_leg, buy_if=("SELL" if self.params['action'].upper() == "BUY" else "BUY"), quantity=0)

    def _make_order_template(self, leg_obj, buy_if="BUY", quantity=None, user_id=None):
        """Return a dict template for orders built from a depth/leg object.

        - buy_if: the action string to use as 'BUY' for entry templates, 'SELL' for exit templates.
        - quantity: optional override for Quantity.
        """
        user_id = user_id if user_id is not None else self.params.get("user_ids")
        if leg_obj is None:
            raise RuntimeError("leg_obj is None when building order template")
        streaming_symbol = leg_obj["response"]["data"]["symbol"]
        trading_symbol = self.option_mapper[streaming_symbol]["tradingsymbol"]

        action = ActionEnum.BUY if buy_if.upper() == "BUY" else ActionEnum.SELL
        order_type = (
            OrderTypeEnum.MARKET if self.params["order_type"].upper() == "MARKET" else OrderTypeEnum.LIMIT
        )

        qty = int(self.params["quantity"]) if quantity is None else quantity

        return {
            "user_id": user_id,
            "Trading_Symbol": trading_symbol,
            "Exchange": self.exchange,
            "Action": action,
            "Order_Type": order_type,
            "Quantity": qty,
            "Slice_Quantity": self.params["slices"],
            "Streaming_Symbol": streaming_symbol,
            "Limit_Price": "0",
            "Disclosed_Quantity": 0,
            "TriggerPrice": 0,
            "ProductCode": ProductCodeENum.NRML,
            "remark": "Lord_Shreeji",
            "IOC":self.params['IOC_timeout']
        }

    # --- runtime helpers --------------------------------------------------------
    def _avg_price(self, data, side_key, n):
        # compute average of the first n bid/ask prices
        if data is None:
            logger.debug("_avg_price called with None data for side %s", side_key)
            return 0.0
        entries = data["response"]["data"][side_key]
        n = int(n)
        if n <= 1 or len(entries) == 0:
            return float(entries[0]["price"]) if entries else 0.0
        s = 0.0
        count = min(n, len(entries))
        for i in range(count):
            s += float(entries[i]["price"])
        return s / count

    def _process_user(self, uid, spread, call_price, put_price, call_price_exit=None, put_price_exit=None, call=None, put=None, bid_or_ask=None, bid_ask_exit=None):
        """Worker that runs ENTRY/EXIT logic for a single user (uid).

        This mirrors the original entry/exit logic but operates on per-user templates
        and updates per-user quantities under a lock.
        """
        # breakpoint()
        try:
            # prepare local copies of templates so a thread can mutate them safely
            od = self.order_templates[uid].copy()
            od_base = self.order_templates_base_leg[uid].copy()
            ex = self.exit_order_templates[uid].copy()
            ex_base = self.exit_order_templates_base_leg[uid].copy()

            # update limit prices the same way original code did
            if self.params["base_leg"] == "CE":
                od["Limit_Price"] = str(round((self.params.get("desired_spread") - call_price) * 20) / 20)
                od_base["Limit_Price"] = str(round(call_price * 20) / 20)
                exit_call = call_price_exit or call_price
                ex["Limit_Price"] = str(round((self.params.get("exit_desired_spread") - exit_call) * 20) / 20)
                ex_base["Limit_Price"] = str(round(exit_call * 20) / 20)
            else:
                exit_put = put_price_exit or put_price
                od["Limit_Price"] = str(round((self.params.get("desired_spread") - exit_put) * 20) / 20)
                od_base["Limit_Price"] = str(round(exit_put * 20) / 20)
                ex["Limit_Price"] = str(round((self.params.get("desired_spread") - exit_put) * 20) / 20)
                ex_base["Limit_Price"] = str(round(exit_put * 20) / 20)

            # ENTRY
            if self.params["action"].upper() == "BUY":
                # breakpoint()
                if spread < self.params["start_price"] and od["Quantity"] > 0 and self.params['run_state'] == 0:
                    
                    if not od["Quantity"] >= self.params["slices"]:
                        od["Slice_Quantity"] = od["Quantity"]
                    od = self.order.place_order(od)
                    self.order.IOC_order(od, od_base)
                    # read latest order data and update per-user counters
                    last_key = f"order:{od['user_id']}" + f"{od['remark']}" + f"{od.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.entry_qtys[uid] += filled
                            # decrement stored template quantity if present
                            if self.order_templates[uid].get("Quantity", 0) > 0:
                                self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                            print("Order placed: ", self.entry_qtys[uid])
                            # breakpoint()
                    return {"uid": uid, "action": "entry"}

                # EXIT
                if (spread > self.params["exit_start"] or self.params['run_state'] == 2) and self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0):
                    ex["Quantity"] = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
                    if not ex["Quantity"] >= self.params["slices"]:
                        ex["Slice_Quantity"] = ex["Quantity"]
                    ex = self.order.place_order(ex)
                    self.order.IOC_order(ex, ex_base)
                    last_key = f"order:{ex['user_id']}" + f"{ex['remark']}" + f"{ex.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data_exit = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data_exit["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.exit_qtys[uid] += filled
                    return {"uid": uid, "action": "exit"}

            else:  # SELL behaviour mirrored
                if spread > self.params["start_price"] and od["Quantity"] > 0 and self.params['run_state'] == 0:
                    if not od["Quantity"] >= self.params["slices"]:
                        od["Slice_Quantity"] = od["Quantity"]
                    od = self.order.place_order(od)
                    self.order.IOC_order(od, od_base)
                    last_key = f"order:{od['user_id']}" + f"{od['remark']}" + f"{od.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.entry_qtys[uid] += filled
                            if self.order_templates[uid].get("Quantity", 0) > 0:
                                self.order_templates[uid]["Quantity"] = max(0, self.order_templates[uid]["Quantity"] - filled)
                    return {"uid": uid, "action": "entry"}

                if (spread < self.params["exit_start"] or self.params['run_state'] == 2) and self.entry_qtys.get(uid, 0) > self.exit_qtys.get(uid, 0):
                    ex["Quantity"] = self.entry_qtys.get(uid, 0) - self.exit_qtys.get(uid, 0)
                    if not ex["Quantity"] >= self.params["slices"]:
                        ex["Slice_Quantity"] = ex["Quantity"]
                    ex = self.order.place_order(ex)
                    self.order.IOC_order(ex, ex_base)
                    last_key = f"order:{ex['user_id']}" + f"{ex['remark']}" + f"{ex.get('order_id', '')}"
                    last_raw = self.r.get(last_key)
                    if last_raw:
                        lastest_order_data_exit = orjson.loads(last_raw.decode())
                        filled = int(lastest_order_data_exit["response"]["data"]["fQty"])
                        with self.templates_lock:
                            self.exit_qtys[uid] += filled
                    return {"uid": uid, "action": "exit"}

        except (redis.RedisError, KeyError, IndexError, TypeError, ValueError) as e:
            logger.exception("per-user (%s) flow failed: %s", uid, e)
        except Exception:
            logger.exception("unexpected error in per-user (%s) worker", uid)
        return {"uid": uid, "error": True}

    
    # --- main logic (kept behavior identical) ----------------------------------
    def main_logic(self):
        lotsize = 75 if self.params["symbol"] == "NIFTY" else 20
        # run until both conditions are met: total entry == total exit AND run_state == 2
        while True:
            try:
                # early exit check: sum per-user counters + legacy counters
                try:
                    total_entry = sum(self.entry_qtys.values()) if hasattr(self, 'entry_qtys') else 0
                except Exception:
                    total_entry = 0
                try:
                    total_exit = sum(self.exit_qtys.values()) if hasattr(self, 'exit_qtys') else 0
                except Exception:
                    total_exit = 0
                # include legacy single counters if present
                total_entry += getattr(self, 'entry_qty', 0)
                total_exit += getattr(self, 'exit_qty', 0)
                try:
                    run_state_val = int(self.params.get('run_state', 0))
                except Exception:
                    run_state_val = 0
                if total_entry == total_exit and run_state_val == 2:
                    logger.info("Exit condition met: total_entry == total_exit == %s and run_state == 2", total_entry)
                    break
                
                if int(self.params['run_state']) == 1:
                    continue # pause
                
                # reload live depths each loop
                sym = self.params["symbol"].upper()
                call = self._depth_from_redis(f"depth:{sym}_{self.params['call_strike']}.0_CE-{self.params['expiry']}")
                put = self._depth_from_redis(f"depth:{sym}_{self.params['put_strike']}.0_PE-{self.params['expiry']}")

                bid_or_ask = "bidValues" if self.params["action"].upper() == "SELL" else "askValues"
                bid_ask_exit = "askValues" if self.params["action"].upper() == "SELL" else "bidValues"

                if int(self.params.get("no_of_bidask_average", 1)) > 1:
                    if self.params["base_leg"] == "CE":
                        call_price = self._avg_price(call, bid_or_ask, self.params["no_of_bidask_average"])
                        put_price = float(put["response"]["data"][bid_or_ask][0]["price"]) if put else 0.0
                        call_price_exit = self._avg_price(call, bid_ask_exit, self.params["no_of_bidask_average"])
                        put_price_exit = float(put["response"]["data"][bid_ask_exit][0]["price"]) if put else 0.0
                    else:
                        put_price = self._avg_price(put, bid_or_ask, self.params["no_of_bidask_average"])
                        call_price = float(call["response"]["data"][bid_or_ask][0]["price"]) if call else 0.0
                        put_price_exit = self._avg_price(put, bid_ask_exit, self.params["no_of_bidask_average"])
                        call_price_exit = float(call["response"]["data"][bid_ask_exit][0]["price"]) if call else 0.0
                else:
                    call_price = float(call["response"]["data"][bid_or_ask][0]["price"]) if call else 0.0
                    put_price = float(put["response"]["data"][bid_or_ask][0]["price"]) if put else 0.0
                    call_price_exit = float(call["response"]["data"][bid_ask_exit][0]["price"]) if call else 0.0
                    put_price_exit = float(put["response"]["data"][bid_ask_exit][0]["price"]) if put else 0.0

                # spread calculation preserved from original code
                spread = round(abs(call_price + put_price) * 20) / 20

                # print(
                #     "\nCondition is OFF, Buy : ",
                #     spread * lotsize,
                #     "  call_price : ",
                #     round(call_price * 20) / 20,
                #     " Put price : ",
                #     round(put_price * 20) / 20,
                #     " Buy spread : ",
                #     spread,
                #     "\n",
                # )
                # breakpoint()
                # If per-user templates are configured, run per-user logic in parallel
                if getattr(self, "uids", None):
                    max_workers = min(8, max(1, len(self.uids)))
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {
                            executor.submit(
                                self._process_user,
                                uid,
                                spread,
                                call_price,
                                put_price,
                                locals().get("call_price_exit"),
                                locals().get("put_price_exit"),
                                call,
                                put,
                                bid_or_ask,
                                bid_ask_exit,
                            ): uid
                            for uid in self.uids
                        }
                        for fut in as_completed(futures):
                            try:
                                res = fut.result()
                                logger.info("per-user result: %s", res)
                            except Exception as e:
                                logger.exception("per-user task failed: %s", e)
                    # throttle a tiny bit before next polling cycle
                    time.sleep(0.1)
                    continue

                # No single-template fallback: per-user tasks handled above.
                # Sleep to maintain the original polling cadence.
                time.sleep(2)
            except redis.RedisError as e:
                logger.exception("redis error in main loop: %s", e)
                time.sleep(0.5)
                continue
            except (KeyError, IndexError, TypeError, AttributeError, ValueError, orjson.JSONDecodeError) as e:
                # expected parsing/access errors - log and continue polling
                logger.warning("transient data error in main loop: %s", e)
                time.sleep(0.1)
                continue
            except Exception:
                logger.exception("unexpected error in main loop")
                time.sleep(0.1)
                continue
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received, exiting main loop")
                os._exit(0)