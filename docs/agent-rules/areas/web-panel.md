---
paths:
  - "web/**"
  - "services/access.py"
  - "services/store_edit.py"
  - "services/store_dashboard.py"
  - "services/store_group_settings.py"
  - "services/panel_group_list.py"
  - "services/customer_card.py"
  - "services/wallet_request_key.py"
  - "services/personal_page.py"
  - "services/promo_admin.py"
  - "services/marketing_edit.py"
  - "services/marketing_view.py"
  - "services/store_plan_edit.py"
  - "services/store_changes.py"
  - "services/store_plan_view.py"
  - "services/settings_overview.py"
  - "services/reseller_admin.py"
  - "services/reseller_edit.py"
  - "services/reseller_sheet.py"
  - "services/lifecycle_view.py"
  - "tests/unit/web_panel_helpers.py"
  - "tests/unit/web_personal_helpers.py"
  - "tests/unit/web_reseller_helpers.py"
  - "tests/unit/test_web_panel_settings.py"
---

# Mini App panel (`web/`)

A Telegram Mini App served by the bot's own aiohttp app: server-rendered
Jinja2 pages, htmx for partial swaps, `initData` for login. Plan and owner's
decisions: `plans/2026-10-10-miniapp-panel.md`. It ships **off**
(`WEB_PANEL_ENABLED`).

## Layers

- **Rule: `web → services/ → database/`.** A route reads or writes through a
  service function and renders a template. No price, limit, validation or
  state rule in a route or a template, and no import from `bot/handlers`. A
  figure is formatted by `services/ux_format.py` (Jinja filters `toman`, `fa`).
  **Why:** a rule written in a route is a second copy the bot's screen does not
  share (the reseller price, inlined, made every reseller pay retail).
  **Test:** `tests/unit/test_web_panel_finance.py` (the page shows the
  figures `get_finance_report` returns).

## Switch off means nothing is mounted

- **Rule: `web/app.py::mount_web_panel` adds no route and no middleware while
  `WEB_PANEL_ENABLED` is off. The polling HTTP listener starts when the panel
  or the separately authenticated `BANK_SMS_ENABLED` receiver is on; the SMS
  switch never mounts panel routes.** Enabled with an empty or non-https `WEB_PANEL_URL`, `main()` stops
  before the database opens.
  **Why:** the panel is a new way in; a launch with the switch off must be the
  bot of `v0.11.0-rc1`.
  **Test:** `tests/unit/test_web_panel_mount.py`, `tests/unit/test_web_panel_startup.py`.

## Every panel path is guarded unless listed public

- **Rule: `web/auth.py::panel_auth` guards all of `/panel` except the shell
  and `/panel/static/`** (`is_public`). It checks the signature with
  aiogram's helper and the bot token, then the age of `auth_date` (one hour,
  small future skew), then the `User` row (banned refused), then
  `authorize`. **Default is admin only** (`services/access.py::is_admin`); the
  one exception is the personal prefix below. A refusal is 401 or 403 with one
  fixed Persian line and never says which check failed. `initData` and the
  token are never logged. Data responses carry `Cache-Control: no-store`.
  **Why:** Telegram sends `initData` to the page, not to the server, so the
  shell is public and static and every later request proves itself. A route
  that must remember to ask is a route someone forgets.
  **Test:** `tests/unit/test_web_panel_auth.py` (forged, stale, future,
  non-admin, banned, a route added later).
- **Rule: the shell (`GET /panel/`) holds no data, no navigation and opens no
  database session.** It asks `GET /panel/me/home` who is looking.
  **Why:** the navigation depends on the role, and a public page cannot know it.
  **Test:** `tests/unit/test_web_panel_finance.py::test_the_shell_carries_no_report_data`.

## Roles come from the signature, and `/panel/me` is the only non-admin prefix

- **Rule: the signed Telegram id decides the role** (`web/auth.py::identify`:
  admin by `is_admin`, reseller by `services/reseller.py::is_reseller`, else
  customer) and the middleware puts `user_id` (the `User` row id) and `role`
  on the request. **No route reads a user id, role or Telegram id from the
  path, query, form or any header other than `initData`.** The personal pages
  have no user id in their URLs at all; the one query parameter is the reseller
  list's `offset`.
  **Why:** a page keyed on a number the browser sends is every customer's data
  one edit away.
  **Test:** `tests/unit/test_web_panel_roles.py::test_a_user_id_smuggled_in_the_request_changes_nothing`,
  `tests/unit/test_web_panel_personal.py` (two customers, two resellers).
- **Rule: only `/panel/me` and what is under it (`PERSONAL_PREFIX`) opens to a
  non-admin, only while `WEB_PANEL_PUBLIC_ENABLED` is on, only with a `User`
  row, never when banned; `/panel/me/reseller` also needs the reseller flag.**
  Every other path, including one added later, stays admin only. An admin may
  open the personal pages with the switch off (their own account). No bot
  button points non-admins here: when they get an entry point is the owner's
  open decision.
  **Why:** the panel is a way in; letting customers in is a decision, and the
  switch ships off.
  **Test:** `tests/unit/test_web_panel_roles.py` (switch off and on, customer on
  the reseller route, every registered admin route, banned, no row).
- **Rule: `GET /panel/me/home` answers with the navigation and first screen of
  the caller's role** (`web/nav.py`); an admin link never reaches a non-admin.
  The admin's first screen is the one constant `ADMIN_FIRST_SCREEN`.
  **Test:** `test_home_gives_each_role_its_own_nav_and_first_screen`,
  `test_home_never_gives_a_non_admin_an_admin_link`.

## Personal pages: no credential, no offer, best-effort panel reads

- **Rule: the customer page and the reseller dashboard show no subscription
  link, QR or token, no discount, offer, promo or nudge,
  no other person's data and no write.** `services/personal_page.py`'s item
  types have no field for a link.
  **Why:** the link is a credential (an open owner decision whether it is ever
  shown); an offer is live only while its bot message is out and is never
  promised to a user with an open order (`marketing.md`), so a page that showed
  one would be a second promise the code does not keep.
  **Test:** `test_no_subscription_link_or_token_appears_on_either_page`,
  `test_no_page_states_a_discount_an_offer_or_a_nudge`,
  `test_the_pages_write_nothing`.
