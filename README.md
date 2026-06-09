# StockSentry: A Warehouse Stock Reconciliation System

An AI-powered stock reconciliation API that replaces manual paper-based warehouse tracking. Built for beverage distribution warehouses handling multiple brands, shifts, and inter-warehouse movements.

## The Problem It Solves

In a typical beverage warehouse, clerks record stock on paper forms. Discrepancies of 1-2 crates are ignored. By the monthly stock take, unresolved variances have accumulated into significant losses, sometimes millions, and take a full day to reconcile manually.

StockSentry catches every variance instantly, at the point it happens, with a full audit trail.

## Features

- **Shift management:** clerks open and close shifts with opening and closing stock counts
- **Sales order recording:** every order logged with order number, delivery number, truck, checker, and forklift operator
- **Automatic variance detection:** system calculates expected vs actual closing stock per brand
- **AI-generated shift summaries:** Claude writes a plain-language summary for the supervisor at shift close
- **Supervisor sign-off workflow:** supervisor approves or flags each shift with notes
- **Inter-warehouse movement tracking:** stock transfers between warehouses tracked and reconciled
- **Slack notifications:** supervisor and security team notified instantly on any variance
- **Full audit trail:** every action logged with who, when, and outcome
- **Monthly report:** replaces the 2-day manual monthly stock take

## Tech Stack

- Python
- FastAPI
- PostgreSQL
- SQLAlchemy
- Claude API (Anthropic)
- Slack API

## Project Structure

stocksentry/
├── stocksentry_models.py   # Database models
├── stocksentry_api.py      # FastAPI endpoints
├── brands.json             # Warehouse and brand configuration
├── requirements.txt        # Dependencies
└── .env                    # Environment variables (not committed)

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /shifts/open | Clerk opens a shift with opening stock |
| POST | /shifts/close | Clerk closes shift — triggers variance calculation |
| POST | /orders/record | Record a dispatched sales order |
| POST | /orders/escalate | Escalate a flagged order to security |
| POST | /movements/send | Record stock leaving a warehouse |
| POST | /movements/receive | Receiving warehouse confirms stock receipt |
| POST | /shifts/signoff | Supervisor approves or flags a shift |
| GET | /shifts/{id} | Get full shift details and variances |
| GET | /shifts | List all shifts |
| GET | /orders | List all orders |
| GET | /movements | List all inter-warehouse movements |
| GET | /audit | View full audit log |
| GET | /report/monthly | Generate monthly reconciliation report |
| GET | /health | Health check |

## Screenshots

### Server running
![Server startup](https://i.imgur.com/cKLlqC7.png)

### All API endpoints
![Swagger UI](https://i.imgur.com/rNNcxFK.png)

### Variance detected and shift flagged
![Variance response 1](https://i.imgur.com/w2BPWlc.png)
![Variance response 2](https://i.imgur.com/X6qONCV.png)

## Setup

1. Clone the repository
2. Create a virtual environment and install dependencies
3. Set up PostgreSQL and update your `.env` file
4. Run the API

```bash
uvicorn stocksentry_api:app --reload --port 8010
```