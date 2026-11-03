"""FSM state groups."""

from aiogram.fsm.state import State, StatesGroup


class PurchaseStates(StatesGroup):
    choosing_plan_type = State()
    choosing_ready_plan = State()  # ready packages with duration/tier toggles
    choosing_volume = State()
    choosing_users = State()
    choosing_duration = State()
    entering_service_name = State()
    choosing_renew_mode = State()
    renewing_custom = State()  # «تمدید از نو»: one screen, volume typed and days tapped
    entering_promo = State()
    confirming = State()
    waiting_for_receipt = State()


class AccountStates(StatesGroup):
    idle = State()


class AdminStates(StatesGroup):
    idle = State()
    waiting_order_id = State()
    waiting_ban_telegram_id = State()
    waiting_unban_telegram_id = State()
    waiting_reject_reason = State()  # custom reject reason text
    # Customer desk
    waiting_customer_query = State()
    waiting_wallet_amount = State()
    waiting_customer_dm = State()  # writing to one customer from their card
    waiting_gift_service_name = State()  # «🎁 ساخت سرویس»: the Latin name to seed the panel user
    waiting_reseller_number = State()  # one rate or one plan price
    waiting_service_days = State()
    waiting_service_gb = State()
    waiting_report_chat_id = State()
    waiting_report_topic_id = State()
    waiting_broadcast_body = State()
    # Outage / restore announcement: facts only, the wording is fixed.
    waiting_outage_server = State()
    waiting_outage_since = State()
    waiting_outage_alternative = State()
    waiting_restore_server = State()
    # Phase A2 — PasarGuard panel settings
    panel_waiting_base_url = State()
    panel_waiting_api_key = State()
    panel_waiting_sub_path = State()
    panel_waiting_timeout = State()
    # One state for all three trial numbers; `panel_field` says which.
    panel_waiting_trial = State()
    # STORE_ADMIN — draft field edits
    store_waiting_cat_title = State()
    store_waiting_cat_sort = State()
    store_waiting_cat_groups = State()
    store_waiting_cat_duration = State()  # months for new category duration
    store_waiting_cat_tier_groups = State()  # premium_panel_group_ids
    store_waiting_cat_new_title = State()  # create free-form group
    store_waiting_cat_kind = State()  # picking حجمی/نامحدود for the new group
    store_waiting_pkg_title = State()
    store_waiting_pkg_price = State()
    store_waiting_pkg_price_confirm = State()  # a 10x / tenth price waits for «بله»
    # Creating a plan. The dimension prompts are not optional: a plan without
    # them is diverted into the dynamic builder at purchase time and its fixed
    # price is discarded.
    store_waiting_new_pkg_size = State()  # only when «✏️ مقدار دیگر» is tapped
    store_waiting_new_pkg_duration = State()
    store_waiting_new_pkg_users = State()  # volume device limit (0 = نامحدود)
    store_waiting_cat_trial = State()  # «GB days» for one group's free trial, 0 = off
    waiting_qr_background = State()  # a photo: the service QR's new background
    store_waiting_new_pkg_price = State()
    store_waiting_new_pkg_title = State()  # optional rename, defaults to derived
    store_waiting_new_pkg_confirm = State()  # looking at the plan before it goes live
    store_waiting_sub_cat_title = State()  # create a child group
    store_waiting_promo = State()  # STORE_STRATEGY promo CRUD
    store_promo_first = State()  # promo wizard: first purchase only? (two buttons)
    store_promo_user_limit = State()  # promo wizard: uses per customer (a number)
    store_promo_total_limit = State()  # promo wizard: total uses (a number)
    store_promo_days = State()  # promo wizard: days until it expires (a number)
    store_waiting_pay_field = State()  # one payment-card field (P4)


class ReferralStates(StatesGroup):
    idle = State()


class DisruptionStates(StatesGroup):
    """«📡 گزارش اختلال»: the ISP is in FSM data; only a typed city waits here."""

    waiting_city = State()


class ResellerStates(StatesGroup):
    """A reseller filing a service under their own customer's name.

    The two label states are shared by every flow that needs one — buying,
    handing out a trial, relabelling an old service. Which flow is running
    lives in FSM data under ``label_flow``, because three copies of «اسمش چیه؟
    شماره‌شو می‌خوای؟» would drift apart the first time one of them changed.
    """

    waiting_service_query = State()
    waiting_customer_name = State()  # step 1: who is this service for
    waiting_customer_phone = State()  # step 2: their number, or «نمی‌خوام»


class MarketingStates(StatesGroup):
    """«📣 پیام‌های بازاریابی»: one typed number for a personal offer; ``mkt_campaign`` in FSM
    data says which campaign."""

    waiting_offer_percent = State()
    waiting_offer_hours = State()


class LifecycleStates(StatesGroup):
    """«🗓 هشدار و حذف سرویس»: one typed number; ``life_field`` in FSM data says which."""

    waiting_value = State()


class AccountLinkStates(StatesGroup):
    waiting_name = State()