- **Rule: panel reads are best-effort and go through `services/`
  (`status_map_for_services`, the loop the bot's lists use).** A failed or
  slow read, or `panel_watch.is_panel_down`, drops that service's usage lines
  and nothing else; the order's expiry date stays; no error text reaches the
  page. A good read is cached 60 s per person and service, in this process.
  **Why:** a customer who cannot see usage still needs the expiry and the
  wallet; a page that fails whole teaches people the panel is broken.
  **Test:** `test_a_failed_panel_read_drops_the_usage_and_leaks_nothing`,
  `test_a_panel_confirmed_down_is_not_asked_and_drops_the_usage`,
  `tests/unit/test_status_map.py`.
- **Rule: a sale is counted by `services/reporting.py::_window`** (narrowed to
  one user by `user_sales`), never by a second query.
  **Test:** `test_the_sales_figures_are_the_existing_definition_of_a_sale`.
- **Rule: personal-page copy passes the bot's own voice, digit, punctuation and
  glossary checks** (`tests/unit/test_web_panel_copy.py`, which imports the
  checkers of `test_brand_voice.py` and `test_customer_copy_consistency.py`).
  A new customer-facing template goes into its `CUSTOMER_TEMPLATES`.
  **Test:** that file.

## One admin check

- **Rule: the admin question is `services/access.py::is_admin`.** The bot's
  `common.is_admin` and `admin._is_admin` delegate to it; the panel calls it.
  **Why:** two copies of the body already existed; a third in `web/` is a list
  that can disagree.
  **Test:** `tests/unit/test_access_is_admin.py`.

## Plain routes, no sub-app

- **Rule: routes are added to the bot's app with full `/panel/...` paths, not
  through `add_subapp`.** **Why:** aiohttp 3.9.5 resolves a sub-app through
  `request.url`, which the installed yarl refuses for a `Host` header that has
  a port: a 500 on every panel request. **Test:**
  `tests/unit/test_web_panel_mount.py::test_a_host_header_with_a_port_is_served`.

## No CDN at runtime

- **Rule: scripts, styles and the font the panel needs are in `web/static/`**
  (`htmx.min.js` and `fonts/Vazirmatn-wght.woff2` pinned, version, source and
  hash in `web/static/NOTICE`); the one external script is Telegram's own
  `telegram-web-app.js`.
  **Why:** the panel's users are in Iran.

## Touch and reading (`web/static/panel.css`, `panel.js`)

- **Rule: anything tapped is at least 40px tall, a form control is at least 16px, and the phone's
  section bar never wraps or cuts a label: it stays on one line and scrolls when a phone is too narrow
  for all sections.** A switch gets its size from `.sw::after`, a back link from its padding.
  **Why:** measured on 2026-10-11 at 360 to 430px: a switch was 24px, a back link 16px, a radio 13px;
  seven sections wrapped «کدهای تخفیف» onto a second line and below 350px the last tab was cut off;
  iOS zooms the whole page when a control under 16px takes focus.
  **Check:** by eye and by measurement in a 360px window (no unit test reads a stylesheet): no control
  under 36px, `scrollWidth` equal to the window's, the bar one line high.
- **Rule: the navigation marks the section of the screen that is shown, not the last tab tapped; a new
  screen opens at its top; a tab or filter of the same screen and every form keep the reader's place;
  a request in flight draws a line on top.** All of it is `panel.js` on htmx's events
  (`htmx:afterSettle`, `htmx:beforeRequest`, `htmx:afterRequest`); the line is paper-coloured because
  the accent marks only the current section.
  **Why:** a reseller's sheet opened from a customer's card left «مشتری‌ها» marked; a section opened
  from the bottom of a long page stayed scrolled down; on a slow network a tap showed nothing.
  **Check:** manually verify the affected screen.

- **Rule: one pattern per job on every screen.** The way back is a `.back` link above the title that
  names where it goes («‹ فروشگاه»), never a button with an arrow emoji; the second button of a confirm
  page is «انصراف» and the first carries no emoji; a row that opens a screen is `.rowbtn.go` (a
  chevron); a form used rarely (a new group) opens from a `details.more`, with a visible label on
  every field (a placeholder is an example, never the label); radios and checkboxes are drawn by
  `panel.css`, not by the browser.
  **Why:** the review of 2026-10-11 against the taste checklist (`design-taste-frontend`: states,
  labels above inputs, one label per intent, shape and colour locks) found three ways back, three
  words for cancel, a placeholder used as a label and browser-blue radios on one panel.
  **Check:** by eye when a template is added: copy the pattern of the nearest screen.

- **Rule: an emoji in the bot's text is drawn as a line icon by `panel.js` (`drawIcons`, the `ICONS`
  table), in the browser; the server's HTML keeps the bot's words byte for byte.** Never strip or
  replace an emoji in a template, a route or a service to change how the panel looks: add the emoji
  to `ICONS`. An emoji with no entry stays an emoji; the preview of a Telegram button (`.tg-btn`,
  `#plan-preview`) is never touched, because the customer will see the emoji; an `<option>` loses
  its emoji (it cannot hold an icon).
  **Why:** the owner's decision of 2026-10-11 (emoji as icons read as careless on a panel; the bot's
  texts stay as they are). The tests compare the panel's text with the bot's, so the swap cannot
  happen on the server without a second copy of every line.
  **Check:** manually verify the affected screen; with the script blocked the page shows the
  emoji and still works.
- **Rule: a line whose wording only fits a chat («بفرست», «—» for empty, a long warning shown
  twice) has a `web_…` twin in `bot/texts/fa.py`, and the route or view service hands the twin
  to the template.** When the meaning of the bot's line changes, change its twin in the same
  commit. Never cut or replace words in a template.
  **Why:** a number is sent in a chat and typed in a form; one string for both read wrong on
  one of them, and a template that edits a string is a second copy of it.
  **Test:** `test_the_cut_prompt_says_write_on_the_web_and_send_in_the_bot`,
  `test_the_form_asks_to_write_a_number_where_the_bot_asks_to_send_one`,
  `test_an_empty_value_is_a_word_on_the_web_and_a_dash_on_the_bots_card`.

## Store screens (`/panel/store`)

- **Rule: a status line, a ⚠️ warning, a group mark or a retail price on a
  store screen is composed in `services/store_dashboard.py`, and the bot's
  group screen calls the same functions** (`group_facts`, `hub_body`,
  `group_marks`, `retail_cell`; the bot HTML-escapes through `quote=`, Jinja
  escapes on output). A template prints what it is given and decides nothing.
  **Why:** the bot's dashboard text lived inside its handler; a copy in a
  template is how the two screens would disagree about whether a group is for
  sale. **Test:** `tests/unit/test_web_panel_store.py::test_the_dashboard_is_the_bots_dashboard`
  (every line `ha._dashboard_text` prints is on the page).
