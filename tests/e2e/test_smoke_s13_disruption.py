"""Section 13: the owner announces an outage and the customers hear about it."""

from __future__ import annotations

import re

import pytest

from tests.e2e._s07_helpers import press_on_last
from tests.e2e.harness import App, smoke

SERVER = "فنلاند"


async def _announce_outage(app: App, admin) -> None:
    """/admin → «✉️ پیام همگانی» → «📢 اعلان قطعی» → «📢 اعلام قطعی» → server → «🕐 همین الان»."""
    await admin.send("/admin")
    await admin.tap("📢 پیام همگانی")
    await admin.tap("📢 اعلان قطعی")
    await admin.tap("📢 اعلام قطعی")
    await admin.send(SERVER)
    await admin.tap("🕐 همین الان")
    await admin.tap("⏭ رد کن")  # no replacement to suggest
    await press_on_last(admin, "همهٔ مشتری")
    await admin.tap("📤 بله، ارسال کن")


@pytest.fixture
async def people(app: App):
    ali = app.customer("ali")
    await ali.start()
    bob = app.customer("bob")
    await bob.start()
    return ali, bob


@smoke("۱۳.۱")
async def test_the_outage_notice_reaches_every_customer_once_and_the_owner_sees_it_through(
    app: App, people
) -> None:
    ali, bob = people
    admin = app.admin()
    marks = {p.name: p.mark() for p in people}
    await _announce_outage(app, admin)
    assert all(
        not p.texts_since(marks[p.name]) for p in people
    ), "nothing leaves before the worker runs"
    await app.run_job("broadcast")
    for person in people:
        got = person.texts_since(marks[person.name])
        assert len(got) == 1 and SERVER in got[0], (person.name, got)
    assert admin.texts_since(0)[-1].startswith("📊"), "the owner is shown the send progress"
    await admin.tap("🔄 به‌روزرسانی")
    assert "رسیده: 2" in admin.last_text and "باقی‌مانده: 0" in admin.last_text, admin.last_text
    # A second worker pass sends nothing again.
    again = {p.name: p.mark() for p in people}
    await app.run_job("broadcast")
    assert all(not p.texts_since(again[p.name]) for p in people)


@smoke("۱۳.۲")
async def test_the_notice_reads_as_whole_persian_and_claims_nothing_it_cannot_prove(
    app: App, people
) -> None:
    ali, _ = people
    admin = app.admin()
    mark = ali.mark()
    await _announce_outage(app, admin)
    await app.run_job("broadcast")
    (notice,) = ali.texts_since(mark)
    body, _, signature = notice.partition("─────")
    assert SERVER in body and "قطعه" in body
    assert not re.search(r"[A-Za-z]", body), f"Latin text in the notice body: {body!r}"
    assert "تومان" not in notice and "٪" not in notice and "%" not in notice, "a price claim"
    # The only digits are the start time the owner chose («همین الان»).
    assert re.findall(r"[0-90-9]+", re.sub(r"[0-9]{2}:[0-9]{2}", "", notice)) == [], notice
    assert "شما" not in notice, "BRAND.md: «تو», never «شما»"
    assert signature.strip().startswith("نئو نتورک"), "a forwarded notice keeps its source"
    assert "�" not in notice and "<" not in notice and "&" not in notice
