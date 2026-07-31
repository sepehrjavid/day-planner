"""Self-contained HTML for the pages a user's browser actually lands on.

Deliberately no external stylesheets, fonts, or scripts: this page renders
immediately after an OAuth callback and should not make third-party requests
while the user is looking at a screen about their calendar credentials.
"""

from html import escape

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; margin: 0; padding: 1.5rem; box-sizing: border-box;
  }}
  main {{ max-width: 30rem; text-align: center; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .5rem; }}
  p {{ margin: .5rem 0; opacity: .85; }}
  .mark {{ font-size: 2.5rem; line-height: 1; margin-bottom: .75rem; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .875rem; opacity: .7;
  }}
</style>
</head>
<body><main>
  <div class="mark">{mark}</div>
  <h1>{heading}</h1>
  {body}
</main></body>
</html>"""


def connected(
    provider: str, email: str | None, found: int, selected: int
) -> str:
    who = f" as {escape(email)}" if email else ""
    plural = "" if found == 1 else "s"
    return _SHELL.format(
        title="Calendar connected",
        mark="&#10003;",
        heading=f"{escape(provider.title())} Calendar connected{who}",
        body=(
            f"<p>Found {found} calendar{plural}; {selected} will be used for "
            "planning. You can change that later.</p>"
            "<p>You can close this tab and head back to the chat.</p>"
        ),
    )


def failed(message: str) -> str:
    return _SHELL.format(
        title="Connection failed",
        mark="&#9888;",
        heading="Couldn't connect that calendar",
        body=f"<p>{escape(message)}</p><p>Head back to the chat and try again.</p>",
    )