- **Rule: a store write carries the published store version the page was drawn
  from, and `services/store_edit.py::assert_store_version` refuses (HTTP 409,
  one Persian line, a reload button) when the current published version
  differs.** The read is inside the transaction that writes (`FOR UPDATE` on a
  server database; SQLite refuses the later writer, and `guarded_store_write`
  maps that «database is locked» to the same refusal, once for every store write). Never merged, never
  retried. So a double-submitted form applies once: the first publish moves the
  version, the second form still carries the old one.
  **Why:** the bot, another tab or an undo can change the catalog while a page
  is open; applying a price drawn from the old state onto the new one is a
  silent overwrite of the owner's own edit.
  **Test:** `test_a_form_from_before_another_edit_is_refused_and_nothing_is_merged`,
  `test_the_same_form_sent_twice_changes_the_price_once`,
  `test_two_forms_sent_at_the_same_moment_apply_once`.
- **Rule: a form names its plan and group by slug, never by row id.**
  `edit_package_price` finds the plan in the draft by `slug`; a publish mints
  new row ids, and a numeric id from an older version names nothing.
  **Why:** `docs/agent-rules/areas/catalog.md` (slug is the identity): a row id from the
  page before the last publish can be another plan's id after it.
  **Test:** `test_a_row_id_from_an_older_version_edits_nothing`.
