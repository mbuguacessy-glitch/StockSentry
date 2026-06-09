# StockSentry: AI Warehouse Stock Reconciliation

A real-time warehouse stock reconciliation system that replaces manual paper-based shift records with a digital audit trail. Built for manufacturing facilities handling multiple shifts, multiple warehouses, and high-volume daily stock movements.

---

## The Problem It Solves

At a large manufacturing facility like Kenya Breweries, stock reconciliation is done manually:

- Clerks record stock counts on paper forms every shift
- Small variances of 1-2 crates are ignored daily — "acceptable loss"
- Those small losses accumulate silently across shifts and warehouses
- Monthly reconciliation takes 2 full days of a manager's time
- By the time a discrepancy is investigated, the trail is cold
- Nobody knows which shift, which clerk, or which truck caused the loss

At scale, a daily 2-crate loss across multiple warehouses and shifts adds up to millions of shillings per month in untracked losses.

---

## How StockSentry Fixes This

Clerk opens shift on phone or computer
↓
Records opening stock count per brand
↓
Records every sales order as it is dispatched

Order number, delivery number, client, truck
Checker name and forklift operator
Ordered quantity vs dispatched quantity
Any variance flagged immediately
↓
Records any interwarehouse stock movements
Sending warehouse logs quantity sent
Receiving warehouse confirms quantity received
Any transit variance flagged immediately
↓
Clerk closes shift with closing stock count
↓
System calculates automatically:
Opening + Movements In - Sales - Movements Out = Expected Closing
Expected vs Actual = Variance
↓
Claude writes a plain language shift summary
Supervisor receives Slack notification with full details
Security notified if variance detected
↓
Supervisor reviews and signs off digitally
Every decision logged permanently
↓
Monthly report generated in one click

---

## Key Features

- **Zero-threshold variance detection** — even 1 unit triggers a flag. No more "acceptable loss"
- **Full chain of custody** — every order records the checker and forklift operator responsible
- **Interwarehouse tracking** — stock movements between warehouses matched and verified
- **Immediate escalation** — variances trigger instant Slack alerts to supervisor and security
- **AI shift summary** — Claude writes a plain language summary so supervisors know exactly what needs attention
- **Digital supervisor sign off** — every shift approved or flagged with name and timestamp
- **One-click monthly report** — replaces 2 days of manual reconciliation
- **Full audit trail** — every count, every sale, every movement, every decision logged permanently in PostgreSQL

---

## Decision Rights — Human vs System

| Decision | Who decides |
|----------|-------------|
| Calculate expected closing stock | System automatically |
| Flag any variance | System automatically |
| Notify supervisor of shift summary | System automatically |
| Notify security of variance | System automatically |
| Investigate a variance | Human — supervisor |
| Sign off on a shift | Human — supervisor only |
| Escalate an order | Human — clerk initiates |
| Recall a truck | Human — supervisor and security |
| Approve monthly report | Human — management |

---

## Tech Stack

| Component | Tool | Role |
|-----------|------|------|
| Backend API | FastAPI | All endpoints and business logic |
| Database | PostgreSQL | Permanent audit trail |
| ORM | SQLAlchemy | Database interaction |
| AI analyst | Claude API | Plain language shift summaries |
| Notifications | Slack API | Supervisor and security alerts |
| Brand config | brands.json | Warehouse, brand, and shift definitions |

---

## Database Tables

**shift_records** — every shift with opening stock, closing stock, sales, variances, and supervisor sign off

**sales_orders** — every order dispatched with order number, delivery number, client, truck, checker, forklift operator, and variance

**interwarehouse_movements** — every stock movement between warehouses with sent, received, and variance

**stocksentry_audit** — every system action with who decided it and the outcome

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/shifts/open` | Clerk opens shift and records opening stock |
| POST | `/shifts/close` | Clerk closes shift — triggers variance calculation |
| GET | `/shifts/{id}` | Get full shift details with variances |
| GET | `/shifts` | List all shifts |
| POST | `/orders/record` | Record a sales order dispatched |
| POST | `/orders/escalate` | Escalate a flagged order to security |
| GET | `/orders` | List all orders |
| POST | `/movements/send` | Record stock leaving a warehouse |
| POST | `/movements/receive` | Receiving warehouse confirms stock |
| GET | `/movements` | List all interwarehouse movements |
| POST | `/shifts/signoff` | Supervisor signs off on a shift |
| GET | `/report/monthly` | Generate monthly reconciliation report |
| GET | `/audit` | Full audit log |
| GET | `/health` | Health check |

---

## Real Test Results

**Shift variance detected:**
- Dark Stout 300ml — Opening: 120 | Sold: 26 | Expected: 94 | Actual: 92 | **Variance: -2**
- System flagged immediately, supervisor notified via Slack

**Order variance detected:**
-Alpha Lager 500ml — Ordered: 50 | Dispatched: 48 | **Variance: 2**
- Checker and forklift operator logged, escalated to security

**Transit variance detected:**
- Alpha Lager 500ml movement — Sent: 100 | Received: 98 | **Variance: -2**
- Both warehouses notified immediately

**Monthly report:**
- Generated in under 1 second
- Previously took 2 full days manually

---

## Measurable Outcomes

| Metric | Before StockSentry | After StockSentry |
|--------|-------------------|-------------------|
| Variance detection time | Monthly (too late) | Real-time per shift |
| Minimum detectable variance | 5-10 crates (ignored) | 1 unit |
| Monthly reconciliation time | 2 full days | Under 1 second |
| Audit trail | Paper books | Permanent PostgreSQL |
| Accountability | Unclear | Full chain of custody |
| Small daily losses | Ignored and accumulated | Flagged and investigated |

---

## Screenshots

### API Documentation
![API Docs](https://i.imgur.com/0VHe1YN.png)

### Shift Variance Detection
![Shift Variance](https://i.imgur.com/9Yq2xhX.png)

### Order Escalation
![Order Escalation](https://i.imgur.com/OnIjq5r.png)

### Monthly Report
![Monthly Report 1](https://i.imgur.com/eVRDxw8.png)
![Monthly Report 2](https://i.imgur.com/RbLRqlV.png)

### Audit Log
![Audit Log](https://i.imgur.com/4j8rild.png)


## Environment Variables

DATABASE_URL=postgresql://user:password@localhost:5432/automation_lab
ANTHROPIC_API_KEY=your_anthropic_api_key
SLACK_BOT_TOKEN=your_slack_bot_token

## How to Run

```bash
pip install -r requirements.txt
python stocksentry_api.py
```

Then open `http://localhost:8010/docs` to test all endpoints.