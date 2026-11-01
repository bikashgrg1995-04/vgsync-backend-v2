# VGSync Backend v2

Backend API for VGSync, a multi-tenant business management platform built with Django REST Framework and PostgreSQL.

## Project status

This repository currently contains the **foundation layer**: tenancy, authentication, and permissions. Business modules are not implemented yet.

| Area | Status |
| --- | --- |
| Companies, Head Office / Branch / Independent businesses | Models and admin |
| Users, roles, Positions, module permissions | Models, admin, and enforcement |
| JWT login, token refresh, current user, change password | API |
| Company / business management APIs | Not yet implemented |
| Inventory, purchases, sales, bike sales, orders, staff and payroll, finance, dashboard, reminders | Not yet implemented (the permission system already defines these modules) |

## Tech stack

- Python, Django 5.2
- Django REST Framework
- PostgreSQL (via `psycopg` 3)
- Simple JWT (`djangorestframework-simplejwt`)
- `django-filter`, `django-cors-headers`, `drf-spectacular`
- `python-dotenv` for configuration

## Project structure

```
config/              Django project settings, URLs, WSGI/ASGI
apps/
  accounts/          User, Position, PositionPermission, JWT login/refresh, /me, change password
  companies/         Company model
  businesses/        Business model (Head Office, Branch, Independent)
  common/            Custom JWT authentication class and DRF permission classes
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

## Tests

```
python manage.py test
```

The settings use PostgreSQL, so the database user needs permission to create a test database. Current tests cover tenant login rules, token refresh revalidation, and module permissions.

## Django Admin

Available at `/admin/` for Super Admin users to manage companies, businesses, users, and Positions (module permissions are edited inline on each Position).
