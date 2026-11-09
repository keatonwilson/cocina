"""Check the hosted sign-in settings haven't drifted (issue #1).

python -m cocina.authcheck <deploy origin>

They're set in the dashboard, not from the repo, so this asks the admin API for
two magic links (generated, not emailed) and reads where each would land:
- one to <origin>/auth/confirm must keep it: the redirect allow-list holds it
- one to an address not on the list must fall back to the Site URL, the origin

ponytail: the email templates aren't checked (that needs the Management API and
an access token); move to `supabase config push` if the settings change often.
"""

import sys
from urllib.parse import parse_qs, urlparse

from cocina import db


def landing(email: str, redirect_to: str) -> str:
    link = db.client().auth.admin.generate_link(
        {"type": "magiclink", "email": email, "options": {"redirect_to": redirect_to}}
    )
    return parse_qs(urlparse(link.properties.action_link).query)["redirect_to"][0]


def main() -> None:
    origin = sys.argv[1].rstrip("/")
    # An existing user, so no account is created. Never printed: the Actions log is public.
    email = db.client().auth.admin.list_users(page=1, per_page=1)[0].email
    problems = []
    if (got := landing(email, f"{origin}/auth/confirm")) != f"{origin}/auth/confirm":
        problems.append(f"{origin}/auth/confirm isn't an allowed redirect (lands on {got})")
    if (got := landing(email, "https://not-allowed.invalid/")).rstrip("/") != origin:
        problems.append(f"Site URL is {got}, not {origin}")
    for p in problems:
        print(f"auth drift: {p}")
    print("auth settings ok" if not problems else "fix in the dashboard: Authentication → URL Configuration")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
