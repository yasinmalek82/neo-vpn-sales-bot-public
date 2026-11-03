---
paths:
  - "services/disruption.py"
  - "bot/handlers/disruption.py"
  - "bot/keyboards/disruption.py"
  - "database/models/disruption.py"
---

# Disruption reports

«📡 گزارش اختلال» under support: ISP button, then a city button or a typed
city, stored in `disruption_reports` and sent to the «📡 اختلال» topic. The
nightly digest ranks ISPs by count with each one's top city.

- **The ISP is a fixed key (`services/disruption.py::ISPS`), never free
  text.** The owner fixes the worst ISP first.
- **A typed city is cleaned** (Arabic ي/ك, spacing) so one city is counted
  once.
- **One report per person per 30 minutes**: repeated taps must not outvote an
  ISP.
- A group connected before this topic existed gets it from the report group's
  «🔗 تغییر گروه» with the same chat id (setup adopts existing topics and
  creates missing ones); until then its reports land in General.
