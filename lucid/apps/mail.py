"""Windows Mail / new Outlook (one-outlook).

There are now THREE distinct desktop "mail" apps on a fresh Windows install
that all answer to the user-facing word "mail":

* Classic Outlook desktop (``OUTLOOK.EXE``, from Microsoft 365 / Office)
  — covered by :mod:`lucid.apps.outlook`.
* "New Outlook" / one-outlook (``olk.exe``), shipped as a Store app and
  often replacing the classic Mail tile on Win11 23H2+.
* Classic UWP Mail (``HxOutlook.exe`` / ``HxMail.exe``), still around on
  some installs.

If any of these is running with a visible window, treat the user's request
to "open Mail" as satisfied; do NOT report unavailable just because the
Store overlay popped up next to it.
"""

SLUG = "mail"
TITLE = "Windows Mail / new Outlook"

TIPS = """\
- [seed · disambiguate] "Mail" on Windows can be (a) classic Outlook (OUTLOOK.EXE), (b) new Outlook (olk.exe), or (c) classic UWP Mail (HxOutlook.exe / HxMail.exe). Any of them with a visible inbox counts — do NOT label the app unavailable just because a Store / "Get the new Outlook" promo overlay appears alongside.
- [seed · launch] Try in order: `launch_app(name='mail')` (this entry's URI/exe chain) → `launch_app(name='outlook')` (classic) → win+r and type `outlookmail:` then Enter. If all three fail, then reply unavailable.
- [seed · read-only] To report which folder is selected without risking opening / replying / forwarding any message: take a single L2 of the active_window and read the highlighted entry in the left rail (Inbox / Drafts / Sent / Junk / …). Do NOT click into the message list.
"""

LAUNCHER = {
    "name": "Windows Mail",
    "description": "Windows Mail / new Outlook (olk.exe / HxOutlook.exe). Use the `outlook` slug for the classic desktop Outlook.",
    # The new Outlook registers this URI; falls through to mailto: on classic.
    "uri": "outlookmail:",
    # `start mail` works on most builds; on others olk works as an alias.
    "exe": "olk",
    # Match any of the three executables. The launcher treats this as the
    # "is it already running" probe.
    "process": "olk.exe|HxOutlook.exe|HxMail.exe|OUTLOOK.EXE",
    "window_title_re": r"(Outlook|Mail|邮件)",
}