- **Rule: a panel write is a `POST`; a `GET` never changes anything, and a page
  read never creates a draft** (`get_catalog_view`, as the bot's browse screens).
  The `X-Telegram-Init-Data` header stays the only way in, which also stops a
  cross-site form post (a browser cannot add that header); no cookie session.
  **Why:** a link or a prefetch must not edit the catalog.
  **Test:** `test_looking_writes_nothing_and_the_write_path_has_no_get`,
  `test_the_store_pages_need_the_panel_login`.
- **Rule: a store write is for a store owner (`is_store_owner`, the check the
  bot's price button and prompt make); an admin who is not one reads, sees
  «(فقط خواندنی)», gets no edit control, and a forged POST is refused 403 with
  the bot's own line (`admin_store_owner_only`).** `guarded_store_write` asks
  `assert_store_owner` first, so a route added later cannot skip it.
  **Why:** `STORE_OWNER_TELEGRAM_IDS` is separate from `ADMIN_TELEGRAM_IDS`
  (an edit control the server would refuse is an inert control).
  **Test:** `test_an_admin_who_is_not_a_store_owner_reads_but_cannot_write`.
- **Rule: the price edit is `services/store_edit.py::set_package_price`, the
  call the bot's typed price prompt makes (parse, refuse zero and non-numbers,
  `update_draft_package`, `commit_and_activate(notes="پلن ویرایش شد")`).**
  The change-log note is Persian because «تغییرات فروشگاه» reads it back to the
  owner and the edit can be undone from the bot.
  **Test:** `test_the_web_edit_and_the_bots_edit_leave_the_same_state`,
  `test_a_price_edit_changes_the_price_and_the_log_says_so`.

## Group settings (`/panel/store/g/<slug>/settings/…`)

The bot's group dashboard ends in four sections (🛒 فروش, 🎟 تست, 🖥 پنل, ✏️ گروه), each
setting in exactly one. The page has the same four, each write a `POST` into the
service function the bot's control calls.

- **Rule: one guard for every store write.** `services/store_edit.py::guarded_store_write`
  (owner, current version, the «locked» mapping) wraps `edit_package_price` and
  `services/store_group_settings.py::edit_group_setting`, which also finds the group
  by slug and refuses a control the group does not show. A write route calls one of
  the two; it does not repeat the try/except.
  **Why:** the version check lives in the transaction that writes; a route that
  forgot it would merge an old form onto a new catalog.
  **Test:** `test_a_settings_write_needs_login_admin_owner_and_the_current_version`,
  `test_a_settings_form_sent_twice_applies_once`,
  `test_two_settings_forms_at_the_same_moment_apply_once`, and
  `test_every_post_route_under_the_store_has_a_guard_case` (a POST route with no
  case in `CASES` fails the build).
- **Rule: when a control shows is `services/store_group_settings.py::visible_controls`,
  and the bot's four section keyboards and the template both ask it.** Tiers and
  durations under the flat list, the custom device limit without custom purchase,
  «تست نماینده» without a trial, the premium panel group without tiers, «نوع»
  once a group has plans or children. A forged POST for a hidden control is
  refused like a stale form (409).
  **Why:** a copy of these conditions in a template is how the two screens would
  disagree about what the owner may set; a control that saves a flag nothing reads
  is the inert control `AGENTS.md` refuses.
  **Test:** `test_a_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it`,
  `test_visible_controls_hides_a_control_that_would_do_nothing`,
  `test_a_forged_post_for_a_control_the_group_does_not_show_is_refused`.
- **Rule: a web write leaves the same state and the same «تغییرات فروشگاه» row as
  the bot's control for the same input.** Every note is in `store_group_settings.py`
  and both callers use it; `EXPECTED_NOTE` in the test spells them out.
  **Test:** `test_a_web_write_leaves_the_state_and_log_the_bots_control_leaves`
  (drives the bot's real handler against a second database).
- **Rule: the stop-sale switches («فروش جدید», «نمایش به مشتری») have no form and no
  route on the web.** The dashboard line «فروش» shows the state; `edit_group_setting`
  raises for those controls even if a route asked.
  **Why:** the plan keeps stop-sale of a group in the bot; «نمایش به مشتری» hides the
  group, which stops the sale the same way.
  **Test:** `test_the_sale_switch_is_not_writable_from_the_web`,
  `test_the_guard_refuses_a_stop_sale_control_even_if_a_route_asked_for_one`.
- **Rule: a destructive change asks first, with the bot's sentence and count.**
  Removing a duration tab: the first request counts (`count_prices_for_duration`)
  and draws the bot's `admin_store_dur_del_confirm`; only a second request with
  `confirm=1` and the same store version removes it. Asking writes nothing, not
  even a draft.
  **Test:** `test_removing_a_duration_asks_first_with_the_bots_count_and_removes_nothing`.
- **Rule: «نامحدود» devices is `0` through the web too** (`require_number`, never
  `int(x or 1)`), and a blank box is the invalid-input line.
  **Test:** `test_unlimited_devices_stays_zero_through_a_web_edit`,
  `test_a_blank_device_limit_is_refused_not_read_as_one`.
- **Rule: a typed number is bounded by the column, not by a price cap.**
  `parse_toman` returns `None` past `2**63 - 1` (SQLite's integer), `parse_sort_order`
  the same, and both answer with `admin_store_invalid_input`.
  **Why:** thirty nines was an `OverflowError` out of the driver: an unhandled
  error in the bot and a 500 on the page.
  **Test:** `tests/unit/test_store_toman_bound.py`,
  `test_a_huge_number_in_a_setting_is_the_invalid_input_line`,
  `test_a_huge_price_on_the_web_is_the_invalid_input_line`,
  `test_a_huge_price_in_the_bots_prompt_is_the_invalid_input_message`.
- **Rule: a panel-group change that would leave live services on the old group is
  refused on the page, with the count and a line that sends the owner to the bot.**
  `services/store_group_settings.py::panel_groups_stranded` asks before the write
  (the groups a purchase gets now, the groups it would get, the accounts of the group)
  and writes nothing; the bot saves and then offers the move
  (`services_left_on_old_groups`). A change that strands nothing, an empty target, or
  the same ids saves as before.
  **Why:** moving services is a panel write and only the bot offers it; the page used
  to save and then report a count nobody could act on. Until the owner decides how the
  page offers the move, it must not create the stranded state.
  **Test:** `test_a_panel_group_change_that_would_strand_services_is_refused_with_the_count`,
  `test_a_panel_group_change_that_strands_nothing_is_saved`,
  `test_emptying_a_group_that_has_services_is_not_a_strand`.
- **Rule: the panel's own groups are listed by `services/panel_group_list.py::fetch_live_groups`,
  for the bot's picker and the page alike, and the page asks the panel only when the owner
  taps «نمایش گروه‌های پنل».** `GET …/settings/panel-groups/pick` (and `…/premium-groups/pick`)
  is owner only, a read that writes nothing, and answers with checkboxes (name, the id in a
  muted `.lat` span, the group's own ids checked, a chosen id the panel no longer lists shown
  checked as «در پنل نیست», `picker_rows` for both screens). Its form posts the same fields to
  the same `panel-groups` / `premium-groups` route as the typed box (`ids` once per checked
  group, joined by `_ids_text` and parsed by `parse_panel_group_ids`; the page's `version` rides
  in the query and is echoed), so the stranded-services refusal, the stale-version refusal and
  an empty choice behave exactly as for the typed box. The panel not answering (error, timeout,
  no key, empty list) is `admin_store_pick_unreachable` as one line, status 200, and the typed
  box stays. The read snapshot is let go (`end_read_transaction`) before the first await on
  the panel; the settings page itself makes no panel call.
  **Why:** the owner typed ids from memory (2026-10-11); a page that listed the groups on
  every draw would make a slow panel a slow settings page and hold a snapshot across the
  network; a second write path would be a second set of rules.
  **Test:** `tests/unit/test_web_panel_store_panel_picker.py`
  (`test_saving_through_the_picker_leaves_what_the_typed_box_leaves`,
  `test_the_settings_page_never_asks_the_panel_and_offers_the_button`,
  `test_a_panel_that_does_not_answer_is_one_line_and_nothing_else`,
  `test_nobody_but_a_store_owner_gets_the_picker_and_the_panel_is_not_asked`,
  `test_the_read_snapshot_is_let_go_before_the_panel_is_awaited`).
- **Open (not built on the web):** moving existing services after a panel-group
  change (a panel write; the page only says how many services sit on the old
  group). Group create and delete and plan ordering are built (plan editor, below).

## The customers screen

- **Rule: a web wallet adjustment carries a request key, and the same request
  sent twice moves the balance once.** The confirm page's token is minted by
  `services/wallet_request_key.py` and signed (HMAC over the admin, the
  customer and the signed amount); its nonce is the `stamp` of the ledger key
  `admin_adjust:{admin}:{customer}:web:{nonce}`, so `adjust_wallet_manually`
  and the ledger's unique index do the rest. A token for customer A, another
  amount, the other direction or another admin is refused («stale_button»)
  and writes nothing. Two deliveries racing each other: the loser rolls back
  and answers with the winner's entry (`apply_wallet_adjustment`), never a 500
  and never a second row.
  SQLite may reject the loser before the winner commits: retry only BUSY/LOCKED
  with a bounded backoff and a fresh transaction. Release the empty replay-read
  snapshot before yielding. `tests/integration/test_wallet_adjust_busy.py`
  covers the delayed commit and bounded rollback behavior.
  **Why:** a retry on a phone with a weak connection is the normal case, and
  the bot's own protection (FSM state cleared after the tap) does not exist on
  the web. The bot already keyed its adjust on the message id; what it lacked
  was an answer for a repeat: a landed *deduction* repeated after the balance
  dropped read «موجودی کافی نیست» because the overdraft check ran before the
  replay lookup.
  **Test:** `tests/unit/test_web_panel_customers.py` (`test_the_same_confirm_sent_twice…`,
  `test_two_deliveries_at_the_same_moment…`, `test_a_key_minted_for_one_customer…`),
  `tests/unit/test_wallet_adjust_service.py`.
- **Rule: the report is written by the ledger call, inside the same
  transaction, and nothing is sent from the request.** No customer message
  goes out from the web (the bot's confirm tap messages the customer; the
  panel has no `Bot` and `bot/main.py` is not touched for it — open decision).
  **Test:** `test_a_failure_after_the_ledger_write_leaves_neither_entry_nor_report`,
  `test_the_report_row_is_written_before_the_commit`.
- **Rule: the wallet form uses the bot's checks and words**
  (`preview_wallet_adjustment`, `parse_signed_toman`; `texts.admin_panel_invalid`,
  `admin_wallet_insufficient`): zero, a minus sign beside a direction, text,
  or a number past 64 bits is «مقدار نامعتبر است.»; a deduction past the
  balance is refused, not clamped. Any admin may adjust, as in the bot.
  **Test:** `test_a_bad_amount_is_refused_in_the_bots_words_and_writes_nothing`,
  `test_a_web_adjustment_and_the_bots_leave_the_same_trail`.
- **Rule: the customer card offers only «تغییر کیف پول» (and, on a reseller's card, a
  link to that shop's price sheet, `/panel/resellers/<id>`).** Cut, activate, ban,
  gift service, manual renew, trial reset, the reseller role, receipts, and zeroing
  the wallet have no route and no control; the card shows their state (banned, role,
  trial) read-only and never a subscription link. A reseller's prices are written
  on the resellers screen below, never from the card.
  **Why:** `plans/2026-10-10-miniapp-panel.md` («Stays in the bot», «Money on
  the web»): those write to the VPN panel or are time-critical. «Zero» is a
  separate service function (`zero_wallet`, its own key) and stays in the bot.
  **Test:** `test_there_are_exactly_these_customer_routes…`,
  `test_the_bots_own_actions_have_no_panel_route`,
  `test_the_card_has_no_subscription_link_and_no_unlisted_control`.
- **Rule: the card's facts come from `services/customer_card.py::card_facts`**,
  which the bot's `_card_text` also calls; a name, username or group title is
  user input and is escaped by the bot (`html.escape`) or by Jinja, never
  `|safe`.
  **Test:** `tests/unit/test_customer_card_snapshot.py` (the bot's card, byte
  for byte, recorded before the move), `test_a_name_with_html_is_escaped_everywhere`.

## Resellers (`/panel/resellers`)

The bot's «🏪 نمایندگان» screens on the web: the list, and one shop's sheet with its rate (toman per
GB, toman per day), its trial quota and extra-trial price, and each ready plan's agreed price beside
the retail price of the same cell, with the percent cut and its undo.

- **Rule: the bot's reseller admin rules are `services/reseller_admin.py` (typed numbers, which plans
  are on a screen, the preview, apply and undo of a cut, the guard of a write); the page reads through
  `services/reseller_sheet.py` and writes through `services/reseller_edit.py`, and the bot's handlers
  call the same functions.** A web write calls the bot's own `set_plan_price`, `clear_plan_price`,
  `set_rate`, `apply_cuts`, `undo_screen_cut` and leaves the rows the bot's handler leaves for the same
  input, with the same refusal lines. The retail price, the quote and the till stay in
  `services/reseller.py` and `services/checkout.py`; nothing on these pages decides a price (the ⚠️
  beside a cell is the quote's `crossed_over`).
  **Why:** the rules lived inside the handler; a copy in a route or template is how the two screens
  would disagree (the reseller price, inlined, made every reseller pay retail).
  **Test:** `tests/unit/test_web_panel_resellers.py::test_a_web_write_leaves_the_rows_the_bots_handler_leaves`
  (the bot's real handlers on a second database, a tabbed group and a flat list),
  `test_a_bad_plan_price_is_refused_in_the_bots_words_and_writes_nothing` and its rate and percent twins,
  `test_the_price_set_on_the_web_is_the_price_the_reseller_is_charged` and the cut, clear, rate and
  slug-survives-a-republish tests next to it.
- **Rule: every reseller write goes through `guarded_reseller_write`.** The form carries what it was
  drawn from (`seen`: the cell's or the rate row's `updated_at`, `none` while there is no row, the undo
  snapshots' ids, the digest of a cut's preview); inside the transaction that writes, the shop's user row
  is locked, the shop must still be a reseller (404 otherwise), and a token that is not the current one,
  or no token at all, is refused (409, one Persian line, a reload button), never merged. SQLite's
  «database is locked» and a lost race to create the row map to the same refusal.
  **Why:** the bot, another tab or an undo can change a shop's prices while a page is open; a price
  drawn from the old state applied onto the new one is a silent overwrite of the owner's own edit, and a
  double-submitted form must apply once. The token is a microsecond timestamp (measured on SQLite: 500
  back-to-back writes, 500 distinct tokens, smallest gap 3.4 ms); a rewrite of the same value changes no
  row and keeps its token, which is harmless.
  **Test:** `test_a_price_form_from_before_another_edit_is_refused_and_nothing_is_merged`,
  `test_a_form_with_no_token_is_refused_like_a_stale_one`, `test_the_same_clear_sent_twice_clears_once`,
  `test_two_forms_sent_at_the_same_moment_apply_once`, `test_the_row_token_has_microsecond_resolution_on_sqlite`.
- **Rule: a cell is written only if the matrix draws it today** (`shown_cells`): a slug the catalog does
  not have, a custom-purchase template, a duration or tier that is not a tab of that plan's group, or a
  flat plan at anything but its own duration at the economy tier is a stale form (409), not a new row.
  The page names a plan and group by slug, never by row id.
  **Why:** `docs/agent-rules/areas/catalog.md`: a row id from before a publish can be another plan's id after it;
  a price on a cell nothing reads is the inert control `AGENTS.md` refuses.
  **Test:** `test_a_forged_form_is_refused_and_writes_nothing`,
  `test_a_price_set_on_the_web_survives_a_republish_that_mints_new_row_ids`,
  `test_a_form_drawn_before_a_republish_still_writes_the_right_cell`.
- **Rule: a percent cut is two requests on one `POST` route and the first writes nothing.** Without
  `confirm=1` it draws the bot's preview and a digest of exactly that set (the percent and every
  duration, plan, old and new price); with `confirm=1` the server rebuilds the set and applies it only if
  its digest is the form's. Any price of the set moved, joined or left in between, or a different percent,
  tab or group, is refused and nothing is written. Only cells that already have an agreed price move, and
  only this screen's plans: another group on the same duration tab is untouched, and so is its undo.
  A flat list's cut is written under the economy tier whatever tier the screen carried.
  **Why:** the bot confirms a stored preview; the web has no session, so the digest is the stored preview.
  **Test:** `test_the_first_cut_request_writes_nothing_and_shows_the_bots_preview`,
  `test_the_confirm_applies_exactly_the_previewed_set`,
  `test_if_a_price_of_the_set_moved_between_preview_and_confirm_nothing_is_written`,
  `test_the_confirm_form_cannot_be_turned_into_another_cut`,
  `test_a_cut_moves_only_cells_with_an_agreed_price_on_this_screens_plans`,
  `test_an_undo_takes_back_exactly_the_cut_on_its_own_screen`, `test_a_second_undo_is_refused_in_the_bots_words`.
- **Rule: granting or removing the reseller role stays in the bot.** No route under `/panel/resellers`
  or `/panel/customers` touches `is_reseller`; a person who is not a reseller has no sheet (404) and no
  write.
  **Why:** `plans/2026-10-10-miniapp-panel.md`: the role is a decision made from the customer card, with
  a confirm for removal.
  **Test:** `test_there_are_exactly_these_reseller_routes_and_none_grants_the_role` (a route added later
  with no case fails the build), `test_a_person_who_is_not_a_reseller_has_no_route_to_write_to`.
- **Rule: any admin may read and write, as in the bot** (`_deny_if_not_admin` is the only check its
  reseller controls make); there is no store-owner check, unlike the store's writes. A non-admin, a
  reseller, a customer and an unsigned request are refused on every route, with the personal switch on or
  off, and a `GET` never writes.
  **Test:** `test_any_admin_may_write_a_store_owner_is_not_needed`, `test_only_an_admin_reaches_any_reseller_route`,
  `test_a_get_never_writes`.
- **Rule: a typed number is bounded by its column** (`parse_reseller_input`): past 64 bits, «²» and the
  like are the invalid-number line; the monthly trial quota is a 32-bit column (`QUOTA_MAX`).
  **Test:** `test_a_number_at_the_columns_bound_is_saved_and_one_past_it_is_the_invalid_line`,
  `tests/unit/test_reseller_admin.py`.
- **Open:** a user id past 64 bits in the URL (`/panel/resellers/999…9`, and equally `/panel/customers/…`)
  is a 500 (`OverflowError` from the driver); admin only. Pinned by a strict `xfail`
  (`test_a_user_id_past_64_bits_in_the_url_is_a_404`); fixing it in the customers routes too is the owner's call.

## Promo codes (`/panel/promo`)

- **Rule: the wizard's validation and its three writes are `services/promo_admin.py`, and
  the bot's handlers and the web routes call the same functions** (`parse_code_and_percent`,
  `parse_promo_number`, `answers_from_form` / `rules_from_answers`, `save_promo_code`,
  `toggle_promo_code`, `delete_promo_code`, `promo_lines`). The three change-log notes
  («کد تخفیف ثبت شد», «کد تخفیف روشن/خاموش شد», «کد تخفیف حذف شد») live there.
  **Why:** the code list, the limits and the note were inside the handler; a copy on the web
  is how a limit or a note would drift. **Test:**
  `tests/unit/test_web_panel_promo.py::test_a_web_write_leaves_the_document_and_log_the_bots_control_leaves`
  (the bot's real wizard and buttons on a second database).
- **Rule: every promo write goes through `guarded_store_write`** (store owner only, the published
  store version the form carries, stale 409, POST only), because codes live in the pricing
  document. Checkout stays the one place that refuses an inactive code; the web toggle writes
  what the bot's toggle writes and nothing else.
  **Test:** `test_a_write_needs_login_admin_owner_and_the_current_version`,
  `test_a_form_sent_twice_applies_once`, `test_two_forms_at_the_same_moment_apply_once`,
  `test_a_code_switched_off_on_the_web_is_refused_at_checkout_and_on_again_is_accepted`, and
  `test_every_post_route_under_the_promo_page_has_a_guard_case` (a POST route with no case fails the build).
- **Rule: a code made on the web is stamped `counted_from` by the same `upsert_draft_promo` the
  bot's wizard calls; deleting and creating again starts from zero; the page's usage line is
  `live_usage_by_code`.** **Why:** `docs/agent-rules/areas/pricing.md`: a re-created code must not inherit the
  deleted code's uses. **Test:** `test_a_code_made_on_the_web_counts_from_the_moment_it_was_created`,
  `test_a_code_deleted_and_created_again_on_the_web_starts_from_zero`,
  `test_the_live_usage_on_the_page_is_the_number_the_checks_count`.
- **Rule: creating a code that exists replaces it (rules and percent) and keeps its count, as the
  bot's wizard does; that is how a code is edited. Both stored shapes (`"10"`, `{"percent","active"}`)
  render and stay valid after an edit.** **Test:** `test_a_duplicate_code_replaces_the_code_as_the_bots_wizard_does`,
  `test_both_stored_shapes_survive_a_web_edit`.
- **Rule: a percent that is not a finite number in (0, 100] is refused, and a typed number the
  reader cannot parse («²») is the invalid-number line.** **Why:** `float("nan")` compares false both
  ways, so the range check passed it and the bot stored a code with percent NaN; `"²".isdigit()` is true
  and `int("²")` raised out of the bot's prompt. **Test:** the `nan` and `²` rows of
  `test_a_bad_input_is_refused_with_the_bots_line_and_nothing_is_written`, `test_the_bot_refuses_the_same_percents`,
  `test_a_typed_number_the_driver_cannot_read_is_a_line_not_a_crash`.
- **Rule: deleting asks first with the bot's sentence** (`admin_store_promo_del_confirm`); only a second
  request with `confirm=1` and the same version deletes. Expiry is typed as days from now and shown as a
  Jalali date, as in the bot. **Test:** `test_deleting_asks_first_with_the_bots_sentence_and_removes_nothing`.

## Marketing campaigns (`/panel/marketing`)

- **Rule: the page is the bot's screen** (`services/marketing_view.campaign_cards`; the bot's `screen_text`
  is built from it) and every write is the bot's own service call (`set_campaign`, `set_offer_percent`,
  `set_offer_hours` with `parse_offer_percent` / `parse_offer_hours`) through `services/marketing_edit.py`:
  a store owner only (the bot's `admin_mkt_not_owner`), same refusal lines. Settings only: the page sends
  nothing, previews no customer message and has no «run now».
  **Why:** `docs/agent-rules/areas/marketing.md`: an offer is live only while its bot message is out, so a page that
  promised one would be a second promise the code does not keep.
  **Test:** `tests/unit/test_web_panel_marketing.py::test_a_web_write_leaves_the_row_the_bots_handler_leaves`.
- **Rule: a campaign's percent stays editable while the campaign is off, and nothing switches on by a GET,
  a missing field or a default value** (the toggle form carries an explicit `to=on|off`; anything else is
  refused). **Why:** the percent is read when the message sends, not when the switch flips; a default that
  switched a campaign on would mail the backlog's successors. **Test:**
  `test_a_percent_stays_editable_while_the_campaign_is_off`, `test_nothing_is_switched_on_by_a_missing_or_unknown_field`,
  `test_every_campaign_ships_off_and_looking_writes_nothing`.
- **Rule: these settings are not in the store version, so a marketing form carries a token of its own** (the row's
  `updated_at`, `none` while there is no row), read again inside the transaction that writes
  (`services/marketing_edit.guarded_marketing_write`); a different token is refused (409, one Persian line, a reload
  button), SQLite's «database is locked» and a lost race to create the row map to the same refusal.
  **Why:** the bot's toggle flips whatever is stored, so a double tap undoes itself; the web's explicit
  target plus the token makes a double-submitted toggle apply once. The token is the whole row, so any other
  change to the settings also stales an open form (safe, never merged).
  **Test:** `test_a_toggle_sent_twice_applies_once_and_never_flips_back`, `test_two_toggles_at_the_same_moment_apply_once`,
  `test_a_form_drawn_before_another_edit_is_refused_and_nothing_is_merged`,
  `test_every_post_route_under_marketing_has_a_guard_case`.

## Change log and undo (`/panel/store/changes`)

- **Rule: the log's text, the undo's confirm sentence with its count of later changes, the undo and
  its own note («برگشت: …») are `services/store_changes.py` (`change_rows` / `change_log_text`,
  `undo_confirm_text`, `apply_undo`); the bot's «تغییرات فروشگاه» handlers and the page call the same
  functions.** Any admin reads the log (as in the bot); an undo control exists only for a store owner
  and only for an entry with a state before it (`ChangeEntry.can_undo`).
  **Why:** the sentence that says «N تغییر بعدی هم برمی‌گرده» lived inside the handler; a copy in a
  template is how the page would stop telling the owner that an undo reverts what came after.
  **Test:** `tests/unit/test_web_panel_store_changes.py::test_the_web_log_lists_the_entries_the_bots_screen_lists`,
  `test_the_confirm_page_states_how_many_later_changes_go_back_and_writes_nothing` (the bot's real
  handlers on a second database).
- **Rule: an undo is two requests on one `POST` route and the first writes nothing.** No `confirm=1`:
  the confirm page (the bot's sentence, `undoes_also`). With `confirm=1` and the same store version:
  `apply_undo`. Both go through `guarded_store_write` (owner, current version, «locked» mapping), so a
  double-submitted confirm applies once and a form drawn before another edit is a 409.
  **Test:** `test_a_web_undo_leaves_the_state_log_and_version_the_bots_handler_leaves` (undoing the undo
  restores the state before it), `test_the_same_confirm_sent_twice_undoes_once`,
  `test_the_undo_needs_login_admin_owner_and_the_current_version`.
- **Rule: an entry is named by its version row id (`ChangeEntry.version_id`), never by its place in the
  list, and an entry the log no longer lists is refused as stale (409, «این تغییر دیگه در لیست نیست»).**
  The id is stable: publishing archives the replaced row in place and only the clone that becomes the next
  draft gets a new id; only the pruning job deletes a row. The page refuses what the bot's forged callback
  would still have tried (an id outside the visible 20, or the oldest state).
  **Why:** a position shifts with every edit; the id of a pruned row names nothing.
  **Test:** `test_an_entry_keeps_its_id_when_later_edits_land`,
  `test_an_entry_pruned_meanwhile_is_refused_as_stale_and_nothing_is_written` (the real pruning job),
  `test_a_position_in_the_list_is_not_an_entry`.
- **Rule: a note is the owner's typing (it quotes a plan or group title); the bot escapes it, Jinja escapes
  it on the log and the confirm page, never `|safe`.** **Test:**
  `test_a_title_with_html_in_a_note_is_escaped_on_the_log_and_the_confirm_page`.
- **Rule: reseller prices key on the plan slug, so an undo that re-mints every row id leaves them
  resolving.** **Test:** `test_a_reseller_price_keyed_on_the_slug_still_resolves_after_an_undo`.
- **Open (owner's decision): a bulk retail price change** (a percent or amount over several plans of a
  group) **does not exist in the bot**, so it is not built here; `services/reseller.py::apply_percent_cut`
  is a different feature (reseller prices). What the owner must decide: which plans (one group, a
  sub-tree, selected plans); percent or amount; the rounding step (suggested: down to the nearest ۱٬۰۰۰
  toman, as `round_percent_cut` does for resellers); whether the 10x warning applies (suggested: yes, ask once as
  `price_jump` does, since a cut of 90 percent or more lands on a tenth); how plans with a typed reseller price are treated (suggested:
  untouched, as retail does not move reseller margin); one change-log note and so one undo (suggested:
  «قیمت N پلن گروه «…» X٪ عوض شد»). Until then nothing is built. Also still bot only: the seed button of
  an empty store.

## Price warning (`PRICE_JUMP_FACTOR`)

- **Rule: a price at least 10 times the plan's current price, or at most a tenth of it,
  is asked about once and then saved; never refused.** One function,
  `services/store_edit.py::price_jump`, raised by `set_package_price` as
  `PriceJumpNeedsConfirm` before anything is written; the default is to ask
  (`confirmed=False`), so a caller cannot forget. No current price, no ask. The panel
  answers with a confirm page that states the old and new price (second request:
  `confirm=1`, same store version); the bot with a confirm step in FSM state
  (`store_waiting_pkg_price_confirm`, `store_price_jump_keyboard`, one `styled()` PRIMARY).
  **Why:** an extra zero in a price field goes live to every customer the moment it
  saves. **Test:** `tests/unit/test_price_jump.py` (both boundaries, no price, the page,
  the bot, a second tap saves nothing more).

## Plan editor (`/panel/store/g/<slug>/plans/…`)

The bot's plan detail and plan wizard, one control per form; every write is a `POST` into a
function of `services/store_plan_edit.py` (the bot's handlers call the same ones).

- **Rule: a typed size, duration, device limit and price is read by
  `read_plan_number` / `read_hwid` / `read_price`, which end in `check_dim`
  (`DIM_LIMITS`); the refusal is the bot's line naming the range, and a ready plan
  carries `volume_gb` + `duration_days` or `concurrent_users` + `duration_months` in
  `options`.** «بدون انقضا» is for a volume plan only; **0 devices is نامحدود and is
  never read as 1** (a blank box is the invalid-input line).
  **Why:** without dimensions `choose_ready_plan` diverts the customer into the builder
  and the fixed price is dropped; `150000` typed as a size became a plan.
  **Test:** `test_a_dimension_out_of_range_is_refused_with_the_bots_range_line`,
  `test_a_ready_plan_made_on_the_web_carries_its_dimensions_in_options`,
  `test_unlimited_devices_stays_zero_on_a_new_and_an_edited_plan`,
  `test_a_blank_device_limit_is_refused_not_read_as_one_or_zero`.
- **Rule: the preview is the customer's button, built by `services/store_catalog.py::shelf_plan_label`,
  the function `ready_plans_keyboard` prints every plan button with, fed with a transient
  in-memory `StorePackage` (never added to a session) and a transient price row.** It is a
  `GET` that writes nothing and makes no draft. Warnings are the bot's: no tab for the duration
  (`no_tab_warning`) and the per-GB ladder as it would be with this plan (`ladder_lines`).
  **Why:** a label re-written for the page is how the owner would approve a button the
  customer never sees. **Test:** `test_the_preview_is_the_label_on_the_customers_real_keyboard`,
  `test_the_preview_follows_what_is_typed_not_what_is_saved` (literal labels),
  `test_the_preview_writes_nothing_and_makes_no_draft`.
- **Rule: a web plan write leaves the state, version and «تغییرات فروشگاه» row the bot's handler
  leaves.** Notes live in `store_plan_edit.py`.
  **Test:** `test_a_web_write_leaves_the_state_and_log_the_bots_handler_leaves` (19 cases:
  create, each edit, delete, order, move, sub-group, group delete, new group).
- **Rule: which plan control shows is `visible_plan_controls`, which group button shows is
  `visible_controls` (`plan_add`, `plan_order`, `subgroup`, `delete`); the bot's keyboards and the
  templates both ask them.** A forged POST for a hidden control is a stale form (409).
  **Test:** `test_a_group_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it`,
  `test_a_plan_control_is_on_the_page_exactly_when_the_bots_keyboard_shows_it`,
  `test_a_hidden_plan_control_is_refused_like_a_stale_form`.
- **Rule: deleting a plan or a group asks first with the bot's sentence (the plan's price, the
  group's plan and child counts); only a second request with `confirm=1` deletes.**
  Nesting (two levels, child groups or plans never both) and «the last top-level group stays» are
  `create_draft_category` / `delete_draft_category`'s, not the template's.
  **Test:** `test_deleting_a_plan_asks_first_with_the_bots_sentence_and_deletes_nothing`,
  `test_deleting_a_group_states_what_goes_with_it_and_deletes_nothing_first`,
  `test_nesting_rules_are_the_services_not_the_templates`.
- **Rule: every POST under `/panel/store` has a guard case.**
  **Test:** `test_every_post_route_under_the_store_has_a_guard_case_here_or_in_the_settings`
  (and the settings file's own enumeration): a route with no case fails the build.
- **Open (plan editor):** the plan's four switches (نمایش به مشتری, فروش جدید, تمدید, تخفیف اضافه)
  are on the page because the bot's plan screen has them; the plan keeps stop-sale of a *group*
  in the bot, so the owner may want the first two off the page. A price per duration / tier cell is
  not in the bot's plan screen (one price, the plan's own duration) and is not on the page.
## The settings screen (`/panel/settings`)

The bot's «⚙️ تنظیمات» has five sections. The web has four of them (marketing is its
own screen): **🗓 هشدار و حذف سرویس** is read and write; **📡 گروه گزارش**, **🔌 اتصال
پنل** and **🎨 پس‌زمینهٔ QR** are read-only.

- **Rule: a lifecycle write goes through
  `services/lifecycle_settings.py::guarded_lifecycle_write`: the store owner only
  (`can_edit`, the check the bot's prompt and mode buttons make; another admin
  reads and gets no control), against the row as the form saw it.** The form
  carries `stamp` (the row's `updated_at`, `"none"` before the first save). A stale,
  missing or lost-race form is refused (409, one Persian line, nothing written), so
  a double-submitted form applies once and a mode switch never flips back. Parsing
  is `parse_field` (the call the bot's prompt makes), the refusals are the bot's
  texts, and the saved line is `saved_line`.
  **Why:** the bot protects a typed value with its FSM state; a web form has none, and
  a phone with a weak connection resends.
  **Test:** `test_a_settings_write_needs_login_admin_owner_and_the_current_stamp`,
  `test_a_settings_form_sent_twice_applies_once`,
  `test_a_mode_toggle_never_flips_back_when_its_form_is_sent_again`,
  `test_two_settings_forms_at_the_same_moment_apply_once`, and
  `test_every_post_route_under_settings_has_a_guard_case` (a POST route with no case
  fails the build).
- **Rule: real deletion («روشن») is refused without a rehearsal reported in the last
  seven days, on the first request and on the confirm one, whoever asks.** The
  precondition is `require_fresh_rehearsal`, called by `set_delete_mode` (the write) and
  `ask_delete_on` (the step before the confirm page); the confirm page writes nothing.
  Changing a deletion number voids the rehearsal in the service, so an open confirm
  page for the old numbers is stale.
  **Why:** `docs/agent-rules/areas/service-lifecycle.md`: a wrong deletion destroys a paying
  customer's service and cannot be undone; the web is one more caller and must not be
  the one that skips the rehearsal.
  **Test:** `test_deletion_cannot_be_switched_on_without_a_fresh_rehearsal`,
  `test_the_confirm_page_writes_nothing_and_only_the_second_request_switches_deletion_on`,
  `test_a_confirm_after_the_numbers_changed_meanwhile_does_not_switch_deletion_on`.
- **Rule: the rehearsal run, the preview, the deletion list and its approval, an
  exemption, and deleting a service have no route and no control on the web.** The
  page shows when the last rehearsal ran and that a list waits, as sentences.
  **Why:** they read the VPN panel or delete from it; the plan keeps panel writes in
  the bot. **Test:** `test_the_settings_pages_offer_no_way_to_delete_a_service_or_run_a_rehearsal`
  (every address a page links to is one of the listed settings routes).
- **Rule: the report group, panel connection and QR background pages are read-only:
  GET only, no form, no input, no button, and looking writes no row** (the bot's
  screen creates the settings row; the page uses `get_readiness(create=False)`).
  **Why:** attaching a group and creating topics are Telegram calls; the panel address
  and key decide where every order goes and testing them is a live panel call; a QR
  upload is a file-handling decision the owner has not made.
  **Test:** `test_the_read_only_sections_have_no_post_route_and_no_form`,
  `test_looking_at_every_settings_page_writes_no_row`.
- **Rule: no settings page renders a secret.** The panel page is built from
  `services/settings_overview.py::load_panel`, which returns two booleans and the
  watcher's stored state; the address (it may carry credentials), the key, a mask of
  it, the sub path and the timeout are not read into the page at all. A new
  secret-looking field in `Settings` must be added to the test's seeded list or the
  build fails.
  **Why:** the page is read on a phone in a chat; a credential in the DOM is a
  credential in a screenshot. **Test:** `test_no_settings_page_renders_a_secret`,
  `test_every_secret_looking_setting_is_seeded_in_the_no_secret_test`.
- **Rule: the words and figures on the lifecycle page are `services/lifecycle_view.py`,
  which the bot's screen also calls** (`current_value_text`, `screen_text`); a template
  prints what it is given.
  **Test:** `test_the_lifecycle_page_shows_the_figures_the_bots_screen_shows`,
  `test_a_web_save_leaves_the_row_the_bots_prompt_leaves` (the bot's real handler on a
  second database; the same row and the same saved line).
- **Rule: a typed number past Python's 4300-digit `int()` limit is the range refusal,
  not a `ValueError`.** `_to_int` in the parsers. **Test:**
  `test_a_beyond_64_bit_number_is_a_refusal_never_a_value_error`.
- **Open (not built on the web):** testing the panel connection (a live call); changing
  the panel address or key, the report group or its topics; uploading or resetting the
  QR background; running the rehearsal and approving the deletion list.
