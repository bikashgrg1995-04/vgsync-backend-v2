# VGSync Backend v2

Backend API for VGSync, a multi-tenant business management platform built with Django REST Framework and PostgreSQL.

## Project status

This repository contains the **foundation layer** (tenancy, authentication, permissions), the **owner's team and payroll tools**, and the start of the inventory module. The remaining business modules are not implemented yet.

| Area | Status |
| --- | --- |
| Companies, Head Office / Branch / Independent businesses | Models and admin |
| Users, roles, Positions, module permissions | Models, admin, and enforcement |
| JWT login, token refresh, current user, change password | API |
| Owner account setup with forced password change at first login | Django Admin and API |
| Team management: staff, roles, Positions and module permissions (owner only) | API |
| Payroll: monthly and daily pay, payments, advances, remaining and carried-over balances (owner only) | API |
| Expenses, with salary payments added automatically (owner only) | API |
| Inventory | Foundation in progress (`apps/inventory`) |
| Purchases: suppliers, purchases with server-calculated totals, receiving stock, supplier payments, purchase returns and supplier credit (needs the `purchases` permission) | API |
| Sales: stock, bike (full payment or EMI) and maintenance sales, customers, confirming (stock out, invoice number), customer payments, credit to receive, sale returns (stock and parts) and customer credit to pay, follow-ups and income (needs the `sales` permission) | API |
| Company / business management APIs | Not yet implemented |
| Orders, dashboard, reminders, analytics, ledger and reports | Not yet implemented (the permission system already defines most of these modules) |

## Tech stack

- Python 3.12+, Django 6.1
- Django REST Framework
- PostgreSQL (via `psycopg` 3)
- Simple JWT (`djangorestframework-simplejwt`)
- `django-filter`, `django-cors-headers`, `drf-spectacular`
- `python-dotenv` for configuration

## Project structure

