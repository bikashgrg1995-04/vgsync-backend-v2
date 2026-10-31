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
| `POST` | `/token/refresh/` | None | Get a new access token from a refresh token |
| `GET` | `/me/` | Bearer | Current user and active business |
| `POST` | `/change-password/` | Bearer | Change password |

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

### Change password

Request body: `current_password`, `new_password`, `confirm_password`. The new password is checked against Django's password validators.

## Tests

```
python manage.py test
```

The settings use PostgreSQL, so the database user needs permission to create a test database. Current tests cover tenant login rules, token refresh revalidation, and module permissions.

## Django Admin

Available at `/admin/` for Super Admin users to manage companies, businesses, users, and Positions (module permissions are edited inline on each Position).
