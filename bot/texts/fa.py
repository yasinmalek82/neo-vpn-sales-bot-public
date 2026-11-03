"""Persian user-facing copy for Telegram UX."""

from __future__ import annotations


class PersianTexts:
    # The brand names itself on the two screens a customer sees most: the
    # first message, and the menu they come back to after everything. It used
    # to name itself on neither — someone could buy, renew and get support
    # without ever learning whose bot this is, and a forwarded screenshot
    # pointed nowhere. The name is not a claim, so BRAND.md's «no unprovable
    # numbers» rule has nothing to say about it.
    welcome_new = (
        "سلام {name} 👋\n\n"
        "اینجا نئو نتورکه. سرویس می‌گیری و وضعیتش رو همین‌جا می‌بینی.\n"
        "👇 از منوی پایین شروع کن."
    )
    welcome_back = "خوش برگشتی {name} 👋\n👇 از منوی پایین ادامه بده."
    banned = "🚫 دسترسیت بسته شده.\nاگه فکر می‌کنی اشتباه شده، به پشتیبانی بگو."
    main_menu = "🏠 نئو نتورک\n\n👇 از منوی زیر ادامه بده."
    support = "💬 پشتیبانی\n\nهر چی هست بپرس — اتصال، تمدید، یا سروری که قطع شده."
    admin_mirza_history_title = "🗃 سوابق میرزا — {count} رکورد"
    admin_mirza_review_title = "🗃 میرزا؛ نیازمند بررسی — {count} رکورد"
    admin_mirza_empty = "رکوردی در این فهرست نیست."
    admin_mirza_unit_unknown = "واحد مبلغ نامشخص"
    admin_mirza_toman_amount = "{amount} تومان"
    admin_mirza_amount_unknown = "مبلغ نیازمند بررسی"
    admin_mirza_record_status = "وضعیت ثبت قدیمی: {status} • {disposition}"
    admin_mirza_record_amount = "مبلغ ثبت قدیمی: {amount}"
    admin_mirza_source_time = "زمان ثبت در منبع: {value}"
    admin_existing_link_ask = "نام کاربری دقیق سرویس موجود روی پنل را بفرست."
    admin_existing_link_preview = "🔗 اتصال سرویس موجود\n\n📡 سرویس: <code>{username}</code>\n👤 آیدی مشتری: {telegram_id}\n\nبا تأیید، این سرویس در فهرست سرویس‌های همین مشتری نمایش داده می‌شود. اتصال فعلی و حجم و انقضای پنل تغییر نمی‌کند."
    admin_existing_link_done = "سرویس موجود به این مشتری وصل شد."
    admin_existing_link_expired = "پیش‌نمایش منقضی شده؛ از پروندهٔ مشتری دوباره شروع کن."
    admin_existing_link_other_owner = "این سرویس به مشتری دیگری متصل است؛ مالکیت تغییر نکرد."
    admin_existing_link_infrastructure = "این نام برای زیرساخت رزرو شده و به مشتری متصل نمی‌شود."
    admin_existing_link_invalid_name = "نام کاربری دقیق سرویس را بدون لینک بفرست."
    admin_existing_link_archived = "این سرویس در سابقهٔ حذف یا آرشیو است؛ اول وضعیت آن را بررسی کن."
    admin_existing_link_changed = "مشخصات سرویس بعد از پیش‌نمایش تغییر کرده؛ نام را دوباره بفرست."
    admin_existing_link_failed = "اتصال سرویس ثبت نشد؛ وضعیت مشتری و سرویس را بررسی کن."
    admin_only = "این بخش فقط برای مدیران در دسترس است."
    disruption_pick_isp = "📡 اینترنتت از کدوم اپراتوره؟"
    disruption_pick_city = "🏙 کدوم شهری؟"
    disruption_write_city = "اسم شهرت رو بنویس، مثلاً «رشت»."
    disruption_bad_city = "فقط اسم شهر رو با حروف بنویس، مثلاً «رشت»."
    disruption_thanks = (
        "📡 گزارشت ثبت شد و به تیم فنی رسید.\n"
        "هر اپراتوری که گزارش بیشتری داشته باشه، زودتر بررسی می‌شه."
    )
    disruption_too_soon = (
        "گزارشت رو همین چند دقیقه پیش گرفتیم.\nاگه هنوز مشکل داری، به پشتیبانی پیام بده."
    )
    disruption_lost = "این گزارش نصفه موند. از «پشتیبانی» دوباره شروع کن."

    # One block, used by both the customer's prompt and the reseller's, and by
    # both «this name will not work» answers. Examples rather than rules: a
    # line of «فقط حروف و عدد انگلیسی» is read past, `ali12` is not.
    #
    # The ❌ list holds only what the bot actually **refuses**. It is tempting
    # to copy a competitor's richer list — `ali_`, `tele@`, `_mahdi` — but
    # `sanitize_service_name` accepts all three and quietly cleans them, so
    # calling them wrong would be the bot lying about itself. What it does
    # instead is say it cleans them, and then show the result.
    username_rules = (
        "فقط حروف و عدد انگلیسی. @ و فاصله و خط تیره رو خودم برمی‌دارم.\n\n"
        "✅ ali12 • mahdi • ws1_ksdf\n"
        "❌ محسن • علی — فارسی نمی‌شه"
    )

    # Outage / restore announcements. BRAND.md rule 6 calls these the most
    # important thing the brand says, so the wording is fixed and the operator
    # only supplies facts. One emoji across the whole delivered message.
    outage_announcement = "سرور {server} از ساعت {since} قطعه\n\nداریم روش کار می‌کنیم."
    outage_alternative_line = "تا درست شدن، از {alternative} استفاده کن."
    outage_followup_promise = "وقتی برگشت همین‌جا خبر می‌دم."
    restore_announcement = (
        "سرور {server} برگشت ✅\n\n"
        "اگه هنوز وصل نمی‌شی، یه بار قطع و وصل کن.\n"
        "بابت قطعی شرمنده."
    )

    # Global escape hatches (/cancel, unknown callbacks).
    cancelled_nothing = "چیزی برای لغو نبود. این هم منوی اصلی:"
    cancelled_done = "لغو شد. این هم منوی اصلی:"
    stale_button = "این دکمه مال یه پیام قدیمیه. از منوی جدید استفاده کن."
    unknown_message = (
        "🤔 این پیام رو نفهمیدم.\n\n"
        "اگه دکمه‌های ربات قبلی رو می‌بینی، پاک شدن. از منوی زیر ادامه بده."
    )
    error_generic = "⚠️ یه مشکلی پیش اومد. چند لحظه دیگه دوباره امتحان کن."
    referral_saved = "🎁 کد دعوت ثبت شد.\nتخفیفش روی خرید اولت اعمال می‌شه."
    referral_ignored_self = "نمی‌تونی خودت رو دعوت کنی."
    referral_already_set = "قبلاً با دعوت یه نفر دیگه اومدی، پس این لینک برات تغییری نمی‌ده."
    referral_too_late = "چون قبلاً از ربات خرید کردی، این لینک دعوت برات ثبت نمی‌شه."

    referral_home = (
        "🎁 دعوت دوستان\n\n"
        "هر کی با لینک تو بیاد، روی خرید اولش {referred_discount}٪ تخفیف می‌گیره.\n"
        "بعد از اینکه سرویسش فعال شد، پاداش تو ثبت می‌شه.\n\n"
        "👥 دعوت‌شده‌ها: {referred}\n"
        "⏳ پاداش در انتظار: {pending}\n"
        "🎁 پاداش تسویه‌شده: {paid}\n\n"
        "🎫 کد تو: <code>{code}</code>"
    )
    referral_pitch_intro = "📤 این متن رو برای دوستات بفرست:"
    referral_reward_notify = (
        "🎁 یه نفر با دعوت تو خرید کرد و سرویسش فعال شد.\n"
        "پاداش تو: {value}٪ ثبت شد (در انتظار تسویه)."
    )
    admin_store_prompt_cat_trial = (
        "تست رایگان این گروه: حجم (گیگ) و مدت (روز) رو با فاصله بفرست.\n"
        "مثلاً: 1 1\n"
        "برای خاموش کردن، 0 بفرست.\n"
        "تست روی همین گروه پنل ساخته می‌شه و هر نفر از هر گروه یه بار می‌تونه بگیره."
    )
    admin_qr_bg_title = (
        "🎨 پس‌زمینهٔ QR سرویس\n"
        "الان: {current}\n\n"
        "برای عوض کردن، یه عکس مربعی بفرست (نه فایل) که وسطش خالی باشه؛ "
        "کد QR وسطش روی یه کادر سفید می‌شینه. این پیش‌نمایشِ همون چیزیه که مشتری می‌بینه."
    )
    admin_qr_bg_current_template = "قالب پیش‌فرض (با آیدی ربات پایینش)"
    admin_qr_bg_current_custom = "عکسی که خودت فرستادی"
    admin_qr_bg_saved = "✅ پس‌زمینهٔ تازه ذخیره شد. پیش‌نمایش:"
    admin_qr_bg_bad = (
        "این عکس قابل استفاده نیست: {reason}\nیه عکس مربعی دیگه بفرست، یا «لغو» رو بزن."
    )
    admin_qr_bg_reason_not_an_image = "عکس نیست"
    admin_qr_bg_reason_too_small = "کوچک‌تر از ۴۰۰ پیکسل است"
    admin_qr_bg_reason_too_large = "بیشتر از ۱۰ مگابایت است"
    admin_qr_bg_reset_ask = (
        "عکس فعلی پاک می‌شه و QR سرویس‌ها دوباره روی قالب پیش‌فرض می‌آد. مطمئنی؟"
    )
    admin_qr_bg_reset_done = "قالب پیش‌فرض برگشت."
    admin_panel_prompt_trial_hwid = (
        "تعداد دستگاه همزمان اکانت تست رو بفرست (مثلاً ۱).\n"
        "برای برگشتن به مقدار پیش‌فرض فایل، «-» بفرست."
    )
    reseller_choose_plan = "📦 پلن‌ها با قیمت نمایندگی تو:"
    reseller_services_title = (
        "👥 مشتری‌های تو\n\n{total} سرویس • نمایش {first} تا {last}\n\n"
        "روی هر کدوم بزن تا تمدید و لینک و مصرفش رو ببینی."
    )
    reseller_services_found = "🔎 نتیجهٔ «{query}» — {total} سرویس"
    reseller_services_none_found = "سرویسی با «{query}» پیدا نشد."
    reseller_services_search_prompt = (
        "🔎 اسم یا شمارهٔ مشتری رو بفرست تا سرویسش رو پیدا کنم.\nچهار رقم آخر شماره هم کافیه."
    )

    # --- the two-step customer label (name, then phone) ---
    # Two steps, because the name seeds the panel username and the phone must
    # not: one box taking «reza 09123456789» would put eleven digits into every
    # username the shop later has to find in PasarGuard. Splitting them also
    # means «چهار رقم آخر» has a number to match — it only finds a phone that
    # was stored as one.
    reseller_label_name_prompt = (
        "👤 این سرویس برای کدوم مشتریه؟\n\n"
        + username_rules
        + "\n\nاسم پنلش می‌شه همین + چهار حرف تصادفی، و با همین توی پنل پیداش می‌کنی."
    )
    reseller_label_name_invalid = (
        "⚠️ این اسم نمی‌شه — هیچ حرف انگلیسی توش نیست.\n\n" + username_rules
    )
    reseller_label_phone_prompt = (
        "📱 شمارهٔ «{name}» رو هم داری؟\n"
        "بفرستش تا بعداً با چهار رقم آخرش پیداش کنی. اگه نداری، دکمهٔ زیر رو بزن."
    )
    reseller_label_phone_invalid = "این شماره نیست. فقط عدد بفرست، یا «شماره ندارم» رو بزن."
    reseller_label_skip_phone = "شماره ندارم"
    reseller_trial_label_free = "🎟 از سهمیهٔ رایگان این ماهت {remaining} تا مونده."
    reseller_trial_label_paid = (
        "🏦 سهمیهٔ رایگانت تموم شده — این یکی {price} تومان از کیف پولت کم می‌کنه."
    )
    reseller_rename_prompt = "✏️ الان ثبت‌شده: {current}\n\nاسم جدیدش رو بفرست."
    reseller_rename_done = (
        "✏️ ثبت شد: {label}\nاز این به بعد توی لیست مشتری‌هات با همین اسم پیداش می‌کنی."
    )
    reseller_trial_free = (
        "🎟 تست رایگان ساخته شد ({days} روز / {gb} گیگ)\n"
        "📂 گروه: {group}\n"
        "از سهمیهٔ این ماهت {remaining} تا مونده."
    )
    reseller_trial_charged = (
        "🎟 تست ساخته شد ({days} روز / {gb} گیگ)\n"
        "📂 گروه: {group}\n"
        "🏦 سهمیهٔ رایگان این ماهت تموم شده بود، {price} تومان از کیف پولت کم شد."
    )
    reseller_trial_exhausted = (
        "🎟 سهمیهٔ تست این ماهت ({quota} تا) تموم شده و قیمتی برای تست بیشتر تنظیم نشده.\n"
        "📅 دورهٔ فعلی: {period}"
    )
    reseller_trial_no_funds = (
        "🏦 سهمیهٔ رایگانت تموم شده و کیف پولت برای {price} تومان کافی نیست.\n"
        "اول کیف پولت رو شارژ کن."
    )
    trial_success = "🎟 تست رایگان فعال شد ({days} روز / {gb} گیگ).\n🔗 لینک سابسکریپشن:\n{sub_url}"
    trial_ready_intro = "🎟 تست رایگان فعال شد ({days} روز / {gb} گیگ)\n📂 گروه: {group}"
    trial_already_claimed = (
        "🎟 تست این سرویس رو قبلاً گرفتی.\nبرای ادامه از «خرید اشتراک» یه سرویس بگیر."
    )
    trial_see_my_services = "🎟 تست رایگانت فعاله.\nلینک و اطلاعات اتصال رو از «سرویس‌های من» ببین."
    trial_panel_missing = (
        "🎟 این تست دیگه روی پنل نیست، برای همین لینکش رو نمی‌فرستم.\n"
        "تست رایگان فقط یک بار ساخته می‌شه."
    )
    trial_already_claimed_with_link = (
        "🎟 تست رایگان قبلاً برات فعال شده.\n🔗 لینک سابسکریپشن:\n{sub_url}"
    )
    trial_already_ready_intro = "🎟 تست رایگان قبلاً برات فعال شده"
    trial_disabled = "🎟 تست رایگان فعلاً خاموشه.\nاز «خرید اشتراک» می‌تونی سرویس بگیری."
    trial_pick_group = "🎟 تست کدوم سرویس رو می‌خوای؟\nاز هر کدوم یه بار می‌تونی تست بگیری."
    trial_group_gone = "🎟 تست این سرویس الان فعال نیست. یکی از بقیه رو ببین."
    reseller_trial_pick_group = "🎟 تست کدوم سرویس رو به مشتری می‌دی؟"
    reseller_trial_none = "🎟 فعلاً هیچ سرویسی تست نماینده نداره.\nبه پشتیبانی پیام بده."
    trial_error = "⚠️ فعال‌سازی تست رایگان نشد. چند لحظه دیگه دوباره امتحان کن."
    renewal_reminder_intro = "⚠️ سرویست به‌زودی منقضی می‌شه ({days} روز مونده)"
    renewal_reminder = (
        "⚠️ سرویست به‌زودی منقضی می‌شه\n\n"
        "📅 سفارش #{order_id} حدود {days} روز دیگه تموم می‌شه.\n"
        "برای ادامه، «تمدید این سرویس» رو بزن یا از منوی خرید اقدام کن."
    )
    purchase_choose_type = "🛒 یکی از گروه‌ها رو انتخاب کن:"
    purchase_choose_time_limit = "📅 این گروه فیلتر محدودیت زمانی داره.\nکدوم دسته رو می‌خوای؟"
    purchase_choose_ready_plan = "📦 یکی از پلن‌ها رو انتخاب کن:"
    purchase_flat_shelf_intro = (
        "📦 پلن‌ها از کم به زیاد چیده شدن.\nقیمت‌ها به تومانه؛ «گیگی» یعنی هر گیگ چند."
    )
    purchase_flat_shelf_intro_unlimited = "📦 پلن‌ها از کم به زیاد چیده شدن.\nقیمت‌ها به تومانه."
    purchase_flat_no_user_limit = "👥 هیچ‌کدوم محدودیت تعداد کاربر ندارن."
    purchase_duration_month = "{n} ماهه"
    purchase_duration_none = "بدون انقضا"
    purchase_tier_economy = "اقتصادی"
    purchase_tier_premium = "ویژه"
    purchase_no_plans_here = "برای این مدت پلنی نذاشتم. یکی از بقیه رو ببین."
    purchase_plan_gone = "این پلن الان برداشته شد. لیست رو دوباره برات آوردم."
    purchase_renew_plan_off = "📅 تمدید این پلن خاموشه. برای ادامه باید یه سرویس جدید بخری."
    purchase_switch_plan_gone = (
        "پلن قبلی این سرویس دیگه فروخته نمی‌شه.\n"
        "یه پلن انتخاب کن تا روی همین سرویس و همین لینک فعال بشه."
    )
    purchase_switch_pick = "یه پلن انتخاب کن تا روی همین سرویس و همین لینک فعال بشه."
    purchase_switch_note = (
        "این پلن روی همین سرویس و همین لینک فعال می‌شه.\n"
        "⚠️ حجم و روزهای باقی‌موندهٔ پلن فعلی حذف می‌شه و پلن جدید از امروز شروع می‌شه."
    )
    purchase_no_ready_plans = "این دسته فعلاً پلنی نداره. یکی از دسته‌های دیگه رو ببین."
    purchase_choose_volume = "📦 چند گیگ لازم داری؟"
    purchase_volume_type_hint = "یکی از گزینه‌ها رو بزن یا فقط عدد رو بفرست ({low} تا {high} گیگ)."
    purchase_volume_typed_invalid = (
        "⚠️ حجم باید یه عدد صحیح باشه، از {low} تا {high} گیگ.\n"
        "یکی از گزینه‌ها رو بزن یا دوباره عدد رو بفرست."
    )
    purchase_custom_not_offered = (
        "🧩 خرید سفارشی الان برای تو فعال نیست. از این دسته‌ها یکی رو انتخاب کن."
    )
    purchase_choose_users = "👥 چند نفر همزمان وصل می‌شن؟"
    purchase_choose_duration_months = "📅 برای چند ماه؟"
    purchase_enter_promo = (
        "🎫 کد تخفیف داری؟ همین‌جا بفرست.\n" "\n" "اگه کد نداری، «بدون کد تخفیف» رو بزن."
    )
    purchase_invalid_promo = "🎫 این کد تخفیف معتبر نیست. دوباره بفرست یا بدون کد ادامه بده."
    purchase_promo_not_applicable = "🎫 این کد روی این پلن اعمال نمی‌شه. بدون کد ادامه بده."
    purchase_promo_expired = "🎫 مهلت این کد تخفیف تموم شده."
    purchase_promo_used_up = "🎫 ظرفیت این کد تخفیف پر شده."
    purchase_promo_first_only = "🎫 این کد فقط برای اولین خریده."
    purchase_promo_user_limit = "🎫 قبلاً از این کد استفاده کردی."
    purchase_price_updated = "💰 قیمت به‌روز شد. مبلغ جدید رو ببین و دوباره تأیید کن."
    purchase_choose_subgroup = "«{parent}» — کدوم رو می‌خوای؟"
    purchase_cancelled = (
        "❌ خرید لغو شد. چیزی از حسابت کم نشد.\nهر وقت خواستی، از «خرید اشتراک» دوباره شروع کن."
    )
    purchase_created = (
        "🧾 سفارش #{order_id} ثبت شد.\n" "\n" "برای تکمیل خرید، پرداخت رو از همین‌جا ادامه بده."
    )
    purchase_error = "⚠️ ثبت سفارش نشد. یه بار دیگه امتحان کن."
    purchase_paused_panel = (
        "⏸ ثبت سفارش جدید الان ممکن نیست.\n"
        "مشکل از سمت ماست و داریم بررسی می‌کنیم. پولی از تو کم نشده؛ کمی بعد دوباره امتحان کن."
    )
    purchase_paused_panel_popup = "ثبت سفارش الان ممکن نیست. کمی بعد دوباره امتحان کن."
    purchase_paid_provisioning_pending = (
        "🏦 مبلغ سفارش #{order_id} ({amount} تومان) از کیف پولت کم شده.\n"
        "\n"
        "نتیجهٔ فعال‌سازی هنوز مشخص نشده؛ سفارشت برای پیگیری ثبت شده.\n"
        "لازم نیست سفارش جدید بسازی.\n"
        "\n"
        "از «خرید اشتراک» ادامه بده تا همین سفارش دوباره پیگیری بشه؛ مبلغش دوباره کسر نمی‌شه."
    )
    purchase_resume_paid_order = (
        "🏦 سفارش #{order_id} رو قبلاً از کیف پولت پرداخت کردی.\n"
        "\n"
        "فعال‌سازی هنوز تأیید نشده. همین سفارش رو دوباره پیگیری می‌کنم؛ پرداخت جدیدی لازم نیست."
    )
    purchase_group_not_ready = (
        "این گروه هنوز آماده فروش نیست. یه گروه دیگه انتخاب کن یا بعداً سر بزن."
    )
    purchase_wallet_short = (
        "🏦 موجودی کیف پولت برای این خرید کافی نیست. یه بار دیگه از خلاصه ادامه بده."
    )

    payment_instructions = (
        "🧾 <b>پرداخت سفارش #{order_id}</b>\n"
        "\n"
        "💳 مبلغ قابل پرداخت کارت: <b>{amount}</b> ریال\n"
        "\n"
        "💳 شماره کارت: <code>{card_number}</code>\n"
        "👤 به نام: {holder}\n"
        "{bank_line}{extra}\n"
        "📸 بعد از واریز، عکس یا فایل تصویری رسید رو همین‌جا بفرست."
    )
    payment_already_confirmed = "پرداخت این سفارش قبلاً تأیید شده؛ لازم نیست رسید رو دوباره بفرستی."
    payment_unique_amount = (
        "💳 مبلغ بالا مخصوص همین سفارشه؛ دقیق واریزش کن.\n"
        "🏦 خرده‌مبلغ {suffix} تومان بعد از تأیید به کیف پولت برمی‌گرده.\n"
        "اگه پیامک بانک به ما برسه و تطبیق بخوره، پرداخت خودکار تأیید می‌شه.\n"
        "رسید رو هم بفرست تا در صورت نیاز دستی بررسی کنیم."
    )
    payment_receipt_received = (
        "📨 رسید سفارش #{order_id} ثبت شد.\n"
        "\n"
        "رسیدت منتظر بررسی دستی ادمینه.\n"
        "بعد از تأیید، کارت سرویس همین‌جا برات میاد.\n"
        "\n"
        "لازم نیست رسید رو دوباره بفرستی."
    )
    payment_need_photo = "📸 رسید رو به‌صورت عکس یا فایل تصویری بفرست."
    payment_submit_error = "⚠️ رسیدت ثبت نشد.\n" "\n" "اگه سفارش هنوز بازه، دوباره همین‌جا بفرستش."
    # A receipt for an order that had already expired. Customer-facing, so BRAND:
    # «تو», one emoji on the line that names the wallet, a number not an adjective.
    notify_late_receipt = (
        "⏰ سفارش #{order_id} قبل از رسیدن رسیدت منقضی شده بود.\n"
        "برای همین، سرویس خودکار فعال نمی‌شه.\n"
        "\n"
        "رسیدت برای بررسی ثبت شد.\n"
        "🏦 اگه واریزت تأیید بشه، {amount} تومان به کیف پولت اضافه می‌شه."
    )
    admin_late_receipt_caption = (
        "⏰ رسید برای سفارش منقضی‌شده\n"
        "مشتری: {user_label}\n"
        "سفارش: #{order_id}\n"
        "مبلغ کارت: {amount} تومان\n"
        "منقضی شد: {expired}\n\n"
        "سفارش فعال نمی‌شه. اگه پول واقعاً رسیده، کیف پول مشتری رو شارژ کن."
    )
    admin_late_receipt_ignored = "نادیده گرفته شد."
    admin_late_receipt_already = "این رسید قبلاً به کیف پول مشتری واریز شده."
    admin_late_receipt_gone = "این رسید دیگه قابل‌پرداخت نیست."
    payment_admin_caption = (
        "🧾 <b>رسید برای بررسی</b>\n"
        "\n"
        "سفارش: <b>#{order_id}</b>\n"
        "👤 مشتری: {user_label}\n"
        "\n"
        "💰 مبلغ: <b>{amount}</b> تومان\n"
        "💳 کارت اعلام‌شده: <code>{card}</code>\n"
        "\n"
        "واریز رو بررسی کن؛ اگه مبلغ به حساب رسیده، تأیید رو بزن."
    )
    payment_ready_intro = "🎉 <b>سرویس سفارش #{order_id} آماده‌ست.</b>"
    payment_rejected_user_reason = (
        "❌ پرداخت سفارش #{order_id} تأیید نشد.\n"
        "\n"
        "دلیل: {reason}\n"
        "\n"
        "اگه فکر می‌کنی اشتباهی شده، به پشتیبانی بگو تا بررسی بشه."
    )
    admin_ann_home = (
        "📢 اعلان قطعی\n\n"
        "متن ثابت است و تو فقط واقعیت‌ها را می‌دهی — این همان چیزی است که رقبا "
        "موقع قطعی نمی‌گویند.\n\n"
        "{status}"
    )
    admin_ann_ask_server = "نام سرور یا لوکیشن؟ (مثال: فنلاند)"
    admin_ann_ask_since = "از چه ساعتی؟ «الان» را بزنید یا ساعت را بفرستید (مثال: ۱۴:۳۰)."
    admin_ann_ask_alternative = (
        "تا درست شدن از چه چیزی استفاده کنند؟\n" "اگر جایگزینی ندارید «رد کن» را بزنید."
    )
    admin_ann_ask_restore_server = "کدام سرور برگشت؟"
    admin_ann_preview = (
        "پیش‌نمایش اعلان:\n\n" "———\n" "{preview}\n" "———\n\n" "مخاطب را انتخاب کنید."
    )
    admin_ann_invalid = "{error}\nدوباره بفرستید."
    admin_action_ok = "انجام شد."
    admin_action_denied = "دسترسی ادمین ندارید."
    admin_panel_clear_confirm = (
        "⚠️ بازگشت به پیش‌فرض .env\n\n"
        "همهٔ تنظیمات پنل که در ربات ذخیره کرده‌اید پاک می‌شود و مقادیر فایل .env جای آن را می‌گیرد.\n"
        "این کار برگشت‌پذیر نیست.\n\n"
        "تنظیمات فعلی:\n{current}"
    )
    admin_confirm_yes = "بله، انجام بده"
    admin_cannot_ban_admin = (
        "این کاربر خودش مدیر است و مسدود نمی‌شود.\n"
        "مدیر مسدودشده دیگر نمی‌تواند وارد ربات شود و فقط از طریق دیتابیس باز می‌گردد."
    )
    admin_action_error = "عملیات انجام نشد."

    account_services_title = "📡 سرویس‌های تو:\nروی هر کدوم بزن تا جزئیاتش باز شه."
    account_services_empty = (
        "📭 هنوز سرویسی نداری.\n\n"
        "با «خرید اشتراک» اولین سرویست رو بساز؛ وضعیت و حجم و لینکش همین‌جا می‌مونه.\n"
        "🎟 اگه واجد شرایطی، می‌تونی اول «تست رایگان» رو بگیری."
    )
    # «سرویس تو» is wrong on a reseller's screen — the service is their
    # customer's — so the title is a separate string the renderer picks.
    account_overview_title_own = "سرویس تو:"
    account_overview_title_customer = "سرویس این مشتری:"
    account_overview = (
        "{title}\n"
        "\n"
        "{expire_badge}🚦 وضعیت: {status}\n"
        "📅 انقضا: {expire}\n"
        "⏳ زمان باقی‌مانده: {time_bar}\n"
        "\n"
        "📦 حجم مصرف‌شده: {used}\n"
        "📦 سقف حجم: {limit}\n"
        "📊 مصرف حجم: {traffic_bar}\n"
        "\n"
        "🔗 لینک سابسکریپشن:\n"
        "{sub_url}"
    )
    account_expire_badge = "⚠️ این سرویس به‌زودی منقضی می‌شه\n\n"
    # Same line without the emoji: the service card composes intro + body, and an
    # intro that already carries one would make the delivered message two-emoji,
    # which BRAND.md rule 3 forbids. The rule is about the message, not the string.
    account_expire_badge_plain = "این سرویس به‌زودی منقضی می‌شه\n\n"
    account_howto_pick_platform = "📖 راهنمای اتصال\n" "سیستم‌عامل دستگاهت رو انتخاب کن:"
    account_howto_android = (
        "🤖 اندروید\n"
        "اپ‌های پیشنهادی:\n"
        "• Happ\n"
        "• V2Box\n"
        "• Streisand\n"
        "\n"
        "مراحل:\n"
        "۱) یکی از اپ‌ها را نصب کن\n"
        "۲) داخل اپ، QR همین پیام را اسکن کن\n"
        " یا لینک را کپی و وارد کن\n"
        "۳) اتصال را روشن کن"
    )
    account_howto_ios = (
        "🍎 آیفون / آی‌او‌اس\n"
        "اپ‌های پیشنهادی (اپ‌استور):\n"
        "• Streisand\n"
        "• V2Box\n"
        "• Happ\n"
        "\n"
        "مراحل:\n"
        "۱) یکی از اپ‌ها را نصب کن\n"
        "۲) QR را اسکن کن یا لینک را وارد کن\n"
        "۳) پروفایل را فعال کن و وصل شو"
    )
    account_howto_windows = (
        "💻 ویندوز\n"
        "اپ‌های پیشنهادی:\n"
        "• Happ\n"
        "• V2Box\n"
        "• Streisand\n"
        "\n"
        "مراحل:\n"
        "۱) یکی از اپ‌ها را نصب کن\n"
        "۲) لینک را از بخش افزودن / سابسکریپشن وارد کن\n"
        " یا QR را اسکن کن\n"
        "۳) سرور را انتخاب کن و وصل شو"
    )
    account_howto_macos = (
        "🖥️ مک\n"
        "اپ‌های پیشنهادی:\n"
        "• Streisand\n"
        "• V2Box\n"
        "• Happ\n"
        "\n"
        "مراحل:\n"
        "۱) یکی از اپ‌ها را نصب کن\n"
        "۲) لینک را وارد کن یا QR را اسکن کن\n"
        "۳) اتصال را فعال کن"
    )
    # Legacy combined howto (older call sites / tests).
    account_revoke_warn = "⚠️ با تغییر لینک، لینک و QR قبلی دیگه کار نمی‌کنه.\nادامه بدم؟"
    # The old link stopping is the whole consequence of this action, and the
    # message used not to mention it — so a customer would keep trying the
    # dead one and conclude the service broke.
    account_revoke_success = (
        "🔐 لینک جدید ساخته شد.\n"
        "لینک قبلی دیگه کار نمی‌کنه — توی برنامه‌ت لینک جدید رو جایگزین کن."
    )
    account_revoke_fail = "🔐 تغییر لینک نشد. چند لحظه دیگه دوباره امتحان کن."
    account_stale = "⚠️ ارتباط با پنل برقرار نشد؛ این آخرین اطلاعات ذخیره‌شده‌ست."
    account_no_url = "نداره"
    account_unlimited = "نامحدود"
    account_unknown = "—"
    account_refreshed = "اطلاعات به‌روز شد."
    account_orders_title = "🧾 سفارش‌های اخیر:"
    account_orders_empty = "🧾 هنوز سفارشی ثبت نشده.\nاولین سرویست رو از «خرید اشتراک» بگیر."
    account_order_line = "#{order_id} · {status} · {price} تومان"
    account_link_title = "🔗 لینک سابسکریپشن (برای کپی روش بزن):"
    account_qr_caption = "📱 QR سابسکریپشن (برای کپی، روی لینک زیر بزن):"
    account_qr_error = "⚠️ ساخت QR نشد. از لینک متنی بالا استفاده کن — همون کار رو می‌کنه."
    account_no_link = "🔗 لینک سابسکریپشن نداری.\nبه‌محض اینکه سرویست فعال بشه، لینک همین‌جا میاد."
    account_need_user = "اول با /start شروع کن."
    account_error = "⚠️ نمایش حساب نشد. چند لحظه دیگه دوباره امتحان کن."
    admin_home = "🛠️ پنل مدیریت"
    admin_home_pending = "💳 {n} پرداخت در انتظار"
    admin_home_pending_stale = " ({n} کهنه)"
    admin_home_pending_none = "💳 پرداختی در انتظار نیست"
    admin_home_today = "📈 امروز: {n} فروش • {amount} تومان"
    admin_home_warn_group = "⚠️ «{title}» گروه پنل ندارد — فروخته نمی‌شود"
    admin_home_warn_report_off = "⚠️ گروه گزارش وصل نیست"
    admin_home_warn_report_unready = "⚠️ {n} تاپیک گزارش هنوز تأیید نشده"
    admin_home_warn_node = "⚠️ سرور «{name}» قطع است (از {since})"
    admin_home_warn_panel = "⚠️ پنل در دسترس نیست (از {since})"
    admin_home_sec_sales = "💰 فروش و سفارش"
    admin_home_sec_people = "👥 مشتری‌ها"
    admin_home_sec_settings = "⚙️ تنظیمات"
    admin_pending_title = "پرداخت‌های در انتظار ({total}) — صفحه {page}:"
    admin_pending_empty = "پرداخت در انتظاری نیست."
    admin_pending_line = "#{order_id} · {amount} تومان · {age}{stale_mark} · {user_label}{receipt}"
    admin_pending_receipt_badge = " 📷 فیش"
    admin_pending_stale_mark = " ⚠️ کهنه"
    admin_pending_filter_stale_empty = "پرداخت کهنه‌ای (بیش از ۱ ساعت) در صف نیست."
    admin_orders_filter = "فیلتر سفارش‌ها را انتخاب کن:"
    admin_orders_title = "سفارش‌ها — {filter} ({total}) — صفحه {page}:"
    admin_orders_empty = "سفارشی برای فیلتر «{filter}» پیدا نشد."
    admin_orders_all = "همه"
    admin_order_line = "#{order_id} · {status} · {amount} تومان"
    admin_order_detail = (
        "🧾 <b>جزئیات سفارش #{order_id}</b>\n"
        "🚦 وضعیت: <b>{status}</b>\n"
        "\n"
        "👤 مشتری: {user_label}\n"
        "حساب مسدود: {banned}\n"
        "\n"
        "پلن: {plan}\n"
        "💰 مبلغ: <b>{price}</b> تومان\n"
        "پرداخت: {payment_status}\n"
        "\n"
        "📡 نام سرویس در پنل:\n"
        "<code>{panel}</code>"
    )
    admin_order_not_found = "سفارش پیدا نشد."
    admin_prompt_order_id = "جستجوی سفارش\n\n" "شناسه عددی سفارش را بفرست (فقط رقم).\n" "مثال: 42"
    admin_prompt_ban_telegram_id = (
        "مسدود کردن کاربر\n\n"
        "آیدی عددی تلگرام کاربر را بفرست.\n"
        "پس از تایید، کاربر از خرید و منو محروم می‌شود."
    )
    admin_prompt_unban_telegram_id = (
        "رفع مسدودی کاربر\n\n" "آیدی عددی تلگرام کاربر را بفرست تا دسترسی‌اش برگردد."
    )
    admin_invalid_id = "شناسه باید عدد باشد."
    admin_user_not_found = "کاربر پیدا نشد."
    admin_ban_ok = "✅ کاربر {telegram_id} مسدود شد و دیگر به ربات دسترسی ندارد."
    admin_unban_ok = "✅ مسدودی کاربر {telegram_id} برداشته شد؛ دسترسی برقرار است."
    admin_retry_ok = "فعال‌سازی دوباره انجام شد."
    admin_retry_failed = "تلاش مجدد فعال‌سازی ناموفق بود."
    admin_stats = (
        "💰 <b>گزارش مالی</b>\n"
        "{badge}\n\n"
        "<b>فروش</b>\n"
        "{rows}\n\n"
        "<b>کیف پول مشتری‌ها</b>\n"
        "بدهی فعلی (قابل خرج): {liability}\n"
        "مجموع شارژشده: {credited} | مجموع خرج‌شده: {spent}\n\n"
        "<b>وضعیت</b>\n"
        "سرویس ثبت‌شده: {active_services} | مشتری خریدار: {customers}\n"
        "کاربران: {users} (مسدود: {banned})\n"
        "{attention}"
    )
    admin_stats_row = "{label}: {revenue}  ({orders} سفارش، میانگین {average})"
    admin_stats_attention_none = "✅ چیزی منتظر رسیدگی نیست."
    admin_stats_attention = "⚠️ نیازمند رسیدگی — در انتظار بررسی: {review} | ناموفق پنل: {failed}"
    web_stats_attention = "⚠️ نیازمند رسیدگی: در انتظار بررسی {review} | ناموفق پنل {failed}"
    admin_stats_note = "درآمد از لحظهٔ فعال‌شدن سرویس شمرده می‌شود، نه ثبت سفارش. امروز و بازه‌ها به وقت تهران حساب می‌شوند."

    # --- customer desk ---
    admin_users_title = (
        "کاربران — {label}\n\n"
        "{total} نفر • نمایش {first} تا {last}\n\n"
        "روی هر کاربر بزن تا کارتش باز شه."
    )
    admin_users_empty = "هنوز هیچ کاربری ثبت نشده."
    admin_users_filter_empty = "هیچ کاربری با فیلتر «{label}» نیست."
    admin_customer_prompt = (
        "👤 <b>جستجوی مشتری</b>\n\n"
        "هرچه از مشتری داری بفرست:\n"
        "• آیدی عددی تلگرام — <code>123456789</code>\n"
        "• یوزرنیم — <code>@yasin</code> یا <code>yasin</code>\n"
        "• بخشی از نام — <code>محمد</code>\n"
        "• کد دعوت — <code>REF42</code>\n"
        "• اسم کانفیگ — <code>reza_a7f3</code>\n"
        "• لینک ساب (کامل بفرست)\n"
        "• شمارهٔ سفارش — <code>#123</code>"
    )
    admin_customer_none = "مشتری‌ای با «{query}» پیدا نشد."
    admin_customer_results = "👤 {count} نتیجه برای «{query}»:"
    admin_customer_card = (
        "👤 <b>{name}</b>\n"
        "{handle} • آیدی <code>{telegram_id}</code>\n"
        "{status} • {role}\n\n"
        "عضویت: {joined}\n"
        "آخرین فعالیت: {last_seen}\n"
        "تست: {trial}\n\n"
        "🏦 کیف پول: <b>{wallet}</b>\n"
        "مجموع شارژ: {topped_up} • مجموع خرید: {spent}\n"
        "🛒 سفارش‌ها: {orders_total} (پرداخت‌شده: {orders_paid})\n"
        "آخرین سفارش: {last_order}\n\n"
        "🎁 زیرمجموعه: {referrals} • معرف: {referrer}\n"
        "کد دعوت: <code>{referral_code}</code>\n\n"
        "📱 سرویس‌ها: {services_count}\n\n"
        "{ledger}"
    )
    admin_customer_role_admin = "⚙️ ادمین"
    admin_customer_role_customer = "مشتری"
    admin_customer_role_reseller = "🏪 نماینده"
    admin_customer_trial_taken = "{title} ✓ {when}"
    admin_customer_trial_open = "{title} —"
    admin_customer_trial_legacy_taken = "{title} ✓ قدیمی؛ تاریخ نامشخص"
    admin_customer_trial_none = "—"
    admin_customer_ban_ask = (
        "🚫 مسدود کردن <b>{name}</b>؟\n"
        "«فقط ربات» یعنی دیگه به ربات دسترسی نداره ولی سرویس‌هاش کار می‌کنه.\n"
        "با «قطع سرویس‌ها» سرویس‌هاش هم روی پنل قطع می‌شه ({n} سرویس)."
    )
    admin_customer_unban_ask = (
        "رفع مسدودی <b>{name}</b>؟\n"
        "می‌تونی سرویس‌های قطع‌شده‌اش ({n} سرویس) رو هم همین‌جا وصل کنی."
    )
    admin_customer_ban_done = "مسدود شد."
    admin_customer_unban_done = "مسدودی برداشته شد."
    admin_customer_services_result = "{done} سرویس {verb} شد"
    admin_customer_services_failed = "، {failed} تا انجام نشد"
    admin_customer_no_referrer = "—"
    # Web twin of the card's empty marker: a word a page can read, not a dash from a chat.
    web_customer_empty = "ندارد"
    admin_customer_status_banned = "🚫 مسدود"
    admin_customer_status_ok = "✅ فعال"
    admin_customer_no_services = "— هنوز سرویسی ندارد"
    admin_customer_ledger_title = "آخرین تراکنش‌های کیف پول:"
    admin_customer_ledger_empty = "کیف پول تراکنشی ندارد."
    admin_customer_not_found = "کاربر پیدا نشد."
    admin_reseller_granted = "نماینده شد."
    admin_reseller_revoked = "نمایندگی برداشته شد."
    admin_reseller_revoke_confirm = (
        "⚠️ حذف نمایندگی «{name}»؟\n\n"
        "از همین لحظه خریدش با قیمت عادی حساب می‌شود.\n"
        "قیمت‌های توافقی‌اش پاک نمی‌شوند؛ اگر دوباره نماینده‌اش کنی برمی‌گردند.\n"
        "سرویس‌ها و کیف پولش دست نمی‌خورد."
    )
    admin_reseller_already_not = "این مشتری الان نماینده نیست؛ چیزی تغییر نکرد."
    admin_reseller_panel = "💵 قیمت‌های نمایندگی <b>{name}</b>"
    admin_reseller_status_month = "این ماه: {sales} فروش • تست {used}/{quota}"
    admin_reseller_status_wallet = "🏦 کیف پول: {wallet}"
    admin_reseller_status_no_rate = (
        "⚠️ نرخ هر گیگ تعریف نشده؛ خرید سفارشی برای این نماینده نشان داده نمی‌شود"
    )
    admin_reseller_custom_intro = (
        "🧩 نرخ خرید سفارشی\n\n"
        "قیمت = (گیگ × نرخ گیگ) + (روز × نرخ روز)\n"
        "اگه جایی از قیمت خرده بالاتر بزنه، خودکار قیمت خرده گرفته می‌شه "
        "و بهت خبر می‌دم."
    )
    admin_reseller_trial_intro = (
        "🧪 تست نمایندگی\n\n"
        "تا سقف سهمیه رایگانه؛ بعدش هر تست از کیف پول خودش کم می‌شه.\n"
        "سهمیه اول هر ماه شمسی ریست می‌شه."
    )
    admin_reseller_plans_intro = (
        "🎁 پلن‌های آماده\n\n"
        "روی هر پلن بزن تا قیمت نمایندگی‌شو بذاری.\n"
        "پلنی که قیمت نداره با قیمت خرده بهش فروخته می‌شه."
    )
    admin_reseller_no_plans = "هنوز پلن آماده‌ای در فروشگاه نیست."
    admin_reseller_prompt_gb = "نرخ هر گیگ برای این نماینده رو به تومان بفرست (مثلاً 1200):"
    admin_reseller_prompt_day = "نرخ هر روز برای این نماینده رو به تومان بفرست (مثلاً 500):"
    admin_reseller_prompt_quota = "چند تست رایگان در ماه بهش بدیم؟ (مثلاً 5):"
    admin_reseller_prompt_tprice = "تست بیشتر از سهمیه، هر کدام چند تومان؟ (مثلاً 5000):"
    admin_reseller_prompt_plan = (
        "قیمت نمایندگی برای «{title}» رو بفرست.\n" "قیمت خرده الان: {retail}"
    )
    admin_reseller_price_cleared = "برگشت به قیمت خرده."

    admin_reseller_matrix_title = "قیمت‌های نمایندگی — {name}"
    admin_reseller_cell_row = "{plan} • خرده {retail} • نماینده {agreed}"
    admin_reseller_cell_none = "—"
    web_reseller_cell_none = "نماینده: تعیین نشده"
    admin_reseller_no_plans_tab = "برای این مدت پلنی با قیمت خرده وجود ندارد."
    admin_reseller_flat_hint = "فهرست ساده: هر پلن با مدت خودش قیمت می‌گیرد. تب مدت این‌جا نیست."
    admin_reseller_pct_btn = "٪ کم کردن از این تب"
    admin_reseller_pct_btn_flat = "٪ کم کردن از این فهرست"
    admin_reseller_pct_prompt = (
        "چند درصد از قیمت‌های نمایندهٔ همین تب کم شود؟\n" "فقط عدد بفرست — بین ۱ تا ۹۰."
    )
    admin_reseller_pct_prompt_flat = (
        "چند درصد از قیمت‌های نمایندهٔ همین فهرست کم شود؟\n" "فقط عدد بفرست — بین ۱ تا ۹۰."
    )
    # Web twins: the same questions where a number is typed into a form, not sent in a chat.
    web_reseller_pct_prompt = (
        "چند درصد از قیمت‌های نمایندهٔ همین تب کم شود؟ یک عدد بین ۱ تا ۹۰ بنویس."
    )
    web_reseller_pct_prompt_flat = (
        "چند درصد از قیمت‌های نمایندهٔ همین فهرست کم شود؟ یک عدد بین ۱ تا ۹۰ بنویس."
    )
    admin_resellers_title = "🏪 نمایندگان ({count})\nروی هر کدام بزن تا قیمت‌هایش را بگذاری."
    admin_resellers_empty = "هنوز نماینده‌ای نداری.\nاز کارت مشتری، «نماینده کردن» را بزن."
    admin_resellers_month_hint = "فروش و تست‌ها مال همین ماه شمسی‌اند."
    admin_resellers_omitted = "{n} نماینده در این فهرست جا نشد."
    admin_reseller_pct_invalid = "عدد بین ۱ تا ۹۰ بفرست."
    admin_reseller_pct_preview = (
        "🔎 پیش‌نمایش — هنوز چیزی ثبت نشده\n\n"
        "{lines}\n\n"
        "{count} قیمت تغییر می‌کند. تأیید می‌کنی؟"
    )
    admin_reseller_pct_preview_line = "{plan}: {before} ← {after}"
    admin_reseller_pct_nothing = "در این تب قیمت نمایندگی‌ای برای کم کردن نیست."
    admin_reseller_pct_done = "{count} قیمت تغییر کرد."
    admin_reseller_undo_btn = "↩️ برگرداندن آخرین تغییر"
    admin_reseller_undo_done = "{count} قیمت به مقدار قبلی برگشت."
    admin_reseller_nothing_to_undo = "چیزی برای برگرداندن نیست."
    # A confirm tapped after the preview fell out of FSM state — a restart, or an
    # old message. «چیزی برای برگرداندن نیست» is the undo screen's line and reads
    # as if the change had already happened and been reverted.
    admin_reseller_pct_expired = "این پیش‌نمایش منقضی شده. دوباره درصد را بزن."

    admin_customer_orders_title = (
        "🛍 سفارش‌های این کاربر: {total} (صفحهٔ {page} از {pages})\n"
        "مبلغ‌ها به تومانه. روی هر سفارش بزن تا جزئیاتش باز شه."
    )
    admin_customer_services_title = "📱 سرویس‌های این کاربر: {n}\nروی هر سرویس بزن تا مدیریتش کنی."
    admin_customer_services_empty = "این کاربر سرویس فعالی نداره."
    admin_customer_orders_empty = "این کاربر هنوز سفارشی ثبت نکرده."
    admin_customer_refs_title = (
        "🎁 زیرمجموعه‌های این کاربر: {total} نفر • صفحهٔ {page} از {pages}\n"
        "روی هر نفر بزن تا کارتش باز شه."
    )
    admin_customer_refs_empty = "این کاربر هنوز کسی رو دعوت نکرده."
    admin_customer_trial_reset_done = "حالا می‌تونه دوباره تست بگیره."
    admin_wallet_zero_confirm = (
        "⚠️ کل موجودی <b>{name}</b> صفر بشه؟\n\n"
        "مبلغی که کم می‌شه: <b>{amount}</b>\n"
        "این کار در دفتر کیف پول ثبت می‌شه و برگشت‌پذیر نیست."
    )
    admin_wallet_zero_already = "کیف پول این کاربر از قبل خالیه."
    admin_wallet_zero_done = "{amount} از کیف پول کم شد."
    admin_customer_dm_prompt = (
        "پیامی که می‌خوای برای <b>{name}</b> بره رو بفرست.\n"
        "همون‌طور که می‌نویسی براش ارسال می‌شه."
    )
    admin_customer_dm_sent = "پیام برای کاربر فرستاده شد."
    admin_customer_dm_failed = (
        "پیام نرفت — احتمالاً کاربر ربات رو بلاک کرده یا هیچ‌وقت استارت نزده."
    )
    admin_wallet_prompt = (
        "🏦 تغییر کیف پول <b>{name}</b>\n"
        "موجودی فعلی: {wallet}\n\n"
        "مبلغ را به تومان بفرست:\n"
        "• شارژ: <code>50000</code>\n"
        "• کسر: <code>-20000</code>"
    )
    admin_wallet_confirm = (
        "⚠️ تأیید تغییر کیف پول\n\n"
        "مشتری: <b>{name}</b>\n"
        "{verb}: <b>{amount}</b>\n"
        "موجودی بعد از اعمال: <b>{after}</b>\n\n"
        "این تغییر در دفتر کیف پول ثبت می‌شود و برگشت‌پذیر نیست."
    )
    admin_wallet_insufficient = "موجودی کافی نیست. موجودی فعلی: {wallet}"
    admin_wallet_verb_credit = "شارژ"
    admin_wallet_verb_debit = "کسر"
    admin_wallet_done = "کیف پول به‌روز شد. موجودی جدید: {wallet}"

    # `notify_*`, not `admin_*`: the owner triggers these, the **customer**
    # reads them, so they follow BRAND.md like any other customer copy. The
    # prefix is what `tests/unit/test_brand_voice.py` keys on — these four had
    # already drifted to «شما» once under an `admin_` name, unchecked.
    notify_wallet_credit = "🏦 کیف پولت {amount} تومان شارژ شد.\n💰 موجودی جدید: {wallet} تومان"
    notify_wallet_debit = "🏦 از کیف پولت {amount} تومان کسر شد.\n💰 موجودی جدید: {wallet} تومان"

    # --- live service operations ---
    admin_service_card = (
        "📱 <b>{name}</b>\n"
        "یوزرنیم پنل: <code>{panel_username}</code>\n"
        "وضعیت: {status}\n"
        "انقضا: {expire}\n"
        "حجم: {data}{extras}\n\n"
        "این اطلاعات مستقیم از پنل خوانده شد."
    )
    admin_service_line_last_online = "آخرین اتصال: {when}"
    admin_service_line_devices = "دستگاه‌ها: {value}"
    admin_service_devices_unlimited = "بدون محدودیت"
    admin_service_line_last_update = "آخرین به‌روزرسانی لینک: {app} • {when}"
    admin_service_devices_title = "📱 <b>دستگاه‌های {name}</b>\nتعداد: {count} از {limit}\n\n{rows}"
    admin_service_devices_empty = "هنوز هیچ دستگاهی ثبت نشده."
    admin_service_devices_reset_confirm = (
        "همهٔ دستگاه‌های ثبت‌شدهٔ «{name}» پاک بشن؟\n"
        "مشتری می‌تونه دوباره با دستگاه‌های جدید وصل بشه."
    )
    admin_service_devices_reset_done = "✅ دستگاه‌ها ریست شدن."
    admin_service_link_screen = (
        "🔗 <b>لینک {name}</b>\n\n<code>{url}</code>\n\n"
        "«بفرست برای مشتری» کارت کامل سرویس رو برای خودش می‌فرسته."
    )
    admin_service_link_none = "برای این سرویس لینکی ذخیره نشده. «لینک جدید» یکی می‌سازه."
    admin_service_link_revoke_confirm = (
        "لینک «{name}» عوض بشه؟\n"
        "لینک قبلی از همین لحظه کار نمی‌کنه و مشتری باید لینک جدید رو وارد کنه."
    )
    admin_service_link_revoked = "✅ لینک جدید ساخته شد. حالا می‌تونی برای مشتری بفرستیش."
    admin_service_link_revoke_failed = "لینک جدید ساخته نشد. پنل جواب نداد یا لینکی برنگردوند."
    admin_service_link_sent = "✅ برای مشتری فرستاده شد."
    admin_service_link_send_failed = "فرستادن برای مشتری انجام نشد. شاید ربات رو مسدود کرده."
    admin_service_qr_failed = "QR ساخته نشد."
    admin_service_unused_value = (
        "💰 ارزش روزهای باقی‌مانده: {amount} تومان\n"
        "({paid} پرداختی ÷ {period} روز × {days} روز مانده) — اگه جبران کیف پول لازم شد"
    )
    admin_service_status_active = "✅ فعال"
    admin_service_status_disabled = "⛔️ قطع‌شده"
    admin_service_expire_none = "بدون انقضا"
    admin_service_expired = "{when} (منقضی شده)"
    admin_service_data_unlimited = "نامحدود"
    admin_service_data_metered = "{used} از {limit} مصرف شده • {remaining} باقی"
    admin_service_prompt_extend = (
        "📅 تمدید «{name}»\n"
        "انقضای فعلی: {expire}\n\n"
        "چند روز اضافه شود؟ فقط عدد. مثال: <code>7</code>"
    )
    admin_service_prompt_data = (
        "📦 افزودن حجم به «{name}»\n"
        "حجم فعلی: {data}\n\n"
        "چند گیگ اضافه شود؟ فقط عدد. مثال: <code>20</code>"
    )
    admin_service_toggle_confirm_off = (
        "⚠️ قطع سرویس «{name}»\n\n"
        "مشتری بلافاصله اتصالش را از دست می‌دهد.\n"
        "حجم، انقضا و لینک اشتراک حفظ می‌شوند و با یک دکمه برمی‌گردد."
    )
    admin_service_data_confirm = (
        "📦 افزودن حجم به «{name}»\n\n"
        "اضافه می‌شود: {amount} گیگ\n"
        "سقف الان: {before}\n"
        "سقف بعد از افزودن: {after}\n\n"
        "اضافه شود؟"
    )
    admin_service_data_nothing_pending = "چیزی برای افزودن در انتظار نیست."
    admin_service_toggle_confirm_on = "فعال‌سازی دوبارهٔ «{name}»؟"
    admin_service_extended = "✅ تمدید شد. انقضای جدید: {expire}"
    admin_service_data_added = "✅ حجم اضافه شد. سقف جدید: {limit}"
    admin_service_disabled = "⛔️ سرویس قطع شد."
    admin_service_enabled = "✅ سرویس فعال شد."

    # Customer-facing — see the note on `notify_wallet_credit`.
    notify_service_extended = "📅 سرویس «{name}» تمدید شد.\n📅 انقضای جدید: {expire}"
    notify_service_link_resent = (
        "🔗 لینک سرویست رو دوباره برات فرستادم. همین رو توی برنامه وارد کن."
    )
    notify_service_data = "📦 به سرویس «{name}» {amount} حجم اضافه شد."
    # The intro above the service card the owner created for a customer.
    # Customer-facing, so BRAND: «تو», no emoji here (the card carries its own).
    notify_admin_service_created = (
        "🎉 سرویس جدیدت آماده‌ست. لینکش پایینه؛ همون رو توی برنامه وارد کن."
    )

    # --- «🎁 ساخت سرویس برای مشتری» (owner only) ---
    admin_gift_pick_group = "🎁 ساخت سرویس برای <b>{name}</b>\nگروه رو انتخاب کن:"
    admin_gift_no_groups = (
        "الان گروهی برای ساخت سرویس نیست: یا خاموشه، یا گروه پنل نداره، یا پلن آماده‌ای توش نیست."
    )
    admin_gift_pick_plan = "🎁 <b>{name}</b> • {group}\nپلن رو انتخاب کن:"
    admin_gift_ask_name = (
        "🎁 {plan}\n"
        "اسم سرویس رو با حروف لاتین بفرست؛ اسم کاربری روی پنل با همین شروع می‌شه.\n"
        "مثلاً: <code>reza</code>"
    )
    admin_gift_name_bad = "این اسم قابل استفاده نیست. فقط حروف لاتین، عدد و _ بفرست."
    admin_gift_name_cleaned = " (از «{raw}» ساخته شد)"
    admin_gift_confirm = (
        "🎁 تأیید ساخت سرویس\n\n"
        "مشتری: <b>{name}</b>\n"
        "گروه: {group}\n"
        "پلن: {plan}\n"
        "اسم سرویس: <code>{service}</code>{cleaned}\n"
        "قیمت: {price}\n"
        "کیف پول مشتری: {wallet}\n\n"
        "«رایگان» بدون هیچ کسری ساخته می‌شه. «از کیف پول» همین قیمت رو از "
        "کیف پول مشتری کم می‌کنه.{reseller_note}"
    )
    admin_gift_reseller_note = "\nنماینده‌ست؛ پرداخت از کیف پول اینجا نیست، فقط هدیه."
    admin_gift_wallet_button = "💳 از کیف پول ({price})"
    admin_gift_expired = "این صفحه منقضی شده؛ از کارت مشتری دوباره شروع کن."
    admin_gift_done = "✅ سرویس ساخته شد و کارتش برای مشتری رفت."
    admin_gift_done_undelivered = (
        "✅ سرویس ساخته شد ولی پیام به مشتری نرسید (شاید ربات رو بلاک کرده). "
        "از کارت سرویس می‌تونی لینک رو بفرستی."
    )
    admin_gift_err_open_order = "این مشتری یه سفارش باز داره. اول اون تموم یا لغو بشه."
    admin_gift_err_group = "این گروه یا پلن الان برای فروش خاموشه یا پنل نداره."
    admin_gift_err_price = "این پلن قیمت نداره."
    admin_gift_err_reseller_wallet = "برای نماینده پرداخت از کیف پول اینجا نیست؛ فقط هدیه."
    admin_gift_err_wallet_short = "موجودی کیف پول مشتری کم شده؛ صفحه رو دوباره باز کن."
    admin_gift_err_key = (
        "این درخواست مال مشتری دیگه‌ایه یا دیگه قابل ادامه نیست؛ از کارت دوباره شروع کن."
    )
    admin_gift_err_generic = "ساخت سرویس انجام نشد. چند لحظه بعد دوباره امتحان کن."
    admin_gift_provision_failed = (
        "ساخت روی پنل انجام نشد. دوباره همون دکمهٔ تأیید رو بزن؛ "
        "همون سفارش ادامه پیدا می‌کنه و دوبار حساب نمی‌شه."
    )
    admin_gift_provision_failed_refunded = (
        "ساخت روی پنل انجام نشد و {amount} به کیف پول مشتری برگشت."
    )

    # --- report group ---
    admin_reports_title = (
        "📢 <b>گروه گزارش</b>\n\n{status}\n\n"
        "هر نوع گزارش به یک تاپیک می‌رود تا بتوانی «کیف پول» را بی‌صدا کنی "
        "بدون اینکه «فروش» یا «پرداخت» را از دست بدهی.\n\n"
        "⚪️ تنظیم‌نشده • 🟡 تنظیم‌شده ولی تست‌نشده • ✅ آماده"
    )
    admin_reports_not_attached = "هنوز گروهی وصل نیست."
    admin_reports_attached = (
        "گروه: <b>{title}</b>\n" "وضعیت گزارش‌دهی: {state}\n" "تاپیک آماده: {ready} از {total}"
    )
    admin_reports_state_on = "✅ روشن"
    admin_reports_state_off = "⛔️ خاموش"
    admin_reports_attach_help = (
        "🔗 <b>وصل کردن گروه گزارش</b>\n\n"
        "قبل از ادامه این سه کار را انجام بده:\n"
        "۱. یک گروه بساز و <b>Topics</b> آن را روشن کن\n"
        "   (تنظیمات گروه ← Topics)\n"
        "۲. همین ربات را به گروه اضافه کن\n"
        "۳. ربات را <b>ادمین</b> کن\n\n"
        "بعد شناسهٔ عددی گروه را بفرست.\n"
        "برای گرفتنش یک پیام از گروه را برای <code>@userinfobot</code> فوروارد کن — "
        "شناسه با <code>-100</code> شروع می‌شود."
    )
    admin_reports_setup_done = (
        "✅ <b>گروه «{title}» آماده شد</b>\n\n"
        "{lines}\n\n"
        "گزارش‌دهی روشن است. از همین حالا رویدادها به تاپیک خودشان می‌روند."
    )
    admin_reports_requeued = "🔁 {count} گزارش که قبلاً نرفته بود دوباره در صف تحویل گذاشته شد."
    admin_reports_setup_partial = (
        "⚠️ <b>گروه «{title}» نیمه‌آماده است</b>\n\n"
        "{lines}\n\n"
        "مشکل‌ها را درست کن و دوباره «🔗 وصل کردن گروه» را بزن، "
        "یا هر تاپیک را جدا با «🧪 تست» بررسی کن."
    )
    admin_reports_line_created = "🆕 {title} — ساخته و تست شد"
    admin_reports_line_reused = "♻️ {title} — از قبل بود، نامش تأیید و تست شد"
    admin_reports_line_failed = "❌ {title} — {reason}"

    # --- broadcast ---
    admin_cast_home = (
        "✉️ <b>پیام همگانی</b>\n\n{status}\n\n"
        "پیام به‌آرامی ارسال می‌شود تا تلگرام ربات را محدود نکند؛ "
        "می‌توانی وسط کار ببندی و بعداً ادامه می‌دهد."
    )
    admin_cast_idle = "ارسالی در جریان نیست."
    admin_cast_running = "🔄 ارسال #{id} در جریان است — {sent} از {total}"
    admin_cast_prompt_body = (
        "✍️ متن پیام را بفرست.\n\n"
        "همان‌طور که می‌نویسی برای مشتری ارسال می‌شود، پس قبل از تأیید یک‌بار بخوانش.\n"
        "حداکثر {limit} کاراکتر."
    )
    admin_cast_pick_audience = (
        "📝 <b>پیش‌نمایش پیام</b>\n"
        "─────────────\n"
        "{preview}\n"
        "─────────────\n\n"
        "این پیام برای چه کسانی برود؟"
    )
    admin_cast_confirm = (
        "⚠️ <b>ارسال به {count} نفر</b>\n"
        "گروه: {audience}\n\n"
        "{preview}\n\n"
        "ارسال‌شده‌ها برگشت‌پذیر نیستند. مطمئنی؟"
    )
    admin_cast_test_sent = "🧪 همین متن برای خودت فرستاده شد. گیرنده‌ای ثبت نشد و ارسالی شروع نشد."
    admin_cast_test_failed = "ارسال آزمایشی انجام نشد. اول ربات را در چت خصوصی خودت استارت کن."
    admin_cast_started = "📤 ارسال شروع شد — {count} گیرنده."
    admin_cast_progress = (
        "📊 <b>ارسال #{id}</b>\n"
        "وضعیت: {state}\n\n"
        "✅ رسیده: {sent}\n"
        "⛔️ بلاک‌کرده: {blocked}\n"
        "❌ ناموفق: {failed}\n"
        "⏳ باقی‌مانده: {pending}\n"
        "جمع: {total}"
    )
    admin_cast_state_sending = "در حال ارسال"
    admin_cast_state_done = "تمام شد"
    admin_cast_state_cancelled = "متوقف شد"
    admin_cast_cancelled = "⛔️ ارسال متوقف شد. پیام‌هایی که رفته‌اند برنمی‌گردند."
    admin_cast_blocked_note = (
        "«بلاک‌کرده» یعنی آن مشتری ربات را بلاک کرده یا اکانتش حذف شده — دوباره تلاش نمی‌شود."
    )
    admin_reports_topic_prompt = (
        "🧵 تاپیک <b>{title}</b>\n\n"
        "شناسهٔ تاپیک را بفرست (فقط عدد).\n\n"
        "برای گرفتنش: داخل آن تاپیک روی یک پیام بزن ← <b>Copy Link</b>. "
        "آخرین عدد لینک، شناسهٔ تاپیک است."
    )
    admin_reports_topic_set = "تاپیک «{title}» تعیین شد. حالا «🧪 تست» را بزن تا مطمئن شویم می‌رسد."
    admin_reports_verified = "✅ «{title}» تست شد و پیام رسید."
    admin_reports_enabled = "✅ گزارش‌دهی روشن شد."
    admin_reports_disabled = "⛔️ گزارش‌دهی خاموش شد."

    admin_panel_title = (
        "تنظیمات پنل PasarGuard\n\n"
        "احراز هویت: فقط API Key\n"
        "آدرس پایه: {base_url}\n"
        "API Key: {api_key_masked}\n"
        "مسیر ساب: {sub_path}\n"
        "گروه پنلِ هر گروه فروشگاه:\n{store_groups}\n"
        "برای تغییر: فروشگاه ← گروه ← ⚙️ تنظیمات بیشتر ← گروه پنل\n\n"
        "تایم‌اوت: {timeout}\n"
        "دستگاه اکانت تست: {trial_hwid} (حجم و مدت تست در هر گروه فروشگاه)\n"
        "آخرین به‌روزرسانی: {updated_at}"
    )
    admin_panel_store_group_line = "• {title}: {groups}"
    admin_panel_store_group_unset = "• {title}: ⚠️ تنظیم نشده — فروخته نمی‌شود"
    admin_panel_store_groups_none = "• هنوز گروه فروشگاهی نیست"
    admin_panel_prompt_base_url = (
        "آدرس پایه پنل را بفرست (مثال: https://panel.example.com). برای پاک کردن، - بفرست:"
    )
    admin_panel_prompt_api_key = (
        "API Key پنل را بفرست (شروع با pg_key_…). "
        "احراز هویت فقط با API Key است؛ یوزر/پسورد پشتیبانی نمی‌شود. "
        "برای پاک کردن، - بفرست:"
    )
    admin_panel_prompt_sub_path = "مسیر سابسکریپشن را بفرست (مثال: sub). برای پاک کردن، - بفرست:"
    admin_panel_prompt_timeout = "تایم‌اوت را به ثانیه بفرست (مثال: 30). برای پاک کردن، - بفرست:"
    admin_panel_saved = "ذخیره شد."
    admin_panel_cleared = "تنظیمات پنل به پیش‌فرض .env برگشت."
    admin_panel_saved_ok = "✅ ذخیره شد — اتصال برقرار است"
    admin_panel_saved_fail = "⚠️ ذخیره شد ولی اتصال برقرار نیست: {error}"
    admin_panel_view_only = "فقط مالک فروشگاه می‌تواند این تنظیمات را تغییر دهد."
    admin_panel_test_ok = "✅ اتصال برقرار است"
    admin_panel_test_fail = "⚠️ اتصال برقرار نیست: {error}"
    admin_panel_invalid = "مقدار نامعتبر است."

    admin_panel_groups_list_title = "گروه‌های زنده پنل ({total}):"
    admin_panel_groups_list_empty = "هیچ گروهی از پنل دریافت نشد."
    admin_panel_groups_list_line = (
        "#{id} {name} • {users} کاربر • {inbounds} اینباند{disabled}{stores}"
    )
    admin_panel_groups_list_line_simple = "#{id} {name}{stores}"
    admin_panel_groups_list_disabled = " ⛔"
    admin_panel_groups_list_stores = " → {stores}"
    admin_panel_groups_list_fail = "دریافت لیست گروه‌ها ناموفق: {error}"

    wallet_menu = "🏦 کیف پول من\n\n💰 موجودی: <b>{balance}</b> تومان"
    wallet_empty_hint = (
        "موجودی کیف پولت صفره.\n\n"
        "🛒 برای گرفتن سرویس مستقیم از «خرید اشتراک» برو — پرداخت کارت‌به‌کارته.\n"
        "اگه فعال‌سازی نشد، پول به همین کیف پول برمی‌گرده و دوباره می‌تونی خرید کنی.\n"
        "شارژ جداگانه لازم نیست."
    )
    purchase_enter_service_name = (
        "📡 یه نام کاربری دلخواه بفرست\n\n"
        + username_rules
        + "\n\nاسم پنلت می‌شه همین + چهار حرف تصادفی."
    )
    purchase_invalid_service_name = (
        "⚠️ این اسم نمی‌شه — هیچ حرف انگلیسی توش نیست.\n\n" + username_rules
    )
    # Only sent when the name actually changed. «خودم برمی‌دارم» is a promise;
    # showing the result is what stops it being a silent surprise in the panel.
    purchase_service_name_cleaned = "📡 اسمش شد: {name}"
    purchase_renew_or_new = (
        "📡 برای این پلن یه سرویس فعال داری.\nهمون رو تمدید کنم یا یکی جدید بسازم؟"
    )
    purchase_renew_or_new_nudge = (
        "اینجا باید یکی از دکمه‌های زیر رو بزنی: تمدید همون سرویس، یا یه سرویس جدید."
    )
    purchase_summary_renew = "📅 تمدید سرویس: {name}"
    # --- تمدید از نو (plans/2026-10-04, ۱.۲): one screen, then the usual summary ---
    purchase_renew_custom_screen = (
        "📅 تمدید سرویس: {name}\n\n"
        "{lead}{volume} گیگ • {duration} — {price} تومان\n\n"
        "حجم دیگه‌ای می‌خوای؟ فقط عدد رو بفرست ({low} تا {high} گیگ).\n"
        "مدت رو هم از دکمه‌ها عوض کن."
    )
    purchase_renew_custom_same = "همان"
    purchase_renew_reset_note = (
        "⚠️ با این تمدید، حجم و زمان از نو شروع می‌شه.\n" "باقی‌ماندهٔ سرویس قبلی حذف می‌شه."
    )
    purchase_summary_wallet = (
        "🧾 <b>خلاصهٔ سفارش</b>\n"
        "\n"
        "{details}\n"
        "\n"
        "💰 قیمت اولیه: {original} تومان\n"
        "🏷 تخفیف: {discount} تومان\n"
        "💰 مبلغ نهایی سفارش: <b>{final}</b> تومان\n"
        "\n"
        "🏦 موجودی کیف پول: {wallet} تومان\n"
        "🏦 کسر از کیف پول: {wallet_applied} تومان\n"
        "💳 مبلغ قابل پرداخت کارت: <b>{card_amount}</b> تومان\n"
        "\n"
        "اگه اطلاعات درسته، «ثبت سفارش» رو بزن."
    )
    purchase_personal_offer_line = "🎁 تخفیف ویژهٔ تو: {percent}٪"
    purchase_resume_open = "🧾 یه سفارش باز داری (#{order_id}). ادامهٔ پرداخت:"
    purchase_open_in_review = (
        "⏳ سفارش #{order_id} هنوز منتظر بررسی دستی ادمینه.\n"
        "\n"
        "لازم نیست سفارش یا رسید رو دوباره ثبت کنی؛ نتیجه همین‌جا برات میاد."
    )
    payment_order_expired = (
        "⏰ مهلت پرداخت سفارش #{order_id} تموم شد.\n"
        "هر وقت خواستی، از «خرید اشتراک» دوباره سفارش بده."
    )
    payment_provision_failed_user = (
        "⚠️ پرداخت سفارش #{order_id} تأیید شد، ولی فعال‌سازی روی پنل انجام نشد.\n\n"
        "🏦 مبلغ {amount} تومان به کیف پولت برگشت.\n"
        "از «کیف پول من» ببینش. می‌تونی دوباره خرید کنی یا صبر کنی تا پیگیری کنیم.\n\n"
        "از دکمه‌های زیر انتخاب کن."
    )
    payment_provision_failed_admin_detail = (
        "تایید پرداخت سفارش #{order_id} انجام شد اما فعال‌سازی پنل ناموفق بود.\n"
        "وضعیت: PROVISION_FAILED\n"
        "جزئیات: {detail}"
    )
    admin_provision_failed_no_refund = (
        "\nپول به کیف پول مشتری برنگشت، چون از پنل معلوم نیست ساخت سرویس واقعاً رد شده. "
        "سفارش برای تلاش مجدد مانده؛ اول وضعیت سرویس را در پنل ببین."
    )
    admin_retry_need_wallet = (
        "موجودی کیف پول کاربر برای تلاش مجدد کافی نیست " "(مبلغ سفارش قبلاً به کیف پول برگشته)."
    )
    payment_wallet_only_ok = (
        "🏦 سفارش #{order_id} از کیف پولت پرداخت شد.\n"
        "دارم فعالش می‌کنم؛ کارت سرویس همین‌جا میاد."
    )

    # --- Admin payment queue / reject reason / resolved ---
    admin_payment_resolved_approved = "✅ تایید شد"
    admin_payment_resolved_rejected_reason = "❌ رد شد\nدلیل: {reason}"
    admin_reject_ask_reason = (
        "رد سفارش #{order_id}\n\n" "دلیل رد را انتخاب کن یا «دلیل سفارشی» را بزن:"
    )
    admin_reject_ask_custom = "دلیل کوتاه رد سفارش #{order_id} را بنویس (حداکثر ۲۰۰ حرف):"
    admin_reject_aborted = "رد لغو شد؛ سفارش هنوز در صف بررسی است."
    admin_reject_reason_too_long = "دلیل خیلی بلند است. حداکثر ۲۰۰ حرف بفرست."
    admin_reject_reason_empty = "دلیل خالی است. یک جمله کوتاه بنویس یا از دکمه‌ها استفاده کن."
    admin_next_pending_empty = "سفارش در انتظار دیگری نیست."
    admin_store_title = "مدیریت فروشگاه\n\n" "{body}"
    admin_store_empty = "پیکربندی فروشگاه خالی یا خوانده نشد."
    admin_store_coming = "مدیریت فروشگاه\n\n" "هنوز چیزی برای مشتری منتشر نشده است."
    admin_store_hub = (
        "🛍 مدیریت فروشگاه\n{status}\n\n" "هر تغییری که تأیید کنی همون لحظه برای مشتری فعال می‌شه."
    )
    admin_store_hub_status = "{groups} گروه، {plans} پلن"
    admin_store_hub_warn = "، ⚠️ {n} گروه بدون گروه پنل"
    admin_store_hub_readonly = "(فقط خواندنی)"

    admin_store_durs_title = "مدت‌های «{title}»"
    admin_store_durs_empty = "هنوز مدتی تعریف نشده. بدون مدت، این گروه برای مشتری خالی است."
    admin_store_dur_add = "➕ افزودن مدت"
    admin_store_dur_prompt = (
        "چند ماهه؟ فقط عدد بفرست — بین ۱ تا ۲۴.\n" "برای پلن بدون تاریخ انقضا، دکمهٔ زیر را بزن."
    )
    admin_store_dur_none_btn = "♾ بدون انقضا"
    admin_store_dur_invalid = "عدد بین ۱ تا ۲۴ بفرست. برای بدون انقضا از دکمه استفاده کن."
    admin_store_dur_exists = "این مدت از قبل هست."
    admin_store_dur_last = "آخرین مدت را نمی‌شود برداشت — گروه بدون مدت برای مشتری خالی می‌شود."
    admin_store_dur_del_confirm = (
        "برداشتن «{label}» از «{title}»؟\n\n"
        "{count} پلن روی این مدت قیمت دارند. قیمت‌ها پاک نمی‌شوند — "
        "اگر دوباره این مدت را اضافه کنی، برمی‌گردند."
    )
    admin_store_tier_groups = "🖧 گروه‌های پنل ویژه"
    admin_store_tier_needs_groups = (
        "اول گروه‌های پنل ویژه را تنظیم کن. بدون آن، مشتری پول ویژه می‌دهد "
        "و همان سرورهای اقتصادی را می‌گیرد."
    )
    admin_store_tier_disabled_no_groups = (
        "گروه‌های پنل ویژه خالی شد، پس تفکیک ویژه/اقتصادی خاموش شد."
    )
    admin_store_owner_only = (
        "فقط مالک فروشگاه می‌تواند این مورد را تغییر دهد. شما دسترسی مشاهده دارید."
    )
    admin_store_saved = "ذخیره شد."

    # Every editing screen says which version it is editing. Before this, not a
    # single edit screen showed draft-vs-live, so the operator could not tell
    # whether a change was already visible to customers.
    admin_store_badge_live = "🟢 همین را مشتری می‌بیند."
    admin_store_badge_empty = "هنوز فروشگاهی ساخته نشده."
    admin_store_invalid_input = "ورودی نامعتبر است. دوباره تلاش کن."
    admin_store_promo_invalid_percent = (
        "درصد تخفیف نامعتبر است. یک عدد بین ۱ تا ۱۰۰ بفرستید. نمونه: <code>SAVE15 15</code>"
    )
    admin_store_lost_context = "مشخص نیست این مقدار برای کدام مورد است. از فهرست دوباره وارد شوید."
    admin_store_promo_del_confirm = (
        "کد «{code}» حذف شود؟ با تأیید همین الان برای مشتری غیرفعال می‌شود."
    )
    admin_store_no_catalog = (
        "هنوز فروشگاهی در دیتابیس نیست. مالک باید «بارگذاری اولیه از فایل قیمت» را بزند."
    )
    admin_store_cat_settings_hint = (
        "تفکیک ویژه/اقتصادی یعنی هر پلن دو قیمت و دو سرور دارد؛ "
        "اول «سرورهای ویژه» را بده، بعد تفکیک را روشن کن."
    )
    admin_store_flat_hint = (
        "فهرست ساده: همهٔ پلن‌های گروه در یک لیست از کم به زیاد، هر پلن با مدت خودش. "
        "تب مدت و تفکیک ویژه/اقتصادی در این حالت نمایش داده نمی‌شود."
    )
    admin_store_ladder_warning = (
        "⚠️ نردبان قیمت: «{small}» به ازای هر گیگ ارزان‌تر از «{big}» نیست "
        "({small_per_gb} در برابر {big_per_gb})."
    )
    admin_store_tl_filter_retired = "این گزینه حذف شده؛ فرایند خرید مشتری از آن نمی‌خواند."
    admin_store_steps_retired = "«مراحل خرید» حذف شده؛ فرایند خرید مشتری از این تنظیم نمی‌خواند."
    admin_store_cat_groups_unset = "⚠️ تنظیم نشده؛ این گروه فروخته نمی‌شود"
    admin_store_cat_groups_inherited = "از گروه والد: {ids}"
    admin_store_dash_title = "📂 {title}"
    admin_store_dash_sale = "فروش: {state}"
    admin_store_dash_panel = "گروه پنل: {groups}"
    admin_store_dash_trial = "تست رایگان: {trial}"
    admin_store_dash_shelf = "شکل قفسه: {shelf}"
    admin_store_dash_custom = "خرید سفارشی: {state}"
    admin_store_custom_off = "خاموش"
    admin_store_custom_resellers = "فقط نماینده‌ها"
    admin_store_custom_everyone = "همه"
    admin_store_dash_plans = "پلن‌ها: {n}"
    admin_store_dash_children = "زیرگروه‌ها: {n}"
    # Web twin of the short form: the page also shows the full sentence as an alert under the facts.
    web_store_cat_groups_unset = "⚠️ تنظیم نشده"
    admin_store_dash_warn_no_panel = "⚠️ گروه پنل تنظیم نشده؛ این گروه فروخته نمی‌شود."
    admin_store_dash_warn_no_tab = (
        "⚠️ {plans} را مشتری نمی‌بیند: مدتش بین تب‌های این گروه نیست. "
        "از «🛒 فروش» ← «⏱ مدت‌ها» آن مدت را اضافه کن، یا «فهرست ساده» را روشن کن."
    )
    admin_store_new_pkg_warn_no_tab = (
        "⚠️ این گروه تب «{duration}» ندارد، پس این پلن را مشتری نمی‌بیند. "
        "بعد از فعال‌سازی از «🛒 فروش» ← «⏱ مدت‌ها» این مدت را اضافه کن، "
        "یا «فهرست ساده» را روشن کن."
    )
    admin_store_dash_warn_no_plans = "⚠️ هنوز پلنی نداره — مشتری چیزی نمی‌بینه"
    admin_store_sec_sale = "🛒 فروش — {title}"
    admin_store_sec_trial = "🎟 تست رایگان — {title}\nهر مشتری از هر گروه یک بار می‌تونه تست بگیره."
    admin_store_sec_panel = "🖥 پنل — {title}\nگروه پنل فعلی: {groups}"
    admin_store_sec_group = "✏️ گروه — {title}"
    admin_store_sec_order = "↕️ چیدمان پلن‌ها — {title}"
    admin_store_pkg_states = "نمایش: {enabled} • فروش جدید: {new_sale} • تمدید: {renew}"
    # --- creating and deleting catalog rows (previously impossible) ---
    admin_store_prompt_sub_cat = (
        "عنوان زیرگروه جدید زیر «{parent}» را بفرست.\n"
        "مثال: ویژه یا اقتصادی\n\n"
        "زیرگروه فقط یک سطح مجاز است."
    )
    admin_store_sub_cat_created = "زیرگروه «{title}» زیر «{parent}» ساخته شد."
    # Volume plans take four steps (حجم، مدت، کاربر همزمان، قیمت), unlimited
    # plans three; the price prompt is shared, so it says «قدم آخر» instead of
    # a count that would be wrong for one of the two.
    admin_store_pick_size_volume = "پلن جدید در «{category}»\n\n<b>۱ از ۴</b> — حجم را انتخاب کن:"
    admin_store_pick_size_unlimited = (
        "پلن جدید در «{category}»\n\n<b>۱ از ۳</b> — تعداد کاربر همزمان را انتخاب کن:"
    )
    admin_store_pick_duration_volume = "<b>۲ از ۴</b> — {size} گیگ، برای چه مدت؟"
    admin_store_pick_duration_unlimited = "<b>۲ از ۳</b> — {size} کاربره، برای چه مدت؟"
    admin_store_pick_users = "تعداد کاربر همزمان این پلن؟"
    admin_store_pick_users_step = "<b>۳ از ۴</b> — تعداد کاربر همزمان این پلن؟"
    admin_store_prompt_size_volume = "حجم را به گیگ بفرست (فقط عدد). مثال: <code>15</code>"
    admin_store_prompt_size_unlimited = (
        "تعداد کاربر همزمان را بفرست (فقط عدد). مثال: <code>3</code>"
    )
    admin_store_prompt_duration_volume = "مدت را به روز بفرست (فقط عدد). مثال: <code>45</code>"
    admin_store_prompt_duration_unlimited = "مدت را به ماه بفرست (فقط عدد). مثال: <code>2</code>"
    admin_store_number_invalid = "فقط یک عدد بفرست. مثال: <code>{example}</code>"
    admin_store_prompt_new_pkg_price = (
        "<b>قدم آخر</b> — قیمت «{title}» را به تومان بفرست.\n" "فقط عدد. مثال: <code>150000</code>"
    )
    admin_store_new_pkg_confirm_intro = (
        "این پلن ساخته شد ولی هنوز برای مشتری فعال نیست.\n" "یه نگاه بنداز، اگه درسته تأیید کن."
    )
    admin_store_prompt_new_pkg_rename = "عنوان تازه رو بفرست:"
    admin_store_plan_live = "پلن فعال شد و مشتری می‌بینتش."
    admin_store_plan_discarded = "پلن ساخته‌نشده دور ریخته شد."
    admin_store_prompt_new_pkg_title = (
        "پلن ساخته شد: «{title}» — {price}\n\n"
        "اگر عنوان دیگری می‌خواهی الان بفرست، وگرنه «همین خوب است» را بزن."
    )
    admin_store_keep_title = "✅ همین عنوان خوب است"
    admin_store_pkg_incomplete = (
        "⚠️ این پلن حجم/مدت ندارد، پس مشتری با زدنش به ساخت دستی می‌رود و "
        "قیمت ثابت نادیده گرفته می‌شود. با «📐 حجم و مدت» درستش کن."
    )
    admin_store_dims_saved = "حجم و مدت ذخیره شد: {dims}"
    admin_store_tl_derived = (
        "«محدودیت زمانی» دیگر جداگانه تنظیم نمی‌شود — از روی مدت پلن تعیین می‌شود. "
        "برای تغییرش «📐 حجم و مدت» را بزن."
    )
    admin_store_pkg_created = "پلن «{title}» با قیمت {price} ساخته شد."
    admin_store_cat_del_confirm = (
        "⚠️ حذف گروه «{title}»\n\n"
        "{scope}\n\n"
        "با تأیید، همین الان از فروشگاه مشتری برداشته می‌شود. "
        "سفارش‌های گذشته قیمت خودشان را نگه می‌دارند."
    )
    admin_store_cat_del_scope_plain = "این گروه پلنی ندارد."
    admin_store_cat_del_scope_plans = "{n} پلن این گروه هم حذف می‌شود."
    admin_store_cat_del_scope_children = "{c} زیرگروه و {n} پلن آن‌ها هم حذف می‌شود."
    admin_store_cat_deleted = "گروه «{title}» حذف شد."
    admin_store_pkg_del_confirm = (
        "⚠️ حذف پلن «{title}»\n\n"
        "قیمت فعلی: {price}\n\n"
        "با تأیید، همین الان از فروشگاه مشتری برداشته می‌شود."
    )
    admin_store_pkg_deleted = "پلن «{title}» حذف شد."
    # A price 10x the current one or a tenth of it is asked about once (never refused).
    admin_store_price_jump_confirm = (
        "⚠️ قیمت «{title}» خیلی عوض می‌شود.\n\n"
        "قیمت فعلی: {old}\n"
        "قیمت تازه: {new}\n\n"
        "با تأیید، همین الان برای مشتری عوض می‌شود. درسته؟"
    )
    admin_store_price_jump_cancelled = "باشه، قیمت همان ماند."
    admin_store_plan_preview_hint = "حجم، مدت و قیمت را بنویس تا دکمهٔ مشتری اینجا دیده شود."

    admin_store_prompt_cat_new = "عنوان فارسی گروه محصول جدید را بفرست (مثال: حجمی اقتصادی):"
    admin_store_prompt_cat_kind = "نوع این گروه محصول را انتخاب کن:"
    admin_store_cat_created = "گروه محصول «{title}» ساخته شد."
    admin_store_prompt_cat_title = "عنوان فارسی جدید گروه محصول را بفرست:"
    admin_store_prompt_cat_sort = "عدد ترتیب نمایش را بفرست:"
    admin_store_pick_groups = (
        "🖥 {what} — {title}\n"
        "روی هر گروه بزن تا انتخاب یا برداشته شه؛ چندتا هم می‌شه. بعد «💾 ذخیره».\n"
        "انتخاب‌شده: {selected}\n\n"
        "{hint}\n"
        "شناسه‌ها رو با کاما هم می‌تونی بفرستی (مثال: 11,12)."
    )
    admin_store_pick_what = "گروه پنل"
    admin_store_pick_what_tier = "گروه پنل ویژه"
    admin_store_pick_none = "هیچ"
    admin_store_pick_hint = (
        "هیچی انتخاب نکنی، از گروه والد استفاده می‌شه؛ "
        "اگه والد هم نداشته باشه این گروه فروخته نمی‌شه."
    )
    admin_store_pick_hint_tier = (
        "«ویژه» همین‌جا از «اقتصادی» جدا می‌شه. هیچی انتخاب نکنی، تفکیک ویژه/اقتصادی خاموش می‌شه."
    )
    admin_store_pick_unreachable = "⚠️ لیست گروه‌ها از پنل نیومد؛ شناسه‌ها رو با کاما بفرست."
    # The same line on the panel, where ids are typed into the box above, not sent.
    web_store_pick_unreachable = (
        "⚠️ لیست گروه‌ها از پنل نیومد؛ شناسه‌ها رو با کاما توی خانهٔ بالا بنویس."
    )
    admin_store_pick_expired = "این صفحه قدیمیه؛ دوباره از «🖥 پنل» باز کن."
    admin_store_pick_max = "حداکثر {n} گروه پنل."
    admin_store_pick_missing = "در پنل نیست"
    admin_store_group_move_ask = (
        "{count} سرویس در این گروه هست. اگه روی گروه قبلی مونده باشن، لینکشون خالی می‌شه.\n"
        "منتقلشون کنم به گروه جدید؟ اسم کاربری، لینک، حجم و انقضا همون می‌مونه."
    )
    admin_store_group_move_report = (
        "انتقال تموم شد.\n"
        "منتقل‌شده: {moved}\n"
        "از قبل درست بودن: {already}\n"
        "روی گروه دیگه‌ای بودن، دست نخوردن: {skipped}\n"
        "ناموفق: {failed}"
    )
    admin_store_group_move_failed_list = "\nسرویس‌های ناموفق (پنل):\n{names}"
    admin_store_group_move_none = "سرویسی برای انتقال پیدا نشد."
    admin_store_group_move_no_target = "گروه پنلی برای این دسته پیدا نشد؛ چیزی منتقل نشد."
    admin_store_group_move_panel_down = "به پنل وصل نشدم؛ چیزی منتقل نشد. بعداً دوباره امتحان کن."
    admin_store_group_move_kept = "باشه. فقط سرویس‌های جدید از گروه جدید استفاده می‌کنن."
    # The Mini App refuses a panel-group change that would leave services behind:
    # moving them is a panel write, and only the bot offers it (right after its own change).
    admin_store_group_strand_refused = (
        "این تغییر {count} سرویس فعال را روی گروه قبلی جا می‌گذارد و لینکشان خالی می‌شود. "
        "جابه‌جایی سرویس‌ها فقط از ربات انجام می‌شود؛ همین تغییر را از ربات بزن تا بعدش "
        "بپرسد سرویس‌ها منتقل شوند یا نه. چیزی ذخیره نشد."
    )

    admin_store_order_mode = "ترتیب نمایش: {mode}"
    admin_store_order_auto = "خودکار (بر اساس حجم)"
    admin_store_order_auto_users = "خودکار (بر اساس تعداد کاربر)"
    admin_store_order_manual = "دستی"
    admin_store_order_switch_auto = "🔀 ترتیب خودکار"
    admin_store_order_switch_manual = "✋ ترتیب دستی"
    admin_store_order_first_manual = "ترتیب فعلی ثبت شد. حالا می‌توانی جابه‌جا کنی."
    admin_store_order_at_edge = "همین‌جا آخرش است."
    admin_store_prompt_pkg_title = "عنوان فارسی جدید را بفرست:"
    admin_store_prompt_pkg_price = "قیمت ثابت به تومان (عدد صحیح) را بفرست:"
    admin_store_price_ready_only = "فقط پلن‌های آماده قیمت ثابت دارند."
    admin_store_seed_ok = "بارگذاری اولیه انجام شد"
    admin_store_seed_skipped = "نسخهٔ فعال از قبل وجود دارد — بارگذاری اولیه انجام نشد."
    admin_store_seeded = "بارگذاری اولیه انجام و برای مشتری منتشر شد: v{version}"
    admin_store_disc_title = (
        "🏷 تخفیف و قوانین\n"
        "ترکیب تخفیف: {combine_fa}\n"
        "صحت پرداخت / مالکیت سرویس: قفل (غیرقابل خاموش کردن)\n"
        "تأیید مشتری برای تمدید خاموش نمی‌شود.\n\n"
        "بهترین تخفیف = بین پرومو و تخفیف معرف، فقط یکی (نه جمع)."
    )
    admin_store_promo_title = "🏷 کدهای تخفیف ({count})\n{hint}"
    web_promo_count = "{count} کد"
    admin_store_promo_owner_hint = "برای افزودن: کد و درصد را در یک خط بفرست (مثال: SAVE15 15)"
    admin_store_promo_ops_hint = "نمای فقط‌خواندنی."
    admin_store_prompt_promo = (
        "کد تخفیف و درصد را بفرست.\n"
        "مثال: WELCOME10 10\n"
        "کد: انگلیسی/عدد/زیرخط — درصد: ۱ تا ۱۰۰"
    )
    # Promo code usage rules (the wizard after «کد و درصد»)
    admin_promo_ask_first_only = (
        "این کد فقط برای اولین خرید باشد؟\n"
        "مشتری‌ای که قبلاً خرید پولی کرده با «بله» نمی‌تواند از آن استفاده کند، "
        "و هر مشتری فقط یک بار می‌تواند."
    )
    admin_promo_ask_user_limit = (
        "هر مشتری حداکثر چند بار می‌تواند از این کد استفاده کند؟ یک عدد بفرست."
    )
    admin_promo_ask_total_limit = (
        "این کد در مجموع چند بار قابل استفاده باشد؟ یک عدد بفرست.\n"
        "سفارشی که منقضی یا رد شود، استفادهٔ خودش را پس می‌دهد."
    )
    admin_promo_ask_days = "این کد چند روز دیگر منقضی شود؟ یک عدد بفرست؛ ۰ یعنی هیچ‌وقت."
    web_promo_ask_user_limit = (
        "هر مشتری حداکثر چند بار می‌تواند از این کد استفاده کند؟ یک عدد بنویس."
    )
    web_promo_ask_total_limit = (
        "این کد در مجموع چند بار قابل استفاده باشد؟ یک عدد بنویس.\n"
        "سفارشی که منقضی یا رد شود، استفادهٔ خودش را پس می‌دهد."
    )
    web_promo_ask_days = "این کد چند روز دیگر منقضی شود؟ یک عدد بنویس؛ ۰ یعنی هیچ‌وقت."
    admin_promo_invalid_number = (
        "عدد نامعتبر است. یک عدد صحیح بفرست، ۰ یا بیشتر (۰ یعنی بدون محدودیت)."
    )
    admin_promo_number_too_big = "این عدد خیلی بزرگ است. عددی تا {max} بفرست."
    admin_promo_yes = "✅ بله"
    admin_promo_no = "❌ نه"
    admin_promo_skip = "رد کردن"
    admin_promo_wizard_lost = "این مرحله دیگر باز نیست. دوباره از «افزودن کد تخفیف» شروع کن."
    admin_promo_row = "{mark} <code>{code}</code> — {percent}٪"
    admin_promo_rules_none = "بدون محدودیت"
    admin_promo_rule_first_only = "فقط اولین خرید"
    admin_promo_rule_per_user = "هر مشتری {n} بار"
    admin_promo_rule_expires = "تا {date}"
    admin_promo_usage = "مصرف: {used} از {limit}"
    admin_promo_usage_unlimited = "مصرف: {used} بار"
    admin_store_pay_title = (
        "💳 کارتی که به مشتری نشان داده می‌شود\n\n"
        "شماره کارت: {card}\n"
        "صاحب کارت: {holder}\n"
        "بانک: {bank}\n"
        "توضیح: {instructions}\n\n"
        "{note}"
    )
    admin_store_pay_from_env = " (از .env)"
    admin_store_pay_note_env = "مقدارهای «از .env» را با دکمه‌های زیر می‌توانی از تلگرام عوض کنی."
    admin_store_pay_note_override = "تغییرها همان لحظه به مشتری‌ها نشان داده می‌شود."
    admin_store_pay_saved = "✅ ذخیره شد."
    admin_store_pay_prompts = {
        "card_number": "شماره کارت ۱۶ رقمی را بفرست. فاصله، خط تیره و رقم فارسی هم اشکالی ندارد.",
        "card_holder_name": "نام صاحب کارت را بفرست (حداکثر ۶۴ حرف).",
        "bank_name": "نام بانک را بفرست (حداکثر ۶۴ حرف).",
        "instructions": "توضیحی که زیر شماره کارت به مشتری نشان داده می‌شود را بفرست (حداکثر ۳۰۰ حرف).",
    }
    admin_store_pay_errors = {
        "card_invalid": "شماره کارت باید دقیقاً ۱۶ رقم باشد. دوباره بفرست.",
        "too_long": "این متن بیش از حد مجاز است. کوتاه‌تر بفرست.",
        "empty": "مقدار خالی ذخیره نمی‌شود. دوباره بفرست.",
        "unknown_field": "این مورد شناخته نشد.",
    }
    admin_store_pay_card_confirm = (
        "شماره کارت جدید:\n<code>{new}</code>\n\n"
        "قبلی: {old}\n\n"
        "از همین لحظه مشتری‌ها برای پرداخت این شماره را می‌بینند. درست است؟"
    )
    admin_store_pay_clear_confirm = (
        "همهٔ مقدارهای ذخیره‌شده پاک می‌شود و دوباره از .env خوانده می‌شود.\n\n" "{current}"
    )
    admin_store_pay_cleared = "✅ به مقدارهای .env برگشت."
    admin_store_changes_title = "تغییرات فروشگاه\n"
    admin_store_changes_empty = "هنوز تغییری ثبت نشده."
    admin_store_changes_hint = (
        "● یعنی وضعیت فعلی فروشگاه.\n"
        "روی هر تغییر بزنی، فروشگاه به قبل از اون برمی‌گرده.\n"
        "تغییرات تا ۷ روز اینجا می‌مونن؛ "
        "۵ تا از جدیدترین‌ها هم همیشه نگه داشته می‌شن."
    )
    admin_store_change_gone = "این تغییر دیگه در لیست نیست."
    admin_store_undo_confirm = "فروشگاه به قبل از این تغییر برگرده؟\n\n«{description}»"
    admin_store_undo_also = (
        "حواست باشه: {count} تغییر بعدی هم با این کار برمی‌گرده، "
        "چون هر برگشت کل فروشگاه رو به اون لحظه می‌بره."
    )
    admin_store_undo_button = "✅ برگردان (همین الان فعال می‌شود)"
    admin_store_undone = "برگشت انجام شد."
    admin_store_undone_body = (
        "فروشگاه به قبل از «{description}» برگشت و همین الان برای مشتری فعاله."
    )
    admin_reject_reasons = {
        "unreadable": "رسید ناخوانا / نامعتبر",
        "wrong_amount": "مبلغ واریزی نادرست",
        "wrong_card": "کارت مقصد اشتباه",
        "duplicate": "رسید تکراری",
    }

    account_status_labels = {
        "active": "فعال",
        "disabled": "غیرفعال",
        "limited": "محدود",
        "expired": "منقضی",
        "on_hold": "در انتظار",
        "ACTIVE": "فعال",
        "PENDING_PAYMENT": "در انتظار پرداخت",
        "PAYMENT_REVIEW": "در حال بررسی پرداخت",
        "PAYMENT_APPROVED": "پرداخت تأیید شده",
        "PROVISIONING": "در حال فعال‌سازی",
        "REJECTED": "رد شده",
        "PROVISION_FAILED": "فعال‌سازی ناموفق",
        "EXPIRED": "منقضی (پرداخت نشده)",
    }

    admin_backup_caption = (
        "💾 بکاپ دیتابیس — {date}\n"
        "حجم: {size}\n"
        "کاربران: {users} • سفارش‌ها: {orders} • تراکنش‌های کیف پول: {ledger}"
    )
    admin_backup_too_large = (
        "بکاپ امروز {size} شد و از سقف ۵۰ مگابایت تلگرام بیشتر است، پس فرستاده نشد.\n"
        "نسخهٔ محلی سر جایش است: {path}"
    )
    admin_backup_failed = "بکاپ امروز انجام نشد: {reason}"

    # ---- Service lifecycle: warnings, and deleting ended services -----------
    # `admin_*`: only the owner reads these. The customer's messages are the
    # `notify_*` strings further down this block.
    admin_life_title = "🗓 هشدار و حذف سرویس"
    admin_life_screen = (
        "🗓 هشدار و حذف سرویس\n\n"
        "هشدار روز: {warn_days}\n"
        "هشدار حجم: {warn_volume}\n"
        "حذف خودکار: {mode}\n"
        "حذف بعد از انقضا: {after_expiry}\n"
        "حذف بعد از اتمام حجم: {after_limited}\n"
        "حذف تست رایگان: {trial}\n"
        "هشدار پیش از حذف: {warn_before}\n"
        "آخرین تمرین: {rehearsal}{note}"
    )
    admin_life_off = "خاموش"
    admin_life_days_list = "{days} روز مانده"
    admin_life_percent_list = "{percents} باقی‌مانده"
    admin_life_days_value = "{n} روز"
    admin_life_trial_value = "{n} روز بعد از پایانش"
    admin_life_before_value = "{n} روز قبل از حذف"
    admin_life_mode_names = {"off": "خاموش", "rehearsal": "تمرین (چیزی حذف نمی‌شه)", "on": "روشن"}
    admin_life_never_rehearsed = "هنوز اجرا نشده"
    admin_life_note_off = (
        "\n\nالان هیچ سرویسی حذف نمی‌شه. اول «تمرین» رو روشن کن تا ببینی چی حذف می‌شد."
    )
    admin_life_note_rehearsal = (
        "\n\nتمرین هر روز اجرا می‌شه، چیزی رو حذف نمی‌کنه و لیست «اگه روشن بود حذف می‌شد» رو "
        "تو گروه گزارش می‌ذاره. بعد از اولین گزارش می‌تونی «روشن» رو بزنی."
    )
    admin_life_note_on = (
        "\n\nحذف روشنه. سرویس پولی اول هشدار می‌گیره و فقط بعد از زمان «هشدار پیش از حذف» حذف "
        "می‌شه. اگه یه اجرا بیشتر از ۲۰ سرویس (یا بیشتر از یک‌چهارم همهٔ سرویس‌ها، وقتی بیشتر از ۵ "
        "تا باشه) بخواد حذف کنه، هیچ‌کدوم حذف نمی‌شه و فهرستشون برای تأیید تو می‌مونه."
    )
    admin_life_prompts = {
        "warn_days": (
            "🗓 هشدار روز\n"
            "چند روز مانده به انقضا برای مشتری هشدار بره؟\n"
            "عددها رو با فاصله بنویس، مثلاً «۳ ۱». هر عدد ۱ تا ۳۶۵ روز. ۰ یعنی خاموش.\n"
            "الان: {current}"
        ),
        "warn_volume_percents": (
            "📦 هشدار حجم\n"
            "وقتی چند درصد از حجم مونده برای مشتری هشدار بره؟\n"
            "عددها رو با فاصله بنویس، مثلاً «۲۰ ۵». هر عدد ۱ تا ۹۹ درصد. ۰ یعنی خاموش.\n"
            "الان: {current}"
        ),
        "delete_after_expiry_days": (
            "🗑 حذف بعد از انقضا\n"
            "سرویس منقضی‌شده چند روز بعد از تاریخ انقضا حذف بشه؟\n"
            "یه عدد بین ۰ تا ۳۶۵ روز بنویس. ۰ یعنی این قانون خاموش.\n"
            "الان: {current}"
        ),
        "delete_after_limited_days": (
            "🗑 حذف بعد از اتمام حجم\n"
            "سرویسی که حجمش تموم شده چند روز بعد از اولین باری که ربات دیدش حذف بشه؟\n"
            "یه عدد بین ۰ تا ۳۶۵ روز بنویس. ۰ یعنی این قانون خاموش.\n"
            "الان: {current}"
        ),
        "delete_trial_after_days": (
            "🗑 حذف تست رایگان\n"
            "تست رایگانی که تموم شده چند روز بعد از پایانش حذف بشه؟\n"
            "یه عدد بین ۰ تا ۳۶۵ روز بنویس. ۰ یعنی این قانون خاموش.\n"
            "الان: {current}"
        ),
        "delete_warn_days_before": (
            "⚠️ هشدار پیش از حذف\n"
            "چند روز قبل از حذف به مشتری خبر بدم؟\n"
            "یه عدد بین ۰ تا ۳۶۵ روز بنویس. ۰ یعنی هشداری نره. تست رایگان هشدار نداره.\n"
            "الان: {current}"
        ),
    }
    # Web twins of the six prompts above: a short label and a hint under it, same limits and
    # exceptions. {max_days} and {max_percent} come from services/lifecycle_settings.
    web_life_fields = {
        "warn_days": (
            "چند روز مانده به انقضا",
            "عددها را با فاصله بنویس، مثلاً ۳ ۱. هر عدد ۱ تا {max_days}. صفر یعنی خاموش.",
        ),
        "warn_volume_percents": (
            "چند درصد حجم مانده",
            "عددها را با فاصله بنویس، مثلاً ۲۰ ۵. هر عدد ۱ تا {max_percent} درصد. "
            "صفر یعنی خاموش.",
        ),
        "delete_after_expiry_days": (
            "چند روز بعد از انقضا",
            "یک عدد بین ۰ تا {max_days} روز بنویس. صفر یعنی این قانون خاموش.",
        ),
        "delete_after_limited_days": (
            "چند روز بعد از اتمام حجم",
            "از اولین باری که ربات سرویس را تمام‌شده دید. "
            "یک عدد بین ۰ تا {max_days} روز بنویس. صفر یعنی این قانون خاموش.",
        ),
        "delete_trial_after_days": (
            "چند روز بعد از پایان تست رایگان",
            "یک عدد بین ۰ تا {max_days} روز بنویس. صفر یعنی این قانون خاموش.",
        ),
        "delete_warn_days_before": (
            "چند روز قبل از حذف",
            "یک عدد بین ۰ تا {max_days} روز بنویس. صفر یعنی هشداری نمی‌رود. "
            "تست رایگان هشدار ندارد.",
        ),
    }
    web_life_now = "الان: {current}"
    admin_life_saved = "✅ ذخیره شد."
    admin_life_input_error = "❌ {reason}"
    admin_life_mode_set = "✅ حالت حذف خودکار: {mode}."
    admin_life_mode_confirm = (
        "🗑 حذف خودکار روشن بشه؟\n\n"
        "از اجرای بعدی، سرویس‌هایی که طبق عددها تموم شدن از پنل حذف می‌شن و برنمی‌گردن. "
        "سفارش‌ها و پرداخت‌ها می‌مونن.\n"
        "قبل از هر حذف، ربات دوباره از پنل می‌پرسه و فقط اگه هنوز تموم‌شده باشه حذفش می‌کنه."
    )
    admin_life_mode_confirm_yes = "🗑 بله، روشن کن"
    admin_life_rehearsal_voided = (
        "تمرین قبلی دیگه معتبر نیست؛ برای روشن کردن دوباره باید تمرین اجرا بشه."
    )
    admin_life_before_off = "خاموش (سرویس پولی بدون هشدار حذف می‌شه)"
    admin_life_batch_line = "\n\n🗂 یه فهرست حذف منتظر تأییدته: {n} سرویس."
    admin_life_preview_button = "👁 پیش‌نمایش"
    admin_life_batch_button = "🗂 فهرست حذف ({n})"
    admin_life_approve_button = "⛔️ حذف همین فهرست ({n})"
    admin_life_approve_yes = "⛔️ بله، حذف کن ({n})"
    admin_life_fresh_button = "🔄 ساخت فهرست تازه"
    admin_life_exempt_button = "🛡 حذف نشود"
    admin_life_unexempt_button = "↩️ حذف شود"
    admin_life_back_to_list = "⬅️ فهرست حذف"
    admin_life_exempt_line = "🛡 از حذف خودکار معاف شده."
    admin_life_list_head = (
        "🗂 فهرست حذف\n\n{deletes} سرویس برای حذف • ساخته‌شده: {created}\n"
        "روی هر سرویس بزن تا کارتش رو ببینی. {pages}"
    )
    admin_life_preview_head = (
        "👁 پیش‌نمایش\n\nاگه همین الان اجرا بشه: {deletes} سرویس حذف می‌شه و {warns} سرویس "
        "هشدار می‌گیره.\nاین فقط نمایشه و چیزی حذف نمی‌شه. {pages}"
    )
    admin_life_list_empty = "🗂 فهرست حذف خالیه."
    admin_life_list_page = "(صفحهٔ {page} از {pages})"
    admin_life_list_void = (
        "\n\n⚠️ این فهرست باطله: {why}.\nحذفش با این فهرست ممکن نیست؛ یه فهرست تازه بساز."
    )
    admin_life_void_reasons = {
        "age": "بیشتر از ۴۸ ساعت از ساختنش گذشته",
        "numbers": "بعد از ساختنش عددهای حذف عوض شده",
        "replaced": "فهرست تازه‌تری جاش اومده",
        "used": "قبلاً استفاده شده",
        "preview": "پیش‌نمایشه",
    }
    admin_life_refused = {
        "busy": "الان یه اجرای دیگه در جریانه؛ چند لحظه بعد دوباره بزن.",
        "mode": "حذف خودکار روشن نیست، پس چیزی حذف نشد. اول حالتش رو «روشن» کن.",
        "missing": "این فهرست پیدا نشد.",
        "used": "این فهرست قبلاً استفاده شده؛ چیزی دوباره حذف نشد.",
        "age": "این فهرست باطله: بیشتر از ۴۸ ساعت از ساختنش گذشته. یه فهرست تازه بساز.",
        "numbers": "این فهرست باطله: بعد از ساختنش عددهای حذف عوض شده. یه فهرست تازه بساز.",
        "replaced": "این فهرست باطله: فهرست تازه‌تری جاش اومده.",
        "preview": "پیش‌نمایش رو نمی‌شه تأیید کرد. از «ساخت فهرست تازه» استفاده کن.",
    }
    admin_life_approve_confirm = (
        "⛔️ {n} سرویس از پنل حذف بشه؟\n\n"
        "فقط همین فهرست. قبل از هر حذف دوباره از پنل پرسیده می‌شه و هر سرویسی که تمدید شده، "
        "معاف شده یا دیگه تموم‌شده نیست رد می‌شه. حذف‌شده برنمی‌گرده."
    )
    admin_life_approve_result = (
        "✅ فهرست اجرا شد\n\nحذف شد: {deleted}\nرد شد: {skipped}\nهشدار حذف رفت: {warned}"
    )
    admin_life_warning_undelivered = "هشدار نرسید: مشتری ربات را بسته"
    admin_life_result_deleted_head = "حذف‌شده‌ها:"
    admin_life_result_skipped_head = "رد شده‌ها:"
    admin_life_result_warned_head = "هشدار حذف براشون رفت (بعد از زمان هشدار حذف می‌شن):"
    admin_life_result_row = "• {title} — {reason}"
    admin_life_preview_working = "⏳ دارم از پنل می‌پرسم؛ چند لحظه صبر کن…"
    admin_life_fresh_done = "🔄 فهرست تازه ساخته شد."
    admin_life_source_daily = "اجرای روزانه"
    admin_life_source_approved = "فهرست تأییدشده"
    admin_life_reason_expired = "{n} روز از انقضا"
    admin_life_reason_limited = "{n} روز بدون حجم"
    admin_life_reason_trial = "تست تمام‌شده"
    admin_life_report_row = "• {title} ({owner}) — {reason}"
    admin_life_report_more = "و {n} مورد دیگر"
    admin_life_report_rehearsal_head = (
        "👁 تمرین حذف سرویس‌های تمام‌شده\n\nهیچ سرویسی حذف نشد و به هیچ مشتری پیامی نرفت."
    )
    admin_life_report_would_delete = "🗑 اگه حذف روشن بود، حذف می‌شد ({n}):"
    admin_life_report_would_delete_after = "⏳ بعد از هشدار حذف می‌شد ({n}):"
    admin_life_report_would_warn = "⚠️ هشدار حذف می‌رفت ({n}):"
    admin_life_report_nothing = "چیزی برای حذف یا هشدار نبود."
    admin_life_report_would_stop = (
        "⛔️ این تعداد از حد یه اجرا بیشتره؛ تو اجرای واقعی هیچ‌کدوم حذف نمی‌شد و فهرستشون "
        "برای تأیید دستی می‌موند."
    )
    admin_life_report_run_head = "🗑 حذف خودکار ({source})\nحذف شد: {deleted} • رد شد: {skipped}"
    admin_life_report_skipped_head = "رد شده‌ها:"
    admin_life_report_stop = (
        "⛔️ حذف خودکار متوقف شد\n\n{n} سرویس برای حذف آماده بود و این از حد یه اجرا بیشتره "
        "(بیشتر از ۲۰ سرویس، یا بیشتر از یک‌چهارم همهٔ سرویس‌ها وقتی بیشتر از ۵ تا باشه). "
        "هیچ‌کدوم حذف نشد.\n\n"
        "فهرستشون رو ببین و تأیید کن: ⚙️ تنظیمات ← 🗓 هشدار و حذف سرویس ← 🗂 فهرست حذف ({n})"
    )
    admin_life_skip_reasons = {
        "exempt": "معاف از حذف",
        "status_active": "دوباره فعاله",
        "status_on_hold": "تو انتظاره",
        "status_disabled": "قطع شده",
        "status_other": "وضعیتش تو پنل عوض شده",
        "open_order": "سفارش در جریان داره",
        "recent_change": "تازه تغییر کرده",
        "not_due": "هنوز وقتش نشده",
        "not_yet_expired": "هنوز منقضی نشده",
        "limited_first_seen": "تازه دیده شد که حجمش تموم شده",
        "already_warned": "هشدارش رفته",
        "warned_recently": "هشدار حذفش تازه رفته",
        "warn_first": "اول باید هشدار حذف بره",
        "warn_window": "اول باید هشدار حذف بره",
        "rule_off": "قانونش خاموشه",
        "panel_error": "پنل جواب نداد",
        "panel_missing": "تو پنل پیدا نشد",
        "delete_failed": "پنل حذف رو قبول نکرد",
        "gone": "قبلاً حذف شده",
        "owner_banned": "صاحبش مسدوده",
        "no_expiry": "انقضا نداره",
        "mode_off": "حذف خاموشه",
        "panel_not_read": "پنل جواب نداد",
        "clock_ahead": "ساعتش درست نیست",
        "other": "شرایط حذف رو نداشت",
    }
    admin_life_report_warn_stop = (
        "⚠️ ارسال هشدار حذف هم متوقف شد\n\n{n} سرویس باید هشدار حذف می‌گرفتن و این از حد یه "
        "اجرا بیشتره؛ هیچ هشداری نرفت. فهرست «هشدار می‌رفت» رو ببین و تأیید کن تا هشدارها بره."
    )
    admin_life_report_reads_failed = (
        "⚠️ خوندن {n} سرویس از پنل شکست خورد. اگه نصف سرویس‌ها یا بیشتر شکست بخورن (یا هیچ‌کدوم "
        "نخونده بشه)، این تمرین حساب نمی‌شه و «روشن» رو باز نمی‌کنه."
    )
    admin_life_report_id_mismatch = (
        "⚠️ {n} سرویس تو پنل با شناسهٔ دیگه‌ای جواب داد (اسمش همونه ولی کاربر فرق داره)، پس به "
        "هیچ‌کدوم دست نخورد:"
    )
    admin_life_back_to_rehearsal = "عدد حذف عوض شد، پس حذف خودکار به «تمرین» برگشت. بعد از یه تمرین تازه می‌تونی دوباره «روشن» کنی."
    admin_life_approve_warn_button = "📣 ارسال هشدار همین فهرست ({n})"
    admin_life_approve_warn_confirm = (
        "📣 به صاحب {n} سرویس هشدار حذف فرستاده بشه؟\n\n"
        "چیزی حذف نمی‌شه. قبل از هر هشدار دوباره از پنل پرسیده می‌شه و سرویسی که دیگه تموم‌شده "
        "نیست یا معافه رد می‌شه."
    )
    admin_life_not_owner = "فقط مالک فروشگاه می‌تونه این عددها رو عوض کنه."

    # ---- Marketing nudges (admin) ------------------------------------------
    admin_mkt_screen = (
        "📣 پیام‌های بازاریابی\n\n"
        "فقط برای مشتری‌های عادی: نماینده‌ها و مشتری‌های نماینده‌ها هیچ‌وقت پیام نمی‌گیرن. "
        "تخفیف فقط وقتی همراه پیامه که زیر همون پیام درصد گذاشته باشی؛ یادآوری سفارش باز تخفیف نداره.\n"
        "هر پیام فقط برای اتفاق‌های بعد از لحظهٔ روشن‌کردنش می‌ره، و هر مشتری توی ۲۴ ساعت "
        "بیشتر از یکی نمی‌گیره (یادآوری سفارش باز جزو این شمارش نیست).\n\n"
        "{lines}"
    )
    admin_mkt_line = "{title}: {state}\n{what}"
    admin_mkt_state_off = "خاموش"
    admin_mkt_state_on = "روشن از {since}"
    admin_mkt_what = {
        "cart": (
            "{cart_minutes} دقیقهٔ آخر مهلت یه سفارش بازِ بدون فیش، یه یادآوری با دکمهٔ ادامه. "
            "برای هر سفارش یک بار."
        ),
        "trial_convert": (
            "بعد از {trial_hours} ساعت از پایان تست رایگان، برای کسی که هنوز چیزی نخریده. "
            "یک بار برای هر مشتری."
        ),
        "winback": (
            "{win_from} تا {win_to} روز بعد از پایان یه اشتراک پولی، اگه مشتری سرویس فعال دیگه‌ای "
            "نداره. اول وضعیت سرویس از پنل خونده می‌شه."
        ),
        "upgrade": (
            "متن هشدار حجم عوض می‌شه و پلن بزرگ‌تر پیشنهاد می‌ده، وقتی حجم زودتر از زمان داره "
            "تموم می‌شه (حداقل {time_left}٪ از زمان مونده باشه). پیام جدا نیست و هشدار دوباره "
            "نمی‌ره."
        ),
    }
    admin_mkt_saved = "✅ ذخیره شد."
    admin_mkt_offer_percent_prompt = (
        "🎁 تخفیف «{campaign}»\n"
        "چند درصد تخفیف شخصی همراه این پیام بره؟ یه عدد صحیح بین ۰ تا {high} بنویس. "
        "۰ یعنی بدون تخفیف.\n"
        "تخفیف فقط برای همون مشتری و برای یک خرید کار می‌کنه و کد نمی‌خواد.\n"
        "الان: {current}"
    )
    admin_mkt_offer_hours_prompt = (
        "⏳ مهلت تخفیف «{campaign}»\n"
        "تخفیف بعد از ارسال پیام چند ساعت اعتبار داشته باشه؟ یه عدد صحیح بین ۱ تا {high} بنویس.\n"
        "الان: {current}"
    )
    admin_mkt_offer_percent_range = "یک عدد صحیح بین ۰ تا {high} بنویس. ۰ یعنی بدون تخفیف."
    admin_mkt_offer_hours_range = "یک عدد صحیح بین ۱ تا {high} ساعت بنویس."
    admin_mkt_offer_no_such_campaign = "این پیام تخفیف نداره."
    admin_mkt_offer_none = "بدون تخفیف"
    admin_mkt_offer_state = "🎁 تخفیف همراه پیام: {percent}٪، {hours} ساعت اعتبار"
    admin_mkt_offer_input_error = "❌ {reason}"
    admin_mkt_not_owner = "فقط مالک فروشگاه می‌تونه این پیام‌ها رو روشن و خاموش کنه."

    # ---- Service lifecycle: what the customer reads ------------------------
    # `notify_*`, not `admin_*`: the CUSTOMER (or the reseller who owns the
    # service) reads these, so the voice rules apply.
    notify_delete_warning = (
        "⚠️ سرویس «{name}» حدود {days} روز دیگه حذف می‌شه.\n"
        "📅 تاریخ حذف: {date}\n"
        "اگه می‌خوای نگهش داری، «تمدید این سرویس» رو بزن."
    )
    notify_trial_ending = (
        "🎟 تست رایگانت تا یه روز دیگه تموم می‌شه.\n" "اگه راضی بودی، از «خرید اشتراک» یه پلن بگیر."
    )
    notify_service_deleted = (
        "🗑 سرویس «{name}» حذف شد.\n" "اگه هنوز بهش نیاز داری، از «خرید اشتراک» یه سرویس جدید بگیر."
    )
    notify_volume_warning = (
        "⚠️ حجم سرویس «{name}» داره تموم می‌شه.\n"
        "📦 فقط {percent}٪ از حجمش مونده.\n"
        "برای ادامه، «تمدید این سرویس» رو بزن."
    )

    # ---- Marketing nudges: what the retail customer reads -------------------
    notify_volume_upgrade = (
        "⚠️ حجم سرویس «{name}» زودتر از زمانش داره تموم می‌شه.\n"
        "📦 فقط {percent}٪ از حجمش مونده و {days} روز از زمانش باقیه.\n"
        "با این مصرف، «{package}» بهت بیشتر می‌خوره."
    )
    notify_cart_reminder = (
        "🛒 سفارشت هنوز بازه و به‌زودی منقضی می‌شه.\n" "اگه هنوز می‌خوایش، از دکمهٔ زیر ادامه بده."
    )
    notify_trial_convert = "🎁 تست رایگانت تموم شده.\n" "اگه راضی بودی، از دکمهٔ زیر اشتراک بگیر."
    notify_winback = (
        "👋 اشتراک «{name}» تموم شده.\n"
        "هر وقت خواستی برگردی، از دکمهٔ زیر تمدید کن یا پلن جدید بگیر."
    )
    # One line a nudge gains only when it carries a personal offer; both numbers come from
    # the owner's settings.
    notify_offer_line = (
        "🎁 تا {hours} ساعت آینده {percent}٪ تخفیف روی خریدت حساب می‌شه؛ کدی لازم نیست."
    )


texts = PersianTexts()
