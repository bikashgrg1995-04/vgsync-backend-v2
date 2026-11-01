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
| Company / business management APIs | Not yet implemented |
| Purchases, sales, bike sales, orders, finance, dashboard, reminders, analytics, ledger and reports | Not yet implemented (the permission system already defines most of these modules) |

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
  finance/           Expenses (salary payments are added automatically)
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
| `POST` | `/change-password/` | Bearer | Change password and sign out everywhere |

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

## Team management API (owner only)

Everything below is limited to the **owner** of the active business, and works on the team of that business:

- A company owner signed in to the **Head Office** manages the company's users, with Positions that belong to the Head Office.
- A **Branch** or **Independent business** owner manages the users of that business.
- A company owner signed in to a **Branch** is read-only (`403` on writes), like everywhere else.

Admins, accountants and staff get `403`. Records of another company or business return `404`.

### Staff

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

## Expenses API (owner only)

Every payroll payment is recorded as a `salary` expense automatically, so salary sits with all other spending. Other expenses are entered by the owner.

| Method | Path | Description |
| --- | --- | --- |
| `GET`, `POST` | `/finance/expenses/` | List (filters: `category`, `status`, `payment_method`, `expense_date__gte`, `expense_date__lte`) or add an expense |
| `GET`, `PATCH` | `/finance/expenses/{id}/` | View or change a manual expense |
| `POST` | `/finance/expenses/{id}/void/` | Void a manual expense, with a reason |
| `GET` | `/finance/expenses/summary/` | Totals of active expenses by category, for the same filters |

Categories: `salary`, `rent`, `utilities`, `transport`, `marketing`, `maintenance`, `other`. Salary expenses are read-only here (`409`, `salary_expense_locked`); void the payroll payment instead. Expenses belong to the active business, so a company owner sees Head Office expenses and a branch owner sees the branch's.

## Lists

Every list endpoint is paginated: `?page=2&page_size=50` (default 25, maximum 100). The response has `count`, `next`, `previous` and `results`.

## Tests

```
python manage.py test
```

The settings use PostgreSQL, so the database user needs permission to create a test database. Tests cover login and token rules, throttling, permissions, the owner's staff and Position management, payroll, and inventory.

## Django Admin

Available at `/admin/` for Super Admin users to manage companies, businesses, users (including creating owners, see [Owner setup and first login](#owner-setup-and-first-login)), and Positions (module permissions are edited inline on each Position). Payroll entries and payments are read-only there; they are changed through the API so every change is validated and attributed.
