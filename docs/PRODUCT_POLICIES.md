# Product policies (locked)

Status date: 2026-09-12  
Source of truth for wallet, purchase hardening, named services, trial, and referral.

## Orders & payment

1. **One open order:** At most one order per user in `PENDING_PAYMENT` or `PAYMENT_REVIEW`. New buy / confirm / resume must reuse that order instead of creating another.
2. **FSM:** Cancel / restart / menu buy must not orphan `waiting_for_receipt`. `/start` and buy resume an unpaid open order (re-show payment instructions).
3. **Receipts:** Images only (`photo` or `document` with `image/*`). Reject others with a Persian message. Admin notify uses `send_photo` only for images.
4. **Unpaid timeout:** Stale `PENDING_PAYMENT` → `EXPIRED` (not `REJECTED`), DM the user, clear via no FSM left / handle gracefully; new purchase allowed afterward.
5. **Unique payment per order:** At most one `payments` row per `order_id` (unique index); submit path remains idempotent under races as far as practical.
6. **Approve / provision fail:** On `PROVISION_FAILED`, always DM the customer per wallet policy below. Surface sanitized panel/error detail to admins. Strip stale admin review keyboards when possible.

## Wallet (B2a)

1. Each user has a **wallet balance** (toman) backed by an **immutable ledger** (credit/debit, reason, nullable `order_id`, unique idempotency key).
2. **N=1 provision-fail credit:** On the first `PROVISION_FAILED` after payment is captured, credit `order.price_toman` to the customer wallet **idempotently**. Persian DM: payment OK, activation failed, amount returned to wallet. Admins get sanitized panel error detail.
3. **Admin retry after credit:** Money is already in the wallet. Retry must **debit** wallet for the order price (idempotent) then provision. If debit fails (insufficient balance), tell the admin and do not provision.
4. **Checkout:** At confirm, show wallet balance and apply `min(balance, price)` as wallet credit reducing the card amount. If fully covered by wallet → skip card receipt; go `PAYMENT_APPROVED` → provision with payment method `wallet`.
5. **Customer UX:** Menu «کیف پول من» shows balance (Persian).
6. **Referrer rewards → wallet:** Follow-up. Do not treat referrer `ReferralLog` as spendable wallet credit in this slice. UX must not claim referrer money is paid into wallet until that lands.

## Named services

1. Customer chooses an **ASCII service name** during purchase.
2. Panel username = sanitized name + random suffix (not `neo_{order_id}`). Renew keeps the same panel username.
3. Store `display_name` / `service_name` on `PasarGuardAccount`; account list is multi-account aware and shows names.
4. Before checkout, if a non-trial fingerprint match exists → offer **renew vs new**.
5. **Renew groups:** Only re-stamp / change panel groups when `product_group_ids` map changed since account create (follow-up if not wired in this PR; renew extend keeps existing groups unless product policy requires update).

## Trial & referral discounts

1. **Trial once per person per store group** (2026-09-26). Each group sets its own trial (GB, days) and it runs on that group's panel groups, so the direct trial tests direct and the tunnel trial tests the tunnel. A trial from before groups had trials counts as the direct (unlimited) group's. Paid renew must **not** extend a trial account (distinct trial fingerprint and/or exclude trial accounts from renew match).
2. **Referred discount:** Applies on the referred user's **first successful paid** purchase only (not every order).
3. Referrer percent reward recording may remain as `ReferralLog`; converting that into wallet credit is a follow-up (see Wallet §6).

## Ended services: warnings and deletion (2026-10-03)

