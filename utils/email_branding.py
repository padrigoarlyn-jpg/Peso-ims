"""Shared PESO email branding helpers."""

import re
from html import escape
from pathlib import Path

PESO_HEADER_CID = "peso-email-header"
PESO_BACKGROUND_CID = "peso-email-background"


def build_branded_html(body):
    """Wrap a plain-text PESO email body in the shared PESO email layout."""
    safe_body = escape(str(body or "")).replace("\r\n", "\n").replace("\r", "\n")

    # Safe, controlled emphasis tokens for transactional email content.
    # The original body is escaped first, so arbitrary HTML still cannot be injected.
    safe_body = re.sub(
        r"\[\[important:(.+?)\]\]",
        r'<strong style="color:#123f87;font-size:17px;font-weight:700;">\1</strong>',
        safe_body,
        flags=re.DOTALL,
    )
    safe_body = re.sub(
        r"\[\[bold:(.+?)\]\]",
        r"<strong>\1</strong>",
        safe_body,
        flags=re.DOTALL,
    )

    # Keep support for existing **bold** bodies.
    safe_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe_body)
    safe_body = safe_body.replace("\n", "<br>")

    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f3f6fb;font-family:Arial,Helvetica,sans-serif;color:#1f2937;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
           background="cid:{PESO_BACKGROUND_CID}" bgcolor="#f3f6fb"
           style="background-color:#f3f6fb;background-image:url('cid:{PESO_BACKGROUND_CID}');
                  background-position:center center;background-size:cover;background-repeat:no-repeat;">
      <tr>
        <td align="center" style="padding:32px 12px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                 style="max-width:714px;background:#ffffff;border:1px solid #e2e8f0;
                        border-radius:12px;overflow:hidden;">
            <tr>
              <td style="padding:0;line-height:0;">
                <img src="cid:{PESO_HEADER_CID}" alt="Public Employment Service Office"
                     width="714"
                     style="display:block;width:100%;max-width:714px;height:auto;border:0;">
              </td>
            </tr>

            <tr>
              <td style="padding:28px 32px;font-size:15px;line-height:1.7;color:#334155;background:#ffffff;">
                {safe_body}
              </td>
            </tr>

            <tr>
              <td style="padding:16px 32px;border-top:1px solid #e5e7eb;font-size:12px;
                         line-height:1.5;color:#64748b;text-align:center;background:#ffffff;">
                Public Employment Service Office (PESO)<br>
                City of San Jose del Monte, Bulacan
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def _attach_inline_image(message, path, filename, content_type, cid, app):
    if not path.is_file():
        app.logger.warning("PESO email image not found: %s", path)
        return False

    message.attach(
        filename=filename,
        content_type=content_type,
        data=path.read_bytes(),
        disposition="inline",
        headers={"Content-ID": f"<{cid}>"},
    )
    return True


def attach_peso_header(message, app):
    path = Path(app.root_path) / "static" / "images" / "email" / "peso_email_header.png"
    return _attach_inline_image(
        message, path, "peso_email_header.png", "image/png", PESO_HEADER_CID, app
    )


def attach_peso_background(message, app):
    path = Path(app.root_path) / "static" / "images" / "email" / "peso_email_background_faded.jpg"
    return _attach_inline_image(
        message, path, "peso_email_background_faded.jpg", "image/jpeg",
        PESO_BACKGROUND_CID, app
    )


def _plain_text_body(body):
    """Remove display-only emphasis markers from the plain-text fallback."""
    text = str(body or "")
    text = re.sub(r"\[\[(?:important|bold):(.+?)\]\]", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    return text


def apply_peso_branding(message, body, app):
    """Keep plain text fallback and add the shared branded HTML version."""
    message.body = _plain_text_body(body)
    message.html = build_branded_html(body)
    # A missing header must never silently produce an unbranded email.
    # The background is optional; the header is the required PESO identity.
    if not attach_peso_header(message, app):
        raise FileNotFoundError(
            "PESO email header is missing: "
            "website/static/images/email/peso_email_header.png"
        )
    background_path = (
        Path(app.root_path) / "static" / "images" / "email"
        / "peso_email_background_faded.jpg"
    )
    if background_path.is_file():
        attach_peso_background(message, app)
    return message