```
config/              Django project settings, URLs, WSGI/ASGI
apps/
  accounts/          User, Position, PositionPermission, JWT auth, and the owner's staff and Position APIs
  companies/         Company model
  businesses/        Business model (Head Office, Branch, Independent)
  common/            JWT authentication class, permissions, throttling, pagination, shared errors
  inventory/         Inventory foundation (products, stock, transfers)
  payroll/           Pay setup, entries, payments and balances
  purchases/         Suppliers, purchases, receiving stock and supplier payments
  sales/             Customers, bikes, sales, EMI, follow-ups, returns and customer payments
  finance/           Expenses and income (salary, supplier and customer payments are added automatically)
  support/           Help & Support: FAQs and support tickets
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Create a PostgreSQL database and a `.env` file in the project root (see below).
4. Run migrations:
   ```
   python manage.py migrate
   ```
5. Create a Super Admin (used for Django Admin only):
   ```
   python manage.py createsuperuser
   ```
6. Start the server:
   ```
   python manage.py runserver
   ```

### Environment variables

Read from `.env` in the project root.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `SECRET_KEY` | Yes | none | Django secret key |
| `DEBUG` | No | `False` | Set to `true` to enable debug mode |
| `ALLOWED_HOSTS` | No | empty | Comma-separated host names |
| `DB_NAME` | Yes | none | PostgreSQL database name |
| `DB_USER` | Yes | none | PostgreSQL user |
| `DB_PASSWORD` | Yes | none | PostgreSQL password |
| `DB_HOST` | No | `localhost` | PostgreSQL host |
| `DB_PORT` | No | `5432` | PostgreSQL port |
| `CORS_ALLOWED_ORIGINS` | No | empty | Comma-separated frontend origins allowed to call the API from a browser |
| `REDIS_URL` | Production | empty | Redis URL for the shared cache, for example `redis://localhost:6379/1` (`rediss://` for TLS). Empty uses a per-process in-memory cache |
| `NUM_PROXIES` | No | `0` | Number of reverse proxies in front of the app (see [Rate limiting](#rate-limiting)) |
| `SUPPORT_EMAIL`, `SUPPORT_PHONE`, `SUPPORT_HOURS`, `SUPPORT_REPLY_TIME` | No | VGSync defaults | Contact details on the Help & Support page (see [Help & Support API](#help--support-api)) |
| `SUPPORT_USER_GUIDE_URL`, `SUPPORT_VIDEO_TUTORIALS_URL` | No | empty | Links for the user guide and video tutorials. The page hides the button while one is empty |

Example:

```
SECRET_KEY=change-me
DEBUG=true
ALLOWED_HOSTS=localhost,127.0.0.1
DB_NAME=vgsync
DB_USER=postgres
DB_PASSWORD=postgres
DB_HOST=localhost
DB_PORT=5432
```

## Tenancy model

- **Company**: a tenant that owns a Head Office and any number of Branches.
- **Business**: one of three types:
  - `head_office`: belongs to a company, has no parent.
  - `branch`: belongs to a company, its parent must be that company's Head Office.
  - `independent`: belongs to no company and has no parent.
- Every business has a unique `business_code`, used at login.
- Usernames are unique **within a tenant**, not globally:
  - Company users and users of that company's branches share one username namespace.
  - Each independent business has its own namespace.
  - Super Admin usernames are globally unique.

## Roles and access

Roles: `super_admin`, `owner`, `admin`, `accountant`, `staff`.

- **Super Admin** belongs to no company or business and can only use Django Admin (`/admin/`). Client API requests with a Super Admin token are rejected.
- Every other user is assigned to exactly one company **or** one business.
- **Company owner, admin, accountant**: full access to the company's Head Office, and **read-only** access to its Branches.
- **Company staff**: Head Office only, with permissions from their Position.
- **Independent business owner, admin, accountant**: full access to their business.
- **Independent business staff**: access to their business, with permissions from their Position.

### Positions and module permissions

Only `staff` users have a Position. A Position belongs to a business (a company's Head Office for company staff) and holds per-module flags: `can_view`, `can_create`, `can_update`, `can_delete`.

Modules: `inventory`, `purchases`, `sales`, `bike_sales`, `orders`, `staff`, `finance`, `dashboard`, `reminders`.

Views protect themselves by setting `permission_module` and using `HasModulePermission`:

| HTTP method | Permission checked |
| --- | --- |
| `GET`, `HEAD` | `can_view` |
| `POST` | `can_create` |
| `PUT`, `PATCH` | `can_update` |
| `DELETE` | `can_delete` |

Views without a `permission_module` are denied. `HasActiveBusinessContext` is available for views that only need a valid active business.

## Authentication API

Base path: `/api/v1/auth/`

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| `POST` | `/login/` | None | Log in with business code, username, and password |
| `POST` | `/token/refresh/` | None | Exchange a refresh token for a new access token and a new refresh token |
| `POST` | `/logout/` | None | Revoke a refresh token |
| `GET` | `/me/` | Bearer | Current user and active business |
| `PATCH` | `/me/` | Bearer | Change your own first name, last name, email and phone (role and username cannot be changed) |
| `POST` | `/change-password/` | Bearer | Change password and sign out everywhere |
| `POST` | `/forgot-password/` | None | Email a one-time reset link (`business_code`, `username`) |
| `POST` | `/reset-password/` | None | Set a new password from the link (`uid`, `token`, `new_password`, `confirm_password`) |

### Forgot and reset password

`/forgot-password/` always returns the same `200` message, whether or not the account exists or has an email, so it cannot be used to find usernames. When the account has an email, the mail contains `FRONTEND_URL/reset-password?uid=...&token=...`. The link is valid for `PASSWORD_RESET_TIMEOUT` seconds (default 3600) and works once. Resetting clears `must_change_password` and signs the user out everywhere. With `EMAIL_HOST` empty, emails are printed to the server console (development).

### Login

Request:

```json
{
  "business_code": "TEST-HO",
  "username": "owner1",
  "password": "your-password"
}
```

The response contains `refresh`, `access`, `user`, and `active_business` (including `access_mode`, either `full` or `read_only`).

The chosen business becomes the **active business context**, stored in the token as `active_business_id`. Use the access token on later requests:

```
Authorization: Bearer <access>
```

### Token behavior

- Access token lifetime: 15 minutes. Refresh token lifetime: 7 days.
- On every request the active business, its company, the user's assignment, and (for staff) the Position are re-validated against the database.
- Refreshing a token re-runs the same checks, so deactivated users, businesses, companies, or Positions can no longer refresh.
- Login failures for an unknown business code, unknown user, or wrong password return the same generic message.
- All of these checks (tenant, role, active business, Position) live in one function, `validate_user_access` in `apps/accounts/access.py`, which login, refresh, `/me/`, and the authentication class all use. Change access rules there.

### Refresh token rotation and logout

- Every call to `/token/refresh/` returns a **new** `refresh` token along with the new `access` token, and the old refresh token is blacklisted. Clients must store the new `refresh` from each response. Reusing an old one returns `401`.
- `POST /logout/` with `{"refresh": "<token>"}` revokes that refresh token. It needs no access token, so a user with an expired access token can still sign out. Logging out twice is not an error.
- Access tokens already issued stay valid until they expire (15 minutes).

### Change password

Request body: `current_password`, `new_password`, `confirm_password`. The new password is checked against Django's password validators.

Changing the password revokes every refresh token the user has, so all their sessions must log in again.

### Forced password change

When a user's `must_change_password` flag is set (an admin can set it on a user in Django Admin, for example after resetting their password), they can still log in, but every API call except `GET /me/` and `POST /change-password/` returns `403` with:

```json
{
  "detail": "You must change your password before continuing.",
  "code": "password_change_required"
}
```

Clients should use the `code` to send the user to a change-password screen. The flag is cleared when the password is changed. The block is applied in the authentication class, so new endpoints are protected by default.

### Rate limiting

Auth endpoints are rate limited. Exceeding a limit returns `429` with a `Retry-After` header.

| Endpoint | Limit | Counted per |
| --- | --- | --- |
| `/login/` | 30 per minute | IP address |
| `/login/` | 5 per minute | business code + username (any IP) |
| `/token/refresh/`, `/logout/` | 60 per minute | IP address |
| `/change-password/` | 10 per hour | user |
| `/forgot-password/` | 10 per hour | IP address |
| `/forgot-password/` | 3 per hour | business code + username |
| `/reset-password/` | 20 per hour | IP address |

Rates are set in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]` in `config/settings.py`. Successful logins count toward the limits too.

Two things to configure for production:

- **Shared cache.** Counters are stored in Django's cache, which is per-process by default, so each worker would count separately. Set `REDIS_URL` so every worker shares one counter. Without it the app falls back to the in-memory cache, which is fine for development and tests.
- **`NUM_PROXIES`.** With the default `0`, the client IP is the socket address and `X-Forwarded-For` is ignored, because clients can spoof it to dodge the limit. Behind one reverse proxy such as nginx, set `NUM_PROXIES=1`, otherwise every request appears to come from the proxy's IP and clients share one limit.

## Owner setup and first login

1. A Super Admin creates the owner in Django Admin (`/admin/` > Users > Add user): set the username and a temporary password, choose role **Owner**, and pick either the **company** (for a company owner, who signs in with the Head Office's business code) or the **business** (for a Branch or Independent business owner).
2. **Must change password** is ticked by default on new users created there. Leave it ticked.
3. Give the owner three things: the **business code**, the **username**, and the **temporary password**.
4. At first login the owner is signed in but every API except `GET /me/` and `POST /change-password/` returns `403` with `code: "password_change_required"` (see [Forced password change](#forced-password-change)). The app should show the change-password screen.
5. After changing the password all sessions are revoked, so the app signs the owner in again with the new password.

Owners cannot be created, edited or deleted through the API. Only Django Admin does that.

## Company and business API (read only)

Companies, Head Offices, Branches and independent businesses are created, edited, switched off and switched on **only by a super admin in Django Admin**. Owners can read them and nothing else. This is how access follows registration and payment: when a business has not registered or paid, the super admin sets `is_active` off on the Company or Business, and its users can no longer sign in, refresh a token or switch to it. Switching it back on restores access, and no data is deleted.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/company/` | The owner's company (`409 no_company` for an independent business) |
| `GET` | `/businesses/` | List the Head Office and Branches (filters: `business_type`, `is_active`, `search`) |
| `GET` | `/businesses/{id}/` | One business |
| `GET` | `/businesses/branches/` | Branch list used by the business switcher |

- Writes to `/company/` and `/businesses/` return `405`.
- A company owner sees the whole company; an independent owner sees only their own business.
- To add a branch: Django Admin, Businesses, add one with type `branch`, the company, the Head Office as parent and a unique code.

## Team management API (owner only)

Everything below is limited to the **owner** of the active business, and works on the team of that business:

- A company owner signed in to the **Head Office** manages the company's users, with Positions that belong to the Head Office.
- A **Branch** or **Independent business** owner manages the users of that business.
- A company owner signed in to a **Branch** is read-only (`403` on writes), like everywhere else.

Admins, accountants and staff get `403`. Records of another company or business return `404`.

### Staff

**Who can use the team and Positions APIs.** The owner can do everything. Staff whose Position grants the `staff` module (the "Administration" position) can manage other **staff** accounts with the matching View/Create/Update/Delete right, and can read Positions. They cannot: manage themselves, create or change Positions, manage admin/accountant accounts, or assign or manage anyone with a Position that grants more than their own. Admin and accountant roles get no team access. `/auth/me/` and the login response now include `permissions` and `can_manage_team` so screens can hide what a person cannot use.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/staff/` | List the team. Filters: `role`, `is_active`, `position`. Search: `search=`. Paginated |
| `POST` | `/staff/` | Add a team member |
| `GET`, `PATCH` | `/staff/{id}/` | View, or edit name, email, phone, role and Position |
| `DELETE` | `/staff/{id}/` | **Deactivate** (not erase): they cannot sign in and their sessions end. History stays |
| `POST` | `/staff/{id}/reactivate/` | Bring a deactivated person back |
| `POST` | `/staff/{id}/reset-password/` | Set a new temporary password; the person must change it at next login |

Adding a person:

```json
{
  "username": "ram",
  "first_name": "Ram",
  "last_name": "Thapa",
  "role": "staff",
  "position": 3
}
```

- `role` is `staff`, `accountant` or `admin`. Staff must have an active `position`; the others must not.
- `password` is optional. If left out, the server generates a random one and returns it **once** as `temporary_password`. Either way the person must change it at first login.
- Usernames are unique within the company (Head Office and its Branches together), ignoring case.
- The username cannot be changed later.

### Positions

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/positions/` | List or create Positions with their module permissions |
| `GET`, `PATCH`, `DELETE` | `/positions/{id}/` | View, edit, or delete a Position |
| `GET` | `/positions/modules/` | The modules a Position can grant access to |

```json
{
  "name": "Cashier",
  "permissions": [
    {"module": "sales", "can_view": true, "can_create": true},
    {"module": "inventory", "can_view": true}
  ]
}
```

- Modules not listed in an update keep their current permissions, and modules listed are set to exactly what is sent.
- `can_create`, `can_update` or `can_delete` require `can_view`.
- A Position still used by staff cannot be deleted (`409`, `position_in_use`) or deactivated; move the staff first.

## Payroll API (owner only)

Same scope rules as the team management API. Money is sent and returned as strings such as `"30000.00"`.

Each person's pay is a running account. The business records what it **owes** (entries) and what it **paid** (payments); what is still owed is always worked out from the two, never typed in.

**Pay types**

- **Monthly**: a fixed `rate` (plus `allowances`) falls due on the person's `salary_day` every month, from `start_date`. The system records each month's salary by itself when the day arrives; nothing needs to be run. Changing the rate affects future months only.
- **Daily**: no fixed rate. The owner enters the amount earned for each day (a `wage` entry).

**How "remaining" works**

Everything earned (salary, wages, bonuses) is listed oldest first. Payments, plus deductions (money the business keeps back), are poured over it from the oldest amount to the newest. What stays unsettled is `remaining`. Unpaid amounts of earlier periods are never lost: they are shown as `previous_remaining` (the period starts on the latest salary day for monthly pay, and today for daily pay) and listed one by one in `unsettled` with `carried_over: true`. Paying more than is owed, or paying before the salary day, leaves an `advance`, which the next amount due uses up.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/payroll/salaries/` | How the team is paid (filter: `pay_type`) |
| `GET`, `PUT` | `/payroll/salaries/{staff_id}/` | View, or set or change `pay_type`, `rate`, `allowances`, `salary_day`, `start_date` |
| `GET` | `/payroll/balances/` | Everyone's account, most owed first. Filters: `owing=true`, `advance=true`, `pay_type`, `search` |
| `GET` | `/payroll/balances/{staff_id}/` | One person's account with each unsettled amount and recent payments |
| `GET` | `/payroll/balances/{staff_id}/statement/` | Statement: every amount earned, deducted and paid, newest first, each with what was still owed (`owed_after`) or paid ahead (`advance_after`) right after it. Filters: `date_from`, `date_to` |
| `GET` | `/payroll/me/` | **Any signed-in person:** their own account (the salary tracker) |
| `GET` | `/payroll/me/statement/` | **Any signed-in person:** their own statement |
| `GET`, `POST` | `/payroll/entries/` | List (filters: `kind`, `staff`, `status`, `entry_date__gte`) or add a `wage`, `bonus` or `deduction` |
| `PATCH` | `/payroll/entries/{id}/` | Change a wage, bonus or deduction |
| `POST` | `/payroll/entries/{id}/void/` | Void an entry, with a reason |
| `GET`, `POST` | `/payroll/payments/` | List or record a payment (full, part or advance) |
| `POST` | `/payroll/payments/{id}/void/` | Void a payment, with a reason |

A payment line in a statement carries its `date`, `amount`, `method`, `reference` and `advance_part` (the part paid before anything was owed), so an advance shows how much was paid, when, and what is left. Recording a payment also returns `balance_after` (`remaining` and `advance`).

The two `/payroll/me/` endpoints are open to every signed-in person with pay set up, whatever their role or Position, and only ever return that person's own figures (`404`, `no_salary` when pay is not set up, as for owners). Everything else under `/payroll/` stays owner only.

A balance contains `total_earned`, `total_deducted`, `total_paid`, `remaining`, `previous_remaining`, `current_remaining`, `advance`, `cycle_start`, `next_salary_date` and `last_payment_date`.

Typical use:

1. Set up each person once: monthly `{"pay_type": "monthly", "rate": "30000.00", "allowances": "2000.00", "salary_day": 5}`, or daily `{"pay_type": "daily"}`.
2. Daily staff: each day `POST /payroll/entries/` with `{"staff": 7, "kind": "wage", "entry_date": "2026-01-10", "amount": "850.00"}` (one wage per person per day; edit it to change the amount).
3. Pay at any time with `POST /payroll/payments/` and `{"staff": 7, "amount": "12000.00", "method": "cash"}`. `method` is `cash`, `bank` or `online`; `paid_on` defaults to today and cannot be in the future.
4. Check `GET /payroll/balances/?owing=true` to see who is still owed money.

Rules:

- Monthly salary entries are created by the system and cannot be edited; void one and add a bonus or deduction to correct it.
- Payments are never edited or deleted. A mistake is voided with a reason, which also voids its expense and puts the amount back on the balance.
- Switching a person from monthly to daily (or back) starts the new type from today. Salary that was already due is recorded first.
- Deactivating someone first records salary due up to today; time spent inactive earns no monthly salary.
- Hourly pay is not supported.

## Purchases API

Buying stock from suppliers, for the active business. It needs the `purchases` module permission (owner, admin and accountant have it by role; staff need it on their Position). Writes need full access to the business, so a company owner is read-only inside a branch. Money is sent and returned as strings.

**Lifecycle**

```
draft  --receive-->  received          (stock goes up, once; the purchase is locked)
draft  --cancel--->  cancelled         (no effect on stock)
```

A draft can be edited freely and has no effect on stock. `receive` adds every line to the business's stock through the inventory stock service (one `purchase_receipt` movement per line, referencing `PUR-000001`), all or nothing, and only once. A received purchase cannot be edited or cancelled; corrections are done with a purchase return (below).

**Totals** are always calculated by the server from the lines, in this order (a total sent by a client is ignored):

```
line total = quantity x unit price
subtotal   = sum of line totals
taxable    = subtotal - discount_amount
vat        = taxable x vat_rate / 100        (vat_rate defaults to 0)
net total  = taxable + vat + freight_amount  (freight is not taxed)
```

Amounts are rounded to the paisa. The discount cannot exceed the subtotal.

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/purchases/` | List (filters: `status`, `supplier`, `payment_status`, `purchase_date__gte`, `purchase_date__lte`, `search`) or create a draft |
| `GET`, `PATCH` | `/purchases/{id}/` | View, or edit a draft. Sending `items` replaces all lines |
| `POST` | `/purchases/{id}/receive/` | Goods arrived: add them to stock, all at once or in parts (see below) |
| `POST` | `/purchases/{id}/cancel/` | Cancel a draft, with a `reason` |
| `GET`, `POST` | `/purchases/payments/` | List or record a supplier payment (full or part, also before the goods arrive) |
| `POST` | `/purchases/payments/{id}/void/` | Void a payment, with a `reason` |

**Receiving in parts.** `POST /purchases/{id}/receive/` with no body receives everything still to come. For a part delivery send `{"items": [{"purchase_item": 12, "quantity": "4"}], "received_on": "2026-11-03", "notes": "First truck"}`. Each call is one delivery, kept under `receipts` on the purchase, and adds stock once. While some quantity is still to come the purchase is `partial`: it can no longer be edited or cancelled, but it can be paid and returned. When every line has fully arrived it becomes `received`. Each line shows `received_quantity` and `remaining_quantity`. A delivery that goes over what is still to come is refused as a whole (`400`).

A `partial` purchase is already billed, so it counts in the supplier's `outstanding` and in the payables report. Quantity not yet delivered shows as `stock_pending_delivery` on the supplier. Only goods that have arrived can be returned (`returnable_quantity`).

Create a purchase:

```json
{
  "supplier": 3,
  "bill_no": "B-1042",
  "purchase_date": "2026-11-02",
  "discount_amount": "151.50",
  "vat_rate": "13",
  "freight_amount": "200.00",
  "items": [
    {"product": 12, "quantity": "10", "unit_price": "100.00"},
    {"product": 15, "quantity": "3", "unit_price": "250.50"}
  ]
}
```

Each purchase also returns `paid_amount`, `remaining_amount` and `payment_status` (`unpaid`, `partial`, `paid`), all derived from its payments. Pay with `POST /purchases/payments/` and `{"purchase": 5, "amount": "400.00", "method": "cheque", "reference": "CHQ-9"}`; `method` is `cash`, `bank`, `online` or `cheque`. A payment cannot make the total paid exceed the net total. A supplier shows `total_purchased` and `outstanding` (received purchases not yet paid for) and `advance_paid` (paid on drafts still waiting for their goods).

Rules:

- Products come from the business's own catalog (a branch uses its Head Office's catalog) and must be active; the supplier must belong to the business and be active.
- Purchase numbers run on within a business (`PUR-000001`). The same supplier `bill_no` cannot be entered twice unless the earlier one was cancelled.
- Payments are never edited or deleted. Voiding one puts the amount back on the purchase and voids its `purchase` expense. A draft with payments cannot be cancelled until they are voided, and cannot be edited below what is already paid.
- Product prices are not changed by purchases, and no sale price is derived from them.
- Suppliers belong to one business. Purchases, suppliers and payments of one business are never visible to another.

### Purchase returns and supplier credit

Two ledgers per supplier, kept apart:

- **Credit to pay** (`credit_to_pay`): what you owe. It starts the moment a purchase is ordered (even before the goods arrive, `to_pay_on_order`), stays after receiving (`outstanding`), and goes down only when you record a payment (or apply credit). Paying before the goods arrive shows as `advance_paid`.
- **Credit to receive** (`credit_to_receive`): what the supplier owes you after a return. Sending goods back does not always bring money back, so a return only creates credit; the decision (refund or replacement) comes after.

```
return  -->  stock goes down, credit to receive goes up      (no money moves, no expense changes)
credit  -->  refund       supplier pays cash back            (credit down; reduces net spending)
        -->  replacement  supplier will send goods instead   (credit down; goods become stock_to_receive;
                                                              stock goes up only when they arrive)
        -->  adjustment   set against a later purchase       (credit down; that purchase owes less)
```

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/purchases/returns/` | List (filters: `purchase`, `supplier`, `status`) or return goods of a **received** purchase |
| `GET` | `/purchases/returns/{id}/` | View a return (`PRT-000001`) |
| `POST` | `/purchases/returns/{id}/void/` | Undo a return, with a `reason`: goods go back into stock and the credit is removed |
| `GET`, `POST` | `/purchases/credits/` | List (filters: `supplier`, `kind`, `status`) or use credit |
| `POST` | `/purchases/credits/{id}/receive/` | The replacement goods arrived: add them to stock (once; optional `received_on`) |
| `POST` | `/purchases/credits/{id}/void/` | Undo a refund, adjustment or a replacement still waiting, with a `reason` |

Return: `{"purchase": 5, "reason": "Wrong specification", "items": [{"purchase_item": 11, "quantity": "4", "stock_condition": "good"}]}`. `stock_condition` is `good` (default) or `damaged`, the stock the goods are taken from.

Use credit with `POST /purchases/credits/`:

- Refund: `{"supplier": 2, "kind": "refund", "amount": "400.00", "method": "bank"}`.
- Replacement goods: `{"supplier": 2, "kind": "replacement", "amount": "400.00", "items": [{"product": 3, "quantity": "4"}]}`. `amount` is the credit used up; the goods may differ from what was returned. They show as `stock_to_receive` and `delivery_status: "pending"` until `receive` adds them to stock (one `purchase_receipt` movement per product). A received replacement cannot be voided.
- Replacement as a normal purchase: enter it as a purchase, then `{"supplier": 2, "kind": "adjustment", "amount": "400.00", "purchase": 9}`. The purchase's `remaining_amount` drops and `credit_applied` shows it.

Rules:

- A line must belong to the chosen purchase, cannot be listed twice, and cannot be returned beyond what is left (`returnable_quantity` on each line). Goods already sold cannot be returned: the stock must still be there, otherwise nothing is returned.
- Credit for a line is its value minus its share of the discount, plus VAT. Freight is never credited, and credits for a purchase never add up to more than its goods.
- A supplier shows `credit_to_receive` and `stock_to_receive` separately from `credit_to_pay`. Credit cannot go below zero, and an adjustment cannot exceed what the purchase still owes.
- A return whose credit was already used cannot be voided (`409`, `credit_in_use`); void the refund or adjustment first.
- Returns, refunds, replacements and adjustments are never edited or deleted.

## Stock orders API (Branch to Head Office)

A Branch orders stock from its Head Office; Head Office dispatches good stock; the Branch checks what arrived (good and damaged apart) and can send damaged stock back with a note; Head Office receives it and reviews it. An order changes no stock until goods move. A business only sees the orders it is part of: a Branch sees its own, Head Office sees those from its branches. Needs the `inventory` module permission, and full access to the business (a company owner viewing a branch is read-only).

| Method | Path | Who | Description |
| --- | --- | --- | --- |
| `GET` | `/inventory/transfers/` | both | List. Filters: `status` (comma separated), `search` (reference, notes, product) |
| `POST` | `/inventory/transfers/` | Branch | Place an order: `{"items": [{"product": 3, "quantity": "10"}], "notes": ""}`. The reference (`ORD-<branch code>-0001`) is made by the server |
| `GET` | `/inventory/transfers/{id}/` | both | Items with ordered, dispatched, to receive, received good/damaged and damaged still to return; plus the history of dispatches, receipts, returns, return receipts and reviews |
| `POST` | `/inventory/transfers/{id}/cancel/` | both | `{"reason": "..."}`. Only before anything is dispatched |
| `POST` | `/inventory/transfers/{id}/dispatch/` | Head Office | `{"items": [{"transfer_item": 1, "quantity": "6"}], "notes": ""}`. Good stock only; partial and repeated dispatches are fine; never more than ordered |
| `POST` | `/inventory/transfers/{id}/receive/` | Branch | `{"items": [{"transfer_item": 1, "good_quantity": "5", "damaged_quantity": "1"}]}`. Good goes to good stock, damaged to damaged stock |
| `POST` | `/inventory/transfers/{id}/return/` | Branch | Send damaged stock back: items as for dispatch, and `notes` (the reason) is required |
| `POST` | `/inventory/transfers/{id}/returns/{batch}/receive/` | Head Office | `{"items": [{"return_item": 1, "received_quantity": "1"}]}`. The whole returned quantity must be received |
| `POST` | `/inventory/transfers/{id}/returns/{batch}/review/` | Head Office | `{"items": [{"receipt_item": 1, "accepted_quantity": "1", "rejected_quantity": "0"}], "remarks": "..."}`. The decision (accepted, partially accepted, rejected) is worked out from the quantities. A review changes no stock |

Statuses: `ordered`, `partially_dispatched`, `dispatched`, `partially_received`, `received`, `cancelled`. Every action answers with the order in full.

## Sales API

Selling stock to customers, for the active business. It needs the `sales` module permission (owner, admin and accountant have it by role; staff need it on their Position). Writes need full access to the business, so a company owner is read-only inside a branch. Money is sent and returned as strings.

```
draft  --confirm-->  confirmed          (stock goes down, once; invoice number given; the sale is locked)
draft  --cancel-->   cancelled
```

A draft can be edited freely and has no effect on stock. `confirm` takes every line out of the business's stock through the inventory stock service (one `sale_issue` movement per line, referencing `INV-000001`), all or nothing, and only once; if one line has too little stock nothing is taken. A confirmed sale cannot be edited or cancelled; corrections are done with a sale return (below).

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/sales/` | List (filters: `status`, `customer`, `payment_status`, `sale_date__gte`, `sale_date__lte`, `search`) or create a draft |
| `GET`, `PATCH` | `/sales/{id}/` | View, or edit a draft. Sending `items` replaces all lines |
| `POST` | `/sales/{id}/confirm/` | Hand over the goods: take the lines out of stock |
| `POST` | `/sales/{id}/cancel/` | Cancel a draft, with a `reason` |
| `GET`, `POST` | `/sales/payments/` | List or record a customer payment (full or part, also on a draft as an advance) |
| `POST` | `/sales/payments/{id}/void/` | Void a payment, with a `reason` |
| `GET`, `POST` | `/sales/local-products/` | List or add local (sales-only) products |
| `GET`, `PATCH`, `DELETE` | `/sales/local-products/{id}/` | View, edit or deactivate a local product |

**Local products.** A local product is a sales-only item of the business (a service, an accessory bought in for a customer, anything not kept in stock). Sell it with `{"local_product": 4, "quantity": "1"}` on a stock or maintenance sale, alone or next to stock lines. The price defaults to its sale price and can be changed on the line. The line is named after the product, nothing is taken from stock when the sale is confirmed, and income, payments and follow-ups work as for any sale. Bike sales cannot contain local products, and a line cannot be both a product and a local product. Local product lines cannot be returned (only goods from stock can). `DELETE` only deactivates a local product; one already on a draft still sells.

Create a sale:

```json
{
  "customer": 3,
  "items": [{"product": 7, "quantity": "2"}, {"product": 9, "quantity": "1", "unit_price": "950.00"}],
  "discount_amount": "100.00",
  "vat_rate": "13"
}
```

Totals are never accepted from the client. Order: line totals, minus the discount, VAT on what is left (same as purchases; `vat_rate` defaults to 0). A line without `unit_price` takes the product's `sale_price`; a product with no sale price needs a price on the line. Changing a line price never changes the product.

Each sale returns `paid_amount`, `remaining_amount` and `payment_status` (`unpaid`, `partial`, `paid`), derived from its payments. A customer shows `total_sales`, `credit_to_receive` (confirmed sales not yet paid) and `advance_received` (paid on drafts).

Rules:

- A sale without a `customer` is a walk-in (optional `walk_in_name` for the invoice). A walk-in sale must be paid in full before it can be confirmed; selling on credit needs a customer.
- Invoice numbers (`INV-000001`) are given at confirmation, in order and without gaps, so cancelled drafts never use one. Draft numbers (`SAL-000001`) run on within a business.
- Payments are never edited or deleted; void one with a reason. A payment cannot make the total received exceed the net total, and voiding a payment cannot leave a walk-in sale unpaid. A draft with payments cannot be cancelled or cut below what was received.
- Only active products of the business's catalog can be sold; sale-only local products are not part of this yet.
- Customers belong to one business; a phone number can be used once per business. Sales, customers and payments of one business are never visible to another.

### Sale returns and customer credit

Two ledgers per customer, kept apart:

- **Credit to receive** (`credit_to_receive`): what the customer still owes you.
- **Credit to pay** (`credit_to_pay`): what you owe the customer after a return. Returned goods do not always mean money back: it can be refunded, set against another sale, or given as other goods.

```
return  -->  goods back in stock; value first takes off what the customer still owes on that sale
        -->  whatever is left over is credit to pay                  (no cash moves, income unchanged)
credit  -->  refund       cash goes back to the customer             (credit down; reduces net income)
        -->  adjustment   set against what the customer owes on another sale  (credit down; that sale owes less)
        -->  replacement  other goods given instead                  (credit down; goods become stock_to_give;
                                                                       stock goes down only when delivered)
```

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/sales/returns/` | List (filters: `sale`, `customer`, `status`, `return_date__gte`, `return_date__lte`) or return goods of a **confirmed** sale |
| `GET` | `/sales/returns/{id}/` | View a return (`SRT-000001`) |
| `POST` | `/sales/returns/{id}/void/` | Undo a return, with a `reason`: goods go out of stock again and its effect on the sale and credit is removed |
| `GET`, `POST` | `/sales/credits/` | List (filters: `customer`, `kind`, `status`, `sale`, `pending=true`) or use credit |
| `POST` | `/sales/credits/{id}/deliver/` | The replacement goods were handed over: take them out of stock (once; optional `delivered_on`) |
| `POST` | `/sales/credits/{id}/void/` | Undo a refund, adjustment or a replacement not yet delivered, with a `reason` |

Return: `{"sale": 5, "reason": "Wrong part", "items": [{"sale_item": 11, "quantity": "4"}]}`. Returned goods always go back into good stock. Each sale line shows `returned_quantity` and `returnable_quantity`; a sale shows `returned_amount` (what returns took off what is owed) and `credit_applied` (credit set against it).

What can be returned:

- **Stock sale:** any line, in parts, until nothing is left.
- **Maintenance:** the parts only. A service charge cannot be returned.
- **Bike sale:** nothing. A bike (full payment or EMI) cannot be returned; its line shows `returnable_quantity: "0.000"`.

Use credit with `POST /sales/credits/`:

- Refund: `{"customer": 2, "kind": "refund", "amount": "600.00", "method": "bank"}`.
- Set against another sale: `{"customer": 2, "kind": "adjustment", "amount": "400.00", "sale": 9}` (not for an EMI sale).
- Replacement goods: `{"customer": 2, "kind": "replacement", "amount": "400.00", "items": [{"product": 3, "quantity": "2"}]}`. The goods show as `stock_to_give` and `delivery_status: "pending"` until `deliver` takes them out of stock (one `sale_issue` movement per product). A delivered replacement cannot be voided.

Rules:

- A line must belong to the chosen sale, cannot be listed twice, and cannot be returned beyond what is left. Value of a line is its amount less its share of the discount, plus VAT; the values never add up to more than the sale.
- A **walk-in** return has no customer to hold credit, so it is refunded with the return: send `refund_method` (`cash`, `bank`, `online`, `cheque`) and optionally `reference`. A customer return takes no `refund_method`.
- Credit cannot go below zero, and an adjustment cannot exceed what the other sale still owes. It cannot be used by another customer.
- A return whose credit was already used cannot be voided (`409`, `credit_in_use`); void the refund, adjustment or replacement first. Voiding a walk-in return voids its refund too. A payment of a sale with an active return cannot be voided (`409`, `returns_exist`).
- Returns and settlements are never edited or deleted.

### Kinds of sale

`sale_type` says what is sold (it is fixed once the draft is made):

| Type | Lines | Notes |
| --- | --- | --- |
| `stock` (default) | `product` + `quantity` | Plain stock sale. Stock goes down on confirm. |
| `maintenance` | `product` (a part, takes stock) and/or `description` + `unit_price` (a service charge, no stock) | Optional `vehicle_chassis_no`, `vehicle_reg_no`. At least one line. |
| `bike` | exactly one `bike_unit` (a registered bike) | Full payment or EMI. The unit is marked sold on confirm; the same bike cannot be sold twice (`409`, `bike_unavailable`). |

Bikes are registered one by one with `POST /sales/bikes/` (`product`, `chassis_no`, `engine_no`, `color`); filters `status` (`in_stock`, `sold`), `product`, `search`. A chassis number is one physical bike, so it is unique across the system. Registering a unit never changes stock (stock comes from purchases or transfers) and there can never be more units in stock than the stock balance of the model.

### EMI (bike sales)

Send `payment_mode: "emi"` with `down_payment`, `emi_months`, `emi_amount` (monthly) and optionally `first_emi_due` (default: one month after the sale date). The server works out the finance charge:

```
down_payment + emi_months x emi_amount - net_total  =  finance_charge   (cannot be negative)
amount due = net_total + finance_charge
```

The down payment must be received before the sale can be confirmed (`409`, `down_payment_due`) and an EMI needs a customer. Confirming creates the monthly instalments; `GET /sales/{id}/` shows them as `installments`, each with `paid_amount` and a status (`paid`, `partial`, `upcoming`, `overdue`). Money received after the down payment covers the instalments in order, so the schedule is always worked out from the payments and never typed in. Payments cannot exceed the amount due, and the down payment of a confirmed EMI sale cannot be voided.

### Follow-ups

Follow-ups are created by the system when a sale is confirmed. They are listed at `GET /sales/followups/` (filters: `kind`, `status`, `customer`, `sale`, `due_date__gte`, `due_date__lte`, `overdue=true`, `search`) and every one shows the `sale_date`, the customer and the vehicle.

| Kind | When | Closes |
| --- | --- | --- |
| `maintenance_post` | 3 days after the service date | When a person marks it done. Records `has_issue` and a note. |
| `maintenance_due` | 90 days after the service date | When a person marks it done. Shows the 3-day check as `previous_followup` (status, `has_issue`, note). |
| `emi_due` | One per EMI instalment, on its due date | By itself when the instalment is paid; opens again if that payment is voided. |
| `credit_due` | A sale left partly unpaid (not EMI); `due_date` on the sale, default 30 days after the sale date | By itself when it is paid; opens again if that payment is voided. |

`POST /sales/followups/{id}/done/` takes `note`, `has_issue` (maintenance only) and optionally `next_due_date` (maintenance only) to book the next follow-up. `POST /sales/followups/{id}/skip/` needs a `note`. A follow-up closed by a person is never reopened by the system.

### Income

Every customer payment is added to income automatically in the category of its sale (`stock_sale`, `bike_sale`, `maintenance`), on the day it is received; money not yet received stays as credit to receive and is not income. Voiding the payment voids its income. Cash refunded to customers for returned goods is not entered as negative income; it is subtracted in the summary instead (see below).

## Customers and suppliers API

Customers and suppliers are parties of their own business. Every purchase, purchase return, supplier payment and credit points at a supplier; every sale, sale return, customer payment and credit points at a customer. Both can be filtered by party (`?supplier=` or `?customer=`).

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/customers/` | List or add customers (profile includes what they owe and the credit you owe them) |
| `GET`, `PATCH`, `DELETE` | `/customers/{id}/` | View, edit or deactivate a customer |

A customer has a `customer_type` (`individual`, `business` or `fleet`; filter with `?customer_type=`) and a `credit_limit` (0 = none set). `over_credit_limit` is true when what the customer owes is above the limit. It is a warning only; a sale is never blocked by it. Phone, email and PAN are optional, but a phone number is unique within a business. `?has_dues=true` lists only customers who still owe money.
| `GET` | `/customers/{id}/statement/` | Full history: sales, payments, returns, credit use and refunds |
| `GET`, `POST` | `/suppliers/` | List or add suppliers (profile includes outstanding, advance, credit to pay and credit to receive) |
| `GET`, `PATCH`, `DELETE` | `/suppliers/{id}/` | View, edit or deactivate a supplier |
| `GET` | `/suppliers/{id}/statement/` | Full history: purchases, payments, returns, credits and replacements |

Statement filters: `date_from`, `date_to` (`YYYY-MM-DD`, bad dates return 400) and `type`. The response gives `opening_balance`, `total_debit`, `total_credit`, `closing_balance` and a paginated `results` list with a running balance on each row. A positive balance means the customer owes you (customers) or you owe the supplier (suppliers). Voided payments and cancelled documents are left out; adjustments do not move the balance.

## Reports API (owner only)

Read-only reports for the active business. Money comes back as strings.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/reports/profit-loss/` | Profit and loss on a cash basis. Filters: `date_from`, `date_to` (bad or reversed dates return 400) |
| `GET` | `/reports/receivables/` | What customers owe, aged by sale date, per customer |
| `GET` | `/reports/payables/` | What is owed to suppliers on received purchases, aged by purchase date |
| `GET` | `/reports/stock-value/` | Stock on hand at purchase price, good and damaged apart |
| `GET` | `/reports/sales-trend/` | Confirmed sales per `group_by` (`day`, `week`, `month`; default `month`), less returns |
| `GET` | `/reports/top-products/` | Best sellers; `by=value` (default) or `quantity` |
| `GET` | `/reports/top-customers/` | Customers by what they bought; walk-in sales are totalled apart |
| `GET` | `/reports/top-suppliers/` | Suppliers by what was bought on received purchases |
| `GET` | `/reports/payroll-summary/` | What staff earned and were paid, per person and per month |

- **Profit and loss:** `income` (less `sale_refunds`) against `expenses` (less `purchase_refunds`), with `profit`, a split by category and a `by_month` list. Voided records are left out. A return that is not refunded in cash moves nothing.
- **Ageing buckets:** `current` (0-30 days), `31_60`, `61_90`, `over_90`. The total matches the sum of customer `credit_to_receive` (without EMI) or supplier `outstanding`. EMI sales are shown separately as `emi_outstanding`, because their instalments fall due over months.
- **Stock value:** `good_value`, `damaged_value`, `total_value` and per product lines, biggest first. Products with no purchase price are counted in `products_without_price` and valued at zero.
- **Analytics:** `sales-trend`, `top-products`, `top-customers`, `top-suppliers` and `payroll-summary` take `date_from` and `date_to`; the `top-*` reports also take `limit` (1 to 100, default 10). Only confirmed sales and received (or partly received) purchases count; drafts, cancelled and void records are left out. `top-products` revenue is the line total before any discount or VAT on the whole sale, and lists local products and service charges under their name with the returned quantity taken off in `net_quantity`. `payroll-summary` shows `earned` (salary, wages and bonus less deductions) and `paid` for the period, and `owed_now`, the running balance of all earnings less all payments.

## Audit log API (owner only)

A history of who changed what, kept by the server for the active business. Nothing is written to it by the client and rows are never edited or deleted.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/audit/` | List, newest first. Filters: `model`, `object_id`, `action` (`create`, `update`, `delete`), `actor`, `created_at__gte`, `created_at__lte`; `search` on the record name and the user |
| `GET` | `/audit/{id}/` | One entry |

Each entry has `actor` and `actor_name`, `action`, `model` (for example `sales.sale`), `object_id`, `object_repr`, `changes` and a one line `summary`. For an update `changes` is `{"status": ["active", "void"]}` (old, new); for a create or delete it is the values. The whole history of one record is `?model=finance.expense&object_id=12`.

What is recorded: creates, changes and deletes of products, local products, stock adjustments and opening stock (not every stock movement, which already has its own history), suppliers, customers, bikes, purchases, sales, payments, returns, credits, expenses, income, payroll payments and salary profiles, staff users, positions and businesses. Voids, cancellations and confirmations show as `status` changes. Passwords are never recorded. Changes made outside an API request (a shell, a migration) are not logged. An entry is written in the same transaction as the change, so a change that is rolled back leaves no entry.

## Notifications API

Follow-ups that need action, shown as notifications for the active business. They are made from pending follow-ups (maintenance checks, EMI instalments, credit to receive) that are due today, and again when they become overdue. A notification disappears once its follow-up is done, skipped or paid.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/notifications/` | List, newest first. Filters: `unread=true` or `false` (for you), `kind` (`followup_due`, `followup_overdue`); `search` |
| `GET` | `/notifications/{id}/` | One notification |
| `GET` | `/notifications/unread-count/` | `{"unread": 3}`, for a badge |
| `POST` | `/notifications/{id}/read/` | Mark it read, for you only |
| `POST` | `/notifications/read-all/` | Mark every unread one read, for you only |

Needs the `reminders` module permission (owner, admin and accountant have it by role; staff need it on their Position). Viewing is enough to mark notifications read, so a company owner can do it inside a branch. Every person has their own read state.

No scheduler is needed: listing makes the business's notifications first, at most once every 15 minutes. To make them for every business on a schedule (for example to send reminders from another tool), run `python manage.py generate_notifications` daily. It is safe to run as often as you like.

### Downloading reports and statements

Every report above, and the customer and supplier statements, can be downloaded by adding `export=xlsx` or `export=pdf` to the same request, for example `GET /reports/profit-loss/?date_from=2026-04-01&export=pdf` or `GET /customers/7/statement/?export=xlsx`. The same filters apply, the response is the file (`Content-Disposition: attachment`, named like `profit-loss-20261104.pdf`), and an unknown value returns `400`. A statement download carries every row, not one page. Excel files have a Summary sheet and one sheet per table, with real numbers (text that looks like a formula is never run). PDFs use DejaVu Sans when it is installed; set `PDF_FONT` and `PDF_FONT_BOLD` in the settings to the paths of other TTF files (for example a Noto Sans Devanagari font) so Nepali names print correctly.

## Help & Support API

Powers the Help & Support page: FAQs, support tickets and how to reach the VGSync team.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/support/faqs/` | Published FAQs in the order set by the team (`id`, `question`, `answer`). `search` looks in the question and the answer |
| `GET` | `/support/tickets/` | Tickets of the active business, newest first. Filters: `status` (`open`, `in_progress`, `resolved`), `category`; `search` looks in the subject and description |
| `POST` | `/support/tickets/` | Raise a ticket: `subject` (3+ characters), `description` (10+ characters), optional `category` (`inventory`, `purchase`, `sales`, `payments`, `reports`, `account`, `other`; default `other`) |
| `GET` | `/support/tickets/{id}/` | One ticket |
| `GET` | `/support/contact/` | `email`, `phone`, `hours`, `reply_time`, `user_guide_url`, `video_tutorials_url` |

Any signed-in user can use it, with no module permission. The owner, admin and accountant see every ticket of the business; staff see only the ones they raised. Raising a ticket asks for help and changes no business data, so a company owner can do it inside a branch. A ticket always starts as `open`; people cannot change it afterwards. The VGSync team moves it to `in_progress` or `resolved` in Django Admin, where FAQs are also written and reordered. Each ticket has a `reference` such as `TKT-00124`. Raising tickets is limited to 20 per hour per person. The first five FAQs are added by a migration.

## Expenses API (owner only)

Every payroll payment is recorded as a `salary` expense, and every supplier payment as a `purchase` expense, automatically, so they sit with all other spending. Other expenses are entered by the owner.

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/finance/expenses/` | List (filters: `category`, `status`, `payment_method`, `expense_date__gte`, `expense_date__lte`) or add an expense |
| `GET`, `PATCH` | `/finance/expenses/{id}/` | View or change a manual expense |
| `POST` | `/finance/expenses/{id}/void/` | Void a manual expense, with a reason |
| `GET` | `/finance/expenses/summary/` | `total`, `purchase_refunds`, `net_total` and `by_category` of active expenses, for the same filters |

Categories: `salary`, `purchase`, `rent`, `utilities`, `transport`, `marketing`, `maintenance`, `other`. Salary and purchase expenses come from payments and are read-only here (`409`, `payment_expense_locked`); void the payment instead. Expenses belong to the active business, so a company owner sees Head Office expenses and a branch owner sees the branch's.

Supplier refunds are cash coming back, so the summary subtracts them: `net_total = total - purchase_refunds`. The original `purchase` expenses stay untouched, and a return or an adjustment never changes expenses.

## Income API (owner only)

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/finance/income/` | List (filters: `category`, `status`, `payment_method`, `income_date__gte`, `income_date__lte`) or add other income |
| `POST` | `/finance/income/{id}/void/` | Void manual income, with a reason |
| `GET` | `/finance/income/summary/` | `total`, `sale_refunds`, `net_total` and `by_category` of active income, for the same filters |

Categories: `stock_sale`, `bike_sale`, `maintenance`, `other`. The sale categories come from customer payments and are read-only (`409`, `payment_income_locked`); void the payment instead. Only `other` can be entered by hand. Income belongs to the active business.

Refunds are cash going back to customers, so `net_total = total - sale_refunds`. A refund belongs to a customer rather than to one kind of sale, so a `category` filter leaves refunds out. A return, credit set against a sale and replacement goods never change income.

## Lists

Every list endpoint is paginated: `?page=2&page_size=50` (default 25, maximum 100). The response has `count`, `next`, `previous` and `results`.

## Tests

```
python manage.py test
```

The settings use PostgreSQL, so the database user needs permission to create a test database. Tests cover login and token rules, throttling, permissions, the owner's staff and Position management, payroll, and inventory.

## Django Admin

Available at `/admin/` for Super Admin users to manage companies, businesses, support FAQs and tickets, users (including creating owners, see [Owner setup and first login](#owner-setup-and-first-login)), and Positions (module permissions are edited inline on each Position). Payroll entries and payments are read-only there; they are changed through the API so every change is validated and attributed.