1. **Warnings** (owner's numbers on «⚙️ تنظیمات» → «🗓 هشدار و حذف سرویس»): days before expiry (default 3 and 1, the env list only seeds it); volume left (default 20% and 5%: one message per threshold per period, the lowest crossed, never for an unlimited service; added volume or a renewal re-arms). A free trial gets neither: it cannot be renewed. A personal trial ending within a day gets one «buy a plan» message; a reseller's trial none.
2. **Deleting ended services is OFF** until the owner turns it on, and `on` is refused until a rehearsal has reported in the last 7 days. Changing a deletion number while it is on sends it back to the rehearsal. Defaults: 10 days after expiry, 10 days after the bot first saw the volume run out, 1 day for a free trial, warning 3 days before. 0 turns a rule off.
3. **Never deleted:** active, on-hold or disabled services; a service with no expiry; one with an order awaiting payment, in review, approved or provisioning; one created or changed in the last day; one the owner exempted («🛡 حذف نشود»); anything the panel did not answer for.
4. **Warn first, then delete:** a paid service is deleted only after its deletion warning was DELIVERED at least the warning days earlier (and after the current ended spell began); a flood of more than 20 warnings waits for the owner's approval; the first real run over old services only warns. A renewal re-arms the warning. A customer who blocked the bot counts as warned, and the owner's report says «هشدار نرسید».
5. **Hard stop:** more than 20 deletions in one run, or more than 25% of services when over 5, deletes nothing; the list waits for the owner («🗂 فهرست حذف»), who can open each service, exempt it, and approve the rest. A list older than 48 hours or made under other numbers is void.
6. A deleted service leaves the lists; orders, payments and the wallet ledger stay as they were. The customer (and a reseller for their customer) gets one notice.

## Marketing nudges (2026-10-10)

1. **Four messages, all OFF.** On «⚙️ تنظیمات» → «📣 پیام‌های بازاریابی», one switch each: an open order about to expire; a free trial that ended without a purchase; a paid service that ended and was not renewed; and a second wording of the volume warning for a service burning volume faster than time. Only the store owner switches them.
2. **Retail customers only.** Never a reseller, a banned user, or anyone about a service whose orders carry a customer label (a former reseller still owns their walk-in customers' services). The free-trial message needs a personal trial. A message carries a discount only when the owner sets a percent for it (point 7).
3. **Only what happens after switch-on.** Switching a message on stores the moment; it never acts on something earlier. Switching off and on starts a new moment.
4. **Quiet by default:** at most one trial-ended or winback message per customer per 24 hours (the open-order reminder and the volume warning are not counted); at most 30 sent per run, the rest wait; one winback per customer per 90 days; every message is claimed once by a stable key (once per order, once per customer, once per period).
5. **Open order:** the last 15 minutes before it expires, while no receipt was sent; skipped altogether when the expiry is 15 minutes or less. **Trial ended:** 24 hours to 7 days after, only for a customer who never paid; read from orders, so a deleted trial still counts. **Winback:** 5 to 30 days after a paid service ended, only when the customer has no other service still running, and only after the panel says «expired» (a failed read sends nothing).
6. **Volume warning, upgrade wording:** replaces the ordinary warning (same key, never both) when at least 40% of the period's time is left and the service's own store group sells a ready package with more volume; it names the smallest such package, shows no price, and offers the plan switch for that service.

7. **Personal offer (optional, per message).** Under the trial-ended, winback and upgrade messages the owner may set a percent (1 to 50, 0 = none) and how many hours it lasts (1 to 168). With a percent, the message gains one line and the customer gets a discount for ONE purchase, with no code, for that many hours from the send. It follows the same rules as the referral discount: a plan that takes no extra discount refuses it, the global discount cap applies, and where the store takes the better of a code and a referral-style percent, the better one wins (the offer stays unused when a code beats it). It never reaches a reseller. If the customer already has the referral discount, the larger of the two applies, a tie goes to the referral. An offer on an order that expires or is rejected is the customer's again until it runs out; on an order whose activation failed it is not. The upgrade offer applies only to a package with more volume than the service they have now. A customer who blocked the bot loses the offer.
8. **No offer for a customer with an open order.** A customer with an order waiting for payment or for the receipt to be checked is taken back to it at its old price, so a discount could not be used. The trial-ended and winback messages are not sent to them at all that run (they are looked at again on the next one, once the order is paid, rejected or expired). The volume warning is never held back: its upgrade wording goes out without the offer line and no offer is written.
9. **One upgrade offer per service per period.** However many thresholds the period crosses (for example the 20% and the 5% warning), only the first upgrade wording of that (limit, expiry) period carries the offer; the later ones go out without it, even if the first was already used or lapsed. A new period (volume added, renewal) can carry a new one. An offer is live only while its message is out: if the send failed and is retried, the offer starts again from the retry.

## Promo code rules (2026-10-10)

1. **Four optional rules on a code**, set in «کد تخفیف» when the owner adds it: only for the first purchase; uses per customer; total uses; days until it expires. Each question can be skipped, which keeps it unlimited; a code made before this (or from the seed file) has no limits. A «first purchase» is a paid service: a free trial is not one. When a code is first-purchase-only, each customer gets it once, so «uses per customer» is not asked.
2. **Checked in this order, each with its own sentence for the customer:** expired, first purchase only, per-customer limit, total limit. The same check runs at the summary and at the till; if the last use goes while the customer is on the summary, the till says so and shows the summary again at the price without the code.
3. **A use is taken when the order is placed and given back when it dies.** An unpaid order holds its place; an order that expires or is rejected frees it; an order whose activation failed does not (the money was taken). Two customers cannot take the last use: the second is refused.
4. **A code that did not win is not used up.** Where the store takes the better of a code and a referral or personal offer, only the one that was applied is counted; a tie goes to the code. Where the store adds them, both are.
5. **Resellers never meet these rules.** Their agreed price replaces retail, so a code is ignored and nothing is counted.
6. **The list shows each code's rules and «مصرف»**: «مصرف: {used} از {limit}», or only the count when the total is unlimited. Counts belong to the code, so publishing the catalog keeps them. Switching a code off keeps its rules and counts, and a switched-off code is still refused.
7. **A code counts its uses from the moment it was created.** Deleting a code and creating it again (even with the same name and limits) starts from zero, for the total limit, the per-customer limit and the number in the list; editing a code that exists (percent, rules, on/off) keeps its count. A code made before this rule has no creation moment and counts every use, as before.

## Config knobs

- `ORDER_EXPIRY_MINUTES` — unpaid → `EXPIRED`
- `WALLET_PROVISION_FAIL_CREDIT_MAX_N` — default `1` (N=1 credit on provision fail)
- Existing referral / trial / pricing settings unchanged unless noted

